"""主控请求校验与 TXT/TSV 解析。"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ALLOWED_BROWSERS = ("chrome", "edge", "firefox")
ALLOWED_STEPS = ("extract", "classify", "infer")


class ValidationError(ValueError):
    """可安全返回给调用方的参数错误。"""


def new_job_id() -> str:
    """生成可同时用于主控和 Worker 的短任务 ID。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"job-{stamp}-{uuid.uuid4().hex[:8]}"


def _text(value: Any, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationError(f"{field} 最长为 {maximum} 个字符")
    if any(char in result for char in ("\r", "\n", "\t")):
        raise ValidationError(f"{field} 不能包含换行符或制表符")
    return result


def parse_uploaded_input(text: str, *, max_items: int) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            raise ValidationError(
                f"上传文件第 {line_number} 行必须是 ID<TAB>名称<TAB>完整URL"
            )
        items.append({"id": parts[0], "name": parts[1], "url": parts[2]})
        if len(items) > max_items:
            raise ValidationError(f"最多允许 {max_items} 个 URL")
    if not items:
        raise ValidationError("上传文件中没有有效 URL")
    return items


def validate_machine(payload: Any, *, existing_token: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    machine_id = _text(payload.get("machine_id"), "machine_id", maximum=64)
    if not ID_RE.fullmatch(machine_id):
        raise ValidationError("machine_id 只允许字母、数字、点、下划线和连字符")
    name = _text(payload.get("name", machine_id), "name", maximum=80)
    base_url = _text(payload.get("base_url"), "base_url", maximum=512).rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError("base_url 必须是完整的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValidationError("base_url 不能包含认证信息、查询参数或片段")
    token = payload.get("token")
    if token is None:
        token = existing_token
    token = _text(token, "token", maximum=512)
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValidationError("enabled 必须是布尔值")
    return {
        "machine_id": machine_id,
        "name": name,
        "base_url": base_url,
        "token": token,
        "enabled": enabled,
    }


def validate_job(payload: Any, *, max_items: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    allowed = {"job_id", "name", "items", "targets", "pcap", "outputs", "analysis"}
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValidationError(f"不支持的任务参数：{', '.join(unknown)}")

    job_id = payload.get("job_id") or new_job_id()
    job_id = _text(job_id, "job_id", maximum=64)
    if not ID_RE.fullmatch(job_id):
        raise ValidationError("job_id 只允许字母、数字、点、下划线和连字符")
    name = _text(payload.get("name", job_id), "name", maximum=120)

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= max_items:
        raise ValidationError(f"items 数量必须在 1 到 {max_items} 之间")
    items: list[dict[str, str]] = []
    seen_items: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise ValidationError(f"items[{index}] 必须是对象")
        item_id = _text(raw.get("id"), f"items[{index}].id", maximum=64)
        if not ID_RE.fullmatch(item_id) or item_id in seen_items:
            raise ValidationError(f"items[{index}].id 格式错误或重复")
        seen_items.add(item_id)
        item_name = _text(raw.get("name"), f"items[{index}].name", maximum=80)
        url = _text(raw.get("url"), f"items[{index}].url", maximum=2048)
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValidationError(f"items[{index}].url 必须是完整的 HTTP/HTTPS URL")
        if parsed.username or parsed.password:
            raise ValidationError(f"items[{index}].url 不能包含用户名或密码")
        items.append({"id": item_id, "name": item_name, "url": url})

    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise ValidationError("targets 必须是非空数组")
    targets: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for index, raw in enumerate(raw_targets):
        if not isinstance(raw, dict):
            raise ValidationError(f"targets[{index}] 必须是对象")
        machine_id = _text(
            raw.get("machine_id"), f"targets[{index}].machine_id", maximum=64
        )
        if not ID_RE.fullmatch(machine_id) or machine_id in seen_targets:
            raise ValidationError(f"targets[{index}].machine_id 格式错误或重复")
        browsers = raw.get("browsers")
        if not isinstance(browsers, list) or not browsers:
            raise ValidationError(f"targets[{index}].browsers 必须是非空数组")
        normalized: list[str] = []
        for value in browsers:
            browser = value.strip().lower() if isinstance(value, str) else ""
            if browser not in ALLOWED_BROWSERS:
                raise ValidationError(
                    f"targets[{index}].browsers 只允许 chrome、edge、firefox"
                )
            if browser not in normalized:
                normalized.append(browser)
        seen_targets.add(machine_id)
        targets.append({"machine_id": machine_id, "browsers": normalized})

    pcap = payload.get("pcap", True)
    if not isinstance(pcap, bool):
        raise ValidationError("pcap 必须是布尔值")
    outputs = payload.get("outputs", {})
    if outputs is None:
        outputs = {}
    if not isinstance(outputs, dict):
        raise ValidationError("outputs 必须是对象")
    unknown_outputs = sorted(set(outputs) - {"html", "reports"})
    if unknown_outputs:
        raise ValidationError(f"outputs 包含不支持的字段：{', '.join(unknown_outputs)}")
    save_html = outputs.get("html", False)
    save_reports = outputs.get("reports", False)
    if not isinstance(save_html, bool):
        raise ValidationError("outputs.html 必须是布尔值")
    if not isinstance(save_reports, bool):
        raise ValidationError("outputs.reports 必须是布尔值")
    analysis = payload.get("analysis") or {}
    if not isinstance(analysis, dict):
        raise ValidationError("analysis 必须是对象")
    steps = analysis.get("steps", [])
    if not isinstance(steps, list) or any(step not in ALLOWED_STEPS for step in steps):
        raise ValidationError("analysis.steps 只允许 extract、classify、infer")
    steps = list(dict.fromkeys(steps))
    if steps and not pcap:
        raise ValidationError("执行分析步骤时必须设置 pcap=true")
    with_coframe = analysis.get("with_coframe", False)
    if not isinstance(with_coframe, bool):
        raise ValidationError("analysis.with_coframe 必须是布尔值")
    suffixes = analysis.get("sni_suffixes", [])
    if not isinstance(suffixes, list) or any(not isinstance(v, str) for v in suffixes):
        raise ValidationError("analysis.sni_suffixes 必须是字符串数组")
    suffixes = [value.strip().lower().lstrip(".") for value in suffixes if value.strip()]

    return {
        "job_id": job_id,
        "name": name,
        "items": items,
        "targets": targets,
        "pcap": pcap,
        "outputs": {"html": save_html, "reports": save_reports},
        "analysis": {
            "steps": steps,
            "with_coframe": with_coframe,
            "sni_suffixes": list(dict.fromkeys(suffixes)),
        },
    }
