"""
评估 infer_packets.py 的推测准确性（仅评估 H2 header / data 两类）。

以 classify_packets.py 的解密输出为 ground truth，
对比 infer_packets.py 的推测输出，按 frame_no 对齐，
报告每条流及整体的 precision / recall / F1 / accuracy。

用法：
    python3 eval_infer.py <gt_dir> <pred_dir>
    python3 eval_infer.py fetch_output/1001-wiki-xxx/capture_chrome_flows \
                          fetch_output/1001-wiki-xxx/capture_chrome_inferred
"""

import csv
import sys
import argparse
from pathlib import Path
from collections import defaultdict

TARGET_CATS = {"header", "data"}

# ---------------------------------------------------------------------------
# 读取流文件
# ---------------------------------------------------------------------------

def read_flow_file(fpath: Path) -> dict[str, str]:
    """读取一条流的 TSV 文件，返回 {frame_no: category}（跳过 # 注释行）。"""
    result = {}
    with fpath.open(encoding="utf-8") as fh:
        lines = (line for line in fh if not line.startswith("#"))
        reader = csv.DictReader(lines, delimiter="\t")
        for row in reader:
            fn = row.get("frame_no", "").strip()
            cat = row.get("category", "").strip()
            if fn:
                result[fn] = cat
    return result

# ---------------------------------------------------------------------------
# 单流评估
# ---------------------------------------------------------------------------

def eval_flow(gt: dict[str, str], pred: dict[str, str]) -> list[tuple]:
    """
    对 GT 中 category ∈ {header, data} 的包逐一比对，
    返回 [(frame_no, gt_cat, pred_cat), ...]。
    """
    records = []
    for frame_no, gt_cat in gt.items():
        if gt_cat not in TARGET_CATS:
            continue
        pred_cat = pred.get(frame_no, "missing")
        records.append((frame_no, gt_cat, pred_cat))
    return records

# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------

def compute_metrics(records: list[tuple]) -> dict:
    """
    输入 [(frame_no, gt, pred), ...]
    返回包含 per-class 和 overall 指标的字典。
    """
    tp = defaultdict(int)
    fp = defaultdict(int)
    fn = defaultdict(int)

    for _, gt, pred in records:
        for c in TARGET_CATS:
            if gt == c and pred == c:
                tp[c] += 1
            elif gt != c and pred == c:
                fp[c] += 1
            elif gt == c and pred != c:
                fn[c] += 1

    metrics = {}
    for c in TARGET_CATS:
        p  = tp[c] / (tp[c] + fp[c]) if (tp[c] + fp[c]) > 0 else 0.0
        r  = tp[c] / (tp[c] + fn[c]) if (tp[c] + fn[c]) > 0 else 0.0
        f1 = 2 * p * r / (p + r)     if (p + r) > 0           else 0.0
        metrics[c] = {
            "tp": tp[c], "fp": fp[c], "fn": fn[c],
            "precision": p, "recall": r, "f1": f1,
        }

    total   = len(records)
    correct = sum(1 for _, gt, pred in records if gt == pred)
    metrics["overall"] = {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total > 0 else 0.0,
    }
    return metrics

# ---------------------------------------------------------------------------
# 打印
# ---------------------------------------------------------------------------

SEP = "-" * 90

def print_flow_row(name: str, m: dict):
    ov = m["overall"]
    h  = m["header"]
    d  = m["data"]
    print(
        f"  {name:<35s}"
        f"  acc={ov['accuracy']:5.1%} ({ov['correct']:3d}/{ov['total']:3d})"
        f"  header P={h['precision']:.2f} R={h['recall']:.2f} F1={h['f1']:.2f}"
        f"  data  P={d['precision']:.2f} R={d['recall']:.2f} F1={d['f1']:.2f}"
    )

