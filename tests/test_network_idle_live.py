"""Opt-in localhost checks; set RUN_NETWORK_IDLE_LIVE=1 and EDGE_TEST_DRIVER.

For Firefox also set NETWORK_TEST_BROWSER=firefox and GECKODRIVER_PATH.

Uses an installed EdgeDriver, never a driver download. Each test owns its Edge
profile and local HTTP server; no test requires public internet access.
"""

import base64
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.edge.options import Options
from browser_service import TimedEdgeService as Service

from browser_discovery import discover_browser
from browser_loading import NetworkIdleTracker, wait_for_network_idle


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
PAGE_HEAD = '<!doctype html><title>Network idle fixture</title><link rel="icon" href="data:,">'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, body, content_type, *, delay=0.0, stalled=False):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            if stalled:
                # Headers arrive, but the response remains unfinished until
                # teardown releases it. This is a real pending network body.
                self.wfile.write(body[:1])
                self.wfile.flush()
                self.server.release.wait(30.0)
                self.wfile.write(body[1:])
            else:
                self.server.release.wait(delay)
                self.wfile.write(body)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/before-load.png":
            return self.send_body(PNG, "image/png", delay=0.35)
        if path == "/iframe-slow.png":
            return self.send_body(PNG, "image/png", delay=1.2)
        if path in {"/after-dom-xhr", "/after-load-fetch"}:
            return self.send_body(b"complete", "text/plain", delay=1.2)
        if path == "/never-finishes":
            return self.send_body(b"unfinished response", "text/plain", stalled=True)
        if path == "/delayed-requests":
            body = PAGE_HEAD + """
                <script>
                window.fixture = {xhrDone: false, fetchDone: false};
                document.addEventListener('DOMContentLoaded', () => {
                    fixture.domAt = performance.now();
                    setTimeout(() => {
                        fixture.xhrAt = performance.now();
                        const xhr = new XMLHttpRequest();
                        xhr.open('GET', '/after-dom-xhr');
                        xhr.onload = () => { fixture.xhrDone = xhr.responseText === 'complete'; };
                        xhr.send();
                    }, 50);
                });
                window.addEventListener('load', () => {
                    fixture.loadAt = performance.now();
                    setTimeout(async () => {
                        fixture.fetchAt = performance.now();
                        const response = await fetch('/after-load-fetch');
                        fixture.fetchDone = await response.text() === 'complete';
                    }, 200);
                });
                </script>
                <img id="slow" src="/before-load.png">
            """
        elif path == "/delayed-iframe":
            body = PAGE_HEAD + """
                <script>
                window.fixture = {frameDone: false};
                window.addEventListener('load', () => {
                    fixture.loadAt = performance.now();
                    setTimeout(() => {
                        fixture.frameAt = performance.now();
                        const frame = document.createElement('iframe');
                        frame.id = 'late-frame';
                        frame.src = '/child-frame';
                        document.body.appendChild(frame);
                    }, 200);
                });
                </script><body>Parent page</body>
            """
        elif path == "/child-frame":
            body = PAGE_HEAD + """
                <script>
                window.addEventListener('load', () => {
                    parent.fixture.frameDone = document.querySelector('#child-image').naturalWidth === 1;
                });
                </script>
                <img id="child-image" src="/iframe-slow.png">
            """
        elif path == "/renavigate-frame":
            body = PAGE_HEAD + """
                <iframe id="existing-frame" src="/initial-frame"></iframe>
                <script>
                window.fixture = {frameDone: false};
                window.addEventListener('load', () => setTimeout(() => {
                    document.querySelector('#existing-frame').src = '/slow-document';
                }, 200));
                </script>
            """
        elif path == "/initial-frame":
            body = PAGE_HEAD + "Initial child document"
        elif path == "/slow-document":
            # Delay response headers so the new loader has not committed yet.
            self.server.release.wait(1.2)
            body = PAGE_HEAD + "<script>parent.fixture.frameDone = true;</script>New child"
        elif path == "/timeout":
            body = PAGE_HEAD + """
                <script>
                window.addEventListener('load', () => {
                    fetch('/never-finishes').then(response => response.text());
                });
                </script><body>Pending response</body>
            """
        else:
            self.send_error(404)
            return
        self.send_body(body.encode("utf-8"), "text/html; charset=utf-8")


