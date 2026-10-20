"""Owned localhost acceptance: RUN_WIKIPEDIA_PACING_LIVE=1, WIKI_TEST_BROWSER."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from wiki_fetcher import WikiFetcher


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/slow":
            self.server.release.wait(4.5)
            body = b"slow fetch completed"
        else:
            body = (b'<!doctype html><title>Wiki pacing</title><link rel="icon" href="data:,">'
                    b'<script>window.addEventListener("load", () => {fetch("/slow")'
                    b'.then(r=>r.text()).then(()=>document.body.dataset.done="yes");});</script>'
                    b'<body>Test</body>')
        self.send_response(503 if self.path == "/error" else 200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass


@unittest.skipUnless(os.getenv("RUN_WIKIPEDIA_PACING_LIVE") == "1", "opt-in live browser")
class WikipediaPacingLiveTests(unittest.TestCase):
    def setUp(self):
        self.browser = os.environ.get("WIKI_TEST_BROWSER", "edge")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.release = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.temp = tempfile.TemporaryDirectory(prefix="wiki-pacing-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def test_wiki_and_other_sites_use_current_completion_policy(self):
        for wikipedia in (True, False):
            with self.subTest(wikipedia=wikipedia):
                fetcher = WikiFetcher(self.root, [self.browser], False, save_html=True)
                with patch("wiki_fetcher._is_wikipedia_url", return_value=wikipedia):
                    record = fetcher._fetch_with(self.base + "/page", self.browser, self.root)
                self.assertIsNone(record.error, record.error)
                self.assertEqual(record.network_summary["completion_policy"], "target_document")
                self.assertEqual(record.network_summary["completion_reason"], "resource_stall")
                self.assertEqual(record.network_summary["pending_count"], 1)
                print(f"{self.browser}: wiki={wikipedia}, response_ms={record.response_time_ms:.0f}", flush=True)

    def test_wiki_rejects_http_error_document(self):
        with patch("wiki_fetcher._is_wikipedia_url", return_value=True):
            record = WikiFetcher(self.root, [self.browser], False)._fetch_with(
                self.base + "/error", self.browser, self.root)
        self.assertIsNotNone(record.error)


if __name__ == "__main__":
    unittest.main()
