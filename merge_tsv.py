"""
将目录下所有 TSV 文件合并为一个（结构相同，共用同一表头）。

用法：
  python3 merge_tsv.py <dir>
  python3 merge_tsv.py ./split_out/
  python3 merge_tsv.py ./split_out/ --output merged.tsv
  python3 merge_tsv.py ./split_out/ --pattern "forward_*.tsv"
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


def main():
    parser = argparse.ArgumentParser(
        description="将目录下所有 TSV 文件（结构相同）合并为一个。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("dir", help="包含 TSV 文件的目录")
    parser.add_argument(
        "--output", "-o", default=None,
        help="输出路径（默认：<dir>/merged.tsv）",
    )
    parser.add_argument(
        "--pattern", "-p", default="*.tsv",
        help="文件匹配模式（默认：*.tsv）",
    )
    parser.add_argument(
        "--sort-id", action="store_true",
        help="合并后按 id 列排序（数值升序）",
    )
    args = parser.parse_args()

    src_dir = Path(args.dir)
    if not src_dir.is_dir():
        log.error("目录不存在：%s", src_dir)
        sys.exit(1)

    output_path = Path(args.output) if args.output else src_dir / "merged.tsv"

    tsv_files = sorted(src_dir.glob(args.pattern))
    # 排除输出文件自身（防止覆盖/循环）
    tsv_files = [f for f in tsv_files if f.resolve() != output_path.resolve()]

    if not tsv_files:
        log.error("未找到匹配文件：%s / %s", src_dir, args.pattern)
        sys.exit(1)

    log.info("找到文件数 : %d", len(tsv_files))

    fieldnames = None
    all_rows: list[dict] = []

    for tsv_path in tsv_files:
        with tsv_path.open(encoding="utf-8") as in_fh:
            reader = csv.DictReader(in_fh, delimiter="\t")
            if fieldnames is None:
                fieldnames = reader.fieldnames
            rows = list(reader)
            all_rows.extend(rows)
            log.info("  %s  (%d 行)", tsv_path.name, len(rows))

    if args.sort_id:
        def _id_key(row: dict):
            try:
                return int(row.get("id", 0))
            except (ValueError, TypeError):
                return 0
        all_rows.sort(key=_id_key)
        log.info("已按 id 排序")

    with output_path.open("w", newline="", encoding="utf-8") as out_fh:
        writer = csv.DictWriter(
            out_fh, fieldnames=fieldnames,
            delimiter="\t", extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(all_rows)

    log.info("合并完成：共 %d 行 → %s", len(all_rows), output_path)


if __name__ == "__main__":
    main()
