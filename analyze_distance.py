"""
给定 TSV 文件，指定标签列、数值特征列、序列特征列，
统计分析类内（intra）和类间（inter，重心距离）特征距离。

距离类型：
  数值特征    欧氏距离（默认先 z-score 归一化）
  序列特征
    sorted-L2  升序对齐至固定百分位点后的 L2 距离
    EMD        Earth Mover's Distance（Wasserstein-1）

类内（intra）：同标签内所有样本两两距离统计
类间（inter） ：各标签重心之间的距离统计
  - 数值 / sorted-L2 → numpy 向量化全量计算
  - EMD              → 仅对 sorted-L2 最近的 Top-K 对计算（--emd-top-k）

输出文件：
  intra_dist.tsv     每标签类内距离统计（每类一行）
  inter_stats.tsv    类间重心距离整体统计（各指标汇总行）
  inter_top_sim.tsv  最易混淆的 Top-K 标签对（类间距离最小）

用法：
  python3 analyze_distance.py <tsv> --label id \\
      --num-cols handshake control header data ack tls_segment \\
      --seq-cols header_lens

  python3 analyze_distance.py forward_zh.wikipedia.org.tsv \\
      --label id \\
      --num-cols handshake control header data \\
      --seq-cols header_lens \\
      --output-dir ./dist_out/ --top-k 50 --emd-top-k 500
"""

import csv
import sys
import logging
import argparse
import numpy as np
from pathlib import Path
from itertools import combinations
from collections import defaultdict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

try:
    from scipy.stats import wasserstein_distance
    from scipy.spatial.distance import pdist
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    log.warning("scipy 未安装，EMD 距离不可用（pip install scipy）")

# 序列转固定长度时使用的百分位点数量
N_QUANTILES = 50


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def load_tsv(path: Path, label_col: str,
             num_cols: list[str], seq_cols: list[str]
             ) -> dict[str, list[dict]]:
    """
    读取 TSV，按标签分组。
    每个样本为 dict：
      {"num": np.array, "seq": {col: list[float]}}
    """
    groups: dict[str, list[dict]] = defaultdict(list)

    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            label = row.get(label_col, "").strip()
            if not label:
                continue

            # 数值特征
            num_vec = []
            for col in num_cols:
                v = row.get(col, "").strip()
                try:
                    num_vec.append(float(v))
                except ValueError:
                    num_vec.append(float("nan"))

            # 序列特征
            seq_dict: dict[str, list[float]] = {}
            for col in seq_cols:
                raw = row.get(col, "").strip()
                if not raw:
                    seq_dict[col] = []
                    continue
                vals = []
                for x in raw.split(","):
                    x = x.strip()
                    if x:
                        try:
                            vals.append(float(x))
                        except ValueError:
                            pass
                seq_dict[col] = vals

            groups[label].append({
                "num": np.array(num_vec, dtype=float),
                "seq": seq_dict,
            })

    return groups


# ---------------------------------------------------------------------------
# 归一化：z-score（按列，跨所有样本）
# ---------------------------------------------------------------------------

def zscore_params(groups: dict, num_cols: list[str]
                  ) -> tuple[np.ndarray, np.ndarray]:
    """计算全局均值和标准差，用于 z-score 归一化。"""
    all_vecs = [s["num"] for samples in groups.values() for s in samples]
    if not all_vecs:
        n = len(num_cols)
        return np.zeros(n), np.ones(n)
    mat = np.vstack(all_vecs)
    mean = np.nanmean(mat, axis=0)
    std  = np.nanstd(mat, axis=0)
    std[std == 0] = 1.0   # 避免除零
    return mean, std


