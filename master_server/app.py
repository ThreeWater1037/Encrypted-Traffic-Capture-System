"""主控 Flask HTTP API。"""

from __future__ import annotations

import hmac
import json
import sqlite3
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from .config import MasterConfig
from .dispatcher import (
    JobDispatcher,
    QueueFullError,
    public_machine,
    summarize_job,
)
from .schema import ValidationError, parse_uploaded_input, validate_job, validate_machine
from .store import MasterStore, TERMINAL_STATUSES, utc_now
from .worker_client import WorkerClient, WorkerRequestError


def _form_bool(name: str, *, default: bool) -> bool:
    raw = request.form.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValidationError(f"{name} 必须是 true 或 false")


def _form_list(name: str, *, default: list[str]) -> list[str]:
    raw = request.form.get(name)
    if raw is None:
        return list(default)
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"{name} 不是有效的 JSON 数组") from exc
        if not isinstance(parsed, list) or not all(isinstance(v, str) for v in parsed):
            raise ValidationError(f"{name} 必须是字符串数组")
        return parsed
    return [part.strip() for part in raw.split(",") if part.strip()]


def _create_job(
    payload: dict[str, Any],
    *,
    store: MasterStore,
    dispatcher: JobDispatcher,
    config: MasterConfig,
    source_filename: str | None = None,
    source_content: bytes | None = None,
):
    normalized = validate_job(payload, max_items=config.max_items)
    if store.get_job(normalized["job_id"]) is not None:
        raise sqlite3.IntegrityError(f"job_id {normalized['job_id']} 已存在")
    missing: list[str] = []
    disabled: list[str] = []
    for target in normalized["targets"]:
        machine = store.get_machine(target["machine_id"])
        if machine is None:
            missing.append(target["machine_id"])
        elif not machine["enabled"]:
            disabled.append(target["machine_id"])
    if missing:
        raise ValidationError(f"目标机器不存在：{', '.join(missing)}")
    if disabled:
        raise ValidationError(f"目标机器已停用：{', '.join(disabled)}")

    source_path = None
    if source_filename and source_content is not None:
        safe_name = Path(source_filename).name
        upload_dir = config.uploads_dir / normalized["job_id"]
        upload_dir.mkdir(parents=True, exist_ok=False)
        path = upload_dir / safe_name
        path.write_bytes(source_content)
        source_path = str(path.relative_to(config.data_dir))
    try:
        store.create_job(
            normalized,
            source_filename=Path(source_filename).name if source_filename else None,
            source_path=source_path,
        )
    except Exception:
        # 数据库插入失败时清理由本次请求创建的上传文件。
        if source_path:
            path = config.data_dir / source_path
            if path.is_file():
                path.unlink()
            if path.parent.is_dir():
                path.parent.rmdir()
        raise
    try:
        dispatcher.enqueue(normalized["job_id"])
    except QueueFullError as exc:
        store.update_job(
            normalized["job_id"],
            status="FAILED",
            stage="DONE",
            error=str(exc),
            finished_at=utc_now(),
        )
        raise
    job = store.get_job(normalized["job_id"])
    return jsonify(summarize_job(job or {})), 202


