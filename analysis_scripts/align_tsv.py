"""
将目录下多个 forward_*.tsv 文件按 id 对齐，合并为宽表。

id 和 wiki_name 只保留一次（各文件一致），其余列（flow/sni/特征列）
以各文件对应的域名为前缀区分，格式为 <domain>__<col>。

用法：
  python3 align_tsv.py <dir>
  python3 align_tsv.py ./fetch_output_firefox/
  python3 align_tsv.py ./fetch_output_firefox/ --output aligned.tsv
  python3 align_tsv.py ./fetch_output_firefox/ --pattern "forward_*.tsv"
  python3 align_tsv.py ./fetch_output_firefox/ --sort-id
"""

import csv
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

SHARED_COLS = ("id", "wiki_name")


def domain_from_filename(filename: str) -> str:
    """从 forward_zh.wikipedia.org.tsv 提取 zh.wikipedia.org"""
    stem = Path(filename).stem  # forward_zh.wikipedia.org
    return stem[len("forward_"):] if stem.startswith("forward_") else stem


def main():
    parser = argparse.ArgumentParser(
        description="将多个 forward_*.tsv 按 id 对齐，合并为宽表。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("dir", help="包含 forward_*.tsv 的目录")
    parser.add_argument(
        "--output", "-o", default=None,
        help="输出路径（默认：<dir>/aligned.tsv）",
    )
    parser.add_argument(
        "--pattern", "-p", default=["forward_*.tsv"], nargs="+",
        help="文件匹配模式，可指定多个（默认：forward_*.tsv）",
    )
    parser.add_argument(
        "--sort-id", action="store_true",
        help="按 id 数值升序排序输出",
    )
    args = parser.parse_args()

    src_dir = Path(args.dir)
    if not src_dir.is_dir():
        log.error("目录不存在：%s", src_dir)
        sys.exit(1)

    output_path = Path(args.output) if args.output else src_dir / "aligned.tsv"
    seen: set[Path] = set()
    tsv_files: list[Path] = []
    for pat in args.pattern:
        for f in sorted(src_dir.glob(pat)):
            if f.resolve() != output_path.resolve() and f.resolve() not in seen:
                tsv_files.append(f)
                seen.add(f.resolve())

    if not tsv_files:
        log.error("未找到匹配文件：%s / %s", src_dir, args.pattern)
        sys.exit(1)

    log.info("找到文件数：%d", len(tsv_files))

    # 按顺序记录 domain 及其需要输出的列（除 SHARED_COLS 外的所有列）
    domains: list[str] = []
    domain_cols: dict[str, list[str]] = {}

    # id -> {"id": ..., "wiki_name": ..., "domain__col": ...}
    merged: dict[str, dict] = {}

    for tsv_path in tsv_files:
        domain = domain_from_filename(tsv_path.name)
        with tsv_path.open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            file_feature_cols = [c for c in (reader.fieldnames or []) if c not in SHARED_COLS]
            domains.append(domain)
            domain_cols[domain] = file_feature_cols

            row_count = 0
            for row in reader:
                rid = row["id"]
                if rid not in merged:
                    merged[rid] = {"id": rid, "wiki_name": row.get("wiki_name", "")}
                for col in file_feature_cols:
                    merged[rid][f"{domain}__{col}"] = row.get(col, "")
                row_count += 1

        log.info("  %-45s  domain=%-35s  %d 行", tsv_path.name, domain, row_count)

    # 构建输出列顺序：id, wiki_name, 然后每个 domain 的特征列
    fieldnames: list[str] = list(SHARED_COLS)
    for domain in domains:
        for col in domain_cols[domain]:
            fieldnames.append(f"{domain}__{col}")

    rows = list(merged.values())
    if args.sort_id:
        def _id_key(r: dict) -> int:
            try:
                return int(r.get("id", 0))
            except (ValueError, TypeError):
                return 0
        rows.sort(key=_id_key)
        log.info("已按 id 数值升序排序")

    with output_path.open("w", newline="", encoding="utf-8") as out_fh:
        writer = csv.DictWriter(
            out_fh,
            fieldnames=fieldnames,
            delimiter="\t",
            extrasaction="ignore",
            restval="",
        )
        writer.writeheader()
        writer.writerows(rows)

    log.info(
        "对齐完成：%d 个 ID，%d 列（含 %d 个域名）→ %s",
        len(rows), len(fieldnames), len(domains), output_path,
    )


if __name__ == "__main__":
    main()
