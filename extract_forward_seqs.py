"""
遍历 fetch_output 目录下所有子文件夹，读取
capture_{browser}_inferred/ 下所有 forward_*.tsv，
提取包计数和 header 长度序列，合并输出为 forward.tsv。

提取规则：
  handshake / control / header / data / ack / tls_segment / unknown
    → 从文件头注释行读计数，如：
      # pkt_count    : 9  handshake=2 control=0 header=0 data=0 ack=6 tls_segment=1 unknown=0

  header_lens
    → 读数据行，对 category=header 的包取 est_h2_payload_range 第一个数字（如 "61-61" → 61）

输出 TSV 列：
  id            词条 ID
  wiki_name     词条名称
  flow          流文件名（不含 .tsv），如 forward_55926_443
  sni           SNI（来自文件头注释）
  handshake     handshake 包个数
  control       control   包个数
  header        header    包个数
  data          data      包个数
  ack           ack       包个数
  tls_segment   tls_segment 包个数
  unknown       unknown   包个数
  header_lens   header 包的 est_h2_payload_range 第一个数，逗号分隔

用法：
  python3 extract_forward_seqs.py <fetch_output_dir>
  python3 extract_forward_seqs.py fetch_output_chrome
  python3 extract_forward_seqs.py fetch_output_chrome --browser chrome
  python3 extract_forward_seqs.py fetch_output --browser firefox --output firefox_forward.tsv
  python3 extract_forward_seqs.py fetch_output_chrome --min-header 5
  python3 extract_forward_seqs.py fetch_output_chrome --split-sni
  python3 extract_forward_seqs.py fetch_output_chrome --split-sni --output-dir ./out/
"""

import csv
import re
import sys
import logging
import argparse
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# 计数字段（顺序与文件头一致）
COUNT_FIELDS = ["handshake", "control", "header", "data", "ack", "tls_segment", "unknown"]

