"""
对 capture_chrome.tsv 中列出的每条流（正向+反向），从 pcap 提取包级信息，
每条流单独输出一个 TSV 文件。文件头部（# 注释行）包含五元组和 SNI。

用法：
    python3 classify_packets.py <pcap> <keylog> [--flow-tsv capture_chrome.tsv]
    python3 classify_packets.py <pcap> <keylog> --output-dir ./flows
"""

import csv
import sys
import shutil
import logging
import argparse
import subprocess
from pathlib import Path
from collections import defaultdict, Counter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

TSHARK_CANDIDATES = [
    "D:/software/Wireshark/tshark.exe",
    "C:/Program Files/Wireshark/tshark.exe",
    "C:/Program Files (x86)/Wireshark/tshark.exe",
    "/Applications/Wireshark.app/Contents/MacOS/tshark",
    "/usr/local/bin/tshark",
    "/usr/bin/tshark",
]

def find_tshark() -> str:
    """定位用于 TLS 解密和 HTTP/2 字段提取的 TShark。"""
    for p in TSHARK_CANDIDATES:
        if Path(p).is_file():
            return p
    found = shutil.which("tshark")
    if found:
        return found
    raise RuntimeError("tshark 未找到")

TSHARK = find_tshark()

# ---------------------------------------------------------------------------
# HTTP/2 帧类型 & TLS content type
# ---------------------------------------------------------------------------

H2_FRAME_TYPES = {
    "0": "DATA", "1": "HEADERS", "2": "PRIORITY", "3": "RST_STREAM",
    "4": "SETTINGS", "5": "PUSH_PROMISE", "6": "PING",
    "7": "GOAWAY", "8": "WINDOW_UPDATE", "9": "CONTINUATION",
}

TLS_CONTENT_TYPES = {
    "20": "ChangeCipherSpec", "21": "Alert",
    "22": "Handshake", "23": "ApplicationData",
}

# ---------------------------------------------------------------------------
# 每包数据行字段（五元组和 SNI 放文件头，不在行内重复）
# ---------------------------------------------------------------------------

PKT_FIELDS = [
    "frame_no",
    "time_rel",          # 相对于 pcap 第一包的时间（秒）
    "pkt_len",
    "tcp_payload_len",   # TCP payload 字节数（0 = 纯 ACK）
    "tls_content_type",  # Handshake / ApplicationData / -
    "h2_frame_type",     # DATA / HEADERS / SETTINGS / ... / -
    "h2_frame_len",      # HTTP/2 payload 长度（字节）
    "category",          # header / data / ack / tls_segment
]

# ---------------------------------------------------------------------------
# tshark 调用
# ---------------------------------------------------------------------------

TSHARK_FIELDS = [
    "frame.number",
    "frame.time_epoch",
    "frame.len",
    "ip.src", "ip.dst",
    "ipv6.src", "ipv6.dst",
    "tcp.srcport", "tcp.dstport",
    "tcp.len",
    "tls.record.content_type",
    "http2.type",
    "http2.length",
]

def run_tshark(pcap: Path, keylog: Path) -> list[dict]:
    """使用 TLS key log 解密 PCAP，并返回逐包字段记录。"""
    cmd = [
        TSHARK, "-r", str(pcap),
        "-o", f"tls.keylog_file:{keylog}",
        "-T", "fields",
        "-E", "separator=\t",
        "-E", "quote=n",
        "-E", "occurrence=a",
    ]
    for f in TSHARK_FIELDS:
        cmd += ["-e", f]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    packets = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        while len(parts) < len(TSHARK_FIELDS):
            parts.append("")
        packets.append(dict(zip(TSHARK_FIELDS, parts)))
    return packets

# ---------------------------------------------------------------------------
# 读取 capture_chrome.tsv 中的流列表
# ---------------------------------------------------------------------------

FlowKey = tuple[str, str, str, str]  # src_ip, dst_ip, src_port, dst_port