def create_app(
    config: MasterConfig | None = None,
    *,
    store: MasterStore | None = None,
    dispatcher: JobDispatcher | None = None,
) -> Flask:
    """创建可注入依赖的主控 Flask 应用。"""
    master_config = config or MasterConfig.from_env()
    master_config.prepare()
    master_store = store or MasterStore(master_config.database_path)
    # 本机 Worker 由环境变量声明，每次启动同步连接配置；其他机器记录保持不变。
    master_store.upsert_machine(
        {
            "machine_id": master_config.bootstrap_worker_id,
            "name": master_config.bootstrap_worker_name,
            "base_url": master_config.bootstrap_worker_url.rstrip("/"),
            "token": master_config.bootstrap_worker_token,
            "enabled": True,
        }
    )
    job_dispatcher = dispatcher or JobDispatcher(master_config, master_store)

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = master_config.max_content_length
    app.config["JSON_AS_ASCII"] = False
    app.extensions["master_config"] = master_config
    app.extensions["master_store"] = master_store
    app.extensions["job_dispatcher"] = job_dispatcher

    @app.before_request
    def authenticate_master_api():
        if request.method == "OPTIONS" or not master_config.token:
            return None
        if request.endpoint in {"health", "static"}:
            return None
        expected = f"Bearer {master_config.token}"
        if not hmac.compare_digest(request.headers.get("Authorization", ""), expected):
            return jsonify({"error": "unauthorized", "message": "主控 Token 无效"}), 401
        return None

    @app.after_request
    def no_cache_and_cors(response):
        response.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        origin = request.headers.get("Origin")
        if origin and origin in master_config.allowed_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            response.headers.add("Vary", "Origin")
        return response

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify({"error": "request_too_large", "message": "上传文件超过主控限制"}), 413

    @app.errorhandler(ValidationError)
    def validation_error(error):
        return jsonify({"error": "validation_error", "message": str(error)}), 400

    @app.errorhandler(sqlite3.IntegrityError)
    def conflict(error):
        return jsonify({"error": "conflict", "message": f"任务或记录已存在：{error}"}), 409

    @app.errorhandler(QueueFullError)
    def queue_full(error):
        return jsonify({"error": "queue_full", "message": str(error)}), 503

    @app.get("/api/v1/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "service": "traffic-capture-master",
                "machines": len(master_store.list_machines()),
                "cache_enabled": False,
            }
        )

    @app.get("/api/v1/machines")
    def list_machines():
        return jsonify({"machines": [public_machine(item) for item in master_store.list_machines()]})

    @app.post("/api/v1/machines")
    def save_machine():
        raw = request.get_json(silent=True)
        existing = None
        if isinstance(raw, dict) and isinstance(raw.get("machine_id"), str):
            existing = master_store.get_machine(raw["machine_id"].strip())
        machine = validate_machine(
            raw, existing_token=existing.get("token") if existing else None
        )
        saved = master_store.upsert_machine(machine)
        return jsonify(public_machine(saved)), 200 if existing else 201

    @app.delete("/api/v1/machines/<machine_id>")
    def delete_machine(machine_id: str):
        if master_store.get_machine(machine_id) is None:
            return jsonify({"error": "not_found", "message": "机器不存在"}), 404
        try:
            master_store.delete_machine(machine_id)
        except sqlite3.IntegrityError:
            return (
                jsonify(
                    {
                        "error": "machine_in_use",
                        "message": "该机器已有实验记录，不能删除；可在编辑中取消启用",
                    }
                ),
                409,
            )
        return jsonify({"machine_id": machine_id, "deleted": True})

    @app.post("/api/v1/machines/<machine_id>/probe")
    def probe_machine(machine_id: str):
        try:
            machine = job_dispatcher.probe(machine_id)
        except KeyError:
            return jsonify({"error": "not_found", "message": "机器不存在"}), 404
        return jsonify(machine), 200 if machine.get("status") != "OFFLINE" else 502

    @app.post("/api/v1/jobs")
    def create_job_json():
        return _create_job(
            request.get_json(silent=True),
            store=master_store,
            dispatcher=job_dispatcher,
            config=master_config,
        )

    @app.post("/api/v1/jobs/from-file")
    def create_job_from_file():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            raise ValidationError("缺少上传字段 file")
        suffix = Path(uploaded.filename).suffix.lower()
        if suffix not in {".txt", ".tsv"}:
            raise ValidationError("file 只允许 .txt 或 .tsv")
        content = uploaded.stream.read()
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValidationError("上传文件必须使用 UTF-8 编码") from exc
        try:
            targets = json.loads(request.form.get("targets", "[]"))
        except json.JSONDecodeError as exc:
            raise ValidationError("targets 必须是 JSON 数组") from exc
        payload = {
            "job_id": request.form.get("job_id") or None,
            "name": request.form.get("name") or request.form.get("job_id") or "文件批量实验",
            "items": parse_uploaded_input(text, max_items=master_config.max_items),
            "targets": targets,
            "pcap": _form_bool("pcap", default=True),
            "analysis": {
                "steps": _form_list(
                    "analysis_steps", default=["extract", "classify", "infer"]
                ),
                "with_coframe": _form_bool("with_coframe", default=False),
                "sni_suffixes": _form_list("sni_suffixes", default=[]),
            },
        }
        return _create_job(
            payload,
            store=master_store,
            dispatcher=job_dispatcher,
            config=master_config,
            source_filename=uploaded.filename,
            source_content=content,
        )

    @app.get("/api/v1/jobs")
    def list_jobs():
        try:
            limit = min(500, max(1, int(request.args.get("limit", "100"))))
        except ValueError:
            raise ValidationError("limit 必须是整数")
        return jsonify({"jobs": [summarize_job(item) for item in master_store.list_jobs(limit=limit)]})

    @app.get("/api/v1/jobs/<job_id>")
    def get_job(job_id: str):
        job = master_store.get_job(job_id)
        if job is None:
            return jsonify({"error": "not_found", "message": "任务不存在"}), 404
        return jsonify(summarize_job(job))

    @app.post("/api/v1/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        job = master_store.get_job(job_id)
        if job is None:
            return jsonify({"error": "not_found", "message": "任务不存在"}), 404
        if job["status"] not in TERMINAL_STATUSES:
            job_dispatcher.cancel(job_id)
        return jsonify(summarize_job(master_store.get_job(job_id) or job))

    @app.get("/api/v1/jobs/<job_id>/results")
    def get_results(job_id: str):
        job = master_store.get_job(job_id)
        if job is None:
            return jsonify({"error": "not_found", "message": "任务不存在"}), 404
        return jsonify(summarize_job(job))

    @app.get("/api/v1/jobs/<job_id>/logs")
    def get_logs(job_id: str):
        job = master_store.get_job(job_id)
        if job is None:
            return jsonify({"error": "not_found", "message": "任务不存在"}), 404
        logs = []
        for target in summarize_job(job)["targets"]:
            machine = master_store.get_machine(target["machine_id"])
            if machine is None or not target.get("worker_task_id"):
                continue
            client = WorkerClient(
                machine["base_url"], machine["token"], timeout=master_config.worker_request_timeout
            )
            try:
                entry = client.get_log(target["worker_task_id"])
                logs.append({"machine_id": machine["machine_id"], "machine_name": machine["name"], **entry})
            except WorkerRequestError as exc:
                logs.append({"machine_id": machine["machine_id"], "machine_name": machine["name"], "text": "", "error": str(exc)})
        return jsonify({"job_id": job_id, "logs": logs})

    return app
