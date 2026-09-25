"""Opt-in localhost request-blocking acceptance for Chrome, Edge and Firefox.

RUN_REQUEST_BLOCKING_LIVE=1; BLOCK_TEST_BROWSER selects an installed browser.
Use CHROMEDRIVER_PATH / EDGEDRIVER_PATH / GECKODRIVER_PATH for offline drivers.
"""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from selenium.common.exceptions import TimeoutException

from browser_loading import NetworkIdleTracker
from wiki_fetcher import AVAILABLE_DRIVERS, PacketCapture, WikiFetcher, _prepare_navigation


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.hits[self.path] += 1
        port = self.server.server_port
        scheme = getattr(self.server, "scheme", "http")
        blocked = f"{scheme}://localhost:{port}/recaptcha/api2/aframe"
        script = f"fetch('{blocked}').then(r=>r.text()).catch(()=>{{}});"
        if self.path == "/":
            body = (f'<!doctype html><title>blocking fixture</title><link rel="icon" href="data:,">'
                    f'<script>{script}fetch("{blocked}?keep=1").then(r=>r.text());'
                    f'fetch("{blocked}-other").then(r=>r.text());fetch("/normal.js").then(r=>r.text());'
                    'new Worker("/worker.js");</script>'
                    f'<iframe src="{scheme}://localhost:{port}/child"></iframe>'
                    f'<iframe src="{blocked}"></iframe>').encode()
            mime = "text/html"
        elif self.path == "/child":
            body = f'<!doctype html><script>{script}</script>'.encode()
            mime = "text/html"
        elif self.path == "/worker.js":
            body, mime = script.encode(), "application/javascript"
        else:
            body, mime = b"normal resource", "text/plain"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(os.environ.get("RUN_REQUEST_BLOCKING_LIVE") == "1", "opt-in browser test")
