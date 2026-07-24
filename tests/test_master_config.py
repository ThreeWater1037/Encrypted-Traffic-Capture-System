from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from master_server.config import MasterConfig


class MasterConfigYamlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "master.yaml"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_config(self, server_token: str = '"master-yaml-token"') -> None:
        self.config_path.write_text(
            f"""
server:
  host: 0.0.0.0
  port: 5220
  token: {server_token}
paths:
  data_dir: ./runtime
limits:
  max_content_length: 8192
  max_items: 300
  max_queue_size: 7
  worker_request_timeout: 9
  poll_interval: 0.5
cors:
  allowed_origins:
    - http://localhost:5173
bootstrap_worker:
  enabled: true
  id: yaml-worker
  name: YAML Worker
  url: http://127.0.0.1:5110
  token: "worker-yaml-token"
""".strip(),
            encoding="utf-8",
        )

    def test_yaml_config_is_loaded_and_relative_path_is_resolved(self) -> None:
        self.write_config()
        with patch.dict(
            os.environ,
            {"MASTER_CONFIG_FILE": str(self.config_path)},
            clear=True,
        ):
            config = MasterConfig.from_env()

        self.assertEqual(config.host, "0.0.0.0")
        self.assertEqual(config.port, 5220)
        self.assertEqual(config.token, "master-yaml-token")
        self.assertEqual(config.data_dir, (self.root / "runtime").resolve())
        self.assertEqual(config.max_queue_size, 7)
        self.assertEqual(config.worker_request_timeout, 9.0)
        self.assertEqual(config.poll_interval, 0.5)
        self.assertEqual(config.allowed_origins, ("http://localhost:5173",))
        self.assertTrue(config.bootstrap_worker_enabled)
        self.assertEqual(config.bootstrap_worker_id, "yaml-worker")
        self.assertEqual(config.bootstrap_worker_token, "worker-yaml-token")
        self.assertEqual(config.config_file, self.config_path.resolve())

    def test_environment_variables_override_yaml(self) -> None:
        self.write_config()
        with patch.dict(
            os.environ,
            {
                "MASTER_CONFIG_FILE": str(self.config_path),
                "MASTER_HOST": "127.0.0.1",
                "MASTER_PORT": "5230",
                "MASTER_DATA_DIR": str(self.root / "env-data"),
                "BOOTSTRAP_LOCAL_WORKER": "false",
                "LOCAL_WORKER_TOKEN": "worker-env-token",
            },
            clear=True,
        ):
            config = MasterConfig.from_env()

        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 5230)
        self.assertEqual(config.data_dir, (self.root / "env-data").resolve())
        self.assertFalse(config.bootstrap_worker_enabled)
        self.assertEqual(config.bootstrap_worker_token, "worker-env-token")

    def test_unquoted_numeric_token_is_rejected(self) -> None:
        self.write_config(server_token="111")
        with patch.dict(
            os.environ,
            {"MASTER_CONFIG_FILE": str(self.config_path)},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "server.token"):
                MasterConfig.from_env()

    def test_unknown_yaml_key_is_rejected(self) -> None:
        self.config_path.write_text(
            "server:\n  host: 127.0.0.1\n  typo: value\n",
            encoding="utf-8",
        )
        with patch.dict(
            os.environ,
            {"MASTER_CONFIG_FILE": str(self.config_path)},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "未知字段"):
                MasterConfig.from_env()


if __name__ == "__main__":
    unittest.main()
