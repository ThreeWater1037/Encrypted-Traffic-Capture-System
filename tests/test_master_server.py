from __future__ import annotations

import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from master_server.app import create_app
from master_server.config import MasterConfig
from master_server.dispatcher import JobDispatcher
from master_server.store import MasterStore


class FakeWorkerClient:
    def __init__(self) -> None:
        self.task_id = ""

    def health(self):
        return {"status": "ok", "worker_id": "worker-local", "busy": False}

    def capabilities(self):
        return {
            "worker_id": "worker-local",
            "os": "windows",
            "browsers": [
                {"name": "chrome", "no_cache_verified": True},
                {"name": "edge", "no_cache_verified": True},
            ],
        }

    def submit_task(self, payload):
        self.task_id = payload["task_id"]
        return {"task_id": self.task_id, "status": "QUEUED", "stage": "QUEUED"}

    def get_task(self, task_id):
        return {
            "task_id": task_id,
            "status": "PARTIAL",
            "stage": "DONE",
            "result": {
                "units": [
                    {
                        "item_id": "1",
                        "browser": "chrome",
                        "status": "SUCCEEDED",
                        "artifacts": {"pcap": {"path": "fetch_output/1/capture.pcap"}},
                    },
                    {"item_id": "2", "browser": "chrome", "status": "FAILED"},
                ]
            },
        }

    def cancel_task(self, task_id):
        return {"task_id": task_id, "status": "CANCELED", "stage": "DONE"}


class MasterServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config = MasterConfig(
            host="127.0.0.1",
            port=5200,
            token="",
            data_dir=Path(self.temp_dir.name),
            poll_interval=0.01,
            bootstrap_worker_enabled=True,
            bootstrap_worker_token="test-worker-token",
        )
        self.config.prepare()
        self.store = MasterStore(self.config.database_path)
        self.dispatcher = JobDispatcher(
            self.config,
            self.store,
            client_factory=lambda _machine: FakeWorkerClient(),
            autostart=False,
        )
        self.app = create_app(
            self.config, store=self.store, dispatcher=self.dispatcher
        )
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.dispatcher.shutdown()
        self.temp_dir.cleanup()

    @staticmethod
    def payload(job_id="job-test-001"):
        return {
            "job_id": job_id,
            "name": "双 URL 本机实验",
            "items": [
                {"id": "1", "name": "Example", "url": "https://example.com/"},
                {"id": "2", "name": "ExampleOrg", "url": "https://example.org/"},
            ],
            "targets": [{"machine_id": "worker-local", "browsers": ["chrome"]}],
            "pcap": True,
            "analysis": {"steps": ["extract", "classify", "infer"]},
        }

    def test_health_disables_cache(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["cache_enabled"])
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_machine_api_never_returns_worker_token(self):
        response = self.client.get("/api/v1/machines")
        machine = response.get_json()["machines"][0]
        self.assertNotIn("token", machine)
        self.assertTrue(machine["token_configured"])

    def test_update_machine_without_token_preserves_existing_secret(self):
        response = self.client.post(
            "/api/v1/machines",
            json={
                "machine_id": "worker-local",
                "name": "更新后的 Worker",
                "base_url": "http://127.0.0.1:5101",
                "enabled": False,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("token", response.get_json())
        stored = self.store.get_machine("worker-local")
        self.assertEqual(stored["token"], "test-worker-token")
        self.assertEqual(stored["name"], "更新后的 Worker")
        self.assertEqual(stored["base_url"], "http://127.0.0.1:5101")
        self.assertFalse(stored["enabled"])

    def test_delete_unused_machine(self):
        created = self.client.post(
            "/api/v1/machines",
            json={
                "machine_id": "unused-worker",
                "name": "待删除 Worker",
                "base_url": "http://127.0.0.1:5199",
                "token": "unused-token",
            },
        )
        self.assertEqual(created.status_code, 201)

        response = self.client.delete("/api/v1/machines/unused-worker")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["deleted"])
        self.assertIsNone(self.store.get_machine("unused-worker"))

    def test_deleted_local_machine_is_not_recreated_when_bootstrap_is_disabled(self):
        self.store.delete_machine("worker-local")
        disabled_config = replace(
            self.config,
            bootstrap_worker_enabled=False,
        )

        create_app(
            disabled_config,
            store=self.store,
            dispatcher=self.dispatcher,
        )

        self.assertIsNone(self.store.get_machine("worker-local"))

    def test_delete_machine_with_experiments_is_rejected(self):
        self.store.create_job(self.payload("machine-delete-guard-001"))

        response = self.client.delete("/api/v1/machines/worker-local")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["error"], "machine_in_use")
        self.assertIsNotNone(self.store.get_machine("worker-local"))

    def test_probe_records_capabilities(self):
        response = self.client.post("/api/v1/machines/worker-local/probe")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ONLINE")
        self.assertEqual(response.get_json()["capabilities"]["browsers"][1]["name"], "edge")

    def test_create_job_expands_url_machine_browser_executions(self):
        response = self.client.post("/api/v1/jobs", json=self.payload())
        self.assertEqual(response.status_code, 202)
        data = response.get_json()
        self.assertEqual(data["status"], "CREATED")
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(len(data["items"]), 2)
        self.assertNotIn("source_path", data)

    def test_file_upload_is_saved_under_master_data(self):
        content = b"1\tExample\thttps://example.com/\n"
        response = self.client.post(
            "/api/v1/jobs/from-file",
            data={
                "job_id": "upload-master-001",
                "name": "文件任务",
                "targets": '[{"machine_id":"worker-local","browsers":["chrome"]}]',
                "file": (io.BytesIO(content), "urls.txt"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 202)
        job = self.store.get_job("upload-master-001")
        self.assertEqual(job["source_filename"], "urls.txt")
        self.assertTrue((self.config.uploads_dir / "upload-master-001" / "urls.txt").is_file())

    def test_dispatcher_aggregates_each_url_result(self):
        self.store.create_job(self.payload("dispatch-test-001"))
        self.dispatcher._run_job("dispatch-test-001")
        job = self.client.get("/api/v1/jobs/dispatch-test-001").get_json()
        self.assertEqual(job["status"], "PARTIAL")
        self.assertEqual(job["items"][0]["status"], "SUCCEEDED")
        self.assertEqual(job["items"][1]["status"], "FAILED")
        self.assertEqual(job["summary"]["progress"], 100)

    def test_unknown_machine_is_rejected_before_job_creation(self):
        payload = self.payload("unknown-machine-001")
        payload["targets"][0]["machine_id"] = "missing"
        response = self.client.post("/api/v1/jobs", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("不存在", response.get_json()["message"])


if __name__ == "__main__":
    unittest.main()
