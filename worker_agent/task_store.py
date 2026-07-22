"""Worker 的 SQLite 任务状态仓库。

数据库只保存请求、状态和结果清单；PCAP、HTML 等大文件始终位于任务目录，
避免把大对象写入 SQLite。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TERMINAL_STATUSES = {
    "SUCCEEDED",
    "PARTIAL",
    "FAILED",
    "CANCELED",
    "INTERRUPTED",
}
ACTIVE_STATUSES = {
    "QUEUED",
    "PREPARING",
    "CAPTURING",
    "ANALYZING",
    "VALIDATING",
    "CANCELING",
}


def utc_now() -> str:
    """生成带时区的 UTC 时间，便于多机器统一排序。"""
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """封装任务表的创建、查询和原子状态更新。"""
    _UPDATABLE_FIELDS = {
        "status",
        "stage",
        "result_json",
        "error",
        "pid",
        "started_at",
        "finished_at",
        "cancel_requested",
    }

    def __init__(self, database_path: Path):
        """初始化数据库路径并确保表结构存在。"""
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        """创建短生命周期连接，并启用 WAL 提升读写并发安全性。"""
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _connection(self):
        """统一提交、回滚和关闭连接，避免 Windows 文件句柄泄漏。"""
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
        """幂等创建任务表和状态索引。"""
        with self._connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    pid INTEGER,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
            )

    def mark_incomplete_interrupted(self) -> int:
        """服务重启时把上次未完成的任务标记为中断，禁止误报成功。"""
        now = utc_now()
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._connection() as connection:
            cursor = connection.execute(
                f"""
                UPDATE tasks
                   SET status = 'INTERRUPTED',
                       stage = 'INTERRUPTED',
                       error = COALESCE(error, 'Worker 服务重启，未完成任务已中断'),
                       pid = NULL,
                       finished_at = ?,
                       updated_at = ?
                 WHERE status IN ({placeholders})
                """,
                (now, now, *sorted(ACTIVE_STATUSES)),
            )
            return cursor.rowcount

    def create_task(self, request_data: dict[str, Any]) -> bool:
        """插入 QUEUED 任务；task_id 已存在时返回 False。"""
        now = utc_now()
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id, status, stage, request_json,
                        created_at, updated_at
                    ) VALUES (?, 'QUEUED', 'QUEUED', ?, ?, ?)
                    """,
                    (
                        request_data["task_id"],
                        json.dumps(request_data, ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        """按 task_id 读取任务，并还原 JSON 与布尔字段。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def update_task(self, task_id: str, **fields: Any) -> None:
        """只允许更新白名单字段，防止动态 SQL 写入任意列。"""
        unknown = set(fields) - self._UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"不允许更新任务字段：{', '.join(sorted(unknown))}")
        if not fields:
            return

        normalized = dict(fields)
        if isinstance(normalized.get("result_json"), (dict, list)):
            normalized["result_json"] = json.dumps(
                normalized["result_json"], ensure_ascii=False, sort_keys=True
            )
        if isinstance(normalized.get("cancel_requested"), bool):
            normalized["cancel_requested"] = int(normalized["cancel_requested"])
        normalized["updated_at"] = utc_now()

        assignments = ", ".join(f"{key} = ?" for key in normalized)
        values = list(normalized.values()) + [task_id]
        with self._connection() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {assignments} WHERE task_id = ?", values
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)

    def request_cancel(self, task_id: str) -> bool:
        """为非终态任务原子设置取消标记。"""
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                   SET cancel_requested = 1, updated_at = ?
                 WHERE task_id = ?
                   AND status NOT IN ('SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED')
                """,
                (utc_now(), task_id),
            )
            return cursor.rowcount == 1

    def is_cancel_requested(self, task_id: str) -> bool:
        """供执行线程轮询当前任务是否收到取消请求。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT cancel_requested FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        return bool(row and row[0])

    def status_counts(self) -> dict[str, int]:
        """按状态统计任务数量，供健康检查展示。"""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """把 SQLite 行转换为 API 和执行器使用的 Python 字典。"""
        result = dict(row)
        result["cancel_requested"] = bool(result["cancel_requested"])
        result["request"] = json.loads(result.pop("request_json"))
        raw_result = result.pop("result_json")
        result["result"] = json.loads(raw_result) if raw_result else None
        return result
