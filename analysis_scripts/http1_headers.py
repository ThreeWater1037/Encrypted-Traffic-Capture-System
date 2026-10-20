"""Measure complete HTTP/1.1 headers from TShark's reassembled PDML tree.

Raw values are used only transiently to verify byte coverage. No header text,
cookies, URLs or decrypted bodies are retained in the result.
"""

import subprocess
import tempfile
import xml.etree.ElementTree as ET

REASSEMBLY_OPTIONS = [
    "-o", "tcp.desegment_tcp_streams:TRUE",
    "-o", "tls.desegment_ssl_records:TRUE",
    "-o", "tls.desegment_ssl_application_data:TRUE",
    "-o", "http.desegment_headers:TRUE",
    "-o", "http.desegment_body:TRUE",
]


def header_length(proto):
    """Return (kind, bytes), or None for body-only/non-1.1 protocol nodes."""
    children = list(proto)
    start = None
    for index, child in enumerate(children):
        for kind in ("request", "response"):
            version = child.find(f".//field[@name='http.{kind}.version']")
            if version is not None:
                if version.get("show") != "HTTP/1.1":
                    return None
                start = (index, int(child.get("pos")), kind)
                break
        if start is not None:
            break
    if start is None:
        return None
    index, offset, kind = start
    pieces = []
    end = None
    for child in children[index:]:
        raw = child.get("value", "")
        size = int(child.get("size", "0"))
        if not raw or not size:
            continue
        try:
            value = bytes.fromhex(raw)
        except ValueError:
            continue
        if len(value) != size:
            continue  # Generated numeric fields are not byte ranges.
        pos = int(child.get("pos"))
        pieces.append((pos, value))
        if child.get("name", "") == "" and value == b"\r\n":
            end = pos + size
            break
    if end is None:
        raise ValueError("Incomplete HTTP/1.1 header: missing terminating CRLF")
    size = end - offset
    if size <= 0 or size > 16 * 1024 * 1024:
        raise ValueError("Invalid HTTP/1.1 header byte range")
    raw_header = bytearray(size)
    covered = bytearray(size)
    for pos, value in pieces:
        lo, hi = pos - offset, pos - offset + len(value)
        if lo < 0 or hi > size:
            raise ValueError("HTTP/1.1 field outside header byte range")
        for i, byte in enumerate(value, lo):
            if covered[i] and raw_header[i] != byte:
                raise ValueError("Conflicting HTTP/1.1 field bytes")
            raw_header[i], covered[i] = byte, 1
    if not all(covered) or raw_header.find(b"\r\n\r\n") != size - 4:
        raise ValueError("Incomplete or ambiguous HTTP/1.1 header byte coverage")
    return kind, size


def read_pdml(source):
    """Map dissection frame to ordered headers, retaining repeated messages."""
    result = {}
    context = ET.iterparse(source, events=("start", "end"))
    _, root = next(context)
    for event, packet in context:
        if event != "end" or packet.tag != "packet":
            continue
        frame = packet.find(".//field[@name='frame.number']")
        entries = []
        # Only top-level HTTP dissections, not embedded message/http bodies.
        for proto in packet.findall("./proto[@name='http']"):
            try:
                info = header_length(proto)
            except ValueError as exc:
                raise ValueError(f"Frame {frame.get('show')}: {exc}") from exc
            if info:
                entries.append({"kind": info[0], "length": info[1]})
        if entries:
            frame_no = int(frame.get("show"))
            if frame_no in result:
                raise ValueError(f"Duplicate PDML frame: {frame_no}")
            result[frame_no] = entries
        root.clear()
    return result


def extract_http1_headers(tshark, pcap, keylog):
    cmd = [tshark, "-r", str(pcap), "-o", f"tls.keylog_file:{keylog}",
           *REASSEMBLY_OPTIONS, "-Y", "tcp.port == 443 && http && !(icmp || icmpv6)", "-T", "pdml"]
    # PDML may contain large bodies: spool to a temporary file, parse per packet,
    # and close/delete the temporary plaintext automatically, even on failure.
    with tempfile.TemporaryFile() as spool:
        subprocess.run(cmd, stdout=spool, stderr=subprocess.PIPE, check=True, timeout=300)
        spool.seek(0)
        return read_pdml(spool)
