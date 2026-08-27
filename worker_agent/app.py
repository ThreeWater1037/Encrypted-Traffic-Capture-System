"""Flask Worker HTTP API。

该层只处理认证、CORS、参数转换和响应格式；耗时抓取永远进入后台任务队列，
避免长时间占用 HTTP 请求线程。
"""

from __future__ import annotations

import hmac
import json
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from browser_discovery import discover_browser

from .config import WorkerConfig
from .schema import (
    TASK_ID_RE,
    ValidationError,
    parse_uploaded_input,
    validate_task_payload,
)
from .task_runner import QueueFullError, TaskConflictError, TaskManager
from .task_store import TERMINAL_STATUSES, TaskStore


def _find_first(candidates: list[str | Path]) -> str | None:
    """从绝对路径候选和 PATH 中返回第一个可用可执行文件。"""
    for candidate in candidates:
        path = Path(candidate)
        if path.is_file():
            return str(path.resolve())
        located = shutil.which(str(candidate))
        if located:
            return str(Path(located).resolve())
    return None


def detect_capabilities(config: WorkerConfig) -> dict[str, Any]:
    """探测当前子机器可用浏览器、TShark、解释器和分析脚本。"""
    chrome = discover_browser("chrome")
    firefox = discover_browser("firefox")
    edge = discover_browser("edge")
    tshark = _find_first(
        [
            "tshark",
            Path(r"D:\software\Wireshark\tshark.exe"),
            Path(r"C:\Program Files\Wireshark\tshark.exe"),
            Path(r"C:\Program Files (x86)\Wireshark\tshark.exe"),
            Path("/Applications/Wireshark.app/Contents/MacOS/tshark"),
        ]
    )

    browsers = []
    if chrome:
        browsers.append(
            {"name": "chrome", "path": chrome, "no_cache_verified": True}
        )
    if edge:
        browsers.append(
            {"name": "edge", "path": edge, "no_cache_verified": True}
        )
    if firefox:
        browsers.append(
            {"name": "firefox", "path": firefox, "no_cache_verified": True}
        )

    try:
        python_version = subprocess.run(
            [str(config.python_executable), "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        task_python_version = (
            python_version.stdout.strip() or python_version.stderr.strip()
        )
    except (OSError, subprocess.SubprocessError):
        task_python_version = "unknown"

    return {
        "worker_id": config.worker_id,
        "os": platform.system().lower(),
        "os_version": platform.version(),
        "architecture": platform.machine(),
        "python": {
            "executable": str(config.python_executable),
            "version": task_python_version,
            "service_version": platform.python_version(),
        },
        "browsers": browsers,
        "capture": {
            "pcap": tshark is not None,
            "tshark_path": tshark,
        },
        "network": {
            # 只公开是否配置，避免把可能敏感的代理地址返回给主控或前端。
            "browser_proxy_configured": config.proxy_url is not None,
        },
        "analysis": {
            "scripts_ready": all(
                (config.project_root / script).is_file()
                for script in (
                    "batch_process.py",
                    "extract_features.py",
                    "classify_packets.py",
                    "infer_packets.py",
                )
            ),
            "steps": ["extract", "classify", "infer"],
        },
        "input": {
            "json_supported": True,
            "file_upload_supported": True,
            "file_extensions": [".txt", ".tsv"],
            "file_encoding": "UTF-8",
            "max_request_bytes": config.max_content_length,
            "max_items": config.max_items,
        },
        "cache_policy": {
            "enabled": False,
            "fresh_profile": True,
            "result_reuse": False,
            "safari_supported": False,
        },
    }


def _aggregate_statuses(statuses: list[str]) -> str:
    """为旧任务结果补状态时使用的三级汇总规则。"""
    if statuses and all(status == "SUCCEEDED" for status in statuses):
        return "SUCCEEDED"
    if any(status in {"SUCCEEDED", "PARTIAL"} for status in statuses):
        return "PARTIAL"
    return "FAILED"


def _result_with_items(result: Any) -> Any:
    """兼容旧 manifest：根据 units 动态补出每个 URL 的 items 状态。"""
    if not isinstance(result, dict) or isinstance(result.get("items"), list):
        return result

    grouped: dict[str, list[dict[str, Any]]] = {}
    for unit in result.get("units", []):
        if isinstance(unit, dict) and isinstance(unit.get("item_id"), str):
            grouped.setdefault(unit["item_id"], []).append(unit)

    items = []
    for item_units in grouped.values():
        first = item_units[0]
        statuses = [str(unit.get("status", "FAILED")) for unit in item_units]
        items.append(
            {
                "item_id": first["item_id"],
                "name": first.get("name"),
                "url": first.get("url"),
                "status": _aggregate_statuses(statuses),
                "browser_statuses": [
                    {
                        "browser": unit.get("browser"),
                        "status": unit.get("status", "FAILED"),
                    }
                    for unit in item_units
                ],
            }
        )

    normalized = dict(result)
    normalized["items"] = items
    normalized["item_summary"] = {
        "total": len(items),
        "succeeded": sum(item["status"] == "SUCCEEDED" for item in items),
        "partial": sum(item["status"] == "PARTIAL" for item in items),
        "failed": sum(item["status"] == "FAILED" for item in items),
    }
    return normalized


def _form_list(name: str, *, default: list[str]) -> list[str]:
    """解析 multipart 中的逗号列表或 JSON 数组字符串。"""
    raw = request.form.get(name)
    if raw is None:
        return list(default)
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{name} 不是有效的 JSON 数组") from exc
        if not isinstance(value, list) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValidationError(f"{name} 必须是字符串数组")
        return value
    return [item for item in re.split(r"[\s,]+", raw) if item]


def _form_bool(name: str, *, default: bool) -> bool:
    """把常见表单布尔写法规范化为 Python bool。"""
    raw = request.form.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValidationError(f"{name} 必须是 true 或 false")


def _include_request_in_response() -> bool:
    """主控可关闭大请求体回显，直接 API 调试仍默认保留。"""
    return request.args.get("include_request", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _include_result_in_response() -> bool:
    return request.args.get("include_result", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _public_task(
    task: dict[str, Any],
    *,
    include_request: bool = False,
    include_items: bool = True,
    include_result: bool = True,
) -> dict[str, Any]:
    """过滤内部字段，生成稳定的公共任务响应结构。"""
    response = {
        key: task.get(key)
        for key in (
            "task_id",
            "status",
            "stage",
            "error",
            "pid",
            "cancel_requested",
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
        )
    }
    if include_request:
        response["request"] = task.get("request")
    if include_result:
        result = _result_with_items(task.get("result"))
        response["result"] = result
        if include_items:
            response["items"] = result.get("items", []) if isinstance(result, dict) else []
    return response


def create_app(
    config: WorkerConfig | None = None,
    *,
    store: TaskStore | None = None,
    manager: TaskManager | None = None,
) -> Flask:
    """构造 Flask 应用，并注入可测试的配置、仓库和任务管理器。"""
    worker_config = config or WorkerConfig.from_env()
    worker_config.prepare()

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = worker_config.max_content_length
    app.config["JSON_AS_ASCII"] = False

    task_store = store or TaskStore(worker_config.database_path)
    task_manager = manager or TaskManager(worker_config, task_store)
    app.extensions["worker_config"] = worker_config
    app.extensions["task_store"] = task_store
    app.extensions["task_manager"] = task_manager

    @app.before_request
    def authenticate_internal_api():
        """健康检查和 CORS 预检除外，其余接口统一校验 Bearer Token。"""
        if request.method == "OPTIONS":
            return None
        if request.endpoint in {"health", "static"}:
            return None
        authorization = request.headers.get("Authorization", "")
        expected = f"Bearer {worker_config.token}"
        if not hmac.compare_digest(authorization, expected):
            return jsonify({"error": "unauthorized", "message": "Worker Token 无效"}), 401
        return None

    @app.after_request
    def disable_http_cache(response):
        """对所有成功和错误响应统一禁用缓存，并按白名单返回 CORS。"""
        response.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        origin = request.headers.get("Origin")
        if origin and origin in worker_config.allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers.add("Vary", "Origin")
        return response

    @app.errorhandler(413)
    def request_too_large(_error):
        """把 Flask 的上传大小异常转换为统一 JSON 错误。"""
        return (
            jsonify(
                {
                    "error": "request_too_large",
                    "message": f"请求体不能超过 {worker_config.max_content_length} 字节",
                }
            ),
            413,
        )

    @app.get("/api/v1/health")
    def health():
        """返回无需认证的轻量健康、队列和忙闲状态。"""
        return jsonify(
            {
                "status": "ok",
                "worker_id": worker_config.worker_id,
                "busy": task_manager.active_task_id is not None,
                "active_task_id": task_manager.active_task_id,
                "queue_size": task_manager.queue_size,
                "queue_capacity": worker_config.max_queue_size,
                "recovered_tasks_on_startup": task_manager.recovered_task_count,
                "task_counts": task_store.status_counts(),
                "cache_enabled": False,
            }
        )

    @app.get("/api/v1/capabilities")
    def capabilities():
        """返回主控端分配任务前需要的真实机器能力。"""
        return jsonify(detect_capabilities(worker_config))

    @app.post("/api/v1/tasks")
    def create_task():
        """接收结构化 JSON，校验后幂等创建后台任务。"""
        try:
            payload = validate_task_payload(request.get_json(silent=True), worker_config)
            task, created = task_manager.submit(payload)
        except ValidationError as exc:
            return jsonify({"error": "validation_error", "message": str(exc)}), 400
        except QueueFullError as exc:
            return jsonify({"error": "queue_full", "message": str(exc)}), 503
        except TaskConflictError as exc:
            return jsonify({"error": "task_conflict", "message": str(exc)}), 409

        include_request = _include_request_in_response()
        include_result = _include_result_in_response()
        if include_request or (include_result and task["status"] in TERMINAL_STATUSES):
            task = task_store.get_task(payload["task_id"]) or task
        response = _public_task(
            task,
            include_request=include_request,
            include_result=include_result,
        )
        response["duplicate"] = not created
        response["status_url"] = f"/api/v1/tasks/{payload['task_id']}"
        return jsonify(response), 202 if created else 200

    @app.post("/api/v1/tasks/from-file")
    def create_task_from_file():
        """接收 UTF-8 txt/TSV 文件，并转换成与 JSON 接口相同的任务结构。"""
        try:
            uploaded = request.files.get("file")
            if uploaded is None or not uploaded.filename:
                raise ValidationError("缺少上传字段 file")
            suffix = Path(uploaded.filename).suffix.lower()
            if suffix not in {".txt", ".tsv"}:
                raise ValidationError("file 只允许上传 .txt 或 .tsv 文件")
            try:
                text = uploaded.stream.read().decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValidationError("上传文件必须使用 UTF-8 编码") from exc

            raw_payload = {
                "task_id": request.form.get("task_id"),
                "items": parse_uploaded_input(
                    text, max_items=worker_config.max_items
                ),
                "browsers": _form_list("browsers", default=["chrome"]),
                "pcap": _form_bool("pcap", default=True),
                "outputs": {
                    "html": _form_bool("save_html", default=False),
                    "reports": _form_bool("save_reports", default=False),
                },
                "analysis": {
                    "steps": _form_list(
                        "analysis_steps",
                        default=[],
                    ),
                    "with_coframe": _form_bool("with_coframe", default=False),
                    "sni_suffixes": _form_list("sni_suffixes", default=[]),
                },
            }
            payload = validate_task_payload(raw_payload, worker_config)
            task, created = task_manager.submit(payload)
        except ValidationError as exc:
            return jsonify({"error": "validation_error", "message": str(exc)}), 400
        except QueueFullError as exc:
            return jsonify({"error": "queue_full", "message": str(exc)}), 503
        except TaskConflictError as exc:
            return jsonify({"error": "task_conflict", "message": str(exc)}), 409

        include_request = _include_request_in_response()
        include_result = _include_result_in_response()
        if include_request or (include_result and task["status"] in TERMINAL_STATUSES):
            task = task_store.get_task(payload["task_id"]) or task
        response = _public_task(
            task,
            include_request=include_request,
            include_result=include_result,
        )
        response["duplicate"] = not created
        response["source_filename"] = Path(uploaded.filename).name
        response["item_count"] = len(payload["items"])
        response["status_url"] = f"/api/v1/tasks/{payload['task_id']}"
        return jsonify(response), 202 if created else 200

    @app.get("/api/v1/tasks/<task_id>")
    def get_task(task_id: str):
        """查询生命周期状态；终态时同时返回每个 URL 的结果。"""
        if not TASK_ID_RE.fullmatch(task_id):
            return jsonify({"error": "invalid_task_id"}), 400
        include_result = request.args.get("include_result", "true").strip().lower() not in {
            "0", "false", "no", "off"
        }
        task = task_store.get_task_status(task_id)
        if task is None:
            return jsonify({"error": "not_found", "message": "任务不存在"}), 404
        if include_result and task["status"] in TERMINAL_STATUSES:
            task = task_store.get_task(task_id) or task
        compact = request.args.get("compact", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }
        return jsonify(
            _public_task(
                task, include_items=not compact, include_result=include_result
            )
        )

    @app.post("/api/v1/tasks/<task_id>/cancel")
    def cancel_task(task_id: str):
        """请求取消排队或正在运行的任务。"""
        if not TASK_ID_RE.fullmatch(task_id):
            return jsonify({"error": "invalid_task_id"}), 400
        task = task_manager.cancel(task_id)
        if task is None:
            return jsonify({"error": "not_found", "message": "任务不存在"}), 404
        return jsonify(_public_task(task)), 200

    @app.post("/api/v1/tasks/<task_id>/resume")
    def resume_task(task_id: str):
        """幂等恢复原任务目录；已完成 URL 由原子检查点跳过。"""
        if not TASK_ID_RE.fullmatch(task_id):
            return jsonify({"error": "invalid_task_id"}), 400
        payload = request.get_json(silent=True)
        resume_token = payload.get("resume_token") if isinstance(payload, dict) else None
        if (
            not isinstance(resume_token, str)
            or not 1 <= len(resume_token) <= 128
            or not TASK_ID_RE.fullmatch(resume_token)
        ):
            return (
                jsonify(
                    {
                        "error": "validation_error",
                        "message": "resume_token 格式错误",
                    }
                ),
                400,
            )
        task, queued = task_manager.resume(task_id, resume_token)
        if task is None:
            return jsonify({"error": "not_found", "message": "任务不存在"}), 404
        response = _public_task(task, include_result=False)
        response["resume_token"] = resume_token
        response["queued_for_resume"] = queued
        return jsonify(response), 202 if queued else 200

    @app.get("/api/v1/tasks/<task_id>/result")
    def get_task_result(task_id: str):
        """仅在终态返回结果清单和受控的相对结果目录。"""
        if not TASK_ID_RE.fullmatch(task_id):
            return jsonify({"error": "invalid_task_id"}), 400
        task = task_store.get_task_result_record(task_id)
        if task is None:
            return jsonify({"error": "not_found", "message": "任务不存在"}), 404
        if task["status"] not in TERMINAL_STATUSES:
            return (
                jsonify(
                    {
                        "error": "not_ready",
                        "status": task["status"],
                        "stage": task["stage"],
                    }
                ),
                409,
            )
        result = _result_with_items(task["result"])
        compact = request.args.get("compact", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }
        return jsonify(
            {
                "task_id": task_id,
                "status": task["status"],
                "error": task["error"],
                **({} if compact else {"items": result.get("items", []) if result else []}),
                "result": result,
                "local_result_dir": f"tasks/{task_id}",
            }
        )

    @app.get("/api/v1/tasks/<task_id>/log")
    def get_task_log(task_id: str):
        """按字节偏移增量读取日志，避免一次返回超大文本。"""
        if not TASK_ID_RE.fullmatch(task_id):
            return jsonify({"error": "invalid_task_id"}), 400
        task = task_store.get_task_status(task_id)
        if task is None:
            return jsonify({"error": "not_found", "message": "任务不存在"}), 404

        try:
            offset = max(0, int(request.args.get("offset", "0")))
            limit = min(65_536, max(1, int(request.args.get("limit", "65536"))))
        except ValueError:
            return jsonify({"error": "invalid_pagination"}), 400

        log_path = worker_config.tasks_dir / task_id / "worker.log"
        if not log_path.is_file():
            return jsonify({"task_id": task_id, "text": "", "next_offset": 0, "eof": True})

        file_size = log_path.stat().st_size
        offset = min(offset, file_size)
        with log_path.open("rb") as stream:
            stream.seek(offset)
            data = stream.read(limit)
        next_offset = offset + len(data)
        return jsonify(
            {
                "task_id": task_id,
                "text": data.decode("utf-8", errors="replace"),
                "next_offset": next_offset,
                "eof": next_offset >= file_size,
            }
        )

    return app
