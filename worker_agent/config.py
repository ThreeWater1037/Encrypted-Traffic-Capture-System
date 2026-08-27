"""Worker 配置模型。

默认读取项目根目录的 ``worker.yaml``。环境变量仍然兼容，并且优先级高于 YAML，
方便临时调试、容器部署和系统服务注入敏感参数。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from browser_proxy import parse_browser_proxy


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "worker.yaml"

_SCHEMA = {
    "worker": {"id", "host", "port", "token"},
    "paths": {"project_root", "python_executable", "data_dir"},
    "limits": {
        "max_queue_size",
        "max_items",
        "task_timeout_seconds",
        "max_content_length",
    },
    "cors": {"allowed_origins"},
    "browsers": {"chrome_binary", "edge_binary", "firefox_binary"},
    "network": {"proxy_url"},
}


def _config_path() -> Path | None:
    """返回显式配置文件，或自动发现项目根目录的 worker.yaml。"""
    explicit = os.getenv("WORKER_CONFIG_FILE")
    if explicit:
        path = Path(os.path.expandvars(explicit)).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"WORKER_CONFIG_FILE 不存在：{path}")
        return path
    return DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.is_file() else None


def _load_yaml(path: Path | None) -> dict[str, dict[str, Any]]:
    """安全读取并校验 YAML 的分区和字段名称。"""
    if path is None:
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise RuntimeError(f"无法读取 Worker YAML 配置 {path}：{exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("Worker YAML 顶层必须是对象")

    unknown_sections = set(loaded) - set(_SCHEMA)
    if unknown_sections:
        raise ValueError(f"Worker YAML 包含未知分区：{', '.join(sorted(unknown_sections))}")

    result: dict[str, dict[str, Any]] = {}
    for section_name, section_value in loaded.items():
        if not isinstance(section_value, dict):
            raise ValueError(f"Worker YAML 的 {section_name} 必须是对象")
        unknown_keys = set(section_value) - _SCHEMA[section_name]
        if unknown_keys:
            names = ", ".join(sorted(unknown_keys))
            raise ValueError(f"Worker YAML 的 {section_name} 包含未知字段：{names}")
        result[section_name] = section_value
    return result


def _value(
    settings: dict[str, dict[str, Any]],
    section: str,
    key: str,
    env_name: str,
    default: Any,
) -> Any:
    """按 环境变量 > YAML > 默认值 的优先级取值。"""
    env_value = os.getenv(env_name)
    if env_value is not None:
        return env_value
    yaml_value = settings.get(section, {}).get(key)
    return default if yaml_value is None else yaml_value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _integer(value: Any, name: str, *, minimum: int = 1, maximum: int | None = None) -> int:
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


def _apply_browser_paths(settings: dict[str, dict[str, Any]], *, base_dir: Path) -> None:
    """把 YAML 中的浏览器路径传给能力探测和采集子进程。"""
    mapping = {
        "chrome_binary": "CHROME_BINARY",
        "edge_binary": "EDGE_BINARY",
        "firefox_binary": "FIREFOX_BINARY",
    }
    for yaml_name, env_name in mapping.items():
        if os.getenv(env_name):
            continue
        raw = settings.get("browsers", {}).get(yaml_name)
        if raw is None or raw == "":
            continue
        os.environ[env_name] = str(_path(raw, f"browsers.{yaml_name}", base_dir=base_dir))


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
    max_items: int = 100_000
    task_timeout_seconds: int = 0
    max_content_length: int = 268_435_456
    allowed_browsers: tuple[str, ...] = ("chrome", "edge", "firefox")
    allowed_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    proxy_url: str | None = None
    config_file: Path | None = None

    @property
    def tasks_dir(self) -> Path:
        return self.data_dir / "tasks"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "worker.db"

    @classmethod
    def from_env(cls) -> "WorkerConfig":
        """读取 YAML 与环境变量；保留原方法名以兼容现有启动代码。"""
        config_file = _config_path()
        settings = _load_yaml(config_file)
        base_dir = config_file.parent if config_file else PROJECT_ROOT

        project_root = _path(
            _value(settings, "paths", "project_root", "PROJECT_ROOT", str(PROJECT_ROOT)),
            "paths.project_root",
            base_dir=base_dir,
        )
        python_executable = _path(
            _value(
                settings,
                "paths",
                "python_executable",
                "PYTHON_EXECUTABLE",
                sys.executable,
            ),
            "paths.python_executable",
            base_dir=base_dir,
        )
        data_dir = _path(
            _value(
                settings,
                "paths",
                "data_dir",
                "WORKER_DATA_DIR",
                str(project_root / "worker_data"),
            ),
            "paths.data_dir",
            base_dir=base_dir,
        )
        _apply_browser_paths(settings, base_dir=base_dir)
        proxy = parse_browser_proxy(
            _value(
                settings,
                "network",
                "proxy_url",
                "WORKER_PROXY_URL",
                None,
            )
        )

        return cls(
            worker_id=_text(
                _value(settings, "worker", "id", "WORKER_ID", "worker-local"),
                "worker.id",
            ),
            host=_text(
                _value(settings, "worker", "host", "WORKER_HOST", "0.0.0.0"),
                "worker.host",
            ),
            port=_integer(
                _value(settings, "worker", "port", "WORKER_PORT", 5100),
                "worker.port",
                maximum=65_535,
            ),
            token=_text(
                _value(
                    settings,
                    "worker",
                    "token",
                    "WORKER_TOKEN",
                    "dev-worker-token",
                ),
                "worker.token",
            ),
            project_root=project_root,
            python_executable=python_executable,
            data_dir=data_dir,
            max_queue_size=_integer(
                _value(settings, "limits", "max_queue_size", "MAX_QUEUE_SIZE", 10),
                "limits.max_queue_size",
            ),
            max_items=_integer(
                _value(settings, "limits", "max_items", "MAX_ITEMS", 100_000),
                "limits.max_items",
            ),
            task_timeout_seconds=_integer(
                _value(
                    settings,
                    "limits",
                    "task_timeout_seconds",
                    "TASK_TIMEOUT_SECONDS",
                    0,
                ),
                "limits.task_timeout_seconds",
                minimum=0,
            ),
            max_content_length=_integer(
                _value(
                    settings,
                    "limits",
                    "max_content_length",
                    "MAX_CONTENT_LENGTH",
                    268_435_456,
                ),
                "limits.max_content_length",
            ),
            allowed_origins=_origins(
                _value(
                    settings,
                    "cors",
                    "allowed_origins",
                    "WORKER_ALLOWED_ORIGINS",
                    ["http://localhost:5173", "http://127.0.0.1:5173"],
                )
            ),
            proxy_url=proxy.url if proxy else None,
            config_file=config_file,
        )

    def prepare(self) -> None:
        """创建数据目录，并检查解释器与核心脚本是否真实存在。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

        if not self.project_root.is_dir():
            raise RuntimeError(f"PROJECT_ROOT 不存在：{self.project_root}")
        if not self.python_executable.is_file():
            raise RuntimeError(f"PYTHON_EXECUTABLE 不存在：{self.python_executable}")
        for script in ("wiki_fetcher.py", "batch_process.py"):
            if not (self.project_root / script).is_file():
                raise RuntimeError(f"项目脚本不存在：{self.project_root / script}")