class RequestBlockingLiveTests(unittest.TestCase):
    def setUp(self):
        self.browser = os.environ.get("BLOCK_TEST_BROWSER", "edge")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.hits = Counter()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.blocked = f"http://localhost:{self.server.server_port}/recaptcha/api2/aframe"
        self.temp = tempfile.TemporaryDirectory(prefix="capture-block-test-")
        self.addCleanup(self.temp.cleanup)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def test_exact_rule_covers_page_child_and_worker_and_can_be_cleared(self):
        root = Path(self.temp.name)
        driver = AVAILABLE_DRIVERS[self.browser].build(root / "keys.log", root)
        self.addCleanup(driver.quit)
        self.addCleanup(driver._capture_cache_policy.close)
        with patch("browser_request_policy.FORBES_RECAPTCHA_URL", self.blocked):
            _prepare_navigation(driver, "https://www.forbeschina.com/article")
        driver.set_page_load_timeout(15)
        try:
            driver.get(self.base + "/")
        except TimeoutException:
            self.fail(json.dumps({"hits": self.server.hits,
                                  "policy": driver._capture_cache_policy.snapshot()}, indent=2))
        try:
            result = NetworkIdleTracker(driver, timeout=15).wait()
        except TimeoutException as exc:
            self.fail(json.dumps(exc.network_idle_summary, indent=2))
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(self.server.hits["/recaptcha/api2/aframe"], 0)
        self.assertEqual(self.server.hits["/recaptcha/api2/aframe?keep=1"], 1)
        self.assertEqual(self.server.hits["/recaptcha/api2/aframe-other"], 1)
        self.assertEqual(self.server.hits["/normal.js"], 1)
        excluded = result["intentionally_blocked_requests"]
        self.assertEqual(len(excluded), 4, json.dumps(result, indent=2))
        self.assertTrue(all(r["url"] == self.blocked and
                            r["policy_reason"] == "forbes_recaptcha_exclusion" for r in excluded))
        _prepare_navigation(driver, self.base + "/")
        driver.get(self.base + "/")
        result = NetworkIdleTracker(driver, timeout=15).wait()
        self.assertEqual(self.server.hits["/recaptcha/api2/aframe"], 4)
        self.assertEqual(result["intentionally_blocked_requests"], [])

    def test_fetcher_persists_exclusion_and_completes(self):
        root = Path(self.temp.name)
        def prepare(driver, url):
            _prepare_navigation(driver, url)
            driver.set_page_load_timeout(15)
        # Only replace the fixture URL; production domain boundaries are tested separately.
        with patch("wiki_fetcher.blocked_urls_for_page", return_value=[self.blocked]), \
             patch("wiki_fetcher._prepare_navigation", side_effect=prepare):
            record = WikiFetcher(root, [self.browser], False)._fetch_with(self.base + "/", self.browser, root)
        self.assertIsNone(record.error, record.error)
        self.assertGreater(record.html_length, 0)
        status = json.loads((root / f"network_status_{self.browser}.json").read_text(encoding="utf-8"))
        self.assertEqual(status["request_blocking"]["urls"], [self.blocked])
        self.assertEqual(len(status["network_summary"]["intentionally_blocked_requests"]), 4)

    @unittest.skipUnless(os.environ.get("RUN_REQUEST_BLOCKING_CAPTURE_LIVE") == "1", "opt-in TLS/PCAP test")
    def test_blocking_preserves_decryptable_https_capture(self):
        root = Path(self.temp.name)
        config = root / "openssl.cnf"
        config.write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="ascii")
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-config", str(config), "-keyout", str(key), "-out", str(cert),
                        "-days", "1", "-subj", "/CN=localhost"],
                       check=True, capture_output=True, timeout=15)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.hits, server.scheme = Counter(), "https"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        blocked = f"https://localhost:{server.server_port}/recaptcha/api2/aframe"
        tool = PacketCapture._find_tool()
        self.assertTrue(tool and tool[0] == "tshark")
        interfaces = PacketCapture._list_interfaces_tshark(tool[1])
        loopbacks = [name for name in interfaces if "loopback" in name.lower() or name in {"lo", "lo0"}]
        if interfaces == ["any"]:
            loopbacks = ["lo"]
        self.assertTrue(loopbacks)
        original_build = AVAILABLE_DRIVERS[self.browser].build
        def build(*args):
            driver = original_build(*args)
            if self.browser != "firefox":
                driver.execute_cdp_cmd("Security.setIgnoreCertificateErrors", {"ignore": True})
            return driver
        # Record only this fixture's loopback port; trust its test certificate only in this session.
        with patch("wiki_fetcher.blocked_urls_for_page", return_value=[blocked]), \
             patch.object(AVAILABLE_DRIVERS[self.browser], "build", side_effect=build), \
             patch.object(PacketCapture, "_list_interfaces_tshark", return_value=loopbacks), \
             patch("wiki_fetcher.PacketCapture", side_effect=lambda pcap_path: PacketCapture(
                 pcap_path, capture_filter=f"tcp port {server.server_port}")):
            record = WikiFetcher(root, [self.browser], True)._fetch_with(
                f"https://127.0.0.1:{server.server_port}/", self.browser, root)
        self.assertIsNone(record.error, record.error)
        self.assertEqual(server.hits["/recaptcha/api2/aframe"], 0)
        self.assertEqual(len(record.network_summary["intentionally_blocked_requests"]), 4)
        self.assertTrue(record.key_log_path and Path(record.key_log_path).stat().st_size > 0)
        self.assertTrue(record.pcap_path and Path(record.pcap_path).stat().st_size > 0)
        decoded = subprocess.run([tool[1], "-n", "-2", "-r", record.pcap_path,
            "-o", f"tls.keylog_file:{record.key_log_path}", "-d", f"tcp.port=={server.server_port},tls",
            "-Y", "http.response", "-T", "fields", "-e", "http.file_data"],
            capture_output=True, text=True, check=True, timeout=30)
        bodies = [bytes.fromhex(line.strip().replace(":", "")) for line in decoded.stdout.splitlines() if line.strip()]
        self.assertIn(b"normal resource", bodies)
        self.assertTrue(any(b"<title>blocking fixture</title>" in body and body.endswith(b"</iframe>")
                            for body in bodies), "Complete HTML must be recoverable from HTTPS PCAP")


if __name__ == "__main__":
    unittest.main()
