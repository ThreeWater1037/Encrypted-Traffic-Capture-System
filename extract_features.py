"""
pcap 流特征提取器
用法：
    python3 extract_features.py <pcap文件>
    python3 extract_features.py <pcap文件> --output flows.tsv
    python3 extract_features.py fetch_output/  # 递归处理目录下所有 pcap
"""

import csv
import sys
import shutil
import logging
import argparse
import subprocess
from pathlib import Path
from dataclasses import dataclass, field, asdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# tshark 可执行路径
# ---------------------------------------------------------------------------

TSHARK_CANDIDATES = [
    "D:/software/Wireshark/tshark.exe",
    "C:/Program Files/Wireshark/tshark.exe",
    "C:/Program Files (x86)/Wireshark/tshark.exe",
    "/Applications/Wireshark.app/Contents/MacOS/tshark",
    "/usr/local/bin/tshark",
    "/usr/bin/tshark",
]

def find_tshark() -> str:
    """按固定候选路径和 PATH 查找 TShark，找不到时立即报错。"""
    for p in TSHARK_CANDIDATES:
        if Path(p).is_file():
            return p
    found = shutil.which("tshark")
    if found:
        return found
    raise RuntimeError("tshark 未找到，请安装 Wireshark")

TSHARK = find_tshark()

# ---------------------------------------------------------------------------
# tshark 导出字段
# ---------------------------------------------------------------------------
# 每个字段对应 tshark 的 -e 参数，输出为 TAB 分隔
# 多值字段（如多个 SNI）tshark 用逗号合并

TSHARK_FIELDS = [
    "frame.time_epoch",       # 包时间戳
    "frame.len",              # 包总长度（含各层头部）
    "ip.src",                 # IPv4 源
    "ip.dst",                 # IPv4 目的
    "ipv6.src",               # IPv6 源
    "ipv6.dst",               # IPv6 目的
    "tcp.srcport",            # TCP 源端口
    "tcp.dstport",            # TCP 目的端口
    "udp.srcport",            # UDP 源端口
    "udp.dstport",            # UDP 目的端口
    "tls.handshake.version",  # TLS 版本（ClientHello/ServerHello）
    "tls.record.version",     # TLS 记录层版本
    "tls.handshake.extensions_server_name",  # SNI
    "tls.handshake.type",     # 握手类型（2=ServerHello, 1=ClientHello）
    "tls.handshake.extensions.supported_version",  # TLS 1.3 真实版本（singular）
    "http.request.version",   # HTTP/1.x 版本字符串
    "http2.type",             # HTTP/2 帧类型（存在即为 h2）
    "quic.version",           # QUIC 版本（存在即为 h3）
    "tls.handshake.extensions_alpn_str", # ALPN 协商结果
]

# ---------------------------------------------------------------------------
# 流记录
# ---------------------------------------------------------------------------

# 五元组 key
FlowKey = tuple[str, str, int, int, str]  # src_ip, dst_ip, src_port, dst_port, transport

@dataclass
class FlowRecord:
    """聚合同一双向五元组的时间、字节、包数、TLS 和 SNI 特征。"""
    src_ip:       str
    dst_ip:       str
    src_port:     int
    dst_port:     int
    transport:    str        # TCP / UDP
    tls_version:  str = "-1"
    app_protocol: str = "other"
    sni:          str = ""
    pkt_count:    int = 0
    byte_count:   int = 0
    start_time:   float = 0.0
    duration_s:   float = 0.0
    _last_time:   float = field(default=0.0, repr=False)

    def update_time(self, ts: float):
        """用新数据包时间更新流的起止时间。"""
        if self.start_time == 0.0:
            self.start_time = ts
        self._last_time = max(self._last_time, ts)
        self.duration_s = round(self._last_time - self.start_time, 6)

FLOW_FIELDS = [
    "src_ip", "dst_ip", "src_port", "dst_port", "transport",
    "tls_version", "app_protocol", "sni",
    "pkt_count", "byte_count", "start_time", "duration_s",
]

# ---------------------------------------------------------------------------
# TLS 版本映射
# ---------------------------------------------------------------------------

TLS_VERSION_MAP = {
    "0x0301": "TLSv1.0",
    "0x0302": "TLSv1.1",
    "0x0303": "TLSv1.2",
    "0x0304": "TLSv1.3",
}

_TLS_VERSION_PRIORITY = ["0x0304", "0x0303", "0x0302", "0x0301"]

def _tls_version_str(raw: str) -> str:
    """
    从 tshark 输出（可能含多个逗号分隔值）中取优先级最高的 TLS 版本。
    优先级：TLSv1.3 > TLSv1.2 > TLSv1.1 > TLSv1.0
    """
    parts = {p.strip().lower() for p in raw.replace(",", " ").split()}
    for ver_hex in _TLS_VERSION_PRIORITY:
        if ver_hex in parts:
            return TLS_VERSION_MAP[ver_hex]
    return "-1"

# ---------------------------------------------------------------------------
# 核心提取逻辑
# ---------------------------------------------------------------------------

