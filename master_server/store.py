"""主控 SQLite 仓库。

SQLite 只保存机器、任务和结构化结果；PCAP 等大文件仍留在 Worker 本地。
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELED", "INTERRUPTED"}
ACTIVE_STATUSES = {
    "CREATED",
    "DISPATCHING",
    "QUEUED",
    "PREPARING",
    "CAPTURING",
    "ANALYZING",
    "VALIDATING",
    "RUNNING",
    "WAITING_FOR_WORKER",
    "CANCELING",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MasterStore:
    """使用短连接实现线程安全的主控数据访问。"""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS machines (
                    machine_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL,
                    token TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'UNKNOWN',
                    last_seen_at TEXT,
                    last_error TEXT,
                    health_json TEXT,
                    capabilities_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    source_filename TEXT,
                    source_path TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    resume_token TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS executions (
                    execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    machine_id TEXT NOT NULL,
                    worker_task_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    browser TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    error TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(job_id, machine_id, item_id, browser),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id),
                    FOREIGN KEY(machine_id) REFERENCES machines(machine_id)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
                CREATE INDEX IF NOT EXISTS idx_executions_job ON executions(job_id);
                CREATE INDEX IF NOT EXISTS idx_executions_worker_task
                    ON executions(worker_task_id);
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "resume_token" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN resume_token TEXT")

    def machine_count(self) -> int:
        with self._connection() as connection:
            row = connection.execute("SELECT COUNT(*) FROM machines").fetchone()
        return int(row[0])

    def upsert_machine(self, machine: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO machines (
                    machine_id, name, base_url, token, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(machine_id) DO UPDATE SET
                    name = excluded.name,
                    base_url = excluded.base_url,
                    token = excluded.token,
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (
                    machine["machine_id"],
                    machine["name"],
                    machine["base_url"],
                    machine["token"],
                    int(machine["enabled"]),
                    now,
                    now,
                ),
            )
        return self.get_machine(machine["machine_id"]) or {}

    def list_machines(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM machines ORDER BY enabled DESC, name"
            ).fetchall()
        return [self._machine(row) for row in rows]

    def get_machine(self, machine_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM machines WHERE machine_id = ?", (machine_id,)
            ).fetchone()
        return self._machine(row) if row else None

    def update_machine_probe(
        self,
        machine_id: str,
        *,
        status: str,
        health: dict[str, Any] | None = None,
        capabilities: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE machines
                   SET status = ?, last_seen_at = ?, last_error = ?,
                       health_json = COALESCE(?, health_json),
                       capabilities_json = COALESCE(?, capabilities_json),
                       updated_at = ?
                 WHERE machine_id = ?
                """,
                (
                    status,
                    now if status in {"ONLINE", "BUSY"} else None,
                    error,
                    json.dumps(health, ensure_ascii=False) if health is not None else None,
                    json.dumps(capabilities, ensure_ascii=False)
                    if capabilities is not None
                    else None,
                    now,
                    machine_id,
                ),
            )

    def delete_machine(self, machine_id: str) -> bool:
        """删除没有实验记录引用的机器；有关联记录时由 SQLite 外键拒绝。"""
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM machines WHERE machine_id = ?",
                (machine_id,),
            )
        return cursor.rowcount == 1

    def create_job(
        self,
        request_data: dict[str, Any],
        *,
        source_filename: str | None = None,
        source_path: str | None = None,
    ) -> None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, name, status, stage, request_json,
                    source_filename, source_path, created_at, updated_at
                ) VALUES (?, ?, 'CREATED', 'CREATED', ?, ?, ?, ?, ?)
                """,
                (
                    request_data["job_id"],
                    request_data["name"],
                    json.dumps(request_data, ensure_ascii=False, sort_keys=True),
                    source_filename,
                    source_path,
                    now,
                    now,
                ),
            )
            worker_task_ids = {
                target["machine_id"]: self.worker_task_id(
                    request_data["job_id"], target["machine_id"]
                )
                for target in request_data["targets"]
            }
            rows = (
                (
                    request_data["job_id"],
                    target["machine_id"],
                    worker_task_ids[target["machine_id"]],
                    item["id"],
                    item["name"],
                    item["url"],
                    browser,
                    now,
                    now,
                )
                for target in request_data["targets"]
                for item in request_data["items"]
                for browser in target["browsers"]
            )
            connection.executemany(
                """
                INSERT INTO executions (
                    job_id, machine_id, worker_task_id,
                    item_id, item_name, url, browser,
                    status, stage, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CREATED', 'CREATED', ?, ?)
                """,
                rows,
            )

    @staticmethod
    def worker_task_id(job_id: str, machine_id: str) -> str:
        """生成稳定且符合 Worker 规则的幂等任务 ID。"""
        suffix = machine_id.replace(".", "-")
        digest = hashlib.sha1(machine_id.encode("utf-8")).hexdigest()[:8]
        return f"{job_id[:80]}-{suffix[:30]}-{digest}"

    def list_jobs(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT job_id, name, status, stage, error, cancel_requested,
                       resume_token, created_at, started_at, finished_at, updated_at
                  FROM jobs ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            job_ids = [str(row["job_id"]) for row in rows]
            counts: dict[str, dict[str, int]] = {job_id: {} for job_id in job_ids}
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                count_rows = connection.execute(
                    f"""
                    SELECT job_id, status, COUNT(*) AS count
                      FROM executions
                     WHERE job_id IN ({placeholders})
                     GROUP BY job_id, status
                    """,
                    job_ids,
                ).fetchall()
                for item in count_rows:
                    counts[str(item["job_id"])][str(item["status"])] = int(
                        item["count"]
                    )
        jobs = []
        for row in rows:
            job = dict(row)
            job["cancel_requested"] = bool(job["cancel_requested"])
            job["execution_counts"] = counts[str(job["job_id"])]
            jobs.append(job)
        return jobs

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._job_with_executions(row) if row else None

    def get_job_control(self, job_id: str) -> dict[str, Any] | None:
        """读取任务请求与控制状态，但不加载数万条 execution。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._job(row) if row else None

    def get_job_status(self, job_id: str) -> dict[str, Any] | None:
        """只读轮询控制字段，避免每次解析包含数万 URL 的 request_json。"""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT job_id, name, status, stage, error, cancel_requested,
                       created_at, started_at, finished_at, updated_at
                  FROM jobs WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["cancel_requested"] = bool(value["cancel_requested"])
        return value

    def get_job_page(
        self, job_id: str, *, offset: int = 0, limit: int = 500
    ) -> dict[str, Any] | None:
        """分页读取任务明细，避免前端轮询一次加载数万条 execution。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return None
            executions = connection.execute(
                """
                SELECT e.*, m.name AS machine_name, m.base_url AS machine_url
                  FROM executions e
                  JOIN machines m ON m.machine_id = e.machine_id
                 WHERE e.job_id = ?
                 ORDER BY e.execution_id
                 LIMIT ? OFFSET ?
                """,
                (job_id, limit, offset),
            ).fetchall()
            count_rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                  FROM executions WHERE job_id = ? GROUP BY status
                """,
                (job_id,),
            ).fetchall()
        value = dict(row)
        value.pop("request_json", None)
        value.pop("resume_token", None)
        value["cancel_requested"] = bool(value["cancel_requested"])
        value["executions"] = [self._execution(item) for item in executions]
        value["execution_counts"] = {
            str(item["status"]): int(item["count"]) for item in count_rows
        }
        value["page"] = {
            "offset": offset,
            "limit": limit,
            "returned": len(executions),
            "total": sum(value["execution_counts"].values()),
        }
        return value

    def execution_status_counts(self, job_id: str) -> dict[str, int]:
        """在 SQLite 内聚合执行状态，避免轮询时加载全部执行明细。"""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM executions WHERE job_id = ? GROUP BY status",
                (job_id,),
            ).fetchall()
        return {str(row[0]): int(row[1]) for row in rows}

    def _job_with_executions(self, row: sqlite3.Row) -> dict[str, Any]:
        job = self._job(row)
        with self._connection() as connection:
            executions = connection.execute(
                """
                SELECT e.*, m.name AS machine_name, m.base_url AS machine_url
                  FROM executions e
                  JOIN machines m ON m.machine_id = e.machine_id
                 WHERE e.job_id = ?
                 ORDER BY e.item_id, e.machine_id, e.browser
                """,
                (job["job_id"],),
            ).fetchall()
        job["executions"] = [self._execution(item) for item in executions]
        return job

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "stage",
            "error",
            "cancel_requested",
            "started_at",
            "finished_at",
            "resume_token",
        }
        if set(fields) - allowed:
            raise ValueError("包含不允许更新的任务字段")
        if not fields:
            return
        values = dict(fields)
        if isinstance(values.get("cancel_requested"), bool):
            values["cancel_requested"] = int(values["cancel_requested"])
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connection() as connection:
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE job_id = ?",
                (*values.values(), job_id),
            )

    def resume_job(self, job_id: str, resume_token: str) -> bool:
        """原子重置终态任务和执行矩阵，保留原请求供检查点续跑。"""
        now = utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                   SET status = 'CREATED', stage = 'RESUMING', error = NULL,
                       cancel_requested = 0, finished_at = NULL,
                       resume_token = ?, updated_at = ?
                 WHERE job_id = ?
                   AND status IN ('PARTIAL','FAILED','CANCELED','INTERRUPTED')
                """,
                (resume_token, now, job_id),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE executions
                   SET status = 'RESUMING', stage = 'RESUMING',
                       error = NULL, result_json = NULL, updated_at = ?
                 WHERE job_id = ?
                """,
                (now, job_id),
            )
            return True

    def update_worker_executions(
        self,
        job_id: str,
        machine_id: str,
        *,
        status: str,
        stage: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE executions
                   SET status = ?, stage = ?, error = ?, updated_at = ?
                 WHERE job_id = ? AND machine_id = ?
                """,
                (status, stage or status, error, utc_now(), job_id, machine_id),
            )

    def update_execution_result(
        self,
        job_id: str,
        machine_id: str,
        item_id: str,
        browser: str,
        *,
        status: str,
        result: dict[str, Any] | None,
        error: str | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE executions
                   SET status = ?, stage = 'DONE', error = ?, result_json = ?, updated_at = ?
                 WHERE job_id = ? AND machine_id = ? AND item_id = ? AND browser = ?
                """,
                (
                    status,
                    error,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    utc_now(),
                    job_id,
                    machine_id,
                    item_id,
                    browser,
                ),
            )

    def update_worker_results(
        self,
        job_id: str,
        machine_id: str,
        *,
        top_status: str,
        units: list[dict[str, Any]],
        error: str | None,
    ) -> None:
        """在单事务中写入一个 Worker 的全部 URL 结果。"""
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE executions
                   SET status = ?, stage = 'DONE', error = ?,
                       result_json = NULL, updated_at = ?
                 WHERE job_id = ? AND machine_id = ?
                """,
                (top_status, error, now, job_id, machine_id),
            )
            rows = []
            for unit in units:
                item_id = unit.get("item_id")
                browser = unit.get("browser")
                if not isinstance(item_id, str) or not isinstance(browser, str):
                    continue
                status = str(unit.get("status") or top_status)
                if status not in TERMINAL_STATUSES:
                    status = "FAILED"
                rows.append(
                    (
                        status,
                        json.dumps(unit, ensure_ascii=False),
                        now,
                        job_id,
                        machine_id,
                        item_id,
                        browser,
                    )
                )
            connection.executemany(
                """
                UPDATE executions
                   SET status = ?, stage = 'DONE', error = NULL,
                       result_json = ?, updated_at = ?
                 WHERE job_id = ? AND machine_id = ?
                   AND item_id = ? AND browser = ?
                """,
                rows,
            )

    def request_cancel(self, job_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET cancel_requested = 1, status = 'CANCELING',
                                stage = 'CANCELING', updated_at = ?
                 WHERE job_id = ?
                   AND status NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELED','INTERRUPTED')
                """,
                (utc_now(), job_id),
            )
        return cursor.rowcount == 1

    def incomplete_job_ids(self) -> list[str]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT job_id FROM jobs WHERE status IN ({placeholders})",
                tuple(sorted(ACTIVE_STATUSES)),
            ).fetchall()
        return [str(row[0]) for row in rows]

    @staticmethod
    def _machine(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["enabled"] = bool(value["enabled"])
        for key in ("health_json", "capabilities_json"):
            raw = value.pop(key)
            value[key.removesuffix("_json")] = json.loads(raw) if raw else None
        return value

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["cancel_requested"] = bool(value["cancel_requested"])
        value["request"] = json.loads(value.pop("request_json"))
        return value

    @staticmethod
    def _execution(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        raw = value.pop("result_json")
        value["result"] = json.loads(raw) if raw else None
        return value
