"""
遍历 fetch_output 目录下所有子文件夹，读取每个子文件夹中的
capture_{browser}.tsv（由 extract_features.py 生成），提取多流统计特征，
合并输出为 multi-flow.tsv。

输出列说明：
  id               词条 ID（目录名 "ID-wiki-词条名" 中的数字部分）
  wiki_name        词条名称
  flow_count       正向流条数（dst_port=443 的流，每条代表一个 TCP 连接）
  sni_count        SNI 种类数
  SNI-{name}       各 SNI 在正向流中出现的次数（动态列，全局所有 SNI 按字母排序）
  upstream_bytes   正向流（client→server）byte_count 升序序列，逗号分隔
  downstream_bytes 反向流（server→client）byte_count 升序序列，逗号分隔

用法：
  python3 extract_multi_flow.py <fetch_output_dir>
  python3 extract_multi_flow.py fetch_output_chrome --browser chrome
  python3 extract_multi_flow.py fetch_output_firefox --browser firefox
  python3 extract_multi_flow.py fetch_output_chrome --output result.tsv
"""

import csv
import sys
import logging
import argparse
from pathlib import Path
from collections import Counter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def parse_id_name(folder_name: str) -> tuple[str, str]:
    """
    从目录名 '123-wiki-词条名' 解析出 ID 和词条名。
    若格式不符，ID 和词条名均返回原始目录名。
    """
    parts = folder_name.split("-wiki-", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return folder_name, folder_name


def read_flow_tsv(tsv_path: Path) -> list[dict]:
    """读取 capture_{browser}.tsv，跳过表头，返回所有数据行。"""
    rows: list[dict] = []
    with tsv_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 单目录处理
# ---------------------------------------------------------------------------

def process_dir(subdir: Path, browser: str) -> dict | None:
    """
    处理单个子目录，返回统计行 dict；找不到 TSV 或无数据则返回 None。

    返回 dict 中包含内部字段 "_sni_counter"（后续转列时使用，不写入 TSV）。
    """
    tsv_path = subdir / f"capture_{browser}.tsv"
    if not tsv_path.exists():
        log.warning("[%s] 未找到 %s，跳过", subdir.name, tsv_path.name)
        return None

    rows = read_flow_tsv(tsv_path)
    if not rows:
        log.info("[%s] TSV 无数据行，跳过", subdir.name)
        return None

    entry_id, wiki_name = parse_id_name(subdir.name)

    # 正向流：client→server（dst_port=443）
    # 反向流：server→client（src_port=443）
    forward_rows = [r for r in rows if r.get("dst_port", "").strip() == "443"]
    reverse_rows = [r for r in rows if r.get("src_port", "").strip() == "443"]

    # SNI 统计（基于正向流；每个正向 TCP 连接计一次）
    sni_counter: Counter = Counter()
    for r in forward_rows:
        sni = r.get("sni", "").strip()
        if sni:
            sni_counter[sni] += 1

    # byte_count 升序序列
    def sorted_sizes(row_list: list[dict]) -> list[int]:
        vals: list[int] = []
        for r in row_list:
            try:
                vals.append(int(r.get("byte_count", 0)))
            except (ValueError, TypeError):
                pass
        return sorted(vals)

    upstream   = sorted_sizes(forward_rows)
    downstream = sorted_sizes(reverse_rows)

    return {
        "id":               entry_id,
        "wiki_name":        wiki_name,
        "flow_count":       len(forward_rows),
        "sni_count":        len(sni_counter),
        "_sni_counter":     sni_counter,          # 内部字段，不直接写 TSV
        "upstream_bytes":   ",".join(str(v) for v in upstream),
        "downstream_bytes": ",".join(str(v) for v in downstream),
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "遍历 fetch_output 目录下所有子文件夹，读取 capture_{browser}.tsv，\n"
            "提取多流统计特征，合并输出为 multi-flow.tsv。\n\n"
            "输出字段：\n"
            "  id               词条 ID\n"
            "  wiki_name        词条名称\n"
            "  flow_count       正向流条数（每条对应一个 TCP 连接）\n"
            "  sni_count        SNI 种类数\n"
            "  SNI-{name}       各 SNI 在正向流中出现的次数（动态列）\n"
            "  upstream_bytes   正向流 byte_count 升序序列（逗号分隔）\n"
            "  downstream_bytes 反向流 byte_count 升序序列（逗号分隔）\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "root",
        help="包含子文件夹的根目录，如 fetch_output_chrome",
    )
    parser.add_argument(
        "--browser", "-b", default="chrome",
        help="浏览器标识，对应 capture_{browser}.tsv（默认：chrome）",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="输出 TSV 路径（默认：<root>/multi-flow.tsv）",
    )
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        log.error("目录不存在：%s", root)
        sys.exit(1)

    output_path = Path(args.output) if args.output else root / "multi-flow.tsv"

    subdirs = sorted(d for d in root.iterdir() if d.is_dir())
    log.info("根目录   : %s", root)
    log.info("浏览器   : %s", args.browser)
    log.info("子目录数 : %d", len(subdirs))

    # ── 第一遍：收集所有行数据 + 全局 SNI 集合 ─────────────────────────
    rows_data: list[dict] = []
    all_snis:  set[str]   = set()

    for subdir in subdirs:
        result = process_dir(subdir, args.browser)
        if result is None:
            continue
        all_snis.update(result["_sni_counter"].keys())
        rows_data.append(result)

    if not rows_data:
        log.warning("没有找到任何有效数据，退出")
        sys.exit(0)

    # ── 构建列名：SNI 列按字母排序 ─────────────────────────────────────
    sorted_snis   = sorted(all_snis)
    sni_col_names = [f"SNI-{sni}" for sni in sorted_snis]

    fieldnames = (
        ["id", "wiki_name", "flow_count", "sni_count"]
        + sni_col_names
        + ["upstream_bytes", "downstream_bytes"]
    )

    # ── 写出 TSV ───────────────────────────────────────────────────────
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t",
                                extrasaction="ignore")
        writer.writeheader()

        for row in rows_data:
            sni_counter = row.pop("_sni_counter")
            out_row: dict = {
                "id":               row["id"],
                "wiki_name":        row["wiki_name"],
                "flow_count":       row["flow_count"],
                "sni_count":        row["sni_count"],
                "upstream_bytes":   row["upstream_bytes"],
                "downstream_bytes": row["downstream_bytes"],
            }
            for sni in sorted_snis:
                out_row[f"SNI-{sni}"] = sni_counter.get(sni, 0)
            writer.writerow(out_row)

    log.info("输出记录数    : %d", len(rows_data))
    log.info("全局 SNI 种类 : %d  %s", len(sorted_snis), sorted_snis)
    log.info("输出文件      : %s", output_path)


if __name__ == "__main__":
    main()
