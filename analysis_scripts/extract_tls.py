#!/usr/bin/env python3
"""通过 TShark 离线提取 TLS 流特征；仅依赖 Python 标准库。"""
import argparse
from collections import Counter
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


PREFERENCES = {
    "tcp.analyze_sequence_numbers": "TRUE",
    "tcp.desegment_tcp_streams": "TRUE",
    "tcp.reassemble_out_of_order": "TRUE",
    "tcp.check_checksum": "FALSE",
    "ip.defragment": "TRUE",
    "ipv6.defragment": "TRUE",
    "tls.desegment_ssl_records": "TRUE",
    "tls.desegment_ssl_application_data": "FALSE",
    "tls.keylog_file": "",
}
REQUIRED_FIELDS = {
    "frame.number", "frame.time_epoch", "frame.cap_len", "frame.len",
    "tcp.stream", "tcp.len", "tcp.srcport", "tcp.dstport", "tcp.flags.syn",
    "tcp.flags.ack", "tcp.payload", "tcp.reassembled.data",
    "ip.src", "ip.dst", "ipv6.src", "ipv6.dst", "tls.record",
    "tls.record.length", "tls.record.content_type", "tls.record.opaque_type",
    "tls.handshake", "tls.handshake.type", "tls.handshake.version",
    "tls.handshake.extensions_server_name", "tls.handshake.extensions.supported_version",
}


class ExtractionError(Exception):
    """可向用户直接展示的输入或解析错误。"""


def field(node, name):
    return node.find('.//field[@name="%s"]' % name)


def shown(node, name, default=None):
    item = field(node, name)
    return item.get("show", default) if item is not None else default


def number(node, name, default=None):
    value = shown(node, name)
    # TShark 4.2 PDML uses True/False for FT_BOOLEAN; newer builds use 1/0.
    if value in ("True", "False"):
        return int(value == "True")
    return int(value, 16 if value.startswith("0x") else 10) if value is not None else default


def bytes_field(item):
    if item is None:
        return None
    value = item.get("value")
    if value is None:
        return None
    data = bytes.fromhex(value)
    if len(data) != int(item.get("size", "-1")):
        return None
    return int(item.get("pos", "0")), data


