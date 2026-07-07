"""
给定一个 TSV 文件和若干列名，将这些列组合成"指纹向量"，
统计有多少行发生冲突（指纹完全相同），按冲突规模从大到小排列输出。

输出格式：
  conflict_size   该指纹出现的次数（1 = 唯一，无冲突；>1 = 冲突）
  group_count     出现该次数的指纹组数
  row_count       该档次贡献的总行数（= conflict_size × group_count）
  row_pct         占全部样本行的百分比

验证：所有 row_count 之和 = 全部样本行数。

用法：
  python3 fingerprint_conflict.py <tsv> --cols COL1 COL2 ...
  python3 fingerprint_conflict.py multi-flow.tsv --cols flow_count sni_count
  python3 fingerprint_conflict.py multi-flow.tsv --cols upstream_bytes downstream_bytes --no-header-check
  python3 fingerprint_conflict.py multi-flow.tsv --cols flow_count sni_count --show-examples 3
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


def main():
    parser = argparse.ArgumentParser(
        description=(
            "将指定列组合为指纹向量，统计行冲突分布。\n\n"
            "输出列说明：\n"
            "  conflict_size  该指纹出现次数（1=唯一，>1=冲突）\n"
            "  group_count    出现该次数的指纹组数\n"
            "  row_count      贡献总行数（= conflict_size × group_count）\n"
            "  row_pct        占全部样本行的百分比\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tsv", help="输入 TSV 文件路径")
    parser.add_argument(
        "--cols", "-c", nargs="+", required=True, metavar="COL",
        help="作为指纹的列名（可多个，空格分隔）",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="输出 TSV 路径（默认：打印到 stdout）",
    )
    parser.add_argument(
        "--show-examples", type=int, default=0, metavar="N",
        help="对每个冲突规模，额外打印最多 N 条冲突样本的 id / wiki_name（若列存在）",
    )
    parser.add_argument(
        "--only-conflicts", action="store_true",
        help="只输出 conflict_size > 1 的行（隐藏唯一指纹行）",
    )
    args = parser.parse_args()

    tsv_path = Path(args.tsv)
    if not tsv_path.exists():
        log.error("文件不存在：%s", tsv_path)
        sys.exit(1)

    # ── 读取 TSV ──────────────────────────────────────────────────────
    with tsv_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        if reader.fieldnames is None:
            log.error("TSV 文件为空或无表头")
            sys.exit(1)

        # 校验列名
        missing = [c for c in args.cols if c not in reader.fieldnames]
        if missing:
            log.error("以下列不存在于 TSV 中：%s", missing)
            log.error("可用列：%s", list(reader.fieldnames))
            sys.exit(1)

        has_id   = "id"        in (reader.fieldnames or [])
        has_name = "wiki_name" in (reader.fieldnames or [])

        # 构建指纹 → [行列表]
        fingerprint_rows: dict[tuple, list[dict]] = {}
        total_rows = 0
        for row in reader:
            total_rows += 1
            key = tuple(row.get(c, "") for c in args.cols)
            fingerprint_rows.setdefault(key, []).append(row)

    log.info("总行数       : %d", total_rows)
    log.info("唯一指纹数   : %d", len(fingerprint_rows))
    log.info("指纹列       : %s", args.cols)

    # ── 统计冲突分布 ──────────────────────────────────────────────────
    # size_counter: conflict_size → group_count
    size_counter: Counter[int] = Counter(len(v) for v in fingerprint_rows.values())

    # 按 conflict_size 从大到小排序
    sorted_sizes = sorted(size_counter.keys(), reverse=True)

    # 验证
    check = sum(sz * cnt for sz, cnt in size_counter.items())
    if check != total_rows:
        log.warning("校验失败：sum(size×count)=%d ≠ total_rows=%d", check, total_rows)
    else:
        log.info("校验通过：sum(conflict_size × group_count) = %d", total_rows)

    # ── 构建示例索引（可选）──────────────────────────────────────────
    # size → list of (fingerprint_key, rows_list)
    examples_by_size: dict[int, list] = {}
    if args.show_examples > 0:
        for key, rows in fingerprint_rows.items():
            sz = len(rows)
            examples_by_size.setdefault(sz, []).append((key, rows))

    # ── 输出 ─────────────────────────────────────────────────────────
    out_fieldnames = ["conflict_size", "group_count", "row_count", "row_pct"]

    def write_rows(writer_or_none):
        for sz in sorted_sizes:
            if args.only_conflicts and sz == 1:
                continue
            cnt = size_counter[sz]
            row_count = sz * cnt
            row_pct   = row_count / total_rows * 100 if total_rows else 0
            if writer_or_none:
                writer_or_none.writerow({
                    "conflict_size": sz,
                    "group_count":   cnt,
                    "row_count":     row_count,
                    "row_pct":       f"{row_pct:.2f}%",
                })
            else:
                print(f"  conflict_size={sz:>8}  group_count={cnt:>8}  "
                      f"row_count={row_count:>8}  ({row_pct:.2f}%)")

            # 打印示例
            if args.show_examples > 0 and sz > 1:
                examples = examples_by_size.get(sz, [])[:args.show_examples]
                for fp_key, fp_rows in examples:
                    fp_str = "  |  ".join(
                        f"{col}={val}" for col, val in zip(args.cols, fp_key)
                    )
                    ids = []
                    for r in fp_rows[:5]:
                        tag = ""
                        if has_id:   tag += r.get("id", "")
                        if has_name: tag += "-" + r.get("wiki_name", "")
                        if tag:
                            ids.append(tag)
                    ids_str = ", ".join(ids)
                    if len(fp_rows) > 5:
                        ids_str += f" ... (+{len(fp_rows)-5})"
                    print(f"      fingerprint: {fp_str}")
                    print(f"      samples    : {ids_str}")

    if args.output:
        out_path = Path(args.output)
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=out_fieldnames, delimiter="\t")
            writer.writeheader()
            write_rows(writer)
        log.info("结果已写出 → %s", out_path)
    else:
        print()
        print(f"{'conflict_size':>14}  {'group_count':>12}  "
              f"{'row_count':>10}  {'row_pct':>8}")
        print("-" * 56)
        write_rows(None)
        print("-" * 56)
        print(f"{'合计':>14}  {'':>12}  {total_rows:>10}  {'100.00%':>8}")
        print()


if __name__ == "__main__":
    main()