def normalize(vec: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    """使用全局均值和标准差对数值向量做 z-score 标准化。"""
    return (vec - mean) / std


# ---------------------------------------------------------------------------
# 序列 → 固定长度百分位向量
# ---------------------------------------------------------------------------

def seq_to_quantiles(seq: list[float], n_q: int = N_QUANTILES) -> np.ndarray | None:
    """将序列转为 n_q 个等距百分位值；序列为空返回 None。"""
    if not seq:
        return None
    a = np.sort(seq)
    q = np.linspace(0, 100, n_q)
    return np.percentile(a, q)


# ---------------------------------------------------------------------------
# 距离函数
# ---------------------------------------------------------------------------

def sorted_l2(a: list[float], b: list[float]) -> float | None:
    """
    sorted-L2：两序列各升序排列后对齐到 N_QUANTILES 百分位点，计算 L2 距离。
    任一为空则返回 None。
    """
    qa = seq_to_quantiles(a)
    qb = seq_to_quantiles(b)
    if qa is None or qb is None:
        return None
    return float(np.linalg.norm(qa - qb))


def emd(a: list[float], b: list[float]) -> float | None:
    """Earth Mover's Distance（Wasserstein-1）。任一为空或无 scipy 则返回 None。"""
    if not HAS_SCIPY or not a or not b:
        return None
    return float(wasserstein_distance(a, b))


def numeric_dist(va: np.ndarray, vb: np.ndarray) -> float | None:
    """欧氏距离（忽略 NaN 位置）。"""
    diff = va - vb
    mask = ~np.isnan(diff)
    if mask.sum() == 0:
        return None
    return float(np.sqrt(np.sum(diff[mask] ** 2)))


# ---------------------------------------------------------------------------
# 重心计算
# ---------------------------------------------------------------------------

def compute_centroid(samples: list[dict], mean: np.ndarray, std: np.ndarray,
                     seq_cols: list[str]
                     ) -> dict:
    """
    计算一个类的重心：
      num_centroid : 各样本归一化数值向量的均值
      seq_centroid : {col: 合并所有样本的值后排序的列表}（用于 EMD）
      seq_q_centroid: {col: 合并值的 N_QUANTILES 百分位向量}（用于 sorted-L2）
    """
    # 数值重心
    norm_vecs = [normalize(s["num"], mean, std) for s in samples]
    num_c = np.nanmean(np.vstack(norm_vecs), axis=0)

    # 序列重心
    seq_c: dict[str, list[float]] = {}
    seq_q_c: dict[str, np.ndarray | None] = {}
    for col in seq_cols:
        pooled = []
        for s in samples:
            pooled.extend(s["seq"].get(col, []))
        pooled.sort()
        seq_c[col] = pooled
        seq_q_c[col] = seq_to_quantiles(pooled)

    return {
        "num":   num_c,
        "seq":   seq_c,
        "seq_q": seq_q_c,
    }


# ---------------------------------------------------------------------------
# 统计辅助
# ---------------------------------------------------------------------------

def dist_stats(vals: list[float]) -> dict:
    """计算一组有效距离的数量、均值、标准差和分位数。"""
    if not vals:
        return {"count": 0, "mean": "", "median": "", "std": "", "min": "", "max": ""}
    a = np.array(vals)
    return {
        "count":  len(a),
        "mean":   round(float(np.mean(a)), 4),
        "median": round(float(np.median(a)), 4),
        "std":    round(float(np.std(a)), 4),
        "min":    round(float(np.min(a)), 4),
        "max":    round(float(np.max(a)), 4),
    }


# ---------------------------------------------------------------------------
# 类内距离
# ---------------------------------------------------------------------------

def intra_distances(groups: dict, mean: np.ndarray, std: np.ndarray,
                    num_cols: list[str], seq_cols: list[str]
                    ) -> list[dict]:
    """计算每个标签内部所有样本对的距离统计。"""
    rows = []
    for label, samples in groups.items():
        n = len(samples)
        pairs = list(combinations(range(n), 2))

        sl2_dists: dict[str, list[float]] = {col: [] for col in seq_cols}
        emd_dists: dict[str, list[float]] = {col: [] for col in seq_cols}
        raw_num_dists: list[float] = []

        for i, j in pairs:
            sa, sb = samples[i], samples[j]
            va = normalize(sa["num"], mean, std)
            vb = normalize(sb["num"], mean, std)
            d = numeric_dist(va, vb)
            if d is not None:
                raw_num_dists.append(d)

            for col in seq_cols:
                a_seq = sa["seq"].get(col, [])
                b_seq = sb["seq"].get(col, [])
                sl2 = sorted_l2(a_seq, b_seq)
                if sl2 is not None:
                    sl2_dists[col].append(sl2)
                em = emd(a_seq, b_seq)
                if em is not None:
                    emd_dists[col].append(em)

        row: dict = {
            "label":        label,
            "sample_count": n,
            "n_pairs":      len(pairs),
        }
        # 数值距离统计
        for k, v in dist_stats(raw_num_dists).items():
            row[f"num_{k}"] = v if k == "count" else v

        # 序列距离统计
        for col in seq_cols:
            safe = col.replace("-", "_")
            for k, v in dist_stats(sl2_dists[col]).items():
                row[f"{safe}_sl2_{k}"] = v
            em_list = emd_dists.get(col, [])
            for k, v in dist_stats(em_list if isinstance(em_list, list) else []).items():
                row[f"{safe}_emd_{k}"] = v

        rows.append(row)

    return rows


# ---------------------------------------------------------------------------
# 类间距离（重心）
# ---------------------------------------------------------------------------

def inter_distances(centroids: dict[str, dict],
                    seq_cols: list[str],
                    top_k: int, emd_top_k: int
                    ) -> tuple[list[dict], list[dict]]:
    """
    返回 (stats_rows, top_sim_rows)
    stats_rows   : 各指标整体统计（1行/指标）
    top_sim_rows : top_k 最相似标签对
    """
    labels = list(centroids.keys())
    n = len(labels)
    if n < 2:
        return [], []

    # ── 数值重心矩阵 → 全量 pdist ──────────────────────────────────────
    num_mat = np.vstack([centroids[label]["num"] for label in labels])
    # 含 NaN 的列用 0 替代再算距离（简单处理）
    num_mat_clean = np.nan_to_num(num_mat, nan=0.0)
    num_flat = pdist(num_mat_clean, metric="euclidean")   # 三角形展开，长度 n*(n-1)/2

    # ── 序列重心 sorted-L2 全量计算 ────────────────────────────────────
    sl2_flats: dict[str, np.ndarray] = {}
    for col in seq_cols:
        q_mat = []
        for label in labels:
            qv = centroids[label]["seq_q"].get(col)
            q_mat.append(qv if qv is not None else np.zeros(N_QUANTILES))
        q_arr = np.vstack(q_mat)
        sl2_flats[col] = pdist(q_arr, metric="euclidean")

    # ── 确定 sorted-L2 最近的 top emd_top_k 对 → 计算 EMD ────────────
    # 用第一个 seq_col 的 sorted-L2 排序（若有）
    emd_flats: dict[str, np.ndarray] = {}
    if HAS_SCIPY and seq_cols and emd_top_k > 0:
        ref_col  = seq_cols[0]
        ref_flat = sl2_flats[ref_col]
        top_idx  = np.argsort(ref_flat)[:emd_top_k]   # 最小距离的索引

        for col in seq_cols:
            emd_arr = np.full(len(ref_flat), np.nan)
            # 三角形索引 → (i, j)
            # 构建完整索引对
            idx = 0
            pair_map: dict[int, tuple[int, int]] = {}
            for ii in range(n):
                for jj in range(ii + 1, n):
                    pair_map[idx] = (ii, jj)
                    idx += 1
            for flat_i in top_idx:
                ii, jj = pair_map[flat_i]
                a_seq = centroids[labels[ii]]["seq"].get(col, [])
                b_seq = centroids[labels[jj]]["seq"].get(col, [])
                em = emd(a_seq, b_seq)
                if em is not None:
                    emd_arr[flat_i] = em
            emd_flats[col] = emd_arr

    # ── Top-K 最相似对（按 num + 第一 seq sorted-L2 之和排序）──────────
    if seq_cols:
        sort_key = num_flat + sl2_flats[seq_cols[0]]
    else:
        sort_key = num_flat
    top_pair_idx = np.argsort(sort_key)[:top_k]

    # 三角形展开索引 → (i, j)
    pair_list: list[tuple[int, int]] = []
    for ii in range(n):
        for jj in range(ii + 1, n):
            pair_list.append((ii, jj))

    top_sim_rows: list[dict] = []
    for flat_i in top_pair_idx:
        ii, jj = pair_list[flat_i]
        row: dict = {
            "label_a":    labels[ii],
            "label_b":    labels[jj],
            "num_dist":   round(float(num_flat[flat_i]), 4),
        }
        for col in seq_cols:
            safe = col.replace("-", "_")
            row[f"{safe}_sl2_dist"] = round(float(sl2_flats[col][flat_i]), 4)
            if col in emd_flats:
                v = emd_flats[col][flat_i]
                row[f"{safe}_emd_dist"] = round(float(v), 4) if not np.isnan(v) else ""
        top_sim_rows.append(row)

    # ── 整体统计 ──────────────────────────────────────────────────────
    stats_rows: list[dict] = []

    def _stat_row(metric: str, arr: np.ndarray) -> dict:
        valid = arr[~np.isnan(arr)]
        if len(valid) == 0:
            return {"metric": metric, "n_pairs": 0}
        return {
            "metric":   metric,
            "n_pairs":  len(valid),
            "mean":     round(float(np.mean(valid)), 4),
            "median":   round(float(np.median(valid)), 4),
            "std":      round(float(np.std(valid)), 4),
            "min":      round(float(np.min(valid)), 4),
            "p5":       round(float(np.percentile(valid, 5)), 4),
            "p25":      round(float(np.percentile(valid, 25)), 4),
            "p75":      round(float(np.percentile(valid, 75)), 4),
            "p95":      round(float(np.percentile(valid, 95)), 4),
            "max":      round(float(np.max(valid)), 4),
        }

    stats_rows.append(_stat_row("numeric_euclid", num_flat))
    for col in seq_cols:
        safe = col.replace("-", "_")
        stats_rows.append(_stat_row(f"{safe}_sorted_l2", sl2_flats[col]))
        if col in emd_flats:
            stats_rows.append(_stat_row(f"{safe}_emd", emd_flats[col]))

    return stats_rows, top_sim_rows


# ---------------------------------------------------------------------------
# 写 TSV
# ---------------------------------------------------------------------------

def write_tsv(path: Path, rows: list[dict]):
    """以稳定列顺序写出分析结果 TSV。"""
    if not rows:
        log.warning("无数据，跳过写出：%s", path)
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, delimiter="\t",
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    log.info("已写出 %d 行 → %s", len(rows), path)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    """组织加载、标准化、类内/类间距离计算和结果输出。"""
    parser = argparse.ArgumentParser(
        description="统计分析 TSV 中各标签的类内/类间特征距离。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tsv", help="输入 TSV 文件路径")
    parser.add_argument("--label", required=True, metavar="COL",
                        help="标签列名，如 id")
    parser.add_argument("--num-cols", nargs="*", default=[], metavar="COL",
                        help="数值特征列名（可多个）")
    parser.add_argument("--seq-cols", nargs="*", default=[], metavar="COL",
                        help="序列特征列名（逗号分隔数字，可多个）")
    parser.add_argument("--no-normalize", action="store_true",
                        help="数值特征不做 z-score 归一化")
    parser.add_argument("--output-dir", "-o", default=None, metavar="DIR",
                        help="输出目录（默认：TSV 同级目录）")
    parser.add_argument("--top-k", type=int, default=100, metavar="K",
                        help="inter_top_sim.tsv 中输出最相似的 K 对（默认 100）")
    parser.add_argument("--emd-top-k", type=int, default=1000, metavar="K",
                        help="类间 EMD 仅对 sorted-L2 最近的 K 对计算（默认 1000）")
    args = parser.parse_args()

    if not args.num_cols and not args.seq_cols:
        parser.error("至少指定一个 --num-cols 或 --seq-cols")

    tsv_path = Path(args.tsv)
    if not tsv_path.exists():
        log.error("文件不存在：%s", tsv_path)
        sys.exit(1)

    out_dir = Path(args.output_dir) if args.output_dir else tsv_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = tsv_path.stem

    # ── 加载 ──────────────────────────────────────────────────────────
    log.info("加载数据：%s", tsv_path)
    groups = load_tsv(tsv_path, args.label, args.num_cols, args.seq_cols)
    n_classes = len(groups)
    n_samples = sum(len(v) for v in groups.values())
    log.info("标签数：%d，样本总数：%d", n_classes, n_samples)

    # ── z-score 参数 ─────────────────────────────────────────────────
    if args.num_cols and not args.no_normalize:
        mean, std = zscore_params(groups, args.num_cols)
        log.info("数值特征 z-score 归一化完成")
    else:
        n = len(args.num_cols)
        mean, std = np.zeros(n), np.ones(n)

    # ── 类内距离 ──────────────────────────────────────────────────────
    log.info("计算类内距离...")
    intra_rows = intra_distances(groups, mean, std, args.num_cols, args.seq_cols)
    # 按 label 数值排序
    intra_rows.sort(key=lambda r: (int(r["label"]) if str(r["label"]).isdigit() else r["label"]))
    write_tsv(out_dir / f"{stem}_intra_dist.tsv", intra_rows)

    # ── 重心 ──────────────────────────────────────────────────────────
    log.info("计算类重心...")
    centroids = {
        label: compute_centroid(samples, mean, std, args.seq_cols)
        for label, samples in groups.items()
    }

    # ── 类间距离 ──────────────────────────────────────────────────────
    log.info("计算类间距离（%d 类，约 %d 对）...",
             n_classes, n_classes * (n_classes - 1) // 2)
    stats_rows, top_sim_rows = inter_distances(
        centroids, args.seq_cols, args.top_k, args.emd_top_k
    )
    write_tsv(out_dir / f"{stem}_inter_stats.tsv", stats_rows)
    write_tsv(out_dir / f"{stem}_inter_top_sim.tsv", top_sim_rows)

    # ── 控制台汇总 ───────────────────────────────────────────────────
    print()
    print("=" * 60)
    # 类内平均距离（有 pairs 的类）
    valid_intra = [r for r in intra_rows if r["n_pairs"] > 0]
    print(f"类内（intra）有效类数：{len(valid_intra)} / {n_classes}")
    def _fmt(label: str, vals: list):
        """打印一行统计：均值 / 中位数 / p5 / p95（各类均值的分布）。"""
        if not vals:
            return
        a = np.array(vals, dtype=float)
        print(f"  {label:40s}"
              f"avg={np.mean(a):.4f}  "
              f"median={np.median(a):.4f}  "
              f"p5={np.percentile(a, 5):.4f}  "
              f"p95={np.percentile(a, 95):.4f}")

    if valid_intra and args.num_cols:
        means = [r["num_mean"] for r in valid_intra if r.get("num_mean") != ""]
        _fmt("数值距离（各类内均值）", means)

    if valid_intra and args.seq_cols:
        for col in args.seq_cols:
            safe = col.replace("-", "_")
            sl2 = [r[f"{safe}_sl2_mean"] for r in valid_intra
                   if r.get(f"{safe}_sl2_mean") != ""]
            _fmt(f"{col} sorted-L2（各类内均值）", sl2)
            emd_vals = [r[f"{safe}_emd_mean"] for r in valid_intra
                        if r.get(f"{safe}_emd_mean") != ""]
            _fmt(f"{col} EMD（各类内均值）", emd_vals)
    # 类间整体
    print("\n类间（inter）统计：")
    for r in stats_rows:
        if not r.get("n_pairs"):
            continue
        print(f"  {r['metric']:35s}  mean={r.get('mean',''):<9}  "
              f"median={r.get('median',''):<9}  p5={r.get('p5','')}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