class Flow:
    """只汇总特征和角色；不执行 TCP/TLS 重组。"""

    def __init__(self, source, destination):
        self.endpoints = (source, destination)
        self.tcp = []
        self.records = []
        self.hello_clients = set()
        self.syn_clients = set()
        self.names = set()
        self.versions = set()

    def side(self, source, destination):
        if (source, destination) == self.endpoints:
            return 0
        if (destination, source) == self.endpoints:
            return 1
        raise ExtractionError("同一个 tcp.stream 出现不一致的端点，无法安全关联")

    def row(self, stats):
        if not self.records:
            stats["filtered_no_tls_record"] += 1
            return None
        candidates = self.hello_clients or self.syn_clients
        client = min(candidates) if candidates else 0
        if len(candidates) > 1:
            stats["role_conflict_flows"] += 1
        elif self.hello_clients:
            stats["tls_confirmed_flows"] += 1
        elif self.syn_clients:
            stats["tcp_inferred_flows"] += 1
        else:
            stats["role_unknown_flows"] += 1
        if len(self.names) > 1:
            stats["sni_conflict_flows"] += 1
        if len(self.versions) > 1:
            stats["version_conflict_flows"] += 1
        source, destination = self.endpoints[client], self.endpoints[client ^ 1]
        times = [t for _, _, t in self.tcp]
        # 完整 Record 应来自有载荷 TCP；保留明确保护而不伪造时间。
        if not times:
            raise ExtractionError("存在 TLS Record，但连接没有对应的 TCP payload")
        start, end = min(times), max(times)
        stats["output_flows"] += 1
        stats["output_tls_records"] += len(self.records)
        stats["output_tcp_payload_packets"] += len(self.tcp)
        stats["sni_flows"] += bool(self.names)
        return {
            "src_ip": source[0], "src_port": source[1],
            "dst_ip": destination[0], "dst_port": destination[1],
            "sni": min(self.names) if self.names else "",
            "start_time": start, "end_time": end, "duration": end - start,
            "tcp_len": [length for length, _, _ in self.tcp],
            "tcp_pktdir": [int(side == client) for _, side, _ in self.tcp],
            "tcp_timestamps": times,
            "tls_record_length": [length for length, _, _, _ in self.records],
            "tls_record_type": [kind for _, kind, _, _ in self.records],
            "tls_record_dir": [int(side == client) for _, _, side, _ in self.records],
            "tls_record_timestamps": [t for _, _, _, t in self.records],
            "tls_version": "0x%04x" % next(iter(self.versions)) if len(self.versions) == 1 else "0x0000",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


def complete_record(record, source):
    """在 TShark 给出的数据源内核对 Header 与完整 body，不推测缺失字节。"""
    length = number(record, "tls.record.length")
    # 直接子字段确保外层类型不会被解密后的子节点覆盖。
    kind_field = record.find('field[@name="tls.record.opaque_type"]')
    if kind_field is None:
        kind_field = record.find('field[@name="tls.record.content_type"]')
    if source is None or length is None or kind_field is None:
        return None
    kind = int(kind_field.get("show"))
    base, data = source
    offset = int(kind_field.get("pos")) - base
    if kind not in range(20, 25) or offset < 0 or offset + 5 + length > len(data):
        return None
    header = data[offset:offset + 5]
    if (header[0] != kind or header[1] != 3 or header[2] > 3
            or int.from_bytes(header[3:5], "big") != length):
        return None
    if int(record.get("size", "0")) < length + 5:
        return None
    return length, kind


def consume_packet(packet, flows, stats):
    stats["packets"] += 1
    if number(packet, "frame.cap_len", 0) < number(packet, "frame.len", 0):
        stats["capture_truncated_packets"] += 1
    tcps = packet.findall('.//proto[@name="tcp"]')
    if not tcps:
        return
    # ICMP errors quote the offending packet. Its dissected TCP header is not a
    # second observed TCP packet and must not contribute payloads, roles or TLS.
    parents = {child: parent for parent in packet.iter() for child in parent}
    observed = []
    for tcp in tcps:
        parent = parents.get(tcp)
        while parent is not None and not (
                parent.tag == 'proto' and parent.get('name') in ('icmp', 'icmpv6')):
            parent = parents.get(parent)
        if parent is not None:
            stats['ignored_icmp_quoted_tcp'] += 1
        else:
            observed.append(tcp)
    tcps = observed
    if not tcps:
        return
    if len(tcps) != 1:
        raise ExtractionError("不支持同帧多层 TCP：frame %s" % shown(packet, "frame.number"))
    tcp = tcps[0]
    stream = shown(tcp, "tcp.stream")
    if stream is None:
        stats["tcp_without_stream"] += 1
        return
    # ICMP 引用已在上方排除；真实 TCP 仍须位于包的协议层列表中，
    # 不能把其他未知嵌套结构直接展开，否则会混合端点及 TLS 数据源。
    protocols = list(packet)
    if tcp not in protocols:
        raise ExtractionError("不支持嵌套 TCP 协议树")
    ips = [p for p in protocols[:protocols.index(tcp)] if p.get("name") in ("ip", "ipv6")]
    if not ips:
        raise ExtractionError("TCP 缺少可关联的 IP 层")
    ip = ips[-1]
    source = (shown(ip, ip.get("name") + ".src"), number(tcp, "tcp.srcport"))
    destination = (shown(ip, ip.get("name") + ".dst"), number(tcp, "tcp.dstport"))
    if None in source or None in destination:
        raise ExtractionError("TCP/IP 端点字段不完整")
    flow = flows.get(stream)
    if flow is None:
        flows[stream] = flow = Flow(source, destination)
    side = flow.side(source, destination)
    millis = int(Decimal(shown(packet, "frame.time_epoch")) * 1000)
    if number(tcp, "tcp.flags.syn", 0):
        flow.syn_clients.add(side ^ int(bool(number(tcp, "tcp.flags.ack", 0))))
    length = number(tcp, "tcp.len", 0)
    if length > 0:
        flow.tcp.append((length, side, millis))
        stats["tcp_payload_packets"] += 1
    payload = bytes_field(field(tcp, "tcp.payload"))
    assembled = None
    for proto in protocols[protocols.index(tcp) + 1:]:
        # 一个包可以包含多组重组数据及多个 TLS proto，按树顺序绑定。
        data = field(proto, "tcp.reassembled.data")
        if data is not None:
            assembled = bytes_field(data)
        if proto.get("name") != "tls":
            continue
        backing = assembled if proto.get("pos") == "0" else payload
        for record in proto.findall('field[@name="tls.record"]'):
            stats["record_candidates"] += 1
            item = complete_record(record, backing)
            if item is None:
                stats["unverified_records"] += 1
                continue
            flow.records.append((*item, side, millis))
        # Handshake 可以跨 Record 重组，不能仅从单个 Record 的子树取 Hello。
        for handshake in proto.findall('.//field[@name="tls.handshake"]'):
            kind = number(handshake, "tls.handshake.type")
            if kind not in (1, 2):
                continue
            if field(handshake, "tls.handshake.version") is None:
                continue
            flow.hello_clients.add(side if kind == 1 else side ^ 1)
            if kind == 1:
                name = shown(handshake, "tls.handshake.extensions_server_name")
                if name:
                    flow.names.add(name)
            else:
                version = number(handshake, "tls.handshake.extensions.supported_version")
                if version is None:
                    version = number(handshake, "tls.handshake.version")
                flow.versions.add(version)


def isolated_environment(directory):
    env = os.environ.copy()
    env.pop("SSLKEYLOGFILE", None)
    env.update(WIRESHARK_CONFIG_DIR=str(directory), XDG_CONFIG_HOME=str(directory),
               APPDATA=str(directory))
    return env


def query(command, env):
    result = subprocess.run(command, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
    if result.returncode:
        raise ExtractionError("TShark 检查失败：" + result.stderr.strip())
    return result.stdout


def check_tshark(executable, env):
    path = shutil.which(executable)
    if path is None:
        raise ExtractionError("找不到 TShark，请安装或使用 --tshark 指定可执行文件路径")
    version = query([path, "--version"], env).strip()
    available = {parts[2] for line in query([path, "-G", "fields"], env).splitlines()
                 for parts in [line.split("\t")] if len(parts) > 2 and parts[0] == "F"}
    missing = REQUIRED_FIELDS - available
    defaults = query([path, "-G", "defaultprefs"], env)
    prefs = set(re.findall(r"^#?([a-zA-Z0-9_.]+):", defaults, re.MULTILINE))
    missing.update(set(PREFERENCES) - prefs)
    if missing:
        raise ExtractionError("TShark 缺少所需字段/选项，请升级：" + ", ".join(sorted(missing)))
    return path, version


def parse_capture(command, env):
    flows, stats = {}, Counter()
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=errors)
        try:
            iterator = ET.iterparse(proc.stdout, events=("start", "end"))
            _, root = next(iterator)
            if root.tag != "pdml":
                raise ExtractionError("TShark 输出不是 PDML")
            for event, packet in iterator:
                if event == "end" and packet.tag == "packet":
                    consume_packet(packet, flows, stats)
                    root.clear()
            code = proc.wait()
            if code:
                errors.seek(0)
                raise ExtractionError("TShark 退出码 %d：%s" % (code, errors.read().decode("utf-8", "replace")[-6000:]))
        except (ET.ParseError, StopIteration) as error:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            errors.seek(0)
            raise ExtractionError("TShark 输出不完整：%s；%s" % (
                error, errors.read().decode("utf-8", "replace")[-6000:])) from error
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
        errors.seek(0)
        diagnostic = errors.read().decode("utf-8", "replace").strip()
    stats["tcp_flows"] = len(flows)
    return flows, stats, diagnostic


def extract(inputs, output, tshark="tshark", decode_as=()):
    output = Path(output).resolve()
    manifest = output.with_suffix(".manifest.json")
    inputs = [Path(p).resolve() for p in inputs]
    if output == manifest or output in inputs or manifest in inputs:
        raise ExtractionError("输出、报告和输入路径不能重合；输出请使用 .jsonl 后缀")
    for path in inputs:
        if not path.is_file():
            raise ExtractionError("输入不是普通文件：%s" % path)
    for path in (output, manifest):
        if path.exists():
            raise ExtractionError("拒绝覆盖已有文件：%s" % path)
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    created = []
    try:
        # 先占用报告路径，防止同一前缀的并发任务互相覆盖。
        with manifest.open("x", encoding="utf-8") as report_file:
            created.append(manifest)
            with tempfile.TemporaryDirectory(prefix="offline-tls-") as config:
                env = isolated_environment(Path(config))
                executable, version = check_tshark(tshark, env)
                empty_keys = Path(config) / "empty.keys"
                empty_keys.touch()
                preferences = dict(PREFERENCES, **{"tls.keylog_file": str(empty_keys)})
                base = [executable, "-n", "-2"]
                for key, value in preferences.items():
                    base.extend(["-o", key + ":" + value])
                for rule in decode_as:
                    base.extend(["-d", rule])
                report = dict(tool="offline-tls", schema="sample-json-17", complete=False,
                              tshark_version=version, preferences=preferences,
                              sequence_order="tshark_display_frame_then_record",
                              tls_timestamp="display_frame_capture_time_unix_ms",
                              tcp_timestamp="payload_packet_capture_time_unix_ms",
                              tls_length="record_header_length_excluding_5_byte_header",
                              direction="client_to_server_1_reverse_0",
                              connection_identity="input_index_and_tcp.stream",
                              output=str(output), inputs=[])
                all_rows, total = [], Counter()
                for index, capture in enumerate(inputs):
                    print("解析 [%d/%d] %s" % (index + 1, len(inputs), capture), file=sys.stderr)
                    before = capture.stat()
                    command = base + ["-r", str(capture), "-T", "pdml"]
                    flows, stats, diagnostic = parse_capture(command, env)
                    after = capture.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ExtractionError("解析期间输入发生变化：%s" % capture)
                    first_line = len(all_rows) + 1
                    for flow in flows.values():
                        row = flow.row(stats)
                        if row is not None:
                            all_rows.append(row)
                    # 零值也固定输出，便于核对过滤分母。
                    for key in ("output_flows", "output_tls_records", "filtered_no_tls_record",
                                "unverified_records", "sni_flows", "capture_truncated_packets",
                                "ignored_icmp_quoted_tcp"):
                        stats.setdefault(key, 0)
                    total.update(stats)
                    report["inputs"].append(dict(index=index, path=str(capture), bytes=before.st_size,
                        mtime_ns=before.st_mtime_ns, command=command, stats=dict(stats),
                        output_first_line=first_line if stats["output_flows"] else None,
                        output_line_count=stats["output_flows"], tshark_stderr=diagnostic))
                with output.open("x", encoding="utf-8", newline="\n") as destination:
                    created.append(output)
                    for row in all_rows:
                        destination.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                report.update(complete=True, stats=dict(total), elapsed_seconds=round(time.monotonic() - started, 3))
                json.dump(report, report_file, ensure_ascii=False, indent=2)
                report_file.write("\n")
        return report
    except BaseException:
        # 仅删除本次独占创建的文件，绝不清理已有输入或他人结果。
        for path in reversed(created):
            try:
                path.unlink()
            except OSError:
                pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path, help="PCAP/PCAPNG 输入（各文件独立建流）")
    parser.add_argument("-o", "--output", required=True, type=Path, help="输出 JSONL，拒绝覆盖已有文件")
    parser.add_argument("--tshark", default="tshark", help="TShark 可执行文件路径")
    parser.add_argument("--decode-as", action="append", default=[], metavar="RULE",
                        help="显式协议识别规则，可重复，例如 tcp.port==8443,tls")
    args = parser.parse_args(argv)
    try:
        report = extract(args.inputs, args.output, args.tshark, args.decode_as)
    except KeyboardInterrupt:
        print("已中断；本次未完成输出已清理", file=sys.stderr)
        return 130
    except (ExtractionError, OSError, ValueError) as error:
        print("提取失败：%s" % error, file=sys.stderr)
        return 1
    print(json.dumps(report["stats"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