@unittest.skipUnless(os.environ.get("RUN_NETWORK_IDLE_LIVE") == "1", "opt-in browser test")
class LiveNetworkIdleTests(unittest.TestCase):
    def setUp(self):
        self.browser = os.environ.get("NETWORK_TEST_BROWSER", "edge")
        if self.browser == "firefox":
            return self.setup_firefox()
        driver_path = os.environ.get("EDGE_TEST_DRIVER")
        self.assertTrue(driver_path and Path(driver_path).is_file(),
                        "EDGE_TEST_DRIVER must name an existing EdgeDriver executable")
        binary = discover_browser("edge")
        self.assertTrue(binary, "An installed Edge browser is required")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.release = threading.Event()
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.close_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

        self.profile = tempfile.TemporaryDirectory(prefix="codex-network-idle-edge-")
        self.addCleanup(self.profile.cleanup)
        options = Options()
        options.binary_location = binary
        options.page_load_strategy = "normal"
        options.set_capability("ms:loggingPrefs", {"performance": "ALL"})
        for argument in ("--headless=new", "--no-proxy-server", "--no-first-run",
                         "--no-default-browser-check", "--disable-background-networking",
                         "--disable-component-update", f"--user-data-dir={self.profile.name}"):
            options.add_argument(argument)
        self.service = Service(driver_path)
        self.addCleanup(self.service.stop)
        self.driver = webdriver.Edge(service=self.service, options=options)
        self.addCleanup(self.driver.quit)
        self.driver.set_page_load_timeout(10)
        self.driver.execute_cdp_cmd("Page.enable", {})
        self.driver.execute_cdp_cmd("Network.enable", {})
        self.driver.execute_cdp_cmd("Network.setCacheDisabled", {"cacheDisabled": True})

    def setup_firefox(self):
        from wiki_fetcher import FirefoxDriver
        self.assertTrue(Path(os.environ["GECKODRIVER_PATH"]).is_file())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.release = threading.Event()
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.close_server)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        profile = tempfile.TemporaryDirectory(prefix="codex-network-idle-firefox-")
        self.addCleanup(profile.cleanup)
        self.driver = FirefoxDriver().build(Path(profile.name) / "keys.log", Path(profile.name))
        self.addCleanup(self.driver.quit)
        self.addCleanup(self.driver._capture_firefox_network.close)
        self.driver.set_page_load_timeout(10)

    def close_server(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=3)

    def navigate(self, path):
        if self.browser == "firefox":
            from wiki_fetcher import _prepare_navigation
            _prepare_navigation(self.driver, self.base_url + path)
            self.driver.get(self.base_url + path)
            return
        self.driver.get_log("performance")
        self.driver.get(self.base_url + path)

    @staticmethod
    def requests_by_path(summary):
        return {urlsplit(item["url"]).path: item for item in summary["requests"]}

    def assert_clean_finish(self, summary):
        self.assertEqual(summary["pending_count"], 0)
        self.assertEqual(summary["failed_requests"], [])
        self.assertGreaterEqual(summary["observed_idle_seconds"], 0.5)

    def test_normal_navigation_keeps_delayed_dom_xhr_and_post_load_fetch(self):
        self.navigate("/delayed-requests")
        summary = wait_for_network_idle(self.driver, idle_seconds=0.5, timeout=6)
        self.assert_clean_finish(summary)
        requests = self.requests_by_path(summary)
        for path, resource_type in (("/before-load.png", "Image"),
                                    ("/after-dom-xhr", "XHR"),
                                    ("/after-load-fetch", "Fetch")):
            self.assertIn(path, requests)
            item = requests[path]
            self.assertEqual(item["state"], "finished")
            self.assertEqual(item["type"], resource_type)
        for path in ("/after-dom-xhr", "/after-load-fetch"):
            item = requests[path]
            self.assertGreaterEqual(item["finished_timestamp"] - item["started_timestamp"], 1.1)
        state = self.driver.execute_script("return {...fixture, imageWidth: document.querySelector('#slow').naturalWidth}")
        self.assertTrue(state["xhrDone"])
        self.assertTrue(state["fetchDone"])
        self.assertEqual(state["imageWidth"], 1)
        self.assertGreaterEqual(state["xhrAt"] - state["domAt"], 40)
        self.assertGreaterEqual(state["fetchAt"] - state["loadAt"], 180)
        print(f"Real {self.browser}: DCL XHR and load+200ms fetch complete; wait={summary['wait_seconds']:.3f}s")

    def test_iframe_inserted_after_load_keeps_its_delayed_image(self):
        self.navigate("/delayed-iframe")
        summary = wait_for_network_idle(self.driver, idle_seconds=0.5, timeout=6)
        self.assert_clean_finish(summary)
        requests = self.requests_by_path(summary)
        for path in ("/child-frame", "/iframe-slow.png"):
            self.assertIn(path, requests)
            self.assertEqual(requests[path]["state"], "finished")
        self.assertNotEqual(requests["/iframe-slow.png"]["frame_id"],
                            requests["/delayed-iframe"]["frame_id"])
        image = requests["/iframe-slow.png"]
        self.assertGreaterEqual(image["finished_timestamp"] - image["started_timestamp"], 1.1)
        state = self.driver.execute_script("return fixture")
        self.assertTrue(state["frameDone"])
        self.assertGreaterEqual(state["frameAt"] - state["loadAt"], 180)
        print(f"Real {self.browser}: post-load iframe and 1.2s image complete; wait={summary['wait_seconds']:.3f}s")

    def test_pending_body_raises_timeout_with_request_evidence(self):
        self.navigate("/timeout")
        started = time.monotonic()
        with self.assertRaises(TimeoutException) as caught:
            wait_for_network_idle(self.driver, idle_seconds=0.5, timeout=0.8)
        elapsed = time.monotonic() - started
        summary = caught.exception.network_idle_summary
        pending = {urlsplit(item["url"]).path: item for item in summary["pending_requests"]}
        self.assertIn("/never-finishes", pending)
        self.assertEqual(pending["/never-finishes"]["state"], "pending")
        self.assertGreaterEqual(summary["pending_count"], 1)
        self.assertGreaterEqual(elapsed, 0.8)
        self.assertLess(elapsed, 5, "The 0.8s timeout should remain bounded under local driver overhead")
        print(f"Real {self.browser}: unfinished fetch failed at bounded timeout ({elapsed:.3f}s)")

    def test_existing_iframe_navigation_waits_for_slow_document(self):
        self.navigate("/renavigate-frame")
        summary = wait_for_network_idle(self.driver, idle_seconds=0.5, timeout=6)
        self.assert_clean_finish(summary)
        document = self.requests_by_path(summary)["/slow-document"]
        self.assertEqual(document["type"], "Document")
        self.assertEqual(document["state"], "finished")
        self.assertGreaterEqual(document["finished_timestamp"] - document["started_timestamp"], 1.1)
        self.assertTrue(self.driver.execute_script("return fixture.frameDone"))
        print(f"Real {self.browser}: existing iframe slow navigation complete; wait={summary['wait_seconds']:.3f}s")

    def test_recheck_waits_for_request_started_during_metadata(self):
        self.navigate("/initial-frame")
        tracker = NetworkIdleTracker(self.driver, idle_seconds=0.5, timeout=6)
        tracker.wait()
        self.driver.execute_script("""
            window.metadataFetchDone = false;
            fetch('/after-load-fetch').then(r => r.text()).then(body => {
                window.metadataFetchDone = body === 'complete';
            });
        """)
        started = time.monotonic()
        summary = tracker.wait()
        self.assert_clean_finish(summary)
        self.assertTrue(self.driver.execute_script("return window.metadataFetchDone"))
        self.assertEqual(self.requests_by_path(summary)["/after-load-fetch"]["state"],"finished")
        self.assertGreaterEqual(time.monotonic()-started, 1.5)
        print(f"Real {self.browser}: metadata-stage fetch was included before capture stop")


if __name__ == "__main__":
    unittest.main()
