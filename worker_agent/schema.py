"""Worker 请求参数与上传文件校验。

所有外部输入都在进入任务队列前完成规范化，避免把任意路径、命令或非法 URL
传给子进程。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .config import WorkerConfig


TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SNI_SUFFIX_RE = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
ALLOWED_STEPS = ("extract", "classify", "infer")
ALLOWED_TOP_LEVEL_KEYS = {
    "task_id",
    "items",
    "browsers",
    "pcap",
    "outputs",
    "analysis",
}
ALLOWED_OUTPUT_KEYS = {"html", "reports"}
ALLOWED_ANALYSIS_KEYS = {"steps", "with_coframe", "sni_suffixes"}


class ValidationError(ValueError):
    """表示可直接转换为 HTTP 400 的客户端输入错误。"""

    pass


def parse_uploaded_input(text: str, *, max_items: int) -> list[dict[str, str]]:
    """解析 UTF-8 txt/TSV 内容，并保留行号用于精确报告格式错误。"""
    items: list[dict[str, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            raise ValidationError(
                f"上传文件第 {line_number} 行格式错误，必须是 ID<TAB>名称<TAB>完整URL"
            )
        items.append({"id": parts[0], "name": parts[1], "url": parts[2]})
        if len(items) > max_items:
            raise ValidationError(f"上传文件最多允许 {max_items} 个 URL")

    if not items:
        raise ValidationError("上传文件中没有有效 URL")
    return items


def _clean_text(value: Any, field: str, *, max_length: int) -> str:
    """统一清理短文本字段，禁止制表符和换行破坏生成的 TSV。"""
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是字符串")
    cleaned = value.strip()
    if not cleaned:
        raise ValidationError(f"{field} 不能为空")
    if len(cleaned) > max_length:
        raise ValidationError(f"{field} 最长为 {max_length} 个字符")
    if any(char in cleaned for char in ("\t", "\r", "\n")):
        raise ValidationError(f"{field} 不能包含制表符或换行符")
    return cleaned


def validate_task_payload(payload: Any, config: WorkerConfig) -> dict[str, Any]:
    """校验并规范化任务 JSON，返回可安全落盘和执行的内部结构。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    unknown_keys = sorted(set(payload) - ALLOWED_TOP_LEVEL_KEYS)
    if unknown_keys:
        raise ValidationError(f"不支持的任务参数：{', '.join(unknown_keys)}")

    task_id = _clean_text(payload.get("task_id"), "task_id", max_length=128)
    if not TASK_ID_RE.fullmatch(task_id):
        raise ValidationError("task_id 只允许字母、数字、点、下划线和连字符")

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValidationError("items 必须是数组")
    if not 1 <= len(raw_items) <= config.max_items:
        raise ValidationError(f"items 数量必须在 1 到 {config.max_items} 之间")

    items: list[dict[str, str]] = []
    seen_item_ids: set[str] = set()
    for index, raw_item in enumerate(raw_items):
        if not isinstance(raw_item, dict):
            raise ValidationError(f"items[{index}] 必须是对象")
        unknown_item_keys = sorted(set(raw_item) - {"id", "name", "url"})
        if unknown_item_keys:
            raise ValidationError(
                f"items[{index}] 包含不支持的字段：{', '.join(unknown_item_keys)}"
            )

        item_id = _clean_text(raw_item.get("id"), f"items[{index}].id", max_length=64)
        if not ITEM_ID_RE.fullmatch(item_id):
            raise ValidationError(
                f"items[{index}].id 只允许字母、数字、点、下划线和连字符"
            )
        if item_id in seen_item_ids:
            raise ValidationError(f"items[{index}].id 重复：{item_id}")
        seen_item_ids.add(item_id)

        name = _clean_text(
            raw_item.get("name"), f"items[{index}].name", max_length=80
        )
        url = _clean_text(raw_item.get("url"), f"items[{index}].url", max_length=2048)
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValidationError(f"items[{index}].url 只允许完整的 HTTP/HTTPS URL")
        if parsed.username or parsed.password:
            raise ValidationError(f"items[{index}].url 不能包含用户名或密码")
        items.append({"id": item_id, "name": name, "url": url})

    raw_browsers = payload.get("browsers")
    if not isinstance(raw_browsers, list) or not raw_browsers:
        raise ValidationError("browsers 必须是非空数组")
    browsers: list[str] = []
    for raw_browser in raw_browsers:
        if not isinstance(raw_browser, str):
            raise ValidationError("browsers 中的值必须是字符串")
        browser = raw_browser.strip().lower()
        if browser not in config.allowed_browsers:
            allowed = ", ".join(config.allowed_browsers)
            raise ValidationError(
                f"暂不支持浏览器 {browser!r}；当前允许：{allowed}。"
                "Safari 暂未满足无缓存保证"
            )
        if browser not in browsers:
            browsers.append(browser)

    pcap = payload.get("pcap", True)
    if not isinstance(pcap, bool):
        raise ValidationError("pcap 必须是布尔值")

    raw_outputs = payload.get("outputs", {})
    if raw_outputs is None:
        raw_outputs = {}
    if not isinstance(raw_outputs, dict):
        raise ValidationError("outputs 必须是对象")
    unknown_output_keys = sorted(set(raw_outputs) - ALLOWED_OUTPUT_KEYS)
    if unknown_output_keys:
        raise ValidationError(
            f"outputs 包含不支持的字段：{', '.join(unknown_output_keys)}"
        )
    save_html = raw_outputs.get("html", False)
    save_reports = raw_outputs.get("reports", False)
    if not isinstance(save_html, bool):
        raise ValidationError("outputs.html 必须是布尔值")
    if not isinstance(save_reports, bool):
        raise ValidationError("outputs.reports 必须是布尔值")

    raw_analysis = payload.get("analysis", {})
    if raw_analysis is None:
        raw_analysis = {}
    if not isinstance(raw_analysis, dict):
        raise ValidationError("analysis 必须是对象")
    unknown_analysis_keys = sorted(set(raw_analysis) - ALLOWED_ANALYSIS_KEYS)
    if unknown_analysis_keys:
        raise ValidationError(
            f"analysis 包含不支持的字段：{', '.join(unknown_analysis_keys)}"
        )

    raw_steps = raw_analysis.get("steps", [])
    if not isinstance(raw_steps, list):
        raise ValidationError("analysis.steps 必须是数组")
    steps: list[str] = []
    for raw_step in raw_steps:
        if not isinstance(raw_step, str) or raw_step not in ALLOWED_STEPS:
            raise ValidationError(
                "analysis.steps 只允许 extract、classify、infer"
            )
        if raw_step not in steps:
            steps.append(raw_step)
    if steps and not pcap:
        raise ValidationError("执行分析步骤时必须设置 pcap=true")

    with_coframe = raw_analysis.get("with_coframe", False)
    if not isinstance(with_coframe, bool):
        raise ValidationError("analysis.with_coframe 必须是布尔值")

    raw_suffixes = raw_analysis.get("sni_suffixes", [])
    if not isinstance(raw_suffixes, list):
        raise ValidationError("analysis.sni_suffixes 必须是数组")
    suffixes: list[str] = []
    for raw_suffix in raw_suffixes:
        suffix = _clean_text(raw_suffix, "analysis.sni_suffixes[]", max_length=253)
        suffix = suffix.lower().lstrip(".")
        if not SNI_SUFFIX_RE.fullmatch(suffix):
            raise ValidationError(f"无效的 SNI 后缀：{raw_suffix!r}")
        if suffix not in suffixes:
            suffixes.append(suffix)

    return {
        "task_id": task_id,
        "items": items,
        "browsers": browsers,
        "pcap": pcap,
        "outputs": {
            "html": save_html,
            "reports": save_reports,
        },
        "analysis": {
            "steps": steps,
            "with_coframe": with_coframe,
            "sni_suffixes": suffixes,
        },
    }
