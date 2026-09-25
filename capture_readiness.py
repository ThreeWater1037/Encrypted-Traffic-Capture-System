"""Capture startup notifications and bounded-memory PCAP structure checks.

The notification belongs to the process passed to ``CaptureReadinessMonitor``.
An existing output file, or TShark's early ``Capturing on`` message, cannot make
that process ready. This module does not start or terminate capture processes.
"""

from __future__ import annotations

import logging
import math
import os
import re
import struct
import threading
import time
from collections import deque
from pathlib import Path
from typing import BinaryIO


class CaptureFormatError(ValueError):
    """The file contains an incomplete or invalid capture structure."""


class CaptureReadinessError(RuntimeError):
    def __init__(self, message: str, details: dict):
        super().__init__(message)
        self.details = details


_PCAP_MAGICS = {
    b"\xd4\xc3\xb2\xa1": ("<", False),
    b"\xa1\xb2\xc3\xd4": (">", False),
    b"\x4d\x3c\xb2\xa1": ("<", True),
    b"\xa1\xb2\x3c\x4d": (">", True),
}
_SHB_MAGIC = b"\x0a\x0d\x0d\x0a"


def _read_exact(stream: BinaryIO, size: int, context: str) -> bytes:
    value = stream.read(size)
    if len(value) != size:
        raise CaptureFormatError(f"Incomplete {context}")
    return value


def _pcap_header(stream: BinaryIO) -> dict:
    stream.seek(0)
    header = _read_exact(stream, 24, "PCAP global header")
    try:
        endian, nanoseconds = _PCAP_MAGICS[header[:4]]
    except KeyError:
        raise CaptureFormatError("Unsupported capture file magic") from None
    major, minor, _zone, _sigfigs, snaplen, _linktype = struct.unpack(
        endian + "HHiIII", header[4:]
    )
    if (major, minor) != (2, 4) or snaplen == 0:
        raise CaptureFormatError("Invalid PCAP version or snapshot length")
    return {"format": "pcap", "interface_count": 1, "endian": endian,
            "nanoseconds": nanoseconds, "snaplen": snaplen}


def _pcapng_blocks(stream: BinaryIO, file_size: int):
    """Read fixed block prefixes/trailers, seeking past packet payloads."""
    offset = 0
    endian = None
    while offset < file_size:
        if file_size - offset < 12:
            raise CaptureFormatError(f"Incomplete PCAPNG block at byte {offset}")
        stream.seek(offset)
        prefix = _read_exact(stream, 12, "PCAPNG block header")
        if prefix[:4] == _SHB_MAGIC:
            if prefix[8:12] == b"\x4d\x3c\x2b\x1a":
                endian = "<"
            elif prefix[8:12] == b"\x1a\x2b\x3c\x4d":
                endian = ">"
            else:
                raise CaptureFormatError("Invalid PCAPNG byte-order magic")
        elif endian is None:
            raise CaptureFormatError("PCAPNG must start with a Section Header Block")
        block_type, length = struct.unpack(endian + "II", prefix[:8])
        minimum = {0x0A0D0D0A: 28, 1: 20, 2: 32, 3: 16, 6: 32}.get(block_type, 12)
        if length < minimum or length % 4:
            raise CaptureFormatError(f"Invalid PCAPNG block length {length} at byte {offset}")
        if offset + length > file_size:
            raise CaptureFormatError(f"Incomplete PCAPNG block at byte {offset}")
        stream.seek(offset + length - 4)
        trailer = struct.unpack(endian + "I", _read_exact(stream, 4, "block trailer"))[0]
        if trailer != length:
            raise CaptureFormatError(f"Mismatched PCAPNG block lengths at byte {offset}")
        stream.seek(offset + 8)
        body = _read_exact(stream, min(length - 12, 20), "block body prefix")
        yield block_type, length, body, endian, offset
        offset += length


def inspect_capture_header(path: Path, expected_interfaces: int = 1) -> dict | None:
    """Return startup header information, or None while headers are incomplete.

    This intentionally reads only enough blocks for startup. Use
    ``validate_capture_file`` after the writer has exited for the whole file.
    """
    if expected_interfaces < 1:
        raise ValueError("expected_interfaces must be positive")
    try:
        with Path(path).open("rb") as stream:
            file_size = os.fstat(stream.fileno()).st_size
            magic = stream.read(4)
            if len(magic) < 4:
                return None
            if magic in _PCAP_MAGICS:
                if file_size < 24:
                    return None
                header = _pcap_header(stream)
                if expected_interfaces != 1:
                    return None
                return {**header, "header_bytes": 24}
            if magic != _SHB_MAGIC:
                raise CaptureFormatError("Unsupported capture file magic")
            interfaces = 0
            try:
                for block_type, length, body, endian, offset in _pcapng_blocks(stream, file_size):
                    if block_type == 0x0A0D0D0A:
                        if struct.unpack(endian + "H", body[4:6])[0] != 1:
                            raise CaptureFormatError("Unsupported PCAPNG version")
                        interfaces = 0
                    elif block_type == 1:
                        interfaces += 1
                        if interfaces >= expected_interfaces:
                            return {"format": "pcapng", "interface_count": interfaces,
                                    "header_bytes": offset + length}
            except CaptureFormatError as exc:
                if str(exc).startswith("Incomplete "):
                    return None
                raise
            return None
    except FileNotFoundError:
        return None


