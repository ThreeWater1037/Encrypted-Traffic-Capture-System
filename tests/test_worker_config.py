from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from worker_agent.config import WorkerConfig


class WorkerConfigYamlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        (self.root / "wiki_fetcher.py").write_text("", encoding="utf-8")
        (self.root / "batch_process.py").write_text("", encoding="utf-8")
        self.config_path = self.root / "worker.yaml"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_config(self, token: str = '"yaml-token"') -> None:
        self.config_path.write_text(
            f"""
worker:
  id: yaml-worker
  host: 127.0.0.1
  port: 5110
  token: {token}
paths:
  project_root: .
  python_executable: {Path(sys.executable).as_posix()}
  data_dir: ./runtime
limits:
  max_queue_size: 3
  max_items: 250
  task_timeout_seconds: 90
  max_content_length: 4096
cors:
  allowed_origins:
    - http://localhost:5173
browsers:
  chrome_binary: ./portable/chrome.exe
""".strip(),
            encoding="utf-8",
        )

    def test_yaml_config_is_loaded_and_relative_paths_are_resolved(self) -> None:
        self.write_config()
        with patch.dict(
            os.environ,
            {"WORKER_CONFIG_FILE": str(self.config_path)},
            clear=True,
        ):
            config = WorkerConfig.from_env()
            self.assertEqual(config.worker_id, "yaml-worker")
            self.assertEqual(config.port, 5110)
            self.assertEqual(config.token, "yaml-token")
            self.assertEqual(config.project_root, self.root.resolve())
            self.assertEqual(config.data_dir, (self.root / "runtime").resolve())
            self.assertEqual(config.max_queue_size, 3)
            self.assertEqual(config.allowed_origins, ("http://localhost:5173",))
            self.assertEqual(
                os.environ["CHROME_BINARY"],
                str((self.root / "portable" / "chrome.exe").resolve()),
            )

    def test_environment_variables_override_yaml(self) -> None:
        self.write_config()
        with patch.dict(
            os.environ,
            {
                "WORKER_CONFIG_FILE": str(self.config_path),
                "WORKER_ID": "env-worker",
                "WORKER_PORT": "5120",
                "WORKER_TOKEN": "env-token",
                "MAX_QUEUE_SIZE": "8",
            },
            clear=True,
        ):
            config = WorkerConfig.from_env()
            self.assertEqual(config.worker_id, "env-worker")
            self.assertEqual(config.port, 5120)
            self.assertEqual(config.token, "env-token")
            self.assertEqual(config.max_queue_size, 8)

    def test_unquoted_numeric_token_is_rejected(self) -> None:
        self.write_config(token="111")
        with patch.dict(
            os.environ,
            {"WORKER_CONFIG_FILE": str(self.config_path)},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "worker.token"):
                WorkerConfig.from_env()

    def test_unknown_yaml_key_is_rejected(self) -> None:
        self.config_path.write_text(
            "worker:\n  id: worker\n  typo: value\n",
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"WORKER_CONFIG_FILE": str(self.config_path)},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "未知字段"):
                WorkerConfig.from_env()


if __name__ == "__main__":
    unittest.main()
