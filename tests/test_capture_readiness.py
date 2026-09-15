from __future__ import annotations

import io
import logging
import queue
import struct
import tempfile
import time
import unittest
from pathlib import Path

from capture_readiness import (
    CaptureFormatError,
    CaptureReadinessError,
    CaptureReadinessMonitor,
    inspect_capture_header,
    validate_capture_file,
)


def block(kind, body, endian="<"):
    size = 12 + len(body)
    assert size % 4 == 0
    return struct.pack(endian + "II", kind, size) + body + struct.pack(endian + "I", size)


def shb(endian="<"):
    return block(0x0A0D0D0A, struct.pack(endian + "IHHq", 0x1A2B3C4D, 1, 0, -1), endian)


def idb(snaplen=65535, endian="<"):
    return block(1, struct.pack(endian + "HHI", 1, 0, snaplen), endian)


def epb(data=b"1234", interface=0, original=None, endian="<"):
    body = struct.pack(endian + "IIIII", interface, 0, 123,
                       len(data), len(data) if original is None else original)
    return block(6, body + data + b"\0" * (-len(data) % 4), endian)


def pcap(data=b"1234", endian="<", nanoseconds=False):
    magic = 0xA1B23C4D if nanoseconds else 0xA1B2C3D4
    return struct.pack(endian + "IHHiIII", magic, 2, 4, 0, 0, 65535, 1) + struct.pack(
        endian + "IIII", 0, 1, len(data), len(data)) + data


class QueueStderr:
    def __init__(self):
        self.lines = queue.Queue()

    def readline(self, size=-1):
        return self.lines.get()

    def write_line(self, line):
        self.lines.put(line + "\n")

    def close(self):
        self.lines.put("")


class Process:
    def __init__(self, stderr=None):
        self.stderr = stderr or QueueStderr()
        self.returncode = None

    def poll(self):
        return self.returncode


class CaptureReadinessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "capture with space.pcap"
        self.logger = logging.getLogger("capture-readiness-tests")
        self.logger.addHandler(logging.NullHandler())

    def monitor(self, tool="tshark", interfaces=1, process=None):
        process = process or Process()
        monitor = CaptureReadinessMonitor(process, tool, self.path, interfaces, self.logger)
        self.addCleanup(monitor.join, 0.1)
        if isinstance(process.stderr, QueueStderr):
            self.addCleanup(process.stderr.close)
        return process, monitor

    def test_existing_file_and_early_message_do_not_signal_readiness(self):
        self.path.write_bytes(shb() + idb() + epb())
        process, monitor = self.monitor()
        process.stderr.write_line("Capturing on 'any'")
        with self.assertRaises(CaptureReadinessError) as context:
            monitor.wait_ready(0.04)
        self.assertIsNone(context.exception.details["notification"])

    def test_wrong_file_notification_does_not_signal_readiness(self):
        self.path.write_bytes(shb() + idb())
        process, monitor = self.monitor()
        process.stderr.write_line('File: "different.pcap"')
        with self.assertRaises(CaptureReadinessError):
            monitor.wait_ready(0.04)

    def test_real_notification_requires_all_complete_interface_headers(self):
        self.path.write_bytes(shb() + idb() + idb()[:12])
        process, monitor = self.monitor(interfaces=2)
        process.stderr.write_line(f'File: "{self.path}"')
        with self.assertRaises(CaptureReadinessError):
            monitor.wait_ready(0.04)
        self.path.write_bytes(shb() + idb() + idb())
        result = monitor.wait_ready(0.2)
        self.assertTrue(result["ready"])
        self.assertEqual(result["interface_count"], 2)

    def test_ready_does_not_require_a_packet_or_fixed_startup_sleep(self):
        self.path.write_bytes(shb() + idb())
        process, monitor = self.monitor()
        process.stderr.write_line(f"File: {self.path}")
        start = time.monotonic()
        self.assertTrue(monitor.wait_ready(0.5)["ready"])
        self.assertLess(time.monotonic() - start, 0.2)

    def test_wireshark_main_info_prefix_is_a_ready_notification(self):
        self.path.write_bytes(shb() + idb())
        process, monitor = self.monitor()
        process.stderr.write_line(
            f' ** (tshark:39364) 23:18:57.874606 [Main INFO] -- File: "{self.path}"'
        )
        result = monitor.wait_ready(0.2)
        self.assertTrue(result["ready"])
        self.assertIn("[Main INFO]", result["notification"])

    def test_file_name_in_unrelated_diagnostic_is_not_ready(self):
        self.path.write_bytes(shb() + idb())
        process, monitor = self.monitor()
        for prefix in ("error opening ", " ** (tshark:39364) 23:18:57.874606 [Main ERROR] -- ",
                       " ** (dumpcap:39364) 23:18:57.874606 [Capchild INFO] -- "):
            process.stderr.write_line(f'{prefix}File: "{self.path}"')
        with self.assertRaises(CaptureReadinessError):
            monitor.wait_ready(0.04)

    def test_early_exit_fails_even_with_ready_header_and_notification(self):
        self.path.write_bytes(shb() + idb())
        process = Process(io.StringIO(f"File: {self.path}\npermission denied\n"))
        process.returncode = 2
        process, monitor = self.monitor(process=process)
        with self.assertRaises(CaptureReadinessError) as context:
            monitor.wait_ready(0.2)
        self.assertEqual(context.exception.details["return_code"], 2)

    def test_stderr_drains_beyond_readiness_and_retains_error_tail(self):
        self.path.write_bytes(shb() + idb())
        process, monitor = self.monitor()
        process.stderr.write_line(f"File: {self.path}")
        monitor.wait_ready(0.2)
        for index in range(1000):
            process.stderr.write_line(f"diagnostic {index}")
        process.stderr.close()
        self.assertTrue(monitor.join(1))
        self.assertEqual(monitor.details["stderr_tail"][-1], "diagnostic 999")
        self.assertLessEqual(len(monitor.details["stderr_tail"]), 30)

    def test_join_does_not_close_or_block_on_a_live_child(self):
        process, monitor = self.monitor()
        self.assertFalse(monitor.stop(0.01))
        process.stderr.write_line("still drained")
        process.stderr.close()
        self.assertTrue(monitor.join(0.2))
        self.assertIn("still drained", monitor.details["stderr_tail"])

    def test_tcpdump_needs_listening_and_full_classic_pcap_header(self):
        process, monitor = self.monitor(tool="tcpdump")
        self.path.write_bytes(pcap()[:20])
        process.stderr.write_line("tcpdump: listening on eth0, link-type EN10MB, snapshot length 262144 bytes")
        with self.assertRaises(CaptureReadinessError):
            monitor.wait_ready(0.04)
        self.path.write_bytes(pcap()[:24])
        self.assertEqual(monitor.wait_ready(0.2)["format"], "pcap")

    def test_empty_file_never_ready(self):
        self.path.write_bytes(b"")
        process, monitor = self.monitor()
        process.stderr.write_line(f"File: {self.path}")
        with self.assertRaises(CaptureReadinessError):
            monitor.wait_ready(0.04)

    def test_timeout_validation(self):
        _, monitor = self.monitor()
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                monitor.wait_ready(value)

    def test_pcapng_roundtrip_with_multiple_interfaces_and_byte_orders(self):
        for endian in ("<", ">"):
            with self.subTest(endian=endian):
                self.path.write_bytes(shb(endian) + idb(endian=endian) + idb(endian=endian)
                                      + epb(b"packet", interface=1, endian=endian))
                self.assertEqual(inspect_capture_header(self.path, 2)["interface_count"], 2)
                result = validate_capture_file(self.path)
                self.assertEqual(result["packet_count"], 1)
                self.assertEqual(result["interface_count"], 2)
                self.assertEqual(result["truncated_packet_count"], 0)

    def test_pcapng_sections_reset_byte_order_and_interface_ids(self):
        self.path.write_bytes(shb() + idb() + epb()
                              + shb(">") + idb(endian=">") + epb(endian=">"))
        result = validate_capture_file(self.path)
        self.assertEqual(result["section_count"], 2)
        self.assertEqual(result["packet_count"], 2)

    def test_classic_pcap_microseconds_and_nanoseconds_both_endian(self):
        for endian in ("<", ">"):
            for nano in (False, True):
                with self.subTest(endian=endian, nanoseconds=nano):
                    self.path.write_bytes(pcap(endian=endian, nanoseconds=nano))
                    self.assertEqual(validate_capture_file(self.path)["packet_count"], 1)

    def test_empty_capture_rejected_after_stop(self):
        for data in (shb() + idb(), pcap()[:24]):
            with self.subTest(data=data[:4]):
                self.path.write_bytes(data)
                with self.assertRaisesRegex(CaptureFormatError, "no packets"):
                    validate_capture_file(self.path)
                self.assertEqual(validate_capture_file(self.path, require_packet=False)["packet_count"], 0)

    def test_incomplete_block_and_trailer_rejected(self):
        good = shb() + idb() + epb()
        for data in (good[:-1], good + b"\0", good[:-4] + b"\0\0\0\0"):
            self.path.write_bytes(data)
            with self.assertRaises(CaptureFormatError):
                validate_capture_file(self.path)

    def test_incomplete_classic_packet_and_invalid_lengths_rejected(self):
        for data in (pcap()[:-1], pcap() + b"\0", pcap()[:24] + struct.pack("<IIII", 0, 0, 10, 2)):
            self.path.write_bytes(data)
            with self.assertRaises(CaptureFormatError):
                validate_capture_file(self.path)

    def test_unknown_interface_or_inflated_captured_length_rejected(self):
        malformed = bytearray(epb())
        struct.pack_into("<I", malformed, 20, 100)
        for packet in (epb(interface=1), bytes(malformed), epb(original=2)):
            self.path.write_bytes(shb() + idb() + packet)
            with self.assertRaises(CaptureFormatError):
                validate_capture_file(self.path)

    def test_snaplen_truncation_reported_separately_from_structure(self):
        self.path.write_bytes(shb() + idb(snaplen=4) + epb(original=8))
        result = validate_capture_file(self.path)
        self.assertTrue(result["structure_valid"])
        self.assertEqual(result["truncated_packet_count"], 1)

    def test_simple_and_obsolete_packet_blocks(self):
        simple = block(3, struct.pack("<I", 4) + b"1234")
        obsolete = block(2, struct.pack("<HHIIII", 0, 0, 0, 0, 4, 4) + b"1234")
        self.path.write_bytes(shb() + idb() + simple + obsolete)
        self.assertEqual(validate_capture_file(self.path)["packet_count"], 2)

    def test_large_packet_body_is_skipped_with_bounded_memory(self):
        payload_size = 8 * 1024 * 1024
        self.path.write_bytes(shb() + idb(snaplen=0))
        with self.path.open("r+b") as stream:
            stream.seek(0, 2)
            length = 32 + payload_size
            stream.write(struct.pack("<IIIIIII", 6, length, 0, 0, 0, payload_size, payload_size))
            stream.seek(payload_size - 1, 1)
            stream.write(b"\0")
            stream.write(struct.pack("<I", length))
        self.assertEqual(validate_capture_file(self.path)["packet_count"], 1)


if __name__ == "__main__":
    unittest.main()
