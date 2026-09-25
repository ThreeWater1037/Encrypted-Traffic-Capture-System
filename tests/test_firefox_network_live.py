"""Opt-in Firefox policy checks against an owned localhost server.

Set RUN_FIREFOX_NETWORK_LIVE=1 and GECKODRIVER_PATH to an installed driver.
"""

from collections import Counter
import base64
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
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.webdriver.firefox.options import Options

from browser_cache import CachePolicyError
from browser_firefox import FirefoxNetworkPolicy
from browser_loading import NetworkIdleTracker
from browser_service import TimedFirefoxService
from wiki_fetcher import FirefoxDriver, PacketCapture, WikiFetcher, _prepare_navigation


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        self.server.hits[path] += 1
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/worker.js":
            body = b"onmessage=async()=>{postMessage(await (await fetch('/worker-cache')).text())}"
            mime = "application/javascript"
        elif path in {"/cache", "/worker-cache"}:
            body, mime = b"from server", "text/plain"
        elif path in {"/image.png", "/cache-image.png"}:
            body = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
            mime = "image/png"
        elif path.startswith("/image-page-"):
            body = b'''<!doctype html><title>Image reuse</title><link rel="icon" href="data:,"><body>
                <script>(async()=>{for(let i=0;i<2;i++){
                    const image=new Image(); image.src='/image.png';
                    document.body.append(image); await image.decode();
                }})()</script>'''
            mime = "text/html"
        else:
            body = b'<!doctype html><title>Firefox policy</title><link rel="icon" href="data:,"><body>fixture'
            mime = "text/html"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=3600" if path in {"/cache", "/worker-cache", "/cache-image.png"} else "no-store")
        self.end_headers()
        self.wfile.write(body)


