"""
从 traffic_feature.tsv 生成 urls.txt
每行输出：ID\t词条名\t完整URL

TSV 文件行格式有两种：
  [''ID'', ''中文名'', ''URL编码路径'', ...]   <- 双单引号风格
  ['ID',   '中文名',   'URL编码路径',   ...]   <- 标准单引号风格
"""

import ast
import sys
from pathlib import Path

WIKI_BASE = "https://zh.wikipedia.org/wiki/"
INPUT_TSV = Path("traffic_feature.tsv")
OUTPUT_TXT = Path("urls.txt")


def parse_row(line: str) -> tuple[str, str, str] | None:
    """
    解析一行，提取 (ID, 中文名, URL路径)。

    文件中存在三种格式：
      A) [''ID'', ''名称'', ''URL'', ...]           全双单引号
      B) [''ID'', "名称含'号", ''URL'', ...]        混合引号（含撇号的名称用双引号）
      C) ['ID',   '名称',   'URL',   ...]            标准单引号
    """
    line = line.strip()
    if not line:
        return None

    # --- 格式 A / B：以 ['' 开头，将 '' 还原为 ' 再 eval ---
    if line.startswith("[''"):
        converted = line.replace("''", "'")
        try:
            row = ast.literal_eval(converted)
            if isinstance(row, list) and len(row) >= 3:
                return str(row[0]), str(row[1]), str(row[2])
        except Exception:
            pass

    # --- 格式 C：标准单引号，直接 eval ---
    if line.startswith("['"):
        try:
            row = ast.literal_eval(line)
            if isinstance(row, list) and len(row) >= 3:
                return str(row[0]), str(row[1]), str(row[2])
        except Exception:
            pass

    return None


def main():
    if not INPUT_TSV.exists():
        print(f"错误：找不到 {INPUT_TSV}", file=sys.stderr)
        sys.exit(1)

    rows: list[tuple[str, str, str]] = []
    errors: list[int] = []

    with INPUT_TSV.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            result = parse_row(raw)
            if result is None:
                if raw.strip():
                    errors.append(lineno)
                continue
            rows.append(result)

    # 写 urls.txt：注释头 + 每行 ID\t词条名\t完整URL
    with OUTPUT_TXT.open("w", encoding="utf-8") as out:
        out.write("# ID\t词条名\t完整URL\n")
        for row_id, name, url_path in rows:
            full_url = WIKI_BASE + url_path
            out.write(f"{row_id}\t{name}\t{full_url}\n")

    print(f"完成：共写入 {len(rows)} 条，解析失败 {len(errors)} 行 → {OUTPUT_TXT}")

    if errors:
        print(f"失败行号：{errors[:20]}{'...' if len(errors) > 20 else ''}")

    print("\n前 5 条预览：")
    for row_id, name, url_path in rows[:5]:
        print(f"  [{row_id}] {name}")
        print(f"       {WIKI_BASE + url_path}")


if __name__ == "__main__":
    main()