def run_tshark(pcap: Path) -> list[dict]:
    """调用 tshark，返回每包的字段字典列表。"""
    cmd = [TSHARK, "-r", str(pcap), "-T", "fields", "-E", "separator=\t",
           "-E", "quote=n", "-E", "occurrence=a"]  # occurrence=a 取全部值，逗号分隔
    for f in TSHARK_FIELDS:
        cmd += ["-e", f]

    log.debug("cmd: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

    packets = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        # 补齐字段数（tshark 有时省略末尾空字段）
        while len(parts) < len(TSHARK_FIELDS):
            parts.append("")
        packets.append(dict(zip(TSHARK_FIELDS, parts)))
    return packets


def extract_flows(packets: list[dict]) -> list[FlowRecord]:
    """将包列表聚合为流列表。start_time 为相对于 pcap 第一个包的偏移秒数。"""
    flows: dict[FlowKey, FlowRecord] = {}

    # 确定 pcap 起始时间（第一个有效时间戳）
    pcap_start: float = 0.0
    for pkt in packets:
        try:
            pcap_start = float(pkt["frame.time_epoch"])
            break
        except ValueError:
            continue

    for pkt in packets:
        # --- 时间戳和包长 ---
        try:
            ts = float(pkt["frame.time_epoch"]) - pcap_start  # 相对时间
        except ValueError:
            continue
        try:
            pkt_len = int(pkt["frame.len"])
        except ValueError:
            pkt_len = 0

        # --- 传输层协议和端口 ---
        if pkt["tcp.srcport"]:
            transport = "TCP"
            try:
                src_port = int(pkt["tcp.srcport"])
                dst_port = int(pkt["tcp.dstport"])
            except ValueError:
                continue
        elif pkt["udp.srcport"]:
            transport = "UDP"
            try:
                src_port = int(pkt["udp.srcport"])
                dst_port = int(pkt["udp.dstport"])
            except ValueError:
                continue
        else:
            continue  # 非 TCP/UDP（如 ICMP）跳过

        # --- IP 地址（优先 IPv4）---
        src_ip = pkt["ip.src"] or pkt["ipv6.src"] or ""
        dst_ip = pkt["ip.dst"] or pkt["ipv6.dst"] or ""
        if not src_ip or not dst_ip:
            continue

        key: FlowKey = (src_ip, dst_ip, src_port, dst_port, transport)

        if key not in flows:
            flows[key] = FlowRecord(
                src_ip=src_ip, dst_ip=dst_ip,
                src_port=src_port, dst_port=dst_port,
                transport=transport,
            )

        flow = flows[key]
        flow.pkt_count += 1
        flow.byte_count += pkt_len
        flow.update_time(ts)

        # --- TLS 版本 ---
        # supported_version 扩展优先（含全部协商版本，取最高）；
        # 回退到 handshake.version，再回退到 record.version
        raw_ver = (pkt["tls.handshake.extensions.supported_version"]
                   or pkt["tls.handshake.version"]
                   or pkt["tls.record.version"] or "")
        if raw_ver:
            v = _tls_version_str(raw_ver)
            # 只升级，不降级（一条流可能有多个握手包）
            cur_pri = _TLS_VERSION_PRIORITY.index(
                next((k for k, vv in TLS_VERSION_MAP.items() if vv == flow.tls_version), "")
            ) if flow.tls_version != "-1" else len(_TLS_VERSION_PRIORITY)
            new_pri = _TLS_VERSION_PRIORITY.index(
                next((k for k, vv in TLS_VERSION_MAP.items() if vv == v), "")
            ) if v != "-1" else len(_TLS_VERSION_PRIORITY)
            if new_pri < cur_pri:  # 索引越小优先级越高
                flow.tls_version = v

        # --- SNI（只在 ClientHello 中出现）---
        sni = pkt["tls.handshake.extensions_server_name"].strip()
        if sni and not flow.sni:
            flow.sni = sni

        # --- 应用层协议 ---
        # 优先级：h3 > h2 > h1 > other
        # occurrence=a 时 ALPN 可能返回逗号分隔的多个值，拆开后逐一检查
        alpn_vals = {v.strip().lower()
                     for v in pkt["tls.handshake.extensions_alpn_str"].split(",")
                     if v.strip()}

        if pkt["quic.version"] or (transport == "UDP" and dst_port == 443):
            flow.app_protocol = "h3"
        elif pkt["http2.type"] or "h2" in alpn_vals:
            if flow.app_protocol not in ("h3",):
                flow.app_protocol = "h2"
        elif pkt["http.request.version"] or alpn_vals & {"http/1.1", "http/1.0"}:
            if flow.app_protocol not in ("h3", "h2"):
                flow.app_protocol = "h1"

    return list(flows.values())


def filter_by_sni(flows: list[FlowRecord],
                  sni_suffixes: list[str]) -> list[FlowRecord]:
    """
    保留 SNI 匹配任意一个后缀的流，同时包含这些流的反方向流。
    后缀匹配不区分大小写，支持带或不带前导点（如 wikipedia.org 或 .wikipedia.org）。

    反方向流说明：SNI 只出现在 ClientHello（正向），反向流 SNI 为空，
    通过正向流的五元组镜像（src↔dst、sport↔dport、同 transport）来查找。
    """
    if not sni_suffixes:
        return flows

    # 统一加前导点，使 "wikipedia.org" 同时匹配 "zh.wikipedia.org" 和 "wikipedia.org"
    normalized = []
    for s in sni_suffixes:
        s = s.lower().strip()
        if not s.startswith("."):
            s = "." + s
        normalized.append(s)

    def sni_matches(sni: str) -> bool:
        """按完整域名或子域后缀匹配 SNI 白名单。"""
        sni = sni.lower()
        for suffix in normalized:
            if sni == suffix.lstrip(".") or sni.endswith(suffix):
                return True
        return False

    # 建立全量流的五元组索引，方便快速查找反向流
    flow_index: dict[FlowKey, FlowRecord] = {
        (f.src_ip, f.dst_ip, f.src_port, f.dst_port, f.transport): f
        for f in flows
    }

    result: list[FlowRecord] = []
    seen: set[FlowKey] = set()

    for flow in flows:
        if not sni_matches(flow.sni):
            continue

        fwd_key: FlowKey = (flow.src_ip, flow.dst_ip, flow.src_port, flow.dst_port, flow.transport)
        rev_key: FlowKey = (flow.dst_ip, flow.src_ip, flow.dst_port, flow.src_port, flow.transport)

        # TLS 版本：取正向与反向中较高的版本，统一应用到双向
        # 原因：ClientHello legacy_version 强制写 0x0303（TLS 1.2），
        #       真实协商版本在 ServerHello 的 supported_versions 扩展（反向流）
        rev = flow_index.get(rev_key)
        tls_rank = {"-1": 0, "TLSv1.0": 1, "TLSv1.1": 2, "TLSv1.2": 3, "TLSv1.3": 4}
        if rev:
            best_tls = max(flow.tls_version, rev.tls_version,
                           key=lambda v: tls_rank.get(v, 0))
            flow.tls_version = best_tls

        if fwd_key not in seen:
            result.append(flow)
            seen.add(fwd_key)

        # 查找并附加反方向流，继承正向流的 sni、app_protocol、tls_version
        if rev and rev_key not in seen:
            rev.sni          = flow.sni
            rev.app_protocol = flow.app_protocol
            rev.tls_version  = flow.tls_version
            result.append(rev)
            seen.add(rev_key)

    return result


def write_tsv(flows: list[FlowRecord], output: Path):
    """将流列表写入 TSV 文件。时间字段保留 6 位小数。"""
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FLOW_FIELDS, delimiter="\t")
        writer.writeheader()
        for flow in flows:
            d = asdict(flow)
            d["start_time"] = f"{d['start_time']:.6f}"
            d["duration_s"] = f"{d['duration_s']:.6f}"
            writer.writerow({k: d[k] for k in FLOW_FIELDS})
    log.info("  → %s (%d 条流)", output, len(flows))