def print_overall(all_records: list[tuple]):
    m = compute_metrics(all_records)
    ov = m["overall"]
    h  = m["header"]
    d  = m["data"]

    print(SEP)
    print("整体统计（所有流合并）")
    print(SEP)
    print(f"  评估包数  : {ov['total']}  （GT category ∈ {{header, data}}）")
    print(f"  分类正确  : {ov['correct']}")
    print(f"  整体准确率: {ov['accuracy']:.1%}")
    print()

    # Confusion matrix
    header_as_header = sum(1 for _, g, p in all_records if g == "header" and p == "header")
    header_as_data   = sum(1 for _, g, p in all_records if g == "header" and p == "data")
    header_as_other  = sum(1 for _, g, p in all_records if g == "header" and p not in TARGET_CATS)
    data_as_header   = sum(1 for _, g, p in all_records if g == "data"   and p == "header")
    data_as_data     = sum(1 for _, g, p in all_records if g == "data"   and p == "data")
    data_as_other    = sum(1 for _, g, p in all_records if g == "data"   and p not in TARGET_CATS)

    print("  混淆矩阵（行=GT，列=预测）")
    print(f"  {'':10s}  {'header':>8s}  {'data':>8s}  {'other':>8s}")
    print(f"  {'header':10s}  {header_as_header:>8d}  {header_as_data:>8d}  {header_as_other:>8d}")
    print(f"  {'data':10s}  {data_as_header:>8d}  {data_as_data:>8d}  {data_as_other:>8d}")
    print()

    print(f"  {'类别':8s}  {'TP':>5s}  {'FP':>5s}  {'FN':>5s}  {'Precision':>10s}  {'Recall':>8s}  {'F1':>6s}")
    for c in ["header", "data"]:
        mm = m[c]
        print(f"  {c:8s}  {mm['tp']:>5d}  {mm['fp']:>5d}  {mm['fn']:>5d}"
              f"  {mm['precision']:>10.3f}  {mm['recall']:>8.3f}  {mm['f1']:>6.3f}")

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="评估 infer_packets.py 的 header/data 推测准确性。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("gt_dir",   help="classify_packets.py 的输出目录（ground truth）")
    parser.add_argument("pred_dir", help="infer_packets.py 的输出目录（推测结果）")
    args = parser.parse_args()

    gt_dir   = Path(args.gt_dir)
    pred_dir = Path(args.pred_dir)

    if not gt_dir.is_dir():
        print(f"错误：gt_dir 不存在：{gt_dir}", file=sys.stderr); sys.exit(1)
    if not pred_dir.is_dir():
        print(f"错误：pred_dir 不存在：{pred_dir}", file=sys.stderr); sys.exit(1)

    gt_files   = {f.name for f in gt_dir.glob("*.tsv")}
    pred_files = {f.name for f in pred_dir.glob("*.tsv")}
    common     = sorted(gt_files & pred_files)
    only_gt    = gt_files - pred_files
    only_pred  = pred_files - gt_files

    if only_gt:
        print(f"警告：以下文件只在 GT 中存在，跳过：{sorted(only_gt)}", file=sys.stderr)
    if only_pred:
        print(f"警告：以下文件只在 pred 中存在，跳过：{sorted(only_pred)}", file=sys.stderr)

    print(SEP)
    print(f"GT 目录  : {gt_dir}")
    print(f"Pred 目录: {pred_dir}")
    print(f"共同流数 : {len(common)}")
    print(SEP)

    all_records: list[tuple] = []

    for fname in common:
        gt   = read_flow_file(gt_dir   / fname)
        pred = read_flow_file(pred_dir / fname)
        records = eval_flow(gt, pred)

        if not records:
            # 该流里没有 header/data 包，跳过
            continue

        m = compute_metrics(records)
        print_flow_row(fname, m)
        all_records.extend(records)

    if not all_records:
        print("没有可评估的 header/data 包。", file=sys.stderr)
        sys.exit(1)

    print()
    print_overall(all_records)


if __name__ == "__main__":
    main()
