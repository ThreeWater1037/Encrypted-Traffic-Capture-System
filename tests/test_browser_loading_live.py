"""Opt-in real Edge checks: set RUN_BROWSER_LOADING_LIVE=1 and EDGE_TEST_DRIVER."""

import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from selenium import webdriver
from selenium.webdriver.edge.options import Options
from selenium.webdriver.edge.service import Service

from browser_discovery import discover_browser
from browser_loading import wait_for_resources
from wiki_fetcher import WikiFetcher

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_error(404)
            return
        if self.path in {"/slow.png", "/stalled.png"}:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            try:
                if self.path == "/stalled.png":
                    self.server.release.wait(20)
                    self.wfile.write(PNG)
                else:
                    for offset in range(0, len(PNG), 5):
                        self.wfile.write(PNG[offset:offset + 5])
                        self.wfile.flush()
                        time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        html = ("<html><title>fixture</title><body>article"
                '<img id="slow" src="/slow.png">'
                '<img src="/stalled.png">'
                '<img src="http://myweb.hit.edu.cn/old.png">'
                '<img src="http://today2.hit.edu.cn/old.png">'
                "</body></html>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)


@unittest.skipUnless(os.environ.get("RUN_BROWSER_LOADING_LIVE") == "1", "opt-in browser test")
class LiveResourceTests(unittest.TestCase):
    def test_streaming_and_stalled_images_and_recapture_files(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.release = threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        options = Options()
        options.binary_location = discover_browser("edge")
        options.page_load_strategy = "eager"
        options.set_capability("ms:loggingPrefs", {"performance": "ALL"})
        options.add_argument("--headless=new")
        options.add_argument("--no-proxy-server")
        options.add_argument("--host-resolver-rules=MAP today.hit.edu.cn 127.0.0.1")
        driver = webdriver.Edge(service=Service(os.environ["EDGE_TEST_DRIVER"]), options=options)
        url = f"http://today.hit.edu.cn:{server.server_port}/"
        captured = []
        def observe(real_driver, skipped):
            started = time.monotonic()
            wait_for_resources(real_driver, skipped)
            captured.append((time.monotonic() - started, real_driver.execute_script(
                "return [document.querySelector('#slow').complete, "
                "document.querySelector('#slow').naturalWidth]")))
        class Builder:
            name = "Edge fixture"
            def build(self, *args, **kwargs):
                assert kwargs == {"skip_stalled_resources": True}
                return driver
        try:
            with tempfile.TemporaryDirectory() as tmp, \
                 patch.dict("wiki_fetcher.AVAILABLE_DRIVERS", {"edge": Builder()}), \
                 patch("wiki_fetcher.wait_for_resources", side_effect=observe):
                fetcher = WikiFetcher(Path(tmp), ["edge"], False)
                item_dir = fetcher._url_dir("fixture")
                record = fetcher._fetch_with(url, "edge", item_dir)
                self.assertIsNone(record.error)
                self.assertEqual(captured[0][1], [True, 1])
                self.assertGreater(captured[0][0], 5)
                self.assertLess(captured[0][0], 15)
                self.assertEqual(len(record.skipped_resources), 3)
                reasons = [item["reason"] for item in record.skipped_resources]
                self.assertEqual(reasons.count("isolated_legacy_host"), 2)
                self.assertEqual(reasons.count("no_progress_for_5_seconds"), 1)
                report = json.loads((item_dir / "resource_status_edge.json").read_text("utf-8"))
                self.assertTrue(report["needs_recapture"])
                self.assertEqual(report["page_url"], url)
                self.assertEqual(len(report["skipped_resources"]), 3)
                history = (Path(tmp) / "pages_needing_recapture.jsonl").read_text("utf-8")
                self.assertEqual(json.loads(history)["page_url"], url)
                print(f"Live result: {captured[0][0]:.2f}s; streaming image intact; "
                      "2 legacy resources isolated; 1 stalled resource marked")
        finally:
            if driver.service.process and driver.service.process.poll() is None:
                driver.quit()
            server.release.set()
            server.shutdown()
            server.server_close()