@unittest.skipUnless(os.environ.get("RUN_FIREFOX_NETWORK_LIVE") == "1", "opt-in Firefox policy test")
class FirefoxPolicyLiveTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.hits = Counter()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.temp = tempfile.TemporaryDirectory(prefix="codex-firefox-policy-")
        self.addCleanup(self.temp.cleanup)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def build(self):
        root = Path(self.temp.name)
        driver = FirefoxDriver().build(root / "keys.log", root)
        self.addCleanup(driver.quit)
        self.addCleanup(driver._capture_firefox_network.close)
        _prepare_navigation(driver, self.base + "/page")
        driver.get(self.base + "/page")
        return driver

    def test_cache_bypass_workers_frames_and_service_worker_policy(self):
        driver = self.build()
        tracker = NetworkIdleTracker(driver)
        tracker.wait()
        self.assertFalse(driver.execute_script("return 'serviceWorker' in navigator"))
        for _ in range(2):
            driver.execute_async_script("const done=arguments[0]; fetch('/cache').then(r=>r.text()).then(done)")
        driver.execute_async_script("""
            const done=arguments[0]; window.fixtureWorker=new Worker('/worker.js');
            fixtureWorker.onmessage=e=>done(e.data); fixtureWorker.postMessage('go');
        """)
        driver.execute_async_script("""
            const done=arguments[0], frame=document.createElement('iframe');
            frame.src=location.href.replace('127.0.0.1','localhost').replace('/page','/cache');
            frame.onload=done; document.body.append(frame);
        """)
        result = tracker.wait()
        self.assertEqual(self.server.hits["/cache"], 3)
        self.assertEqual(self.server.hits["/worker-cache"], 1)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["cache_hit_requests"], [])
        cache_requests = [r for r in result["requests"] if urlsplit(r["url"]).path == "/cache"]
        self.assertEqual(len(cache_requests), 3, result)
        worker_requests = [r for r in result["requests"] if urlsplit(r["url"]).path == "/worker-cache"]
        self.assertEqual(len(worker_requests), 1, "worker request missing")
        self.assertEqual(worker_requests[0]["state"], "finished")
        self.assertEqual(driver._capture_firefox_network.snapshot()["service_worker_policy"], "disabled_in_profile")

    def test_legacy_intercept_and_redirect_keep_diagnostics(self):
        driver = self.build()
        policy = driver._capture_firefox_network
        policy.reset_observation()
        policy.block_hosts({"today2.hit.edu.cn", "myweb.hit.edu.cn"})
        driver.get(self.base + "/redirect")
        driver.execute_async_script("""
            const done=arguments[0]; Promise.all([
              fetch('http://today2.hit.edu.cn/blocked').catch(()=>{}),
              fetch('https://myweb.hit.edu.cn/blocked').catch(()=>{})
            ]).then(done);
        """)
        result = NetworkIdleTracker(driver).wait()
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["redirect_count"], 1)
        self.assertEqual(len(result["failed_requests"]), 2, result)
        self.assertTrue(all(r["blockedReason"] == "inspector" for r in result["failed_requests"]))

    def test_negative_control_cache_hit_is_rejected(self):
        # Leave profile caches enabled to prove runtime detection is effective,
        # then explicitly turn off BiDi bypass for the negative control only.
        options = Options()
        options.add_argument("--headless")
        options.set_capability("webSocketUrl", True)
        driver = webdriver.Firefox(service=TimedFirefoxService(os.environ["GECKODRIVER_PATH"]), options=options)
        self.addCleanup(driver.quit)
        policy = FirefoxNetworkPolicy(driver).start()
        self.addCleanup(policy.close)
        driver.get(self.base + "/page")
        policy._request("network.setCacheBehavior", {"cacheBehavior": "default"})
        for _ in range(2):
            driver.execute_async_script("const done=arguments[0]; fetch('/cache').then(r=>r.text()).then(done)")
        self.assertEqual(self.server.hits["/cache"], 1)
        with self.assertRaises(CachePolicyError):
            policy.check()
        self.assertTrue(policy.snapshot()["cache_hits"])

    def test_same_document_image_reuse_passes_with_download_evidence(self):
        # Firefox 156 reuses images within a document even with HTTP bypass and
        # disabled disk/memory caches. Permit this only with original download
        # evidence; raw cache flags remain visible in the shared request ledger.
        driver = self.build()
        driver.execute_async_script("""
            const done=arguments[0];
            (async()=>{for(let i=0;i<2;i++){
                const image=new Image(); image.src='/image.png';
                document.body.append(image); await image.decode();
            } done(true)})().catch(e=>done(String(e)));
        """)
        self.assertEqual(self.server.hits["/image.png"], 1)
        result = NetworkIdleTracker(driver).wait()
        self.assertEqual(result["cache_hit_requests"], [])
        self.assertEqual(len(result["same_document_image_reuses"]), 1)
        policy = driver._capture_firefox_network.snapshot()
        self.assertEqual(len(policy["same_document_image_reuses"]), 1)
        self.assertEqual(policy["cache_hits"], [])

    def test_fetcher_writes_firefox_status_and_successful_cleanup(self):
        output = Path(self.temp.name)
        fetcher = WikiFetcher(output, ["firefox"], False)
        result = fetcher._fetch_with(self.base + "/page", "firefox", output)
        self.assertIsNone(result.error)
        status = json.loads((output / "network_status_firefox.json").read_text(encoding="utf-8"))
        self.assertEqual(status["network_summary"]["pending_count"], 0)
        self.assertEqual(status["network_summary"]["cache_policy"]["protocol"], "webdriver_bidi")
        self.assertIsNone(status["cleanup_summary"]["service"]["error"])
        self.assertIn("network_recheck", status["phase_timings"])

    def test_two_capture_urls_each_download_shared_image(self):
        root = Path(self.temp.name)
        fetcher = WikiFetcher(root, ["firefox"], False)
        for index in (1, 2):
            output = root / str(index)
            output.mkdir()
            result = fetcher._fetch_with(self.base + f"/image-page-{index}", "firefox", output)
            self.assertIsNone(result.error)
            self.assertEqual(self.server.hits["/image.png"], index)
            status = json.loads((output / "network_status_firefox.json").read_text(encoding="utf-8"))
            summary = status["network_summary"]
            self.assertEqual(summary["cache_hit_requests"], [])
            self.assertEqual(len(summary["same_document_image_reuses"]), 1)

    def test_first_image_from_previous_page_cache_is_rejected(self):
        options = Options()
        options.add_argument("--headless")
        options.set_capability("webSocketUrl", True)
        driver = webdriver.Firefox(service=TimedFirefoxService(os.environ["GECKODRIVER_PATH"]), options=options)
        self.addCleanup(driver.quit)
        driver.get(self.base + "/page")
        # Prime an image before this capture observation. Serve cacheable image
        # bytes via a separate endpoint, then deliberately enable caches below.
        script = """const done=arguments[0], image=new Image();
            image.src='/cache-image.png'; document.body.append(image);
            image.decode().then(()=>done(true)).catch(e=>done(String(e)));"""
        self.assertIs(driver.execute_async_script(script), True)
        policy = FirefoxNetworkPolicy(driver).start()
        self.addCleanup(policy.close)
        policy._request("network.setCacheBehavior", {"cacheBehavior": "default"})
        driver.get(self.base + "/page?second")
        self.assertIs(driver.execute_async_script(script), True)
        self.assertEqual(self.server.hits["/cache-image.png"], 1)
        with self.assertRaisesRegex(CachePolicyError, "/cache-image.png"):
            policy.check()
        self.assertEqual(policy.snapshot()["same_document_image_reuses"], [])

    @unittest.skipUnless(os.environ.get("RUN_FIREFOX_CAPTURE_LIVE") == "1", "opt-in TLS/PCAP test")
    def test_tls_keylog_decrypts_complete_local_response(self):
        root = Path(self.temp.name)
        openssl = os.environ.get("FIREFOX_TEST_OPENSSL") or shutil.which("openssl")
        self.assertTrue(openssl, "OpenSSL is required to generate the localhost test certificate")
        config = root / "openssl.cnf"
        config.write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="ascii")
        cert, key = root / "cert.pem", root / "key.pem"
        subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-config", str(config), "-keyout", str(key), "-out", str(cert),
                        "-days", "1", "-subj", "/CN=localhost"],
                       check=True, capture_output=True, timeout=15)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.hits = Counter()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tool = PacketCapture._find_tool()
        self.assertTrue(tool and tool[0] == "tshark")
        interfaces = PacketCapture._list_interfaces_tshark(tool[1])
        loopbacks = [name for name in interfaces if "loopback" in name.lower() or name in {"lo", "lo0"}]
        if interfaces == ["any"]:
            loopbacks = ["lo"]
        self.assertTrue(loopbacks, "A loopback capture interface is required")
        # Capture only this owned local fixture, not the host's other traffic.
        with patch.object(PacketCapture, "_list_interfaces_tshark", return_value=loopbacks):
            fetcher = WikiFetcher(root, ["firefox"], capture_pcap=True)
            record = fetcher._fetch_with(f"https://127.0.0.1:{server.server_port}/page", "firefox", root)
        self.assertIsNone(record.error)
        self.assertTrue(record.key_log_path and Path(record.key_log_path).stat().st_size > 0)
        self.assertTrue(record.pcap_path and Path(record.pcap_path).stat().st_size > 0)
        decoded = subprocess.run([tool[1], "-n", "-2", "-r", record.pcap_path,
            "-o", f"tls.keylog_file:{record.key_log_path}", "-d", f"tcp.port=={server.server_port},tls",
            "-Y", "http.response", "-T", "fields", "-e", "http.file_data"],
            capture_output=True, text=True, check=True, timeout=30)
        bodies = [bytes.fromhex(line.strip().replace(":", "")) for line in decoded.stdout.splitlines() if line.strip()]
        expected = b'<!doctype html><title>Firefox policy</title><link rel="icon" href="data:,"><body>fixture'
        self.assertIn(expected, bodies, "PCAP must decrypt to the entire HTTPS response body")


if __name__ == "__main__":
    unittest.main()
