"""Opt-in real Chromium checks for HTTP-cache and Service Worker bypass.

Set RUN_BROWSER_CACHE_LIVE=1 and EDGE_TEST_DRIVER to an installed driver.
For Chrome, set CACHE_TEST_BROWSER=chrome and CACHE_TEST_DRIVER instead.
Fixtures serve deliberately cacheable resources and count actual HTTP requests.
"""

import base64
from collections import Counter
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.chrome.options import Options as ChromeOptions

from browser_discovery import discover_browser
from browser_cache import CachePolicyError, ChromiumCachePolicy
from browser_loading import configure_uncached_network
from browser_service import TimedChromeService, TimedEdgeService


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
HEAD = '<!doctype html><title>Cache fixture</title><link rel="icon" href="data:,">'


class ChromiumCachePolicyTests(unittest.TestCase):
    def policy(self):
        policy = ChromiumCachePolicy(Mock())
        policy._connected = True
        policy._request = Mock(return_value={})
        return policy

    @staticmethod
    def attach(policy, kind="iframe"):
        policy._handle_event({"method": "Target.attachedToTarget", "params": {
            "sessionId": "session", "waitingForDebugger": True,
            "targetInfo": {"targetId": "target", "type": kind, "url": "https://fixture.test/"}}})
        policy._initializations.put(None)
        policy._initialize_loop()

    def test_subtarget_configuration_precedes_execution(self):
        policy = self.policy()
        self.attach(policy)
        methods = [call.args[0] for call in policy._request.call_args_list]
        self.assertEqual(methods, ["Page.enable", "Network.enable", "Network.setCacheDisabled",
                                   "Network.setBypassServiceWorker", "Target.setAutoAttach",
                                   "Runtime.runIfWaitingForDebugger"])
        self.assertTrue(policy.snapshot()["targets"][0]["initialized"])
        self.assertEqual(policy.snapshot()["pending_target_count"], 0)

    def test_worker_uses_network_domain_and_recursive_attach(self):
        policy = self.policy()
        self.attach(policy, "worker")
        methods = [call.args[0] for call in policy._request.call_args_list]
        self.assertNotIn("Page.enable", methods)
        self.assertIn("Network.setBypassServiceWorker", methods)
        self.assertIn("Target.setAutoAttach", methods)

    def test_failed_configuration_releases_pause_and_rejects_capture(self):
        policy = self.policy()

        def request(method, *args):
            if method == "Network.setCacheDisabled":
                raise CachePolicyError("cache policy denied")
            return {}

        policy._request.side_effect = request
        self.attach(policy)
        self.assertIn("Runtime.runIfWaitingForDebugger",
                      [call.args[0] for call in policy._request.call_args_list])
        self.assertFalse(policy.snapshot()["targets"][0]["paused"])
        with self.assertRaisesRegex(CachePolicyError, "cache policy denied"):
            policy.check()

    def test_cached_redirect_fails_and_observations_are_drainable(self):
        policy = self.policy()
        event = {"sessionId": "child", "method": "Network.requestWillBeSent", "params": {
            "requestId": "request", "request": {"url": "https://fixture.test/final"},
            "redirectResponse": {"url": "https://fixture.test/redirect", "fromDiskCache": True}}}
        policy._handle_event(event)
        self.assertEqual(policy.snapshot()["cache_hits"][0]["url"], "https://fixture.test/redirect")
        with self.assertRaisesRegex(CachePolicyError, "cache response"):
            policy.check()
        self.assertEqual(policy.drain_events(), [event])
        self.assertEqual(policy.drain_events(), [])
        policy.reset_observation()
        self.assertEqual(policy.snapshot()["cache_hits"], [])
        policy.check()

    def test_http304_is_a_cache_violation(self):
        policy = self.policy()
        policy._handle_event({"sessionId": "child", "method": "Network.responseReceivedExtraInfo",
                              "params": {"requestId": "request", "statusCode": 304}})
        self.assertEqual(policy.snapshot()["cache_hits"][0]["reasons"], ["HTTP304"])
        with self.assertRaises(CachePolicyError):
            policy.check()

    def test_close_is_idempotent_even_before_start(self):
        policy = ChromiumCachePolicy(Mock())
        policy._socket = Mock()
        policy.close()
        policy.close()
        policy._socket.close.assert_called_once()

    def deferred_service_worker(self, *, error=None, drop_enable=False):
        policy = ChromiumCachePolicy(Mock(), timeout=0.02)
        policy._connected = True
        policy._socket = Mock()
        sent = []

        def send(raw):
            message = json.loads(raw)
            sent.append(message)
            # Model Chromium: replies are deferred until startup is released.
            if message['method'] != 'Runtime.runIfWaitingForDebugger':
                return
            for command in sent:
                if drop_enable and command['method'] == 'Network.enable':
                    continue
                item = policy._pending.get(command['id'])
                if item is None:
                    continue
                item['reply'] = ({'error': error} if error and command['method'] == 'Network.setCacheDisabled'
                                 else {'result': {}})
                item['event'].set()

        policy._socket.send.side_effect = send
        self.attach(policy, 'service_worker')
        return policy, sent

    def test_new_service_worker_sends_policy_then_resume_before_waiting(self):
        policy, sent = self.deferred_service_worker()
        self.assertEqual([item['method'] for item in sent], [
            'Network.enable', 'Network.setCacheDisabled', 'Network.setBypassServiceWorker',
            'Target.setAutoAttach', 'Runtime.runIfWaitingForDebugger'])
        self.assertTrue(all(item['sessionId'] == 'session' for item in sent))
        self.assertTrue(policy.snapshot()['targets'][0]['initialized'])
        self.assertFalse(policy.snapshot()['targets'][0]['paused'])
        self.assertEqual(policy.snapshot()['errors'], [])
        self.assertEqual(policy._pending, {})

    def test_new_service_worker_policy_failure_still_rejects_capture(self):
        policy, _ = self.deferred_service_worker(error='cache policy denied')
        with self.assertRaisesRegex(CachePolicyError, 'cache policy denied'):
            policy._raise_errors()
        self.assertFalse(policy.snapshot()['targets'][0]['initialized'])
        self.assertFalse(policy.snapshot()['targets'][0]['paused'])
        self.assertEqual(policy._pending, {})

    def test_new_service_worker_missing_reply_still_times_out(self):
        policy, _ = self.deferred_service_worker(drop_enable=True)
        with self.assertRaisesRegex(CachePolicyError, 'Network.enable timed out'):
            policy._raise_errors()
        self.assertFalse(policy.snapshot()['targets'][0]['initialized'])
        self.assertEqual(policy._pending, {})


class CacheHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        record = {"host": self.headers.get("Host"), "path": path,
                  "if_none_match": self.headers.get("If-None-Match"),
                  "if_modified_since": self.headers.get("If-Modified-Since")}
        with self.server.request_lock:
            self.server.requests.append(record)
        content_type = "text/html; charset=utf-8"
        if path == "/page":
            port = self.server.server_port
            body = (HEAD + '<script src="/script.js"></script><img id="image" src="/image.png">'
                    '<iframe src="/frame"></iframe>'
                    f'<iframe src="http://localhost:{port}/frame"></iframe>').encode()
        elif path == "/frame":
            body = (HEAD + '<script src="/frame.js"></script><img src="/frame.png">').encode()
        elif path in {"/script.js", "/frame.js"}:
            body, content_type = b"window.cacheFixtureScript = true;", "application/javascript"
        elif path in {"/image.png", "/frame.png"}:
            body, content_type = PNG, "image/png"
        elif path == "/sw-page":
            body = HEAD.encode()
        elif path in {"/sw.js", "/startup-sw.js"}:
            body = b"""
                self.addEventListener('install', event => event.waitUntil((async () => {
                    const cache = await caches.open('fixture-cache');
                    await cache.put('/sw-resource', new Response('from-sw-cache', {
                        headers: {'Content-Type': 'text/plain'}}));
                    await self.skipWaiting();
                })()));
                self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));
                self.addEventListener('fetch', event => {
                    if (new URL(event.request.url).pathname === '/sw-resource') {
                        event.respondWith(caches.match('/sw-resource'));
                    }
                });
            """
            if path == "/startup-sw.js":
                body += b"""
                    const startup = (async () => {
                        await (await fetch('/worker-resource')).text();
                        await (await fetch('/worker-resource')).text();
                    })();
                    self.addEventListener('install', event => event.waitUntil(startup));
                """
            content_type = "application/javascript"
        elif path == "/sw-resource":
            body, content_type = b"from-real-server", "text/plain"
        elif path == "/worker.js":
            body = b"""
                const child = new Worker('/nested-worker.js');
                child.onmessage = event => self.postMessage(event.data);
                child.onerror = event => self.postMessage({error: event.message});
            """
            content_type = "application/javascript"
        elif path == "/nested-worker.js":
            body = b"""
                (async () => {
                    const first = await (await fetch('/worker-resource')).text();
                    const second = await (await fetch('/worker-resource')).text();
                    self.postMessage([first, second]);
                })().catch(error => self.postMessage({error: String(error)}));
            """
            content_type = "application/javascript"
        elif path == "/worker-resource":
            body, content_type = b"worker-network", "text/plain"
        else:
            self.send_error(404)
            return
        # A conditional request would produce a bodyless 304, making an
        # accidental revalidation observable instead of silently passing.
        conditional = record["if_none_match"] or record["if_modified_since"]
        status = 304 if conditional else 200
        record["status"] = status
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("ETag", '"cache-fixture-v1"')
        self.send_header("Last-Modified", "Wed, 01 Jan 2025 00:00:00 GMT")
        self.send_header("Content-Length", str(0 if conditional else len(body)))
        self.end_headers()
        if not conditional:
            self.wfile.write(body)