# 匹配 "key=数字" 的模式
_RE_COUNT = re.compile(r"(\w+)=(\d+)")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def parse_id_name(folder_name: str) -> tuple[str, str]:
    """从 'ID-wiki-词条名' 解析 ID 和词条名。"""
    parts = folder_name.split("-wiki-", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return folder_name, folder_name


def parse_pkt_count_line(line: str) -> dict[str, int]:
    """
    解析 '# pkt_count    : 9  handshake=2 control=0 ...' 这类注释行。
    返回 {field: count} 字典，缺失字段默认 0。
    """
    counts = {f: 0 for f in COUNT_FIELDS}
    for key, val in _RE_COUNT.findall(line):
        if key in counts:
            counts[key] = int(val)
    return counts


# ---------------------------------------------------------------------------
# 单流文件处理
# ---------------------------------------------------------------------------

def process_flow_file(tsv_path: Path) -> dict:
    """
    解析单个 forward_*.tsv，返回：
      {sni, handshake, control, header, data, ack, tls_segment, unknown, header_lens}
    """
    sni    = ""
    counts = {f: 0 for f in COUNT_FIELDS}
    header_lens: list[int] = []
    comment_lines: list[str] = []

    with tsv_path.open(encoding="utf-8") as fh:
        data_lines: list[str] = []
        for raw_line in fh:
            if raw_line.startswith("#"):
                comment_lines.append(raw_line)
            else:
                data_lines.append(raw_line)

    # ── 解析注释头 ────────────────────────────────────────────────────
    for line in comment_lines:
        stripped = line[1:].strip()
        if stripped.startswith("sni"):
            _, _, v = stripped.partition(":")
            sni = v.strip()
        elif "pkt_count" in stripped:
            counts = parse_pkt_count_line(stripped)

    # ── 解析数据行：只取 header 类包的 est_h2_payload_range ────────────
    if data_lines:
        reader = csv.DictReader(data_lines, delimiter="\t")
        for row in reader:
            if row.get("category", "").strip() != "header":
                continue
            r = row.get("est_h2_payload_range", "").strip()
            if r and r != "-":
                try:
                    header_lens.append(int(r.split("-")[0]))
                except ValueError:
                    pass

    result: dict = {"sni": sni}
    result.update(counts)
    result["header_lens"] = ",".join(str(v) for v in header_lens)
    return result


# ---------------------------------------------------------------------------
# 单目录处理
# ---------------------------------------------------------------------------

def process_dir(subdir: Path, browser: str) -> list[dict]:
    """
    处理单个子目录，返回该目录下所有 forward_*.tsv 对应的行列表。
    """
    inferred_dir = subdir / f"capture_{browser}_inferred"
    if not inferred_dir.is_dir():
        return []

    entry_id, wiki_name = parse_id_name(subdir.name)
    forward_files = sorted(inferred_dir.glob("forward_*.tsv"))
    if not forward_files:
        return []

    rows: list[dict] = []
    for fp in forward_files:
        flow_info = process_flow_file(fp)
        row = {
            "id":        entry_id,
            "wiki_name": wiki_name,
            "flow":      fp.stem,
        }
        row.update(flow_info)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    """汇总所有正向推断流，输出适合距离分析的单一 TSV。"""
    parser = argparse.ArgumentParser(
        description=(
            "遍历 fetch_output 目录，读取 capture_{browser}_inferred/ 下所有\n"
            "forward_*.tsv，提取包计数及 header 长度序列，输出为 forward.tsv。\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("root", help="包含子文件夹的根目录，如 fetch_output_chrome")
    parser.add_argument(
        "--browser", "-b", default="chrome",
        help="浏览器标识，对应 capture_{browser}_inferred/（默认：chrome）",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="合并输出 TSV 路径（默认：<root>/forward.tsv）；--split-sni 时忽略此参数",
    )
    parser.add_argument(
        "--min-header", type=int, default=0, metavar="N",
        help="过滤：header 包个数低于 N 的流不计入输出（默认：0，不过滤）",
    )
    parser.add_argument(
        "--split-sni", action="store_true",
        help="按 SNI 分文件输出，每个 SNI 生成独立 TSV（文件名：forward_{sni}.tsv）",
    )
    parser.add_argument(
        "--output-dir", default=None, metavar="DIR",
        help="--split-sni 模式下的输出目录（默认：<root>）",
    )
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        log.error("目录不存在：%s", root)
        sys.exit(1)

    subdirs = sorted(d for d in root.iterdir() if d.is_dir())
    log.info("根目录   : %s", root)
    log.info("浏览器   : %s", args.browser)
    log.info("子目录数 : %d", len(subdirs))
    if args.min_header > 0:
        log.info("过滤条件 : header >= %d", args.min_header)
    if args.split_sni:
        log.info("输出模式 : 按 SNI 分文件")

    fieldnames = (
        ["id", "wiki_name", "flow", "sni"]
        + COUNT_FIELDS
        + ["header_lens"]
    )

    total_dirs    = 0
    total_flows   = 0
    skipped_flows = 0

    if args.split_sni:
        # ── 按 SNI 分文件输出 ────────────────────────────────────────
        out_dir = Path(args.output_dir) if args.output_dir else root
        out_dir.mkdir(parents=True, exist_ok=True)

        # sni → 打开的 (file_handle, DictWriter)
        writers: dict[str, tuple] = {}

        def _sni_filename(sni: str) -> Path:
            """把 SNI 清理为安全文件名，用于按域名拆分输出。"""
            # SNI 中的点保留，替换不安全字符
            safe = sni.replace("/", "_").replace("\\", "_") or "unknown"
            return out_dir / f"forward_{safe}.tsv"

        try:
            for subdir in subdirs:
                rows = process_dir(subdir, args.browser)
                if not rows:
                    continue
                kept = [r for r in rows if r.get("header", 0) >= args.min_header]
                skipped_flows += len(rows) - len(kept)
                if not kept:
                    continue
                total_dirs  += 1
                total_flows += len(kept)
                for row in kept:
                    sni = row.get("sni", "") or "unknown"
                    if sni not in writers:
                        fpath = _sni_filename(sni)
                        fh = fpath.open("w", newline="", encoding="utf-8")
                        w  = csv.DictWriter(fh, fieldnames=fieldnames,
                                            delimiter="\t", extrasaction="ignore")
                        w.writeheader()
                        writers[sni] = (fh, w)
                    writers[sni][1].writerow(row)
        finally:
            for fh, _ in writers.values():
                fh.close()

        log.info("处理目录数 : %d", total_dirs)
        log.info("输出流行数 : %d", total_flows)
        if args.min_header > 0:
            log.info("过滤丢弃数 : %d（header < %d）", skipped_flows, args.min_header)
        log.info("输出 SNI 数 : %d", len(writers))
        for sni in sorted(writers):
            log.info("  %s → %s", sni, _sni_filename(sni).name)

    else:
        # ── 合并输出为单个文件 ────────────────────────────────────────
        output_path = Path(args.output) if args.output else root / "forward.tsv"

        with output_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t",
                                    extrasaction="ignore")
            writer.writeheader()

            for subdir in subdirs:
                rows = process_dir(subdir, args.browser)
                if not rows:
                    continue
                kept = [r for r in rows if r.get("header", 0) >= args.min_header]
                skipped_flows += len(rows) - len(kept)
                if kept:
                    total_dirs  += 1
                    total_flows += len(kept)
                    writer.writerows(kept)

        log.info("处理目录数 : %d", total_dirs)
        log.info("输出流行数 : %d", total_flows)
        if args.min_header > 0:
            log.info("过滤丢弃数 : %d（header < %d）", skipped_flows, args.min_header)
        log.info("输出文件   : %s", output_path)


if __name__ == "__main__":
    main()
