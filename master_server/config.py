"""主控服务配置。

默认读取项目根目录的 ``master.yaml``。环境变量继续兼容，并且优先级高于 YAML，
便于临时调试、系统服务部署和敏感参数注入。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "master.yaml"

_SCHEMA = {
    "server": {"host", "port", "token"},
    "paths": {"data_dir"},
    "limits": {
        "max_content_length",
        "max_items",
        "max_queue_size",
        "worker_request_timeout",
        "poll_interval",
    },
    "cors": {"allowed_origins"},
    "bootstrap_worker": {"enabled", "id", "name", "url", "token"},
}


def _config_path() -> Path | None:
    """返回显式配置文件，或自动发现项目根目录的 master.yaml。"""
    explicit = os.getenv("MASTER_CONFIG_FILE")
    if explicit:
        path = Path(os.path.expandvars(explicit)).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"MASTER_CONFIG_FILE 不存在：{path}")
        return path
    return DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.is_file() else None


def _load_yaml(path: Path | None) -> dict[str, dict[str, Any]]:
    """安全读取 YAML，并尽早拒绝拼错的分区和字段。"""
    if path is None:
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeError(f"无法读取主控 YAML 配置 {path}：{exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("主控 YAML 顶层必须是对象")

    unknown_sections = set(loaded) - set(_SCHEMA)
    if unknown_sections:
        names = ", ".join(sorted(unknown_sections))
        raise ValueError(f"主控 YAML 包含未知分区：{names}")

    result: dict[str, dict[str, Any]] = {}
    for section_name, section_value in loaded.items():
        if not isinstance(section_value, dict):
            raise ValueError(f"主控 YAML 的 {section_name} 必须是对象")
        unknown_keys = set(section_value) - _SCHEMA[section_name]
        if unknown_keys:
            names = ", ".join(sorted(unknown_keys))
            raise ValueError(
                f"主控 YAML 的 {section_name} 包含未知字段：{names}"
            )
        result[section_name] = section_value
    return result


def _value(
    settings: dict[str, dict[str, Any]],
    section: str,
    key: str,
    env_names: str | tuple[str, ...],
    default: Any,
) -> Any:
    """按 环境变量 > YAML > 默认值 的优先级取值。"""
    names = (env_names,) if isinstance(env_names, str) else env_names
    for env_name in names:
        env_value = os.getenv(env_name)
        if env_value is not None:
            return env_value
    yaml_value = settings.get(section, {}).get(key)
    return default if yaml_value is None else yaml_value


def _text(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串")
    text = value.strip()
    if not allow_empty and not text:
        raise ValueError(f"{name} 不能为空")
    return text


def _integer(
    value: Any,
    name: str,
    *,
    minimum: int = 1,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if parsed < minimum or (maximum is not None and parsed > maximum):
        if maximum is None:
            raise ValueError(f"{name} 必须大于等于 {minimum}")
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return parsed


def _number(value: Any, name: str, *, minimum: float = 0.1) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是数字")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if parsed < minimum:
        raise ValueError(f"{name} 必须大于等于 {minimum}")
    return parsed


def _boolean(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{name} 必须是 true 或 false")


def _path(value: Any, name: str, *, base_dir: Path) -> Path:
    text = _text(value, name)
    expanded = Path(os.path.expandvars(text)).expanduser()
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return expanded.resolve()


def _origins(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        values = value
    else:
        raise ValueError("cors.allowed_origins 必须是字符串数组或逗号分隔字符串")
    origins = tuple(item.strip() for item in values if item.strip())
    if not origins:
        raise ValueError("cors.allowed_origins 不能为空")
    return origins


@dataclass(frozen=True)
class MasterConfig:
    """主控不可变运行配置。"""

    host: str
    port: int
    token: str
    data_dir: Path
    max_content_length: int = 268_435_456
    max_items: int = 100_000
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
    config_file: Path | None = None

    @property
    def database_path(self) -> Path:
        return self.data_dir / "master.db"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @classmethod
    def from_env(cls) -> "MasterConfig":
        """读取 YAML 与环境变量；保留原方法名以兼容现有启动代码。"""
        config_file = _config_path()
        settings = _load_yaml(config_file)
        base_dir = config_file.parent if config_file else PROJECT_ROOT

        return cls(
            host=_text(
                _value(settings, "server", "host", "MASTER_HOST", "127.0.0.1"),
                "server.host",
            ),
            port=_integer(
                _value(settings, "server", "port", "MASTER_PORT", 5200),
                "server.port",
                maximum=65_535,
            ),
            token=_text(
                _value(settings, "server", "token", "MASTER_TOKEN", ""),
                "server.token",
                allow_empty=True,
            ),
            data_dir=_path(
                _value(
                    settings,
                    "paths",
                    "data_dir",
                    "MASTER_DATA_DIR",
                    str(PROJECT_ROOT / "master_data"),
                ),
                "paths.data_dir",
                base_dir=base_dir,
            ),
            max_content_length=_integer(
                _value(
                    settings,
                    "limits",
                    "max_content_length",
                    "MASTER_MAX_CONTENT_LENGTH",
                    268_435_456,
                ),
                "limits.max_content_length",
            ),
            max_items=_integer(
                _value(
                    settings,
                    "limits",
                    "max_items",
                    "MASTER_MAX_ITEMS",
                    100_000,
                ),
                "limits.max_items",
            ),
            max_queue_size=_integer(
                _value(
                    settings,
                    "limits",
                    "max_queue_size",
                    "MASTER_MAX_QUEUE_SIZE",
                    100,
                ),
                "limits.max_queue_size",
            ),
            worker_request_timeout=_number(
                _value(
                    settings,
                    "limits",
                    "worker_request_timeout",
                    "WORKER_REQUEST_TIMEOUT",
                    15.0,
                ),
                "limits.worker_request_timeout",
            ),
            poll_interval=_number(
                _value(
                    settings,
                    "limits",
                    "poll_interval",
                    "MASTER_POLL_INTERVAL",
                    2.0,
                ),
                "limits.poll_interval",
            ),
            allowed_origins=_origins(
                _value(
                    settings,
                    "cors",
                    "allowed_origins",
                    "MASTER_ALLOWED_ORIGINS",
                    ["http://localhost:5173", "http://127.0.0.1:5173"],
                )
            ),
            bootstrap_worker_enabled=_boolean(
                _value(
                    settings,
                    "bootstrap_worker",
                    "enabled",
                    "BOOTSTRAP_LOCAL_WORKER",
                    False,
                ),
                "bootstrap_worker.enabled",
            ),
            bootstrap_worker_id=_text(
                _value(
                    settings,
                    "bootstrap_worker",
                    "id",
                    "LOCAL_WORKER_ID",
                    "worker-local",
                ),
                "bootstrap_worker.id",
            ),
            bootstrap_worker_name=_text(
                _value(
                    settings,
                    "bootstrap_worker",
                    "name",
                    "LOCAL_WORKER_NAME",
                    "本机 Worker",
                ),
                "bootstrap_worker.name",
            ),
            bootstrap_worker_url=_text(
                _value(
                    settings,
                    "bootstrap_worker",
                    "url",
                    "LOCAL_WORKER_URL",
                    "http://127.0.0.1:5100",
                ),
                "bootstrap_worker.url",
            ),
            bootstrap_worker_token=_text(
                _value(
                    settings,
                    "bootstrap_worker",
                    "token",
                    ("LOCAL_WORKER_TOKEN", "WORKER_TOKEN"),
                    "dev-worker-token",
                ),
                "bootstrap_worker.token",
            ),
            config_file=config_file,
        )

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