@unittest.skipUnless(os.environ.get("RUN_BROWSER_CACHE_LIVE") == "1", "opt-in browser cache test")
class LiveBrowserCacheTests(unittest.TestCase):
    def setUp(self):
        browser = os.environ.get("CACHE_TEST_BROWSER", "edge")
        self.assertIn(browser, ('edge', 'chrome'))
        self.browser_name = browser
        driver_path = os.environ.get("CACHE_TEST_DRIVER") or os.environ.get("EDGE_TEST_DRIVER")
        self.assertTrue(driver_path and Path(driver_path).is_file(),
                        "CACHE_TEST_DRIVER or EDGE_TEST_DRIVER must name an existing driver")
        binary = discover_browser(browser)
        self.assertTrue(binary, "An installed Chromium browser is required")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CacheHandler)
        self.server.requests = []
        self.server.request_lock = threading.Lock()
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.close_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.profile = tempfile.TemporaryDirectory(prefix="codex-cache-edge-")
        self.addCleanup(self.profile.cleanup)
        options = ChromeOptions() if browser == 'chrome' else Options()
        options.binary_location = binary
        options.set_capability("goog:loggingPrefs" if browser == 'chrome' else "ms:loggingPrefs", {"performance": "ALL"})
        for argument in ("--headless=new", "--no-proxy-server", "--no-first-run",
                         "--no-default-browser-check", "--disable-background-networking",
                         "--disable-component-update", "--site-per-process",
                         f"--user-data-dir={self.profile.name}"):
            options.add_argument(argument)
        self.service = (TimedChromeService if browser == 'chrome' else TimedEdgeService)(driver_path)
        self.addCleanup(self.service.stop)
        self.driver = (webdriver.Chrome if browser == 'chrome' else webdriver.Edge)(service=self.service, options=options)
        self.addCleanup(self.driver.quit)
        self.driver.set_page_load_timeout(10)
        self.driver.set_script_timeout(10)
        self.driver.execute_cdp_cmd("Page.enable", {})
        self.driver.execute_cdp_cmd("Network.enable", {})

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)

    def counts(self):
        with self.server.request_lock:
            return Counter((item["host"], item["path"]) for item in self.server.requests)

    def events(self):
        return [json.loads(item["message"])["message"]
                for item in self.driver.get_log("performance")]

    @staticmethod
    def cache_events(events):
        return [event for event in events
                if event["method"] == "Network.requestServedFromCache" or (
                    event["method"] == "Network.responseReceived" and any(
                        event["params"]["response"].get(key, False)
                        for key in ("fromDiskCache", "fromServiceWorker", "fromPrefetchCache")))]

    def test_repeated_page_js_images_and_cross_site_iframe_reach_server(self):
        # Deliberately warm normal browser caches, proving these fixtures can
        # be cached before exercising the production helper in this session.
        self.driver.get(self.base_url + "/page")
        self.events()
        initial = self.counts()
        self.driver.get(self.base_url + "/page")
        cached_events = self.cache_events(self.events())
        self.assertTrue(cached_events, "The cache-enabled control must really reuse a resource")
        before = self.counts()
        self.assertTrue(any(before[key] == initial[key] for key in initial if key[1].endswith(".png")))

        port = self.server.server_port
        expected = {(f"127.0.0.1:{port}", path) for path in
                    ("/page", "/script.js", "/image.png", "/frame", "/frame.js", "/frame.png")}
        expected.update((f"localhost:{port}", path) for path in
                        ("/frame", "/frame.js", "/frame.png"))
        self.assertTrue(expected <= before.keys())
        # Preserve the negative control which exposed the original target
        # scope gap: the main-target helper alone cannot bypass OOPIF cache.
        configure_uncached_network(self.driver)
        self.driver.get(self.base_url + "/page")
        main_target_only = self.counts()
        cross_script = (f"localhost:{port}", "/frame.js")
        self.assertEqual(main_target_only[cross_script], before[cross_script])
        print(f"Real {self.browser_name} negative control: main-target-only CDP reused warmed cross-site iframe JS")
        before = main_target_only
        policy = ChromiumCachePolicy(self.driver).start()
        self.addCleanup(policy.close)
        request_offset = len(self.server.requests)
        response_counts = []
        for visit in range(1, 3):
            configure_uncached_network(self.driver)
            self.events()
            policy.reset_observation()
            self.driver.get(self.base_url + "/page")
            policy.check()
            events = policy.drain_events()
            responses = [event["params"]["response"] for event in events
                         if event["method"] == "Network.responseReceived"
                         and urlsplit(event["params"]["response"]["url"]).port == port]
            response_counts.append(len(responses))
            self.assertEqual(self.cache_events(events), [])
            counts = self.counts()
            for key in sorted(expected):
                self.assertEqual(counts[key] - before[key], visit, key)
            self.assertTrue(all(response["status"] == 200 for response in responses))
        with self.server.request_lock:
            uncached_requests = self.server.requests[request_offset:]
        self.assertEqual(len(uncached_requests), 18)
        self.assertTrue(all(item["status"] == 200 and not item["if_none_match"]
                            and not item["if_modified_since"] for item in uncached_requests))
        self.assertTrue(any(item["type"] == "iframe" and item["initialized"]
                            for item in policy.snapshot()["targets"]))
        print(f"Real {self.browser_name} cache: warm-cache control detected; 2 uncached visits each fetched "
              "all 9 main/same-site/cross-site resources, 18 HTTP 200, 0 conditional requests; "
              f"CDP response counts={response_counts}")

    def test_active_service_worker_cache_is_bypassed(self):
        self.driver.get(self.base_url + "/sw-page")
        registered = self.driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            (async () => {
                await navigator.serviceWorker.register('/sw.js');
                await navigator.serviceWorker.ready;
                if (!navigator.serviceWorker.controller) {
                    await new Promise(resolve => navigator.serviceWorker.addEventListener(
                        'controllerchange', resolve, {once: true}));
                }
                return !!navigator.serviceWorker.controller;
            })().then(done, error => done({error: String(error)}));
        """)
        self.assertIs(registered, True)
        # Existing HTTP-cache bypass alone must still expose the SW cache.
        self.driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})
        self.events()
        script = """
            const done = arguments[arguments.length - 1];
            fetch('/sw-resource').then(response => response.text()).then(done,
                error => done({error: String(error)}));
        """
        self.assertEqual(self.driver.execute_async_script(script), "from-sw-cache")
        control = self.events()
        self.assertTrue(any(event["method"] == "Network.responseReceived"
                            and event["params"]["response"].get("fromServiceWorker")
                            for event in control))
        key = (f"127.0.0.1:{self.server.server_port}", "/sw-resource")
        self.assertEqual(self.counts()[key], 0)

        configure_uncached_network(self.driver)
        policy = ChromiumCachePolicy(self.driver).start()
        self.addCleanup(policy.close)
        policy.reset_observation()
        self.events()
        for expected_count in (1, 2):
            self.assertEqual(self.driver.execute_async_script(script), "from-real-server")
            policy.check()
            events = policy.drain_events()
            responses = [event["params"]["response"] for event in events
                         if event["method"] == "Network.responseReceived"
                         and event["params"]["response"]["url"].endswith("/sw-resource")]
            self.assertEqual(len(responses), 1)
            self.assertEqual(responses[0]["status"], 200)
            self.assertEqual(self.cache_events(events), [])
            self.assertEqual(self.counts()[key], expected_count)
        requests = [item for item in self.server.requests if item["path"] == "/sw-resource"]
        self.assertTrue(all(item["status"] == 200 and not item["if_none_match"]
                            and not item["if_modified_since"] for item in requests))
        print(f"Real {self.browser_name} SW: active CacheStorage worker served control locally with HTTP cache disabled; "
              "production helper fetched same URL from server twice, 2 HTTP 200, 0 cache/SW flags")

    def test_new_service_worker_initializes_without_protocol_timeout(self):
        # Production installs the policy before navigation/registration. An
        # already-running SW (the test above) does not cover paused startup.
        self.driver.get(self.base_url + "/sw-page")
        configure_uncached_network(self.driver)
        policy = ChromiumCachePolicy(self.driver).start()
        self.addCleanup(policy.close)
        result = self.driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            (async () => {
                await navigator.serviceWorker.register('/startup-sw.js');
                await navigator.serviceWorker.ready;
                return await (await fetch('/sw-resource')).text();
            })().then(done, error => done({error: String(error)}));
        """)
        policy.check()
        self.assertEqual(result, "from-real-server")
        snapshot = policy.snapshot()
        workers = [item for item in snapshot['targets'] if item['type'] == 'service_worker']
        self.assertTrue(workers, snapshot)
        self.assertTrue(all(item['initialized'] for item in workers), snapshot)
        self.assertEqual(snapshot['errors'], [])
        self.assertEqual(snapshot['cache_hits'], [])
        # Cache rules and event coverage must apply to the worker's very first
        # script execution, not just page requests after registration resolves.
        requests = [item for item in self.server.requests if item['path'] == '/worker-resource']
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(item['status'] == 200 and not item['if_none_match']
                            and not item['if_modified_since'] for item in requests))
        responses = [event for event in policy.drain_events()
                     if event['method'] == 'Network.responseReceived'
                     and event['params']['response']['url'].endswith('/worker-resource')]
        # Browser and page auto-attach can observe the same SW via two sessions.
        self.assertEqual(len({event['params']['requestId'] for event in responses}), 2)

    def test_nested_workers_repeat_cacheable_fetches_on_network(self):
        self.driver.get(self.base_url + "/sw-page")
        configure_uncached_network(self.driver)
        policy = ChromiumCachePolicy(self.driver).start()
        self.addCleanup(policy.close)
        policy.reset_observation()
        result = self.driver.execute_async_script("""
            const done = arguments[arguments.length - 1];
            const worker = new Worker('/worker.js');
            worker.onmessage = event => done(event.data);
            worker.onerror = event => done({error: event.message});
            window.fixtureWorker = worker;
        """)
        self.assertEqual(result, ["worker-network", "worker-network"])
        policy.check()
        requests = [item for item in self.server.requests if item["path"] == "/worker-resource"]
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(item["status"] == 200 and not item["if_none_match"]
                            and not item["if_modified_since"] for item in requests))
        snapshot = policy.snapshot()
        workers = [item for item in snapshot["targets"] if item["type"] == "worker"
                   and item["url"].startswith(self.base_url + "/")]
        # Edge can retain a new-tab worker; assert the two fixture identities.
        self.assertEqual(len({item["target_id"] for item in workers}), 2, workers)
        self.assertTrue(all(item["initialized"] for item in workers))
        self.assertEqual(snapshot["cache_hits"], [])
        self.assertEqual(snapshot["errors"], [])
        print(f"Real {self.browser_name} nested workers: both worker targets initialized before execution; "
              "same max-age resource fetched twice, 2 HTTP 200, 0 cache flags")


if __name__ == "__main__":
    unittest.main()
