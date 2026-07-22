"""
对 capture_chrome.tsv 中列出的每条流，不解密密文流量，
仅凭 TLS record 头部（明文可见的 content_type + length）推测每个包的
H2 帧类型和帧 payload 长度范围，每条流单独输出一个 TSV 文件。

TLS 加密开销：
  TLS 1.3 → 17 字节（16 AEAD tag + 1 inner content type）
  TLS 1.2 → 16 字节（16 AEAD tag）

用法：
    python3 infer_packets.py <pcap> [--flow-tsv capture_chrome.tsv]
    python3 infer_packets.py <pcap> --output-dir ./flows_inferred
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
    """定位只读取明文 TLS record 元数据所需的 TShark。"""
    for p in TSHARK_CANDIDATES:
        if Path(p).is_file():
            return p
    found = shutil.which("tshark")
    if found:
        return found
    raise RuntimeError("tshark 未找到")

TSHARK = find_tshark()

# ---------------------------------------------------------------------------
# TLS 加密开销（每条 TLS record 的固定 overhead）
# ---------------------------------------------------------------------------

TLS_OVERHEAD: dict[str, int] = {
    "TLSv1.3": 17,   # 16-byte AEAD tag + 1-byte inner content type
    "TLSv1.2": 16,   # 16-byte AEAD tag only
}
DEFAULT_OVERHEAD = 17

TLS_CONTENT_TYPES = {
    "20": "ChangeCipherSpec",
    "21": "Alert",
    "22": "Handshake",
    "23": "ApplicationData",
}

# ---------------------------------------------------------------------------
# 每包数据行字段
# ---------------------------------------------------------------------------

PKT_FIELDS = [
    "frame_no",
    "time_rel",              # 相对于 pcap 第一包的时间（秒）
    "pkt_len",
    "tcp_payload_len",       # TCP payload 字节数（0 = 纯 ACK）
    "tls_content_type",      # 明文可见：Handshake / ApplicationData / -
    "tls_record_len",        # 明文可见：各 TLS record 的加密载荷长度（逗号分隔）
    "est_plaintext_len",     # 推算：tls_record_len − overhead（逗号分隔）
    "inferred_h2_type",      # 推测 H2 帧类型（逗号分隔，对应各 TLS record）
    "est_h2_payload_range",  # 推测 H2 payload 长度范围（逗号分隔）
    "category",              # ack / handshake / control / header / data / unknown
]

# ---------------------------------------------------------------------------
# tshark 调用（不需要 keylog）
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
    "tls.record.length",
]

def run_tshark(pcap: Path) -> list[dict]:
    """不传 keylog，只解析明文可见字段。"""
    cmd = [
        TSHARK, "-r", str(pcap),
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
    """读取 extract 阶段的双向流表并建立五元组索引。"""
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
# 单条 TLS ApplicationData record 的 H2 推测
# ---------------------------------------------------------------------------

def infer_single_record(
    rec_len: int, direction: str, overhead: int, with_ctrl: bool = False
) -> tuple[str, str, str]:
    """
    返回 (inferred_h2_type, est_h2_payload_range, sub_category)。

    with_ctrl=False（默认）：假设每条 TLS record 内只有一个 H2 帧，
                             min == max == est - 9，范围最紧。
    with_ctrl=True         ：假设可能附带控制帧，min 相应缩小：
                             DATA    → min = est - 22（附带 ≤13B 控制帧）
                             HEADERS → min = max(0, est - 18)（最多 2 帧并存）
    """
    est = rec_len - overhead

    if est < 0:
        return "?", "?", "unknown"

    if est == 0:
        return "?", "0-0", "unknown"

    if est < 9:
        # 不足以容纳一个完整的 H2 帧头（9字节），可能是 TLS padding
        return "?", f"0-{est}", "unknown"

    # ---- 精确命中固定大小控制帧 ----
    # SETTINGS ACK / 空 payload 帧：9字节帧头 + 0字节 payload
    if est == 9:
        return "SETTINGS_ACK", "0-0", "control"

    # WINDOW_UPDATE / RST_STREAM：9 + 4 = 13
    if est == 13:
        return "WINDOW_UPDATE|RST_STREAM", "4-4", "control"

    # PRIORITY：9 + 5 = 14
    if est == 14:
        return "PRIORITY", "5-5", "control"

    # PING：9 + 8 = 17
    if est == 17:
        return "PING", "8-8", "control"

    # SETTINGS with N entries（每个 entry 6字节）：9 + 6*N
    if est >= 15 and (est - 9) % 6 == 0:
        n = (est - 9) // 6
        if 1 <= n <= 8:
            return f"SETTINGS({n})", f"{est - 9}-{est - 9}", "control"

    # ---- 基于大小和方向的启发式推测 ----
    # max 始终为 est - 9（单帧独占整个明文空间）
    max_payload = est - 9

    if with_ctrl:
        # 考虑附带控制帧：min 反映同一 record 内可能并存的帧占用
        #   DATA    → 附带 ≤13B 控制帧：min = est - 22
        #   HEADERS → 最多 2 帧并存：   min = max(0, est - 18)
        #   HEADERS|DATA → 类型不确定：  min = 0
        if direction == "forward":
            if est <= 500:
                return "HEADERS", f"{max(0, est - 18)}-{max_payload}", "header"
            else:
                return "HEADERS|DATA", f"0-{max_payload}", "data"
        else:
            if est <= 800:
                return "HEADERS", f"{max(0, est - 18)}-{max_payload}", "header"
            else:
                return "DATA", f"{max(0, est - 22)}-{max_payload}", "data"
    else:
        # 不考虑附带控制帧：假设单帧独占，min == max == est - 9
        if direction == "forward":
            if est <= 500:
                return "HEADERS", f"{max_payload}-{max_payload}", "header"
            else:
                return "HEADERS|DATA", f"{max_payload}-{max_payload}", "data"
        else:
            if est <= 800:
                return "HEADERS", f"{max_payload}-{max_payload}", "header"
            else:
                return "DATA", f"{max_payload}-{max_payload}", "data"

# ---------------------------------------------------------------------------
# 单包推测入口（处理一个包内可能有多条 TLS record 的情况）
# ---------------------------------------------------------------------------

def infer_packet(
    raw_ct_list: list[str],
    raw_rec_len_list: list[str],
    tcp_payload_len: int,
    direction: str,
    overhead: int,
    with_ctrl: bool = False,
) -> dict:
    """
    返回推测结果字典，包含：
      tls_content_type, tls_record_len, est_plaintext_len,
      inferred_h2_type, est_h2_payload_range, category
    """
    # --- ACK ---
    if tcp_payload_len == 0:
        return {
            "tls_content_type":    "-",
            "tls_record_len":      "-",
            "est_plaintext_len":   "-",
            "inferred_h2_type":    "-",
            "est_h2_payload_range": "-",
            "category":            "ack",
        }

    # --- TCP 续传包（大 TLS record 跨多个 TCP segment，此包内既无 content_type 也无 record_len）---
    # 注意：多段 TLS record 的"拼齐包"（最后一段）有 record_len 但无 content_type，不属于此类
    if not raw_ct_list and not raw_rec_len_list:
        return {
            "tls_content_type":    "-",
            "tls_record_len":      "-",
            "est_plaintext_len":   "-",
            "inferred_h2_type":    "-",
            "est_h2_payload_range": "-",
            "category":            "tls_segment",
        }

    # 取第一个 content_type 作为代表（同一包内通常一致）
    # 多段 TLS record 的"拼齐包"：有 record_len 但 content_type 为空，视为 ApplicationData
    first_ct = raw_ct_list[0] if raw_ct_list else "23"
    ct_label = TLS_CONTENT_TYPES.get(first_ct, "-" if not first_ct else first_ct)

    # --- TLS 握手包 ---
    if first_ct in ("20", "22"):
        return {
            "tls_content_type":    ct_label,
            "tls_record_len":      ",".join(raw_rec_len_list) or "-",
            "est_plaintext_len":   "-",
            "inferred_h2_type":    "-",
            "est_h2_payload_range": "-",
            "category":            "handshake",
        }

    # --- Alert ---
    if first_ct == "21":
        return {
            "tls_content_type":    ct_label,
            "tls_record_len":      ",".join(raw_rec_len_list) or "-",
            "est_plaintext_len":   "-",
            "inferred_h2_type":    "-",
            "est_h2_payload_range": "-",
            "category":            "unknown",
        }

    # --- ApplicationData（content_type == 23，或拼齐包无 content_type）---
    if not raw_rec_len_list:
        return {
            "tls_content_type":    ct_label,
            "tls_record_len":      "-",
            "est_plaintext_len":   "-",
            "inferred_h2_type":    "?",
            "est_h2_payload_range": "?",
            "category":            "unknown",
        }

    rec_lens: list[int] = []
    for s in raw_rec_len_list:
        try:
            rec_lens.append(int(s))
        except ValueError:
            pass

    if not rec_lens:
        return {
            "tls_content_type":    ct_label,
            "tls_record_len":      ",".join(raw_rec_len_list),
            "est_plaintext_len":   "?",
            "inferred_h2_type":    "?",
            "est_h2_payload_range": "?",
            "category":            "unknown",
        }

    # 对每条 TLS record 分别推测
    h2_types   = []
    h2_ranges  = []
    sub_cats   = []
    est_plains = []

    for rl in rec_lens:
        h2t, h2r, sub = infer_single_record(rl, direction, overhead, with_ctrl)
        h2_types.append(h2t)
        h2_ranges.append(h2r)
        sub_cats.append(sub)
        est_plains.append(str(rl - overhead))

    # 聚合 category：以最主要的 sub_category 为准
    cat_priority = {"data": 4, "header": 3, "control": 2, "unknown": 1}
    final_cat = max(sub_cats, key=lambda c: cat_priority.get(c, 0))

    return {
        "tls_content_type":    ct_label,
        "tls_record_len":      ",".join(str(r) for r in rec_lens),
        "est_plaintext_len":   ",".join(est_plains),
        "inferred_h2_type":    ",".join(h2_types),
        "est_h2_payload_range": ",".join(h2_ranges),
        "category":            final_cat,
    }

# ---------------------------------------------------------------------------
# 主处理逻辑
# ---------------------------------------------------------------------------

def process(pcap: Path, flow_tsv: Path, output_dir: Path, with_ctrl: bool = False):
    """在不解密负载的前提下推断包类别，并按流写入结果目录。"""
    flow_meta = read_flow_tsv(flow_tsv)
    if not flow_meta:
        log.error("capture_chrome.tsv 中未找到有效流记录，退出")
        sys.exit(1)
    log.info("从 TSV 读取流数: %d", len(flow_meta))

    packets = run_tshark(pcap)
    log.info("读取包数: %d", len(packets))

    pcap_start = 0.0
    for pkt in packets:
        try:
            pcap_start = float(pkt["frame.time_epoch"])
            break
        except ValueError:
            continue

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

        meta = flow_meta[key]
        overhead = TLS_OVERHEAD.get(meta["tls_version"], DEFAULT_OVERHEAD)

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

        # 一个包可能含多条 TLS record → occurrence=a 给出逗号分隔值
        raw_ct_list      = [t.strip() for t in pkt["tls.record.content_type"].split(",") if t.strip()]
        raw_rec_len_list = [
            length.strip()
            for length in pkt["tls.record.length"].split(",")
            if length.strip()
        ]

        inferred = infer_packet(
            raw_ct_list, raw_rec_len_list,
            tcp_payload_len, meta["direction"], overhead,
            with_ctrl=with_ctrl,
        )

        flow_pkts[key].append({
            "frame_no":             pkt["frame.number"],
            "time_rel":             f"{ts:.6f}",
            "pkt_len":              pkt_len,
            "tcp_payload_len":      tcp_payload_len,
            **inferred,
        })

    # 输出：每条流一个文件
    output_dir.mkdir(parents=True, exist_ok=True)
    log.info("开始写出流文件，共 %d 条流", len(flow_meta))

    for key, meta in flow_meta.items():
        all_pkts  = flow_pkts.get(key, [])
        direction = meta["direction"]
        overhead  = TLS_OVERHEAD.get(meta["tls_version"], DEFAULT_OVERHEAD)

        fname = f"{direction}_{meta['src_port']}_{meta['dst_port']}.tsv"
        fpath = output_dir / fname

        cats = Counter(p["category"] for p in all_pkts)
        pkts = all_pkts

        with fpath.open("w", newline="", encoding="utf-8") as fh:
            fh.write(f"# src_ip       : {meta['src_ip']}\n")
            fh.write(f"# dst_ip       : {meta['dst_ip']}\n")
            fh.write(f"# src_port     : {meta['src_port']}\n")
            fh.write(f"# dst_port     : {meta['dst_port']}\n")
            fh.write(f"# transport    : {meta['transport']}\n")
            fh.write(f"# tls_version  : {meta['tls_version']}\n")
            fh.write(f"# tls_overhead : {overhead} bytes\n")
            fh.write(f"# app_protocol : {meta['app_protocol']}\n")
            fh.write(f"# sni          : {meta['sni']}\n")
            fh.write(f"# direction    : {direction}\n")
            fh.write(f"# pkt_count    : {len(pkts)}"
                     f"  handshake={cats['handshake']} control={cats['control']}"
                     f" header={cats['header']} data={cats['data']}"
                     f" ack={cats['ack']} tls_segment={cats['tls_segment']}"
                     f" unknown={cats['unknown']}"
                     + "\n")
            fh.write("#\n")

            writer = csv.DictWriter(fh, fieldnames=PKT_FIELDS, delimiter="\t")
            writer.writeheader()
            writer.writerows(pkts)

        log.info("  %s  (%d 包)", fpath.name, len(pkts))

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    """解析命令行参数并执行密文侧包类型推断。"""
    parser = argparse.ArgumentParser(
        description=(
            "对 capture_chrome.tsv 中的每条流，不解密密文流量，\n"
            "推测每个包的 H2 帧类型和帧 payload 长度范围，每条流输出一个 TSV 文件。\n"
            "\n"
            "────────────────────────────────────────────────────────\n"
            "文件头（# 注释行）字段说明\n"
            "────────────────────────────────────────────────────────\n"
            "  src_ip        源 IP 地址（IPv4 或 IPv6）\n"
            "  dst_ip        目的 IP 地址\n"
            "  src_port      源 TCP 端口\n"
            "  dst_port      目的 TCP 端口\n"
            "  transport     传输层协议，固定为 TCP\n"
            "  tls_version   TLS 版本（TLSv1.2 / TLSv1.3），来自 capture_chrome.tsv\n"
            "  tls_overhead  推算明文长度时减去的加密开销（字节）\n"
            "                  TLSv1.3 = 17（16 字节 AEAD tag + 1 字节 inner content type）\n"
            "                  TLSv1.2 = 16（16 字节 AEAD tag）\n"
            "  app_protocol  应用层协议，通常为 h2（HTTP/2）\n"
            "  sni           TLS ClientHello 中的 Server Name Indication\n"
            "  direction     流方向：forward（客户端→服务端）/ reverse（服务端→客户端）\n"
            "  pkt_count     包总数及各 category 的分布计数\n"
            "\n"
            "────────────────────────────────────────────────────────\n"
            "数据行字段说明\n"
            "────────────────────────────────────────────────────────\n"
            "  frame_no\n"
            "      Wireshark/tshark 帧编号，可在 pcap 中定位原始包。\n"
            "\n"
            "  time_rel\n"
            "      相对于 pcap 第一包的时间（秒，6 位小数）。\n"
            "\n"
            "  pkt_len\n"
            "      以太网帧总长度（字节），含所有协议头。\n"
            "\n"
            "  tcp_payload_len\n"
            "      TCP payload 字节数（即 tcp.len）。\n"
            "      为 0 表示纯 ACK，无应用层数据。\n"
            "\n"
            "  tls_content_type\n"
            "      TLS record 头部的 content_type 字段，明文可见，无需解密。\n"
            "        Handshake       = TLS 握手消息（ClientHello / ServerHello 等）\n"
            "        ChangeCipherSpec = TLS 1.2 密钥切换信令\n"
            "        ApplicationData = 加密的应用数据（TLS 1.3 的握手续传也显示为此值）\n"
            "        -               = 该包不含 TLS record 起始头（TCP 续传包或纯 ACK）\n"
            "\n"
            "  tls_record_len\n"
            "      TLS record 头部的 length 字段，即加密载荷的字节数，明文可见。\n"
            "      一个 TCP 包内可含多条 TLS record，用逗号分隔。\n"
            "      对于跨多个 TCP 分段的大 record，此值出现在最后一个分段上。\n"
            "      - 表示该包无完整 TLS record 头部（续传中间段或纯 ACK）。\n"
            "\n"
            "  est_plaintext_len\n"
            "      推算的明文长度（字节）= tls_record_len - tls_overhead。\n"
            "      对应 tls_record_len 中每条 record 分别计算，逗号分隔。\n"
            "      - 表示无法推算（握手包、续传包等）。\n"
            "\n"
            "  inferred_h2_type\n"
            "      根据 est_plaintext_len 和流方向推测的 HTTP/2 帧类型。\n"
            "      对应 est_plaintext_len 中每条 record，逗号分隔。\n"
            "      推测规则（按 est_plaintext_len 精确匹配优先）：\n"
            "        9  字节 → SETTINGS_ACK（0 字节 payload）\n"
            "       13  字节 → WINDOW_UPDATE 或 RST_STREAM（4 字节 payload）\n"
            "       14  字节 → PRIORITY（5 字节 payload）\n"
            "       17  字节 → PING（8 字节 payload）\n"
            "       9+6N 字节（N=1..8）→ SETTINGS（N 个参数）\n"
            "      其余按大小和方向启发推测：\n"
            "        forward 小包（≤500 B）→ HEADERS（请求头）\n"
            "        reverse 小包（≤800 B）→ HEADERS（响应头）\n"
            "        reverse 大包（>800 B） → DATA（响应正文）\n"
            "      - 表示握手/续传包，不推测。\n"
            "      ? 表示明文长度不足以容纳一个完整 H2 帧头（<9 字节）。\n"
            "\n"
            "  est_h2_payload_range\n"
            "      推测的 H2 帧 payload 长度范围，格式为 \"min-max\"（字节）。\n"

            "      max = est_plaintext_len - 9（单帧独占整个明文空间，固定值）。\n"
            "      min 由 --with-coframe 参数控制：\n"
            "        默认（不加参数）：假设单帧独占，min == max == est - 9，范围最紧，\n"
            "                         形如 \"2757-2757\"。\n"
            "        --with-coframe ：假设可能附带控制帧，min 相应缩小：\n"
            "                         DATA    → min = est - 22（附带 ≤13B 控制帧）\n"
            "                         HEADERS → min = max(0, est - 18)（最多 2 帧并存）\n"
            "                         HEADERS|DATA → min = 0（类型不确定，保守值）\n"
            "      精确命中固定大小控制帧时 min == max（例如 WINDOW_UPDATE → 4-4）。\n"
            "      - 或 ? 含义同 inferred_h2_type。\n"
            "\n"
            "  category\n"
            "      推测的包类别（基于上述所有字段综合判断）：\n"
            "        ack         纯 ACK，tcp_payload_len == 0\n"
            "        handshake   TLS 握手包（content_type = Handshake / ChangeCipherSpec）\n"
            "        control     推测为 H2 控制帧（SETTINGS / WINDOW_UPDATE / PING 等）\n"
            "        header      推测为 H2 HEADERS 帧（请求头或响应头）\n"
            "        data        推测为 H2 DATA 帧（请求/响应正文）\n"
            "        tls_segment TCP 续传包：大 TLS record 跨多个 TCP 分段，\n"
            "                    此包处于中间段，无完整 TLS record 信息\n"
            "        unknown     无法归类（明文估算长度 <9 字节或 Alert 等罕见情形）\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pcap", help="pcap 文件路径（不需要 keylog）")
    parser.add_argument(
        "--flow-tsv", "-f", default=None,
        help="capture_chrome.tsv 路径（默认：pcap 同目录下的 capture_chrome.tsv）",
    )
    parser.add_argument(
        "--output-dir", "-o", default=None,
        help="输出目录（默认：pcap 同目录下的 {pcap_stem}_inferred/）",
    )
    parser.add_argument(
        "--with-coframe", action="store_true", default=False,
        help=(
            "est_h2_payload_range 的 min 计算模式：\n"
            "  不加此参数（默认）：假设单帧独占，min == max == est_plaintext - 9，范围最紧。\n"
            "  加上此参数        ：假设可能附带一个控制帧，min 相应缩小：\n"
            "    DATA    → min = est - 22（附带 ≤13B 控制帧）\n"
            "    HEADERS → min = max(0, est - 18)（最多 2 帧并存）\n"
        ),
    )
    args = parser.parse_args()

    pcap = Path(args.pcap)
    if not pcap.exists():
        log.error("pcap 不存在：%s", pcap)
        sys.exit(1)

    flow_tsv = (Path(args.flow_tsv) if args.flow_tsv
                else pcap.parent / f"{pcap.stem}.tsv")
    if not flow_tsv.exists():
        log.error("flow-tsv 不存在：%s", flow_tsv)
        sys.exit(1)

    output_dir = (Path(args.output_dir) if args.output_dir
                  else pcap.parent / f"{pcap.stem}_inferred")

    log.info("pcap      : %s", pcap)
    log.info("flow_tsv  : %s", flow_tsv)
    log.info("output_dir: %s", output_dir)

    process(pcap, flow_tsv, output_dir, with_ctrl=args.with_coframe)


if __name__ == "__main__":
    main()
