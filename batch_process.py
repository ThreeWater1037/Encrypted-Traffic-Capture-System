"""
批量处理 fetch_output 下所有子文件夹，对每个 capture_XX.pcap 依次运行：
  1. extract_features.py  → capture_XX.tsv
  2. classify_packets.py  → capture_XX_flows/   （需要 tls_keys_XX.log）
  3. infer_packets.py     → capture_XX_inferred/

XX 由 pcap 文件名自动推断（chrome / firefox / safari 等）。

用法：
    python3 batch_process.py <fetch_output_dir>
    python3 batch_process.py fetch_output --skip-existing
    python3 batch_process.py fetch_output --only extract classify
    python3 batch_process.py fetch_output --with-coframe --jobs 4
"""

import sys
import logging
import argparse
import subprocess
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

SCRIPTS = Path(__file__).parent

def tsv_has_data(path: Path) -> bool:
    """TSV 文件存在且含有至少一条数据行（跳过表头）。"""
    if not path.exists():
        return False
    with path.open(encoding="utf-8") as fh:
        next(fh, None)          # 跳过表头
        return any(True for _ in fh)  # 有没有下一行

# ---------------------------------------------------------------------------
# 从 pcap 文件名推断 browser key 及关联路径
# ---------------------------------------------------------------------------

def browser_paths(pcap: Path) -> dict:
    """
    输入: capture_chrome.pcap  →
    返回:
      browser : "chrome"
      keylog  : capture_chrome.pcap 同目录 / tls_keys_chrome.log
      tsv     : capture_chrome.tsv
      flows   : capture_chrome_flows/
      inferred: capture_chrome_inferred/
    """
    stem = pcap.stem                        # capture_chrome
    browser = stem.removeprefix("capture_") # chrome
    d = pcap.parent
    return {
        "browser":  browser,
        "keylog":   d / f"tls_keys_{browser}.log",
        "tsv":      d / f"capture_{browser}.tsv",
        "flows":    d / f"capture_{browser}_flows",
        "inferred": d / f"capture_{browser}_inferred",
    }

# ---------------------------------------------------------------------------
# 单步运行
# ---------------------------------------------------------------------------

def run(cmd: list[str], label: str) -> bool:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
        if result.returncode != 0:
            log.error("[%s] 失败（exit %d）\n%s",
                      label, result.returncode, result.stderr.strip())
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("[%s] 超时（>600s）", label)
        return False
    except Exception as e:
        log.error("[%s] 异常：%s", label, e)
        return False

# ---------------------------------------------------------------------------
# 单个 pcap 的处理
# ---------------------------------------------------------------------------

def process_pcap(
    pcap: Path,
    steps: set[str],
    skip_existing: bool,
    with_coframe: bool,
    sni_suffixes: list[str],
) -> dict:
    """
    处理一个 pcap 文件，返回：
      {"pcap": str, "ok": bool, "skipped": list, "failed": list}
    """
    p     = browser_paths(pcap)
    label = f"{pcap.parent.name}/{pcap.name}"
    result = {"pcap": str(pcap), "ok": True, "skipped": [], "failed": []}

    # ── step 1: extract_features ──────────────────────────────────────────
    if "extract" in steps:
        if skip_existing and p["tsv"].exists():
            log.info("[%s] extract  跳过（已存在）", label)
            result["skipped"].append("extract")
        else:
            cmd = [sys.executable, str(SCRIPTS / "extract_features.py"), str(pcap)]
            if sni_suffixes:
                cmd += ["--sni-suffix"] + sni_suffixes
            if run(cmd, f"{label}/extract"):
                log.info("[%s] extract  完成", label)
            else:
                result["ok"] = False
                result["failed"].append("extract")
                return result   # tsv 缺失，后续两步无法运行

    # ── step 2: classify_packets ──────────────────────────────────────────
    if "classify" in steps:
        if not tsv_has_data(p["tsv"]):
            log.info("[%s] classify 跳过：%s 无数据行", label, p["tsv"].name)
            result["skipped"].append("classify")
        elif not p["keylog"].exists():
            log.warning("[%s] classify 跳过：未找到 %s", label, p["keylog"].name)
            result["skipped"].append("classify")
        elif skip_existing and p["flows"].exists() and any(p["flows"].iterdir()):
            log.info("[%s] classify 跳过（已存在）", label)
            result["skipped"].append("classify")
        else:
            cmd = [
                sys.executable, str(SCRIPTS / "classify_packets.py"),
                str(pcap), str(p["keylog"]),
                "--flow-tsv", str(p["tsv"]),
                "--output-dir", str(p["flows"]),
            ]
            if run(cmd, f"{label}/classify"):
                log.info("[%s] classify 完成", label)
            else:
                result["ok"] = False
                result["failed"].append("classify")

    # ── step 3: infer_packets ─────────────────────────────────────────────
    if "infer" in steps:
        if not tsv_has_data(p["tsv"]):
            log.info("[%s] infer    跳过：%s 无数据行", label, p["tsv"].name)
            result["skipped"].append("infer")
        elif skip_existing and p["inferred"].exists() and any(p["inferred"].iterdir()):
            log.info("[%s] infer    跳过（已存在）", label)
            result["skipped"].append("infer")
        else:
            cmd = [
                sys.executable, str(SCRIPTS / "infer_packets.py"),
                str(pcap),
                "--flow-tsv", str(p["tsv"]),
                "--output-dir", str(p["inferred"]),
            ]
            if with_coframe:
                cmd.append("--with-coframe")
            if run(cmd, f"{label}/infer"):
                log.info("[%s] infer    完成", label)
            else:
                result["ok"] = False
                result["failed"].append("infer")

    return result

