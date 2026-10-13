"""Offline header-sequence regression tests; no browser or network required."""

import csv
import json
import importlib
import io
import shutil
import struct
import subprocess
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from analysis_scripts import extract_header_seqs as seq
from analysis_scripts.extract_forward_seqs import process_dir as legacy_process_dir
from analysis_scripts.flow_validation import validate_connections
from analysis_scripts.http1_headers import header_length, read_pdml, REASSEMBLY_OPTIONS


def write_flow(folder, direction, rows, port="5000", sni="www.forbeschina.com", server="10.0.0.2"):
    folder.mkdir(parents=True, exist_ok=True)
    client = ("10.0.0.1", port)
    remote = (server, "443")
    src, dst = (client, remote) if direction == "forward" else (remote, client)
    path = folder / f"{direction}_{src[1]}_{dst[1]}.tsv"
    meta = dict(src_ip=src[0], src_port=src[1], dst_ip=dst[0], dst_port=dst[1],
                transport="TCP", direction=direction, sni=sni)
    counts = Counter(row.get("category", "unknown") for row in rows)
    columns = ["frame_no", "category", "est_h2_payload_range", "h2_frame_type",
               "h2_frame_len", "tls_content_type", "tcp_payload_len",
               "http1_header_lens", "http1_message_types", "tcp_flags"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        for key, value in meta.items():
            fh.write(f"# {key}: {value}\n")
        fh.write(f"# pkt_count: {len(rows)} " + " ".join(f"{k}={counts[k]}" for k in seq.COUNT_FIELDS) + "\n")
        writer = csv.DictWriter(fh, columns, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    return path


def inferred(frame, value, category="header"):
    return dict(frame_no=frame, category=category, est_h2_payload_range=value)


def decrypted(frame, types="-", lengths="-", ct="ApplicationData", payload="100"):
    return dict(frame_no=frame, category="data", h2_frame_type=types,
                h2_frame_len=lengths, tls_content_type=ct, tcp_payload_len=payload,
                tcp_flags="0x0010")


class HeaderSequenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.entry = self.root / "001-wiki-sample"

    def test_legacy_inferred_forward_identical(self):
        folder = self.entry / "capture_chrome_inferred"
        write_flow(folder, "forward", [inferred(1, "61-80"), inferred(2, "80-99,20-30"),
                                      inferred(3, "61,99"), inferred(4, "?"),
                                      inferred(5, "100-120", "data")])
        old = legacy_process_dir(self.entry, "chrome")
        new = seq.summarize_directory(folder, "inferred", ("forward",))["forward"]
        self.assertEqual(new, old)
        self.assertEqual(new[0]["header_lens"], "61,80")

    def test_both_modes_all_browser_labels_and_summary(self):
        for browser in ("chrome", "edge", "firefox"):
            with self.subTest(browser=browser):
                for mode in seq.MODES:
                    suffix = "inferred" if mode == "inferred" else "flows"
                    folder = self.entry / f"capture_{browser}_{suffix}"
                    fwd = [inferred(1, "10-20"), inferred(3, "30-40")]
                    rev = [inferred(2, "0-0"), inferred(4, "40-50")]
                    if mode == "decrypted":
                        fwd = [decrypted(1, "HEADERS", "10"), decrypted(3, "HEADERS", "30")]
                        rev = [decrypted(2, "HEADERS", "0"), decrypted(4, "HEADERS", "40")]
                    write_flow(folder, "forward", fwd)
                    write_flow(folder, "reverse", rev, sni="")
                out = self.root / "out" / browser
                files = seq.extract(self.root, browser, seq.MODES, seq.DIRECTIONS, out)
                self.assertEqual(len(files), 6)
                for mode in seq.MODES:
                    for direction, expected in (("forward", "10,30"), ("reverse", "0,40"),
                                                ("bidirectional", "10,-0,30,-40")):
                        with (out / mode / f"{direction}.tsv").open() as fh:
                            reader = csv.DictReader(fh, delimiter="\t")
                            self.assertEqual(reader.fieldnames, seq.DECRYPTED_FIELDS if mode == "decrypted" else seq.FIELDS)
                            row = next(reader)
                        self.assertEqual(row["header_lens"], expected)
                        self.assertEqual(row["sni"], "www.forbeschina.com")
                        self.assertEqual(int(row["header"]), 4 if direction == "bidirectional" else 2)
                report = json.loads((out / "summary.json").read_text())
                self.assertTrue(all(c["paired"] for c in report["connections"]))

    def test_decrypted_mixed_multiple_headers_and_control(self):
        folder = self.entry / "capture_chrome_flows"
        write_flow(folder, "forward", [decrypted(5, "SETTINGS,HEADERS,DATA,HEADERS", "6,42,300,0"),
                                      decrypted(1, ct="Handshake"),
                                      decrypted(3, "SETTINGS", "6"),
                                      decrypted(4, "DATA", "100"),
                                      decrypted(6, payload="0")])
        row = seq.summarize_directory(folder, "decrypted", ("forward",))["forward"][0]
        self.assertEqual(row["header_lens"], "42,0")
        self.assertEqual([row[k] for k in ("header", "handshake", "control", "data", "ack")], [1]*5)

    def test_no_h2_does_not_fabricate_lengths(self):
        category, lengths = seq.decrypted_info(decrypted(1))
        self.assertEqual(lengths, [])
        self.assertEqual(category, "tls_segment")

    def test_malformed_h2_lengths_fail(self):
        with self.assertRaisesRegex(ValueError, "mismatch"):
            seq.decrypted_info(decrypted(1, "HEADERS,DATA", "40"))

    def test_missing_direction_strict_or_explicitly_audited(self):
        folder = self.entry / "capture_chrome_inferred"
        write_flow(folder, "forward", [inferred(1, "10-20")])
        with self.assertRaisesRegex(ValueError, "Missing direction"):
            seq.extract(self.root, "chrome", ("inferred",), seq.DIRECTIONS, self.root / "out")
        self.assertFalse((self.root / "out").exists())
        audit = []
        result = seq.summarize_directory(folder, "inferred", allow_unpaired=True, audit=audit)
        self.assertFalse(audit[0]["paired"])
        self.assertEqual(result["bidirectional"][0]["header_lens"], "10")

    def test_reverse_only_preserves_sign(self):
        folder = self.entry / "capture_chrome_inferred"
        write_flow(folder, "reverse", [inferred(1, "10-20")])
        row = seq.summarize_directory(folder, "inferred", allow_unpaired=True)["bidirectional"][0]
        self.assertEqual(row["flow"], "bidirectional_5000_443")
        self.assertEqual(row["header_lens"], "-10")

    def test_duplicate_frame_across_directions_fails(self):
        folder = self.entry / "capture_chrome_inferred"
        for direction in seq.DIRECTIONS[:2]:
            write_flow(folder, direction, [inferred(1, "10-20")])
        with self.assertRaisesRegex(ValueError, "Duplicate frame"):
            seq.summarize_directory(folder, "inferred")

    def test_connections_with_same_sni_are_not_merged(self):
        folder = self.entry / "capture_chrome_inferred"
        for port in ("5000", "6000"):
            write_flow(folder, "forward", [inferred(1, "10-20")], port)
            write_flow(folder, "reverse", [inferred(2, "20-30")], port)
        rows = seq.summarize_directory(folder, "inferred")["bidirectional"]
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["flow"], rows[1]["flow"])

    def test_split_sni_and_min_header(self):
        folder = self.entry / "capture_chrome_inferred"
        write_flow(folder, "forward", [inferred(1, "10-20")], sni="a:b")
        out = self.root / "out"
        seq.extract(self.root, "chrome", ("inferred",), ("forward",), out, 1, True)
        self.assertTrue((out / "inferred/forward_a%3Ab.tsv").exists())
        other = self.root / "filtered"
        seq.extract(self.root, "chrome", ("inferred",), ("forward",), other, 2)
        self.assertEqual(len((other / "inferred/forward.tsv").read_text().splitlines()), 1)

    def test_missing_mode_does_not_write_partial_results(self):
        folder = self.entry / "capture_chrome_inferred"
        write_flow(folder, "forward", [inferred(1, "10-20")])
        with self.assertRaisesRegex(ValueError, "No capture_chrome_flows"):
            seq.extract(self.root, "chrome", seq.MODES, ("forward",), self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_legacy_filename_collision_rejected_before_output(self):
        common = dict(direction="forward", src_port="5000", dst_port="443")
        with self.assertRaisesRegex(ValueError, "collision"):
            validate_connections({("a",): common, ("b",): common}, [])

    def test_reused_endpoint_connections_rejected(self):
        key = ("10.0.0.1", "10.0.0.2", "5000", "443")
        meta = {key: dict(direction="forward", src_port="5000", dst_port="443")}
        packets = [dict(zip(("ip.src", "ip.dst", "tcp.srcport", "tcp.dstport"), key),
                        **{"tcp.stream": str(i)}) for i in (0, 1)]
        with self.assertRaisesRegex(ValueError, "Reused TCP"):
            validate_connections(meta, packets)

    def test_port_collision_uses_tcp_stream_suffix_and_pairs_by_endpoints(self):
        meta, packets = {}, []
        folder = self.entry / 'capture_chrome_inferred'
        for stream, server in (("7", "10.0.0.2"), ("9", "10.0.0.3")):
            for direction in ("forward", "reverse"):
                key = ("10.0.0.1", server, "5000", "443")
                if direction == "reverse":
                    key = (server, "10.0.0.1", "443", "5000")
                meta[key] = dict(direction=direction, src_port=key[2], dst_port=key[3])
                packets.append(dict(zip(("ip.src", "ip.dst", "tcp.srcport", "tcp.dstport"), key),
                                    **{"tcp.stream": stream}))
        names = validate_connections(meta, packets)
        self.assertEqual(len(set(names.values())), 4)
        for index, (key, info) in enumerate(meta.items()):
            path = write_flow(folder, info['direction'], [inferred(index+1, '10-20')],
                              server=key[1] if info['direction']=='forward' else key[0])
            path.rename(folder/names[key])
        result = seq.summarize_directory(folder, 'inferred')
        self.assertEqual({r['flow'] for r in result['bidirectional']},
                         {'bidirectional_5000_443_tcp_7', 'bidirectional_5000_443_tcp_9'})
        self.assertTrue(all(r['header_lens']=='10,-10' for r in result['bidirectional']))

    def test_packet_readers_exclude_udp_and_non443(self):
        table = self.root / "flows.tsv"
        table.write_text("src_ip\tdst_ip\tsrc_port\tdst_port\ttransport\n"
                         "a\tb\t5000\t443\tTCP\n"
                         "b\ta\t443\t5000\tTCP\n"
                         "a\tb\t5001\t443\tUDP\n"
                         "a\tb\t5002\t80\tTCP\n", encoding="utf-8")
        with patch("shutil.which", return_value="tshark"):
            for name in ("infer_packets", "classify_packets"):
                module = importlib.import_module(f"analysis_scripts.{name}")
                meta = module.read_flow_tsv(table)
                self.assertEqual(len(meta), 2)
                self.assertEqual({v["direction"] for v in meta.values()}, {"forward", "reverse"})

    def test_tshark_errors_propagate_and_inferred_clears_keylog(self):
        with patch("shutil.which", return_value="tshark"):
            for name in ("infer_packets", "classify_packets"):
                module = importlib.import_module(f"analysis_scripts.{name}")
                args = [self.root / "capture.pcap"]
                if name == "classify_packets":
                    args.append(self.root / "keys.log")
                with patch.object(module.subprocess, "run", return_value=Mock(stdout="")) as run:
                    self.assertEqual(module.run_tshark(*args), [])
                    self.assertTrue(run.call_args.kwargs["check"])
                    if name == "infer_packets":
                        self.assertIn("tls.keylog_file:", run.call_args.args[0])
                with patch.object(module.subprocess, "run", side_effect=subprocess.CalledProcessError(2, "tshark")):
                    with self.assertRaises(subprocess.CalledProcessError):
                        module.run_tshark(*args)

    def test_http1_both_protocols_all_browser_labels(self):
        for browser in ("chrome", "edge", "firefox"):
            folder = self.entry / f"capture_{browser}_flows"
            for direction, frame, sizes, kinds in (("forward", 1, "80,100", "request,request"),
                                                   ("reverse", 2, "60", "response")):
                row = decrypted(frame)
                row.update(http1_header_lens=sizes, http1_message_types=kinds)
                write_flow(folder, direction, [row], sni="www.forbeschina.com")
                write_flow(folder, direction, [decrypted(frame + 2, "HEADERS", "42")],
                           port="6000", sni="h2.example")
            out = self.root / "out" / browser
            seq.extract(self.root, browser, ("decrypted",), seq.DIRECTIONS, out,
                        decrypted_protocol="both")
            self.assertEqual(sorted(p.name for p in (out / "decrypted").iterdir()),
                             ["bidirectional.tsv", "forward.tsv", "reverse.tsv"])
            with (out / "decrypted/bidirectional.tsv").open() as fh:
                rows = list(csv.DictReader(fh, delimiter="\t"))
            self.assertEqual(len(rows), 2)
            self.assertEqual([r["protocol"] for r in rows], ["HTTP/1.1", "HTTP/2"])
            self.assertEqual(rows[0]["header_lens"], "80,100,-60")
            self.assertEqual(rows[0]["header"], "2")
            self.assertEqual(rows[1]["header_lens"], "42,-42")

    def test_http1_rejects_old_intermediate_schema(self):
        folder = self.entry / "capture_chrome_flows"
        path = write_flow(folder, "forward", [decrypted(1)])
        text = path.read_text().replace("\thttp1_header_lens\thttp1_message_types", "")
        path.write_text(text)
        with self.assertRaisesRegex(ValueError, "rerun classify_packets"):
            seq.load_flow(path, "decrypted", "http1")

    def test_pdml_header_byte_coverage_and_duplicate_fields(self):
        proto = ET.Element("proto", name="http")
        start = b"GET / HTTP/1.1\r\n"
        line = b"X-Raw: \xff\r\n"
        first = ET.SubElement(proto, "field", name="", pos="7", size=str(len(start)), value=start.hex())
        ET.SubElement(first, "field", name="http.request.version", show="HTTP/1.1")
        for name in ("http.request.line", "http.unknown_header"):
            ET.SubElement(proto, "field", name=name, pos=str(7+len(start)), size=str(len(line)), value=line.hex())
        last = ET.SubElement(proto, "field", name="", pos=str(7+len(start)+len(line)), size="2", value="0d0a")
        ET.SubElement(proto, "field", name="http.file_data", pos="999", size="20000", value="ff"*20000)
        self.assertEqual(header_length(proto), ("request", len(start)+len(line)+2))
        proto.remove(last)
        with self.assertRaisesRegex(ValueError, "missing terminating"):
            header_length(proto)

    @unittest.skipUnless(shutil.which("tshark"), "TShark not installed")
    def test_real_tshark_http1_reassembly_pipeline_chunking_and_retransmit(self):
        # Handcrafted TCP capture. Plaintext isolates HTTP reassembly; the supplied
        # real TLS capture exercises actual keylog decryption separately.
        req1 = b"GET /a HTTP/1.1\r\nHost: test\r\nX-Long: " + b"x"*2000 + b"\r\n\r\n"
        fake = b"HTTP/1.1 200 Not-A-Header\r\n\r\n"
        req2 = b"POST /b HTTP/1.1\r\nHost: test\r\nContent-Length: " + str(len(fake)).encode() + b"\r\n\r\n"
        req3 = b"GET /c HTTP/1.1\r\nHost: test\r\n\r\n"
        res1 = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(fake)).encode() + b"\r\n\r\n"
        res2 = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        chunk = f"{len(fake):x}\r\n".encode() + fake + b"\r\n0\r\nX-Trailer: ignored\r\n\r\n"
        res3 = b"HTTP/1.1 204 No Content\r\n\r\n"
        frames = []

        def packet(reverse, seqno, ack, flags, payload=b""):
            src, dst = (b"\x0a\x00\x00\x01", b"\x0a\x00\x00\x02")
            sp, dp = 5000, 443
            if reverse:
                src, dst, sp, dp = dst, src, dp, sp
            tcp = struct.pack('!HHIIBBHHH', sp, dp, seqno, ack, 0x50, flags, 65535, 0, 0) + payload
            ip = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20+len(tcp), len(frames), 0, 64, 6, 0, src, dst)
            frames.append(b'\0'*12 + b'\x08\x00' + ip + tcp)

        packet(False, 100, 0, 2)
        packet(True, 1000, 101, 18)
        packet(False, 101, 1001, 16)
        packet(False, 101, 1001, 24, req1[:29])
        packet(False, 101, 1001, 24, req1[:29])  # retransmission must not duplicate headers
        packet(False, 130, 1001, 24, req1[29:])
        cseq = 101 + len(req1)
        packet(True, 1001, cseq, 24, res1[:12])
        packet(True, 1013, cseq, 24, res1[12:]+fake)
        sseq = 1001+len(res1)+len(fake)
        packet(False, cseq, sseq, 24, req2+fake+req3)
        packet(True, sseq, cseq+len(req2+fake+req3), 24, res2+chunk+res3)
        pcap = self.root / 'synthetic.pcap'
        with pcap.open('wb') as fh:
            fh.write(struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1))
            for i, frame in enumerate(frames):
                fh.write(struct.pack('<IIII', i+1, 0, len(frame), len(frame)))
                fh.write(frame)
        cmd = ['tshark', '-r', str(pcap), '-d', 'tcp.port==443,http', *REASSEMBLY_OPTIONS,
               '-Y', 'http', '-T', 'pdml']
        pdml = subprocess.run(cmd, capture_output=True, check=True).stdout
        events = read_pdml(io.BytesIO(pdml))
        messages = [e for row in events.values() for e in row]
        self.assertEqual([e['length'] for e in messages if e['kind'] == 'request'],
                         [len(req1), len(req2), len(req3)])
        self.assertEqual([e['length'] for e in messages if e['kind'] == 'response'],
                         [len(res1), len(res2), len(res3)])
        self.assertTrue(any(len(row) == 2 for row in events.values()))


if __name__ == "__main__":
    unittest.main()