def validate_capture_file(path: Path, require_packet: bool = True) -> dict:
    """Validate complete records/blocks without reading packet bodies into RAM.

    This verifies file structure and stored packet bounds. It does not assert
    network delivery, decrypted response completeness, or absence of packet loss.
    The writer must have exited before this function is called.
    """
    path = Path(path)
    with path.open("rb") as stream:
        initial_stat = os.fstat(stream.fileno())
        file_size = initial_stat.st_size
        magic = stream.read(4)
        packet_count = 0
        truncated_packets = 0
        if magic in _PCAP_MAGICS:
            header = _pcap_header(stream)
            offset = 24
            while offset < file_size:
                stream.seek(offset)
                seconds, fraction, captured, original = struct.unpack(
                    header["endian"] + "IIII", _read_exact(stream, 16, "PCAP packet header")
                )
                if fraction >= (1_000_000_000 if header["nanoseconds"] else 1_000_000):
                    raise CaptureFormatError(f"Invalid PCAP timestamp at byte {offset}")
                if captured > original or captured > header["snaplen"]:
                    raise CaptureFormatError(f"Invalid PCAP packet lengths at byte {offset}")
                offset += 16 + captured
                if offset > file_size:
                    raise CaptureFormatError("Incomplete PCAP packet payload")
                packet_count += 1
                truncated_packets += captured < original
            result = {"format": "pcap", "interface_count": 1, "section_count": 1,
                      "block_count": 0}
        elif magic == _SHB_MAGIC:
            interfaces: list[int] = []
            total_interfaces = 0
            section_count = 0
            block_count = 0
            for block_type, length, body, endian, offset in _pcapng_blocks(stream, file_size):
                block_count += 1
                if block_type == 0x0A0D0D0A:
                    if struct.unpack(endian + "H", body[4:6])[0] != 1:
                        raise CaptureFormatError("Unsupported PCAPNG version")
                    section_count += 1
                    interfaces = []
                elif block_type == 1:
                    interfaces.append(struct.unpack(endian + "I", body[4:8])[0])
                    total_interfaces += 1
                elif block_type in (2, 3, 6):
                    if block_type == 3:
                        interface_id = 0
                        original = struct.unpack(endian + "I", body[:4])[0]
                        if not interfaces:
                            raise CaptureFormatError("Packet appears before its interface description")
                        captured = min(original, interfaces[0]) if interfaces[0] else original
                        expected_length = 16 + ((captured + 3) & ~3)
                        if length != expected_length:
                            raise CaptureFormatError(f"Invalid simple packet length at byte {offset}")
                    else:
                        interface_id = struct.unpack(endian + ("H" if block_type == 2 else "I"),
                                                     body[:2] if block_type == 2 else body[:4])[0]
                        captured, original = struct.unpack(endian + "II", body[12:20])
                        if length < 32 + ((captured + 3) & ~3):
                            raise CaptureFormatError(f"Invalid packet block length at byte {offset}")
                    if interface_id >= len(interfaces):
                        raise CaptureFormatError(f"Unknown packet interface {interface_id}")
                    snaplen = interfaces[interface_id]
                    if captured > original or (snaplen and captured > snaplen):
                        raise CaptureFormatError(f"Invalid packet lengths at byte {offset}")
                    packet_count += 1
                    truncated_packets += captured < original
            if total_interfaces == 0:
                raise CaptureFormatError("PCAPNG has no interface descriptions")
            result = {"format": "pcapng", "interface_count": total_interfaces,
                      "section_count": section_count, "block_count": block_count}
        else:
            raise CaptureFormatError("Unsupported or incomplete capture file magic")
        final_stat = os.fstat(stream.fileno())
        if (final_stat.st_size, final_stat.st_mtime_ns) != (file_size, initial_stat.st_mtime_ns):
            raise CaptureFormatError("Capture file changed during validation")
    if require_packet and packet_count == 0:
        raise CaptureFormatError("Capture contains no packets")
    return {**result, "packet_count": packet_count, "file_bytes": file_size,
            "truncated_packet_count": truncated_packets, "structure_valid": True}