def process_pcap(pcap: Path, output: Path, sni_suffixes: list[str]):
    """完成单个 PCAP 的解析、流聚合、SNI 过滤和 TSV 输出。"""
    log.info("处理: %s", pcap)
    packets = run_tshark(pcap)
    log.info("  包数: %d", len(packets))
    flows = extract_flows(packets)
    log.info("  流数（过滤前）: %d", len(flows))

    if sni_suffixes:
        flows = filter_by_sni(flows, sni_suffixes)
        log.info("  流数（SNI 过滤后）: %d  后缀=%s", len(flows), sni_suffixes)

    write_tsv(flows, output)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    """解析命令行参数，支持处理单个 PCAP 或递归处理目录。"""
    parser = argparse.ArgumentParser(
        description=(
            "从 pcap 文件提取每条流的特征，输出 TSV。\n"
            "支持单个 pcap 文件或递归扫描目录下所有 *.pcap。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "input", help="pcap 文件路径 或 包含 pcap 的目录"
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="输出 TSV 路径（单文件模式）。目录模式下每个 pcap 旁生成同名 .tsv，此参数忽略。",
    )
    parser.add_argument(
        "--sni-suffix", nargs="+", default=[], metavar="SUFFIX",
        help=(
            "SNI 后缀白名单，只保留 SNI 匹配这些后缀的流（不区分大小写）。"
            "可指定多个，例如：--sni-suffix wikipedia.org wikimedia.org"
        ),
    )
    args = parser.parse_args()

    target = Path(args.input)
    sni_suffixes: list[str] = args.sni_suffix

    if target.is_dir():
        pcaps = sorted(target.rglob("*.pcap"))
        if not pcaps:
            log.error("目录中未找到 *.pcap 文件：%s", target)
            sys.exit(1)
        log.info("找到 %d 个 pcap 文件", len(pcaps))
        for pcap in pcaps:
            out = pcap.with_suffix(".tsv")
            process_pcap(pcap, out, sni_suffixes)
    elif target.is_file():
        out = Path(args.output) if args.output else target.with_suffix(".tsv")
        process_pcap(target, out, sni_suffixes)
    else:
        log.error("路径不存在：%s", target)
        sys.exit(1)


if __name__ == "__main__":
    main()
