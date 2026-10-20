"""Echo-server language acceptance for the production Wikipedia dispatch path."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from wiki_fetcher import WikiFetcher


class LanguageHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.headers_seen.append((self.path, self.headers.get("Accept-Language")))
        self.server.user_agents_seen.append((self.path, self.headers.get("User-Agent")))
        body = (b'<!doctype html><title>Language probe</title><link rel="icon" href="data:,">'
                b'<body><script>document.body.dataset.language=navigator.language;'
                b'document.body.dataset.languages=JSON.stringify(navigator.languages);'
                b'fetch("/resource");</script></body>')
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(os.getenv("RUN_WIKIPEDIA_LANGUAGE_LIVE") == "1", "opt-in live browser")
class WikipediaLanguageLiveTests(unittest.TestCase):
    def test_wikipedia_language_on_navigation_and_fetch(self):
        self._exercise("")

    def test_wikipedia_bot_identity_on_navigation_and_fetch(self):
        self._exercise("operator@example.invalid")

    def _exercise(self, contact):
        browser = os.environ.get("WIKI_TEST_BROWSER", "edge")
        server = ThreadingHTTPServer(("127.0.0.1", 0), LanguageHandler)
        server.headers_seen = []
        server.user_agents_seen = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(prefix="wiki-language-") as tmp:
                root = Path(tmp)
                fetcher = WikiFetcher(root, [browser], False, save_html=True)
                with patch("wiki_fetcher._is_wikipedia_url", return_value=True), \
                     patch.dict(os.environ, {"WIKIPEDIA_BOT_CONTACT": contact}):
                    record = fetcher._fetch_with(f"http://127.0.0.1:{server.server_port}/page", browser, root)
                self.assertIsNone(record.error, record.error)
                html = (root / f"body_{browser}.html").read_text(encoding="utf-8")
                self.assertIn('data-language="zh-CN"', html)
                self.assertIn('data-languages="[&quot;zh-CN&quot;', html)
                headers = dict(server.headers_seen)
                for path in ("/page", "/resource"):
                    self.assertEqual(headers[path].split(",")[0].strip(), "zh-CN")
                    user_agent = dict(server.user_agents_seen)[path]
                    if contact:
                        self.assertEqual(user_agent, f"EncryptedTrafficCaptureBot/1.0 ({contact})")
                    else:
                        self.assertNotIn("EncryptedTrafficCaptureBot", user_agent)
                print(json.dumps({"browser": browser, "navigator.language": "zh-CN",
                                  "accept_language": headers,
                                  "user_agents": dict(server.user_agents_seen)}, ensure_ascii=True), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == "__main__":
    unittest.main()