class CaptureReadinessMonitor:
    """Drain stderr continuously and wait for process-specific startup evidence."""

    def __init__(self, process, tool: str, output_path: Path, expected_interfaces: int,
                 logger: logging.Logger | None = None):
        if tool not in {"tshark", "dumpcap", "tcpdump"}:
            raise ValueError(f"Unsupported capture tool: {tool}")
        if expected_interfaces < 1:
            raise ValueError("expected_interfaces must be positive")
        if process.stderr is None:
            raise ValueError("Capture stderr must be a readable text PIPE")
        self.process = process
        self.tool = tool
        self.output_path = Path(output_path)
        self.expected_interfaces = expected_interfaces
        self.logger = logger or logging.getLogger("wiki_fetcher")
        self._started = time.monotonic()
        self._changed = threading.Event()
        self._lock = threading.Lock()
        self._tail: deque[str] = deque(maxlen=30)
        self._notification: str | None = None
        self._reader_error: str | None = None
        self._stderr_eof = False
        self._thread = threading.Thread(target=self._drain_stderr,
                                        name=f"{tool}-capture-stderr", daemon=True)
        self._thread.start()

    @staticmethod
    def _normalize_path(value: str) -> str:
        return os.path.normcase(os.path.abspath(value))

    def _is_ready_notification(self, line: str) -> bool:
        if self.tool == "tcpdump":
            return re.search(r"(?:^|\s)listening on .+[, ]", line) is not None
        # Current Wireshark emits this through ws_info("File: ..."). Keep the
        # prefix grammar strict so unrelated diagnostics containing "File:"
        # cannot serve as a startup notification.
        match = re.match(
            r"^\s*(?:\*\*\s+\((?:tshark|dumpcap)(?:\.exe)?:\d+\)\s+"
            r"\d{2}:\d{2}:\d{2}\.\d+\s+\[Main\s+INFO\]\s+"
            # Some Linux builds include source location and function in wslog.
            r"(?:\S+:\d+\s+--\s+capture_input_new_file\(\):\s+|--\s+)"
            r")?"
            r"File:\s*(.*?)\s*$", line,
        )
        if match is None:
            return False
        value = match.group(1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return self._normalize_path(value) == self._normalize_path(str(self.output_path))

    def _drain_stderr(self) -> None:
        try:
            while True:
                line = self.process.stderr.readline(65536)
                if not line:
                    break
                text = line.rstrip("\r\n")
                with self._lock:
                    self._tail.append(text)
                    if self._is_ready_notification(text):
                        self._notification = text
                self._changed.set()
                # The continuously drained pipe preserves Worker output and avoids
                # deadlock even when the capture tool emits lengthy diagnostics.
                try:
                    self.logger.info("  %s: %s", self.tool, text)
                except Exception:
                    pass
        except Exception as exc:
            with self._lock:
                self._reader_error = repr(exc)
        finally:
            # Only the reader closes its pipe, after readline has finished. A
            # different thread closing a blocked TextIOWrapper can itself hang.
            try:
                self.process.stderr.close()
            except Exception:
                pass
            with self._lock:
                self._stderr_eof = True
            self._changed.set()

    @property
    def details(self) -> dict:
        with self._lock:
            return {"tool": self.tool, "output_path": str(self.output_path),
                    "expected_interfaces": self.expected_interfaces,
                    "notification": self._notification, "stderr_tail": list(self._tail),
                    "stderr_eof": self._stderr_eof, "reader_error": self._reader_error,
                    "wait_seconds": time.monotonic() - self._started,
                    "return_code": self.process.poll()}

    def wait_ready(self, timeout: float = 5.0) -> dict:
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        deadline = time.monotonic() + timeout
        last_header_error = None
        while True:
            self._changed.clear()
            details = self.details
            if details["return_code"] is not None:
                raise CaptureReadinessError(
                    f"{self.tool} exited before capture became ready (code={details['return_code']})",
                    details,
                )
            if details["reader_error"]:
                raise CaptureReadinessError("Capture stderr reader failed", details)
            if details["notification"]:
                try:
                    header = inspect_capture_header(self.output_path, self.expected_interfaces)
                    if header is not None and self.process.poll() is None:
                        expected_format = "pcap" if self.tool == "tcpdump" else "pcapng"
                        if header["format"] != expected_format:
                            raise CaptureFormatError(f"{self.tool} output must be {expected_format}")
                        return {**self.details, **header, "ready": True}
                except (OSError, CaptureFormatError) as exc:
                    last_header_error = str(exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                details = {**self.details, "header_error": last_header_error}
                raise CaptureReadinessError(f"{self.tool} capture was not ready within {timeout:g}s", details)
            # Headers can become visible after their stderr notification. Event
            # wakeups handle diagnostics immediately; the short timeout rechecks
            # file visibility without a fixed startup delay.
            self._changed.wait(min(remaining, 0.02))

    def join(self, timeout: float = 1.0) -> bool:
        """Wait briefly for stderr EOF, normally after the process has exited.

        Never close a pipe underneath a blocked reader or stop draining a live
        child. The caller owns process shutdown; a bounded join returns False if
        some process still holds stderr open.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite and nonnegative")
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def stop(self, timeout: float = 1.0) -> bool:
        """Join the reader after caller-owned capture process shutdown."""
        return self.join(timeout)
