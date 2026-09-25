"""Owned localhost acceptance: RUN_TARGET_COMPLETION_LIVE=1, TARGET_TEST_BROWSER."""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from wiki_fetcher import WikiFetcher, UrlEntry, _prepare_navigation


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.hits[self.path] += 1
        path = urlsplit(self.path).path
        port = self.server.server_port
        if path == "/page":
            body = (f'<!doctype html><title>Target OK</title><link rel="icon" href="data:,">'
                    f'<script>fetch("http://localhost:{port}/g/collect?v=2&en=page_view").catch(()=>{{}});'
                    'fetch("/g/collect-other");</script>'
                    f'<iframe src="http://localhost:{port}/hung"></iframe><body>Target content</body>').encode()
        elif path in {"/hung", "/hung-document"}:
            body = b'<!doctype html><title>unfinished</title><body>' + b'x' * 1000
        else:
            body = b'<!doctype html><title>fixture</title><body>fixture</body>'
        self.send_response(503 if path == "/error" else 200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            if path in {"/hung", "/hung-document"}:
                self.wfile.write(body[:30])
                self.wfile.flush()
                self.server.release.wait(15)
                self.wfile.write(body[30:])
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


@unittest.skipUnless(os.environ.get("RUN_TARGET_COMPLETION_LIVE") == "1", "opt-in live browser")
class TargetCompletionLiveTests(unittest.TestCase):
    def setUp(self):
        self.browser = os.environ.get("TARGET_TEST_BROWSER", "edge")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.hits, self.server.release = Counter(), threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.temp = tempfile.TemporaryDirectory(prefix="target-completion-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def close_server(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def test_stalled_iframe_does_not_fail_target_and_analytics_query_is_blocked(self):
        rule = f"http://localhost:{self.server.server_port}/g/collect"
        with patch("browser_request_policy.FORBES_ANALYTICS_URL", rule), \
             patch("wiki_fetcher.blocked_urls_for_page", return_value=[rule]):
            fetcher = WikiFetcher(self.root, [self.browser], False)
            record = fetcher._fetch_with(self.base + "/page", self.browser, self.root)
        self.assertIsNone(record.error, record.error)
        self.assertEqual(record.page_title, "Target OK")
        self.assertLess(record.phase_timings["navigation"], 5)
        self.assertLess(record.phase_timings["network_idle"], 5)
        summary = record.network_summary
        self.assertFalse(summary["network_complete"])
        self.assertEqual(summary["completion_reason"], "resource_stall")
        self.assertEqual(summary["target_document"]["status"], 200)
        self.assertTrue(any(urlsplit(r["url"]).path == "/hung" for r in summary["pending_requests"]))
        blocked = summary["intentionally_blocked_requests"]
        self.assertEqual(len(blocked), 1, json.dumps(summary))
        self.assertEqual(blocked[0]["policy_reason"], "forbes_analytics_exclusion")
        self.assertEqual(self.server.hits["/g/collect?v=2&en=page_view"], 0)
        self.assertEqual(self.server.hits["/g/collect-other"], 1)
        # Background browser HTTPS can also create a keylog. Test artifact absence explicitly.
        keylog = self.root / f"tls_keys_{self.browser}.log"
        keylog.unlink(missing_ok=True)
        self.assertFalse(fetcher._mark_complete(
            UrlEntry("1", "test", self.base + "/page"), self.root, self.browser, record))
        print(f"{self.browser}: target complete, iframe warning, GA query blocked; "
              f"navigation={record.phase_timings['navigation']:.2f}s "
              f"resource_wait={record.phase_timings['network_idle']:.2f}s")

    def test_target_http_failure_is_fast_and_has_no_checkpoint(self):
        fetcher = WikiFetcher(self.root, [self.browser], False)
        record = fetcher._fetch_with(self.base + "/error", self.browser, self.root)
        self.assertIn("503", record.error or "")
        self.assertFalse(fetcher._mark_complete(
            UrlEntry("1", "error", self.base + "/error"), self.root, self.browser, record))

    def test_navigation_timeout_remains_failure_with_short_deadline(self):
        def prepare(driver, url):
            _prepare_navigation(driver, url)
            self.assertEqual(driver.timeouts.page_load, 30)
            driver.set_page_load_timeout(1)
        with patch("wiki_fetcher._prepare_navigation", side_effect=prepare):
            record = WikiFetcher(self.root, [self.browser], False)._fetch_with(
                self.base + "/hung-document", self.browser, self.root)
        self.assertIsNotNone(record.error)
        self.assertNotIn("navigation", record.phase_timings)
