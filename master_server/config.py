"""主控服务配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
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


def _env_float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if value < minimum:
        raise ValueError(f"{name} 必须大于等于 {minimum}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔环境变量，避免把任意非空字符串都误判为启用。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true 或 false")


@dataclass(frozen=True)
class MasterConfig:
    """主控不可变运行配置。"""

    host: str
    port: int
    token: str
    data_dir: Path
    max_content_length: int = 20_971_520
    max_items: int = 10_000
    max_queue_size: int = 100
    worker_request_timeout: float = 15.0
    poll_interval: float = 2.0
    allowed_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    bootstrap_worker_enabled: bool = False
    bootstrap_worker_id: str = "worker-local"
    bootstrap_worker_name: str = "本机 Worker"
    bootstrap_worker_url: str = "http://127.0.0.1:5100"
    bootstrap_worker_token: str = "dev-worker-token"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "master.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @classmethod
    def from_env(cls) -> "MasterConfig":
        project_root = Path(__file__).resolve().parents[1]
        return cls(
            host=os.getenv("MASTER_HOST", "127.0.0.1"),
            port=_env_int("MASTER_PORT", 5200),
            token=os.getenv("MASTER_TOKEN", ""),
            data_dir=Path(
                os.getenv("MASTER_DATA_DIR", project_root / "master_data")
            ).expanduser().resolve(),
            max_content_length=_env_int("MASTER_MAX_CONTENT_LENGTH", 20_971_520),
            max_items=_env_int("MASTER_MAX_ITEMS", 10_000),
            max_queue_size=_env_int("MASTER_MAX_QUEUE_SIZE", 100),
            worker_request_timeout=_env_float("WORKER_REQUEST_TIMEOUT", 15.0),
            poll_interval=_env_float("MASTER_POLL_INTERVAL", 2.0),
            allowed_origins=tuple(
                value.strip()
                for value in os.getenv(
                    "MASTER_ALLOWED_ORIGINS",
                    "http://localhost:5173,http://127.0.0.1:5173",
                ).split(",")
                if value.strip()
            ),
            bootstrap_worker_enabled=_env_bool("BOOTSTRAP_LOCAL_WORKER", False),
            bootstrap_worker_id=os.getenv("LOCAL_WORKER_ID", "worker-local"),
            bootstrap_worker_name=os.getenv("LOCAL_WORKER_NAME", "本机 Worker"),
            bootstrap_worker_url=os.getenv(
                "LOCAL_WORKER_URL", "http://127.0.0.1:5100"
            ),
            bootstrap_worker_token=os.getenv(
                "LOCAL_WORKER_TOKEN", os.getenv("WORKER_TOKEN", "dev-worker-token")
            ),
        )

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
