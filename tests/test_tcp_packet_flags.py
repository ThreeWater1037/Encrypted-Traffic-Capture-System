"""TCP classification through packet extraction and both sequence modes."""
import csv
import importlib
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analysis_scripts import extract_header_seqs as seq
from analysis_scripts.tcp_flags import empty_tcp_category, TCP_CONTROL_FIELDS, tcp_flags_text


class TcpFlagsTests(unittest.TestCase):
    def test_flags_in_all_packet_classifiers(self):
        with patch("shutil.which", return_value="tshark"):
            modules = [(importlib.import_module(prefix + "infer_packets"),
                        importlib.import_module(prefix + "classify_packets"))
                       for prefix in ("", "analysis_scripts.")]
        for flags, expected in (("0x10", "ack"), ("16", "ack"),
                                ("0x02", "syn"), ("0x12", "syn_ack"),
                                ("0x01", "fin"), ("0x11", "fin_ack"),
                                ("0x04", "rst"), ("0x14", "rst_ack"),
                                ("0x18", "tcp_other"), ("0x50", "tcp_other"),
                                ("0", "unknown")):
            with self.subTest(flags=flags):
                self.assertEqual(empty_tcp_category(flags), expected)
                for infer, classify in modules:
                    self.assertEqual(infer.infer_packet([], [], 0, "forward", 17,
                                                       tcp_flags=flags)["category"], expected)
                    self.assertEqual(classify.classify_pkt("", [], 0, flags), expected)
                for protocol in ("http1", "http2"):
                    self.assertEqual(seq.decrypted_info({"tcp_payload_len": "0",
                                                        "tcp_flags": flags}, protocol),
                                     (expected, []))
        for infer, classify in modules:
            self.assertNotEqual(infer.infer_packet(["23"], ["100"], 105, "forward", 17,
                                                  tcp_flags="0x10")["category"], "ack")
            self.assertNotEqual(classify.classify_pkt("23", [], 105, "0x10"), "ack")

    def test_unusual_flag_combinations_are_not_lost(self):
        for flags, expected in (("0x18", "PSH|ACK"), ("0x50", "ACK|ECE"),
                                ("0x03", "FIN|SYN"), ("0x110", "ACK|BIT_0x100")):
            with self.subTest(flags=flags):
                self.assertEqual(empty_tcp_category(flags), "tcp_other")
                self.assertEqual(tcp_flags_text(flags), expected)

    def test_missing_flags_reject_old_tsv_in_both_modes(self):
        for flags in (None, "", "-", "bogus", "0x10,0x02", "-1", "4096"):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, "original PCAP"):
                empty_tcp_category(flags)
        with self.assertRaisesRegex(ValueError, "original PCAP"):
            seq.decrypted_info({"tcp_payload_len": "0"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "forward_1_443.tsv"
            path.write_text("# pkt_count: 1 ack=1\ncategory\ttcp_payload_len\nack\t0\n")
            for prefix in ("", "analysis_scripts."):
                extractor = importlib.import_module(prefix + "extract_forward_seqs")
                with self.assertRaisesRegex(ValueError, "original PCAP"):
                    extractor.process_flow_file(path)

    @unittest.skipUnless(shutil.which("tshark"), "TShark unavailable")
    def test_real_tshark_packet_to_summary_all_browser_names(self):
        """Synthetic PCAP, real TShark; this does not launch live browsers."""
        from analysis_scripts import infer_packets as infer, classify_packets as classify
        # SYN and retry, SYN+ACK, ACK, FIN+ACK, RST, RST+ACK, empty flags.
        flags = (2, 2, 18, 16, 17, 4, 20, 0)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            keylog = root / "keys.log"
            keylog.write_text("")
            table = root / "flows.tsv"
            table.write_text("src_ip\tdst_ip\tsrc_port\tdst_port\ttransport\ttls_version\tapp_protocol\tsni\n"
                             "10.0.0.1\t10.0.0.2\t5000\t443\tTCP\t-1\tother\t\n")
            for browser in ("chrome", "edge", "firefox"):
                with self.subTest(browser=browser):
                    pcap = root / f"capture_{browser}.pcap"
                    with pcap.open("wb") as fh:
                        fh.write(struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1))
                        for i, bits in enumerate(flags):
                            tcp = struct.pack("!HHIIBBHHH", 5000, 443, 1, 1, 0x50, bits, 65535, 0, 0)
                            ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 40, i, 0, 64, 6, 0,
                                             bytes((10, 0, 0, 1)), bytes((10, 0, 0, 2)))
                            frame = bytes(12) + b"\x08\x00" + ip + tcp
                            fh.write(struct.pack("<IIII", i + 1, 0, len(frame), len(frame)) + frame)
                    for mode, suffix, module in (("inferred", "inferred", infer),
                                                ("decrypted", "flows", classify)):
                        folder = root / f"capture_{browser}_{suffix}"
                        if mode == "inferred":
                            module.process(pcap, table, folder)
                        else:
                            module.process(pcap, keylog, table, folder)
                        path = next(folder.glob("forward_*.tsv"))
                        with path.open() as fh:
                            rows = list(csv.DictReader((l for l in fh if not l.startswith("#")), delimiter="\t"))
                        self.assertEqual([int(r["tcp_flags"], 16) for r in rows], list(flags))
                        result = seq.summarize_directory(folder, mode, ("forward",))["forward"][0]
                        self.assertEqual([result[k] for k in ("ack", *TCP_CONTROL_FIELDS, "unknown")],
                                         [1, 2, 1, 0, 1, 1, 1, 0, 1])
                        self.assertEqual([r["tcp_flags_text"] for r in rows],
                                         ["SYN", "SYN", "SYN|ACK", "ACK", "FIN|ACK", "RST", "RST|ACK", "NONE"])
                        self.assertEqual(sum(result[k] for k in seq.COUNT_FIELDS), 8)
                        self.assertEqual(result["header_lens"], "")


if __name__ == "__main__":
    unittest.main()
