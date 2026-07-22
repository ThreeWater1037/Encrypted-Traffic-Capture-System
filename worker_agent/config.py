"""Worker 配置模型。

本模块只负责读取环境变量、计算本地目录并验证运行前提，不包含任务执行逻辑。
这样 API、队列和测试可以共享同一份确定的配置。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """读取整数环境变量，并在服务启动阶段尽早拒绝非法值。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise ValueError(f"{name} 必须大于等于 {minimum}")
    return value


@dataclass(frozen=True)
class WorkerConfig:
    """子机器 Worker 的不可变运行配置。"""
    worker_id: str
    host: str
    port: int
    token: str
    project_root: Path
    python_executable: Path
    data_dir: Path
    max_queue_size: int = 10
    max_items: int = 10_000
    task_timeout_seconds: int = 21_600
    max_content_length: int = 20_971_520
    allowed_browsers: tuple[str, ...] = ("chrome", "edge", "firefox")
    allowed_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )

    @property
    def tasks_dir(self) -> Path:
        """返回所有任务独立目录的共同根目录。"""
        return self.data_dir / "tasks"

    @property
    def database_path(self) -> Path:
        """返回仅保存任务元数据的 SQLite 文件路径。"""
        return self.data_dir / "worker.db"

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        """从环境变量构造配置，未设置的字段使用安全默认值。"""
        project_root = Path(
            os.getenv("PROJECT_ROOT", Path(__file__).resolve().parents[1])
        ).expanduser().resolve()
        python_executable = Path(
            os.getenv("PYTHON_EXECUTABLE", sys.executable)
        ).expanduser().resolve()
        data_dir = Path(
            os.getenv("WORKER_DATA_DIR", project_root / "worker_data")
        ).expanduser().resolve()

        return cls(
            worker_id=os.getenv("WORKER_ID", "worker-local"),
            host=os.getenv("WORKER_HOST", "0.0.0.0"),
            port=_env_int("WORKER_PORT", 5100),
            token=os.getenv("WORKER_TOKEN", "dev-worker-token"),
            project_root=project_root,
            python_executable=python_executable,
            data_dir=data_dir,
            max_queue_size=_env_int("MAX_QUEUE_SIZE", 10),
            max_items=_env_int("MAX_ITEMS", 10_000),
            task_timeout_seconds=_env_int("TASK_TIMEOUT_SECONDS", 21_600),
            max_content_length=_env_int("MAX_CONTENT_LENGTH", 20_971_520),
            allowed_origins=tuple(
                origin.strip()
                for origin in os.getenv(
                    "WORKER_ALLOWED_ORIGINS",
                    "http://localhost:5173,http://127.0.0.1:5173",
                ).split(",")
                if origin.strip()
            ),
        )

    def prepare(self) -> None:
        """创建数据目录，并检查解释器与核心脚本是否真实存在。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

        if not self.project_root.is_dir():
            raise RuntimeError(f"PROJECT_ROOT 不存在：{self.project_root}")
        if not self.python_executable.is_file():
            raise RuntimeError(
                f"PYTHON_EXECUTABLE 不存在：{self.python_executable}"
            )
        for script in ("wiki_fetcher.py", "batch_process.py"):
            if not (self.project_root / script).is_file():
                raise RuntimeError(f"项目脚本不存在：{self.project_root / script}")