def read_flow_tsv(tsv_path: Path) -> dict[FlowKey, dict]:
    """
    返回 {(src_ip, dst_ip, src_port, dst_port): flow_meta} 映射。
    flow_meta 包含 tsv 中的所有字段，并补充 direction 字段。
    """
    flows: dict[FlowKey, dict] = {}
    with tsv_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            src_ip   = row.get("src_ip", "").strip()
            dst_ip   = row.get("dst_ip", "").strip()
            src_port = row.get("src_port", "").strip()
            dst_port = row.get("dst_port", "").strip()
            if not (src_ip and dst_ip and src_port and dst_port):
                continue
            # 以 dst_port==443 判断正向，src_port==443 判断反向
            direction = "forward" if dst_port == "443" else "reverse"
            key: FlowKey = (src_ip, dst_ip, src_port, dst_port)
            flows[key] = {
                "src_ip":       src_ip,
                "dst_ip":       dst_ip,
                "src_port":     src_port,
                "dst_port":     dst_port,
                "transport":    row.get("transport", "TCP").strip(),
                "tls_version":  row.get("tls_version", "").strip(),
                "app_protocol": row.get("app_protocol", "").strip(),
                "sni":          row.get("sni", "").strip(),
                "direction":    direction,
            }
    return flows

# ---------------------------------------------------------------------------
# 包分类
# ---------------------------------------------------------------------------

def classify_pkt(raw_ct: str, raw_h2_types: list[str], tcp_payload_len: int) -> str:
    """依据 TLS content type、HTTP/2 frame type 和载荷长度标注数据包。"""
    if raw_ct == "22":
        return "header"
    if raw_h2_types:
        return "data" if any(t == "0" for t in raw_h2_types) else "header"
    if tcp_payload_len == 0:
        return "ack"
    return "tls_segment"

# ---------------------------------------------------------------------------
# 主处理逻辑
# ---------------------------------------------------------------------------