# ---------------------------------------------------------------------------
# 单目录处理（扫描所有 capture_*.pcap）
# ---------------------------------------------------------------------------

def process_one(
    subdir: Path,
    steps: set[str],
    skip_existing: bool,
    with_coframe: bool,
    sni_suffixes: list[str],
) -> dict:
    """
    扫描子目录下所有 capture_*.pcap，逐一处理。
    返回 {"dir": str, "ok": bool, "skipped": list, "failed": list}
    """
    name  = subdir.name
    pcaps = sorted(subdir.glob("capture_*.pcap"))

    result = {"dir": name, "ok": True, "skipped": [], "failed": []}

    if not pcaps:
        log.warning("[%s] 跳过：未找到 capture_*.pcap", name)
        result["ok"] = False
        result["failed"].append("no_pcap")
        return result

    for pcap in pcaps:
        r = process_pcap(pcap, steps, skip_existing, with_coframe, sni_suffixes)
        if not r["ok"]:
            result["ok"] = False
            result["failed"].extend(r["failed"])
        result["skipped"].extend(r["skipped"])

    return result

# ---------------------------------------------------------------------------
# 并行包装
# ---------------------------------------------------------------------------

def process_one_star(args):
    try:
        return process_one(*args)
    except Exception:
        return {"dir": str(args[0].name), "ok": False,
                "skipped": [], "failed": ["exception"]}

# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "批量处理 fetch_output 下所有子文件夹，\n"
            "对每个 capture_XX.pcap（XX=chrome/firefox/safari 等）依次运行：\n"
            "  1. extract_features.py  → capture_XX.tsv\n"
            "  2. classify_packets.py  → capture_XX_flows/\n"
            "  3. infer_packets.py     → capture_XX_inferred/\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("root", help="包含子文件夹的根目录（如 fetch_output）")
    parser.add_argument(
        "--only", nargs="+",
        choices=["extract", "classify", "infer"],
        default=["extract", "classify", "infer"],
        metavar="STEP",
        help="只运行指定步骤（extract / classify / infer），默认全部运行",
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="若输出已存在则跳过该步骤，避免重复处理",
    )
    parser.add_argument(
        "--with-coframe", action="store_true",
        help="传给 infer_packets.py：假设可能附带控制帧，扩大 est_h2_payload_range 下界",
    )
    parser.add_argument(
        "--sni-suffix", nargs="+", default=[], metavar="SUFFIX",
        help="传给 extract_features.py 的 SNI 后缀白名单，例如：--sni-suffix wikipedia.org",
    )
    parser.add_argument(
        "--jobs", "-j", type=int, default=1,
        help="并行工作进程数（默认 1，串行）",
    )
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        log.error("目录不存在：%s", root); sys.exit(1)

    subdirs = sorted(d for d in root.iterdir() if d.is_dir())
    if not subdirs:
        log.warning("未找到任何子目录：%s", root); sys.exit(0)

    steps = set(args.only)
    log.info("根目录  : %s", root)
    log.info("子目录数: %d", len(subdirs))
    log.info("执行步骤: %s", ", ".join(sorted(steps)))
    log.info("并行数  : %d", args.jobs)

    task_args = [
        (d, steps, args.skip_existing, args.with_coframe, args.sni_suffix)
        for d in subdirs
    ]

    results = []
    if args.jobs <= 1:
        for ta in task_args:
            results.append(process_one(*ta))
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(process_one_star, ta): ta[0].name
                       for ta in task_args}
            for fut in as_completed(futures):
                results.append(fut.result())

    total   = len(results)
    success = sum(1 for r in results if r["ok"])
    failed  = [r for r in results if not r["ok"]]

    print()
    print("=" * 60)
    print(f"完成：{success}/{total} 个目录成功")
    if failed:
        print(f"失败（{len(failed)} 个）：")
        for r in failed:
            print(f"  {r['dir']}  failed={r['failed']}")
    print("=" * 60)


if __name__ == "__main__":
    main()
