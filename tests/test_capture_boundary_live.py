"""Opt-in real sniffer check of immediate startup and stop, without a browser."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wiki_fetcher import PacketCapture


BODY = bytes(range(256)) * 2048


class BoundaryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)
        self.wfile.flush()


def field_values(value, name):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == name:
                yield from child if isinstance(child, list) else [child]
            else:
                yield from field_values(child, name)
    elif isinstance(value, list):
        for child in value:
            yield from field_values(child, name)


@unittest.skipUnless(os.environ.get("RUN_CAPTURE_BOUNDARY_LIVE") == "1", "opt-in sniffer test")
class LiveCaptureBoundaryTests(unittest.TestCase):
    def test_immediate_request_and_stop_after_required_idle_keep_full_body(self):
        output = Path(os.environ["CAPTURE_BOUNDARY_OUTPUT"]).resolve()
        output.mkdir(parents=True, exist_ok=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), BoundaryHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_port
        path = output / ("boundary-" + uuid.uuid4().hex + ".pcap")
        capture = PacketCapture(path)
        self.addCleanup(capture.stop)
        capture.start()
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(connection.close)
        # No delay after READY. The one completed request has no timer-created
        # successors, so this models the production 500ms network-idle window.
        # A separate negative probe proved that zero idle can lose queued tail
        # packets despite a structurally valid file and normal process exit.
        connection.request("GET", "/capture-boundary")
        response = connection.getresponse()
        received = response.read()
        self.assertEqual(received, BODY)
        time.sleep(0.5)
        self.assertEqual(capture.stop(), path)
        connection.close()
        tool = capture._find_tool()
        self.assertEqual(tool[0], "tshark")
        command = [tool[1], "-n", "-2", "-r", str(path), "-d", f"tcp.port=={port},http",
                   "-Y", f"tcp.port == {port}", "-T", "json", "--no-duplicate-keys", "-J", "frame tcp http"]
        decoded = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=30, check=True)
        packets = json.loads(decoded.stdout)
        flags = [int(value,16) for value in field_values(packets,"tcp.flags")]
        self.assertTrue(any(value & 2 and not value & 16 for value in flags),"Missing client SYN")
        self.assertTrue(any(value & 2 and value & 16 for value in flags),"Missing server SYN/ACK")
        self.assertIn("/capture-boundary", list(field_values(packets,"http.request.uri")))
        self.assertIn("200", list(field_values(packets,"http.response.code")))
        bodies = [bytes.fromhex(value.replace(":", "")) for value in field_values(packets,"http.file_data")]
        self.assertIn(BODY,bodies,"PCAP must reconstruct all response bytes, without a tail sleep")
        evidence = {"pcap":str(path),"required_network_idle_seconds":0.5,
                    "additional_tail_sleep_seconds":0,"body_bytes":len(BODY),"body_sha256":hashlib.sha256(BODY).hexdigest(),
                    "full_body_matched":True,"handshake_verified":True,"capture":capture.summary}
        path.with_suffix(".json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
        print(f"Immediate capture boundaries verified: {len(BODY)} response bytes and TCP handshake; {path}")


if __name__ == "__main__":
    unittest.main()
