"""Opt-in local Edge regression for HIT's shared network-idle path.

Set RUN_BROWSER_LOADING_LIVE=1 and EDGE_TEST_DRIVER to an installed driver.
All three HIT hostnames resolve to the test server; no driver is downloaded.
"""

import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from unittest.mock import patch

from selenium import webdriver
from selenium.webdriver.edge.options import Options

from browser_discovery import discover_browser
from browser_service import TimedEdgeService
from wiki_fetcher import WikiFetcher, _initialize_chromium_network


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)
POST_BODY = b"post-response-" * 4096


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def record_request(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        with self.server.request_lock:
            self.server.requests.append({
                "method": self.command, "host": self.headers.get("Host"),
                "path": urlsplit(self.path).path, "body": body,
            })

    def send_body(self, body, content_type, *, delay=0.0, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            # Response headers must not make the collector stop before the
            # body completes; these responses deliberately stream in two parts.
            self.wfile.write(body[:1])
            self.wfile.flush()
            self.server.release.wait(delay)
            self.wfile.write(body[1:])
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        self.record_request()
        if self.path == "/after-dom-post":
            return self.send_body(POST_BODY, "text/plain", delay=1.2)
        self.send_body(b"missing", "text/plain", status=404)

    def do_GET(self):
        self.record_request()
        if self.path == "/slow.png":
            return self.send_body(PNG, "image/png", delay=0.35)
        if self.path == "/metadata-fetch":
            return self.send_body(b"metadata-complete", "text/plain", delay=0.7)
        if self.path == "/missing-resource":
            return self.send_body(b"missing-fixture", "text/plain", status=404)
        if self.path in {"/today2-legacy.png", "/myweb-legacy.png"}:
            # Both endpoints are reachable locally if CDP fails to block them.
            return self.send_body(PNG, "image/png")
        if self.path != "/":
            return self.send_body(b"missing", "text/plain", status=404)
        html = b"""<!doctype html><title>HIT shared idle fixture</title>
            <link rel="icon" href="data:,">
            <script>
            window.fixture = {postDone: false, missingDone: false, metadataDone: false};
            document.addEventListener('DOMContentLoaded', () => {
                fixture.domAt = performance.now();
                fetch('/after-dom-post', {method: 'POST', body: 'fixture-post'})
                    .then(r => r.text()).then(body => {
                        fixture.postDone = body === 'post-response-'.repeat(4096);
                        fixture.postLength = body.length;
                        fixture.postEndAt = performance.now();
                    });
                fetch('/missing-resource').then(async r => {
                    fixture.missingStatus = r.status;
                    fixture.missingDone = await r.text() === 'missing-fixture';
                });
            });
            </script>
            <body><img id="slow" src="/slow.png">
                <img src="http://today2.hit.edu.cn/today2-legacy.png">
                <img src="http://myweb.hit.edu.cn/myweb-legacy.png">
            </body>"""
        self.send_body(html, "text/html; charset=utf-8")


class MetadataEdge(webdriver.Edge):
    """Make metadata collection trigger a real late request before recheck."""

    @property
    def page_source(self):
        self.execute_script("""
            if (!fixture.metadataStarted) {
                fixture.metadataStarted = true;
                fixture.metadataStartAt = performance.now();
                fetch('/metadata-fetch').then(r => r.text()).then(body => {
                    fixture.metadataDone = body === 'metadata-complete';
                });
            }
        """)
        return super().page_source

    def quit(self):
        try:
            if hasattr(self, "fixture_snapshots") and not getattr(self, "fixture_quit_seen", False):
                self.fixture_quit_seen = True
                self.fixture_snapshots.append(self.execute_script("""
                    return {...fixture,
                        imageComplete: document.querySelector('#slow').complete,
                        imageWidth: document.querySelector('#slow').naturalWidth};
                """))
        finally:
            super().quit()


@unittest.skipUnless(os.environ.get("RUN_BROWSER_LOADING_LIVE") == "1", "opt-in browser test")
class LiveResourceTests(unittest.TestCase):
    def test_hit_uses_shared_idle_with_post_metadata_and_legacy_blocks(self):
        driver_path = os.environ.get("EDGE_TEST_DRIVER")
        self.assertTrue(driver_path and Path(driver_path).is_file(),
                        "EDGE_TEST_DRIVER must name an existing EdgeDriver executable")
        binary = discover_browser("edge")
        self.assertTrue(binary, "An installed Edge browser is required")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.release = threading.Event()
        server.request_lock = threading.Lock()
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        snapshots = []
        drivers = []
        test_case = self

        class Builder:
            name = "Edge HIT fixture"

            def build(self, key_log_path, profile_dir, proxy, **kwargs):
                test_case.assertEqual(kwargs, {})
                options = Options()
                options.binary_location = binary
                options.page_load_strategy = "normal"
                options.set_capability("ms:loggingPrefs", {"performance": "ALL"})
                resolver_rules = ", ".join(
                    f"MAP {host} 127.0.0.1:{server.server_port}"
                    for host in ("today.hit.edu.cn", "today2.hit.edu.cn", "myweb.hit.edu.cn")
                )
                for argument in (
                    "--headless=new", "--no-proxy-server", "--no-first-run",
                    "--no-default-browser-check", "--disable-background-networking",
                    "--disable-component-update", f"--user-data-dir={profile_dir}",
                    f"--ssl-key-log-file={key_log_path}",
                    f"--host-resolver-rules={resolver_rules}",
                ):
                    options.add_argument(argument)
                driver = MetadataEdge(service=TimedEdgeService(driver_path), options=options)
                driver.fixture_snapshots = snapshots
                drivers.append(driver)
                test_case.assertEqual(driver.capabilities["pageLoadStrategy"], "normal")
                return _initialize_chromium_network(driver)

        try:
            with tempfile.TemporaryDirectory() as tmp, \
                    patch.dict("wiki_fetcher.AVAILABLE_DRIVERS", {"edge": Builder()}):
                fetcher = WikiFetcher(Path(tmp), ["edge"], False)
                item_dir = fetcher._url_dir("fixture")
                record = fetcher._fetch_with("http://today.hit.edu.cn/", "edge", item_dir)
                self.assertIsNone(record.error)
                self.assertEqual(len(snapshots), 1)
                state = snapshots[0]
                self.assertTrue(state["postDone"])
                self.assertEqual(state["postLength"], len(POST_BODY))
                self.assertTrue(state["missingDone"])
                self.assertEqual(state["missingStatus"], 404)
                self.assertTrue(state["metadataDone"])
                self.assertTrue(state["imageComplete"])
                self.assertEqual(state["imageWidth"], 1)
                self.assertGreaterEqual(state["metadataStartAt"] - state["postEndAt"], 400)

                summary = record.network_summary
                self.assertEqual(summary["pending_count"], 0)
                self.assertEqual(summary["idle_seconds"], 0.5)
                self.assertGreaterEqual(summary["observed_idle_seconds"], 0.5)
                # The previous two-second quiet window violates this bound.
                self.assertLess(summary["observed_idle_seconds"], 1.5)
                requests = {urlsplit(r["url"]).path: r for r in summary["requests"]}
                for path in ("/", "/slow.png", "/after-dom-post", "/metadata-fetch"):
                    self.assertEqual(requests[path]["state"], "finished")
                    self.assertEqual(requests[path]["status"], 200)
                self.assertEqual(requests["/missing-resource"]["state"], "finished")
                self.assertEqual(requests["/missing-resource"]["status"], 404)
                post = requests["/after-dom-post"]
                self.assertGreaterEqual(post["finished_timestamp"] - post["started_timestamp"], 1.1)
                self.assertGreaterEqual(record.phase_timings["network_recheck"], 1.1)
                blocked = summary["failed_requests"]
                self.assertEqual({urlsplit(r["url"]).hostname for r in blocked},
                                 {"today2.hit.edu.cn", "myweb.hit.edu.cn"})
                self.assertEqual(len(blocked), 2, json.dumps(blocked, ensure_ascii=False, indent=2))
                self.assertTrue(all(r.get("blockedReason") == "inspector" for r in blocked))
                self.assertEqual(summary["cache_hit_requests"], [])

                persisted = json.loads((item_dir / "network_status_edge.json").read_text("utf-8"))
                self.assertIsNone(persisted["error"])
                self.assertEqual(persisted["network_summary"], summary)
                with server.request_lock:
                    received = list(server.requests)
                self.assertFalse(any("legacy.png" in r["path"] for r in received))
                posts = [r for r in received if r["method"] == "POST"]
                self.assertEqual([(r["path"], r["body"]) for r in posts],
                                 [("/after-dom-post", b"fixture-post")])
                print("Real Edge HIT: complete 1.2s POST and PNG, recorded 404, "
                      "two legacy hosts blocked, metadata fetch rechecked; "
                      f"idle={summary['observed_idle_seconds']:.3f}s")
        finally:
            for driver in drivers:
                if driver.service.process and driver.service.process.poll() is None:
                    policy = vars(driver).get("_capture_cache_policy")
                    if policy is not None:
                        policy.close()
                    driver.quit()
            server.release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