def process(pcap: Path, keylog: Path, flow_tsv: Path, output_dir: Path):
    """把解密后的数据包归入目标流，并为每条流输出独立 TSV。"""
    # 读取流列表
    flow_meta = read_flow_tsv(flow_tsv)
    if not flow_meta:
        log.error("capture_chrome.tsv 中未找到有效流记录，退出")
        sys.exit(1)
    log.info("从 TSV 读取流数: %d", len(flow_meta))

    # 读取 pcap 所有包
    packets = run_tshark(pcap, keylog)
    log.info("读取包数: %d", len(packets))

    # pcap 起始时间
    pcap_start = 0.0
    for pkt in packets:
        try:
            pcap_start = float(pkt["frame.time_epoch"])
            break
        except ValueError:
            continue

    # 按流分组
    flow_pkts: dict[FlowKey, list] = defaultdict(list)

    for pkt in packets:
        src_ip = pkt["ip.src"] or pkt["ipv6.src"]
        dst_ip = pkt["ip.dst"] or pkt["ipv6.dst"]
        sp = pkt["tcp.srcport"].split(",")[0].strip()
        dp = pkt["tcp.dstport"].split(",")[0].strip()
        if not (src_ip and dst_ip and sp and dp):
            continue

        key: FlowKey = (src_ip, dst_ip, sp, dp)
        if key not in flow_meta:
            continue

        try:
            ts = float(pkt["frame.time_epoch"]) - pcap_start
        except ValueError:
            continue
        try:
            pkt_len = int(pkt["frame.len"])
        except ValueError:
            pkt_len = 0
        try:
            tcp_payload_len = int(pkt["tcp.len"].split(",")[0])
        except (ValueError, IndexError):
            tcp_payload_len = 0

        raw_ct = pkt["tls.record.content_type"].split(",")[0].strip()
        tls_ct = TLS_CONTENT_TYPES.get(raw_ct, "-" if not raw_ct else raw_ct)

        raw_h2_types = [t.strip() for t in pkt["http2.type"].split(",") if t.strip()]
        raw_h2_lens = [
            length.strip()
            for length in pkt["http2.length"].split(",")
            if length.strip()
        ]
        h2_types_str = ",".join(H2_FRAME_TYPES.get(t, t) for t in raw_h2_types)
        h2_lens_str  = ",".join(raw_h2_lens)

        category = classify_pkt(raw_ct, raw_h2_types, tcp_payload_len)

        flow_pkts[key].append({
            "frame_no":         pkt["frame.number"],
            "time_rel":         f"{ts:.6f}",
            "pkt_len":          pkt_len,
            "tcp_payload_len":  tcp_payload_len,
            "tls_content_type": tls_ct,
            "h2_frame_type":    h2_types_str or "-",
            "h2_frame_len":     h2_lens_str  or "-",
            "category":         category,
        })

    # 输出：每条流一个文件
    output_dir.mkdir(parents=True, exist_ok=True)
    log.info("开始写出流文件，共 %d 条流", len(flow_meta))

    for key, meta in flow_meta.items():
        pkts = flow_pkts.get(key, [])
        direction = meta["direction"]

        fname = f"{direction}_{meta['src_port']}_{meta['dst_port']}.tsv"
        fpath = output_dir / fname

        cats = Counter(p["category"] for p in pkts)

        with fpath.open("w", newline="", encoding="utf-8") as fh:
            # 文件头：五元组 + SNI
            fh.write(f"# src_ip       : {meta['src_ip']}\n")
            fh.write(f"# dst_ip       : {meta['dst_ip']}\n")
            fh.write(f"# src_port     : {meta['src_port']}\n")
            fh.write(f"# dst_port     : {meta['dst_port']}\n")
            fh.write(f"# transport    : {meta['transport']}\n")
            fh.write(f"# tls_version  : {meta['tls_version']}\n")
            fh.write(f"# app_protocol : {meta['app_protocol']}\n")
            fh.write(f"# sni          : {meta['sni']}\n")
            fh.write(f"# direction    : {direction}\n")
            fh.write(f"# pkt_count    : {len(pkts)}"
                     f"  header={cats['header']} data={cats['data']}"
                     f" ack={cats['ack']} tls_segment={cats['tls_segment']}\n")
            fh.write("#\n")

            writer = csv.DictWriter(fh, fieldnames=PKT_FIELDS, delimiter="\t")
            writer.writeheader()
            writer.writerows(pkts)

        log.info("  %s  (%d 包)", fpath.name, len(pkts))

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    """校验输入文件并启动基于密钥的包级分类。"""
    parser = argparse.ArgumentParser(
        description=(
            "对 capture_chrome.tsv 中的每条流（正向+反向），从 pcap 提取包级信息，\n"
            "每条流单独输出一个 TSV 文件，文件头部包含五元组和 SNI。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pcap",   help="pcap 文件路径")
    parser.add_argument("keylog", help="TLS key log 文件路径")
    parser.add_argument(
        "--flow-tsv", "-f", default=None,
        help="capture_chrome.tsv 路径（默认：pcap 同目录下的 capture_chrome.tsv）",
    )
    parser.add_argument(
        "--output-dir", "-o", default=None,
        help="输出目录（默认：pcap 同目录下的 {pcap_stem}_flows/）",
    )
    args = parser.parse_args()

    pcap   = Path(args.pcap)
    keylog = Path(args.keylog)

    if not pcap.exists():
        log.error("pcap 不存在：%s", pcap)
        sys.exit(1)
    if not keylog.exists():
        log.error("keylog 不存在：%s", keylog)
        sys.exit(1)

    flow_tsv = (Path(args.flow_tsv) if args.flow_tsv
                else pcap.parent / f"{pcap.stem}.tsv")
    if not flow_tsv.exists():
        log.error("flow-tsv 不存在：%s", flow_tsv)
        sys.exit(1)

    output_dir = (Path(args.output_dir) if args.output_dir
                  else pcap.parent / f"{pcap.stem}_flows")

    log.info("pcap      : %s", pcap)
    log.info("keylog    : %s", keylog)
    log.info("flow_tsv  : %s", flow_tsv)
    log.info("output_dir: %s", output_dir)

    process(pcap, keylog, flow_tsv, output_dir)


if __name__ == "__main__":
    main()
