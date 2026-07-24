from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

from worker_agent.app import create_app
from worker_agent.config import WorkerConfig
from worker_agent.task_runner import TaskManager
from worker_agent.task_store import TaskStore


class WorkerAgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.project_root = Path(__file__).resolve().parents[1]
        self.config = WorkerConfig(
            worker_id="test-worker",
            host="127.0.0.1",
            port=5100,
            token="test-token",
            project_root=self.project_root,
            python_executable=Path(sys.executable),
            data_dir=Path(self.temp_dir.name),
            max_queue_size=10,
            max_items=100,
            task_timeout_seconds=30,
        )
        self.config.prepare()
        self.store = TaskStore(self.config.database_path)
        self.manager = TaskManager(self.config, self.store, autostart=False)
        self.app = create_app(
            self.config,
            store=self.store,
            manager=self.manager,
        )
        self.app.testing = True
        self.client = self.app.test_client()
        self.auth = {"Authorization": "Bearer test-token"}

    def tearDown(self) -> None:
        self.manager.shutdown()
        self.temp_dir.cleanup()

    @staticmethod
    def payload(task_id: str = "exec-test-001") -> dict:
        return {
            "task_id": task_id,
            "items": [
                {
                    "id": "1",
                    "name": "Example",
                    "url": "https://example.com/",
                }
            ],
            "browsers": ["chrome"],
            "pcap": True,
            "analysis": {
                "steps": ["extract", "classify", "infer"],
                "with_coframe": False,
                "sni_suffixes": ["example.com"],
            },
        }

    def test_health_is_public_and_disables_http_cache(self) -> None:
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["worker_id"], "test-worker")
        self.assertEqual(response.get_json()["cache_enabled"], False)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["Pragma"], "no-cache")

    def test_internal_endpoints_require_token(self) -> None:
        response = self.client.get("/api/v1/capabilities")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()["error"], "unauthorized")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_capabilities_report_browser_proxy_configuration(self) -> None:
        response = self.client.get("/api/v1/capabilities", headers=self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["network"]["browser_proxy_configured"],
            False,
        )

    def test_submit_is_queued_and_idempotent(self) -> None:
        first = self.client.post(
            "/api/v1/tasks", json=self.payload(), headers=self.auth
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.get_json()["status"], "QUEUED")
        self.assertEqual(first.get_json()["duplicate"], False)

        second = self.client.post(
            "/api/v1/tasks", json=self.payload(), headers=self.auth
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["duplicate"], True)
        self.assertEqual(self.manager.queue_size, 1)

    def test_status_endpoint_adds_per_url_status_to_legacy_result(self) -> None:
        payload = self.payload("legacy-result-001")
        self.store.create_task(payload)
        self.store.update_task(
            payload["task_id"],
            status="PARTIAL",
            stage="DONE",
            result_json={
                "status": "PARTIAL",
                "units": [
                    {
                        "item_id": "1",
                        "name": "Example",
                        "url": "https://example.com/",
                        "browser": "chrome",
                        "status": "PARTIAL",
                    }
                ],
            },
        )

        response = self.client.get(
            "/api/v1/tasks/legacy-result-001", headers=self.auth
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0]["item_id"], "1")
        self.assertEqual(response.get_json()["items"][0]["status"], "PARTIAL")
        self.assertEqual(
            response.get_json()["result"]["item_summary"]["partial"], 1
        )

    def test_upload_txt_creates_multi_url_task(self) -> None:
        content = (
            "1\tExample\thttps://example.com/\n"
            "2\tExampleOrg\thttps://example.org/\n"
        ).encode("utf-8")
        response = self.client.post(
            "/api/v1/tasks/from-file",
            data={
                "task_id": "upload-test-001",
                "browsers": "chrome,firefox",
                "pcap": "true",
                "analysis_steps": "extract,classify,infer",
                "with_coframe": "false",
                "sni_suffixes": "",
                "file": (io.BytesIO(content), "urls.txt"),
            },
            headers=self.auth,
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 202)
        data = response.get_json()
        self.assertEqual(data["item_count"], 2)
        self.assertEqual(data["source_filename"], "urls.txt")
        self.assertEqual(data["request"]["browsers"], ["chrome", "firefox"])
        self.assertEqual(len(data["request"]["items"]), 2)

    def test_upload_txt_rejects_malformed_line(self) -> None:
        response = self.client.post(
            "/api/v1/tasks/from-file",
            data={
                "task_id": "upload-invalid-001",
                "file": (io.BytesIO(b"https://example.com/\n"), "urls.txt"),
            },
            headers=self.auth,
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("ID<TAB>", response.get_json()["message"])

    def test_upload_preflight_allows_configured_frontend_origin(self) -> None:
        response = self.client.options(
            "/api/v1/tasks/from-file",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Access-Control-Allow-Origin"],
            "http://localhost:5173",
        )

    def test_cancel_queued_task(self) -> None:
        self.client.post("/api/v1/tasks", json=self.payload(), headers=self.auth)
        response = self.client.post(
            "/api/v1/tasks/exec-test-001/cancel", headers=self.auth
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "CANCELED")

    def test_safari_is_rejected_without_no_cache_guarantee(self) -> None:
        payload = self.payload()
        payload["browsers"] = ["safari"]
        response = self.client.post(
            "/api/v1/tasks", json=payload, headers=self.auth
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Safari", response.get_json()["message"])

    def test_edge_is_accepted(self) -> None:
        payload = self.payload("edge-test-001")
        payload["browsers"] = ["edge"]
        response = self.client.post(
            "/api/v1/tasks", json=payload, headers=self.auth
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["request"]["browsers"], ["edge"])

    def test_analysis_requires_pcap(self) -> None:
        payload = self.payload()
        payload["pcap"] = False
        response = self.client.post(
            "/api/v1/tasks", json=payload, headers=self.auth
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("pcap=true", response.get_json()["message"])

    def test_remote_path_and_command_parameters_are_rejected(self) -> None:
        payload = self.payload()
        payload["output_dir"] = r"C:\unsafe"
        response = self.client.post(
            "/api/v1/tasks", json=payload, headers=self.auth
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("output_dir", response.get_json()["message"])

    def test_log_endpoint_reads_incrementally(self) -> None:
        self.client.post("/api/v1/tasks", json=self.payload(), headers=self.auth)
        log_path = self.config.tasks_dir / "exec-test-001" / "worker.log"
        log_path.write_text("第一行\n第二行\n", encoding="utf-8")

        response = self.client.get(
            "/api/v1/tasks/exec-test-001/log?offset=0&limit=9",
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertGreater(data["next_offset"], 0)
        self.assertTrue(data["text"])

    def test_manifest_reports_compact_storage_summary(self) -> None:
        payload = self.payload("manifest-test-001")
        task_dir = self.config.tasks_dir / payload["task_id"]
        item_dir = task_dir / "fetch_output" / "1-wiki-Example"
        item_dir.mkdir(parents=True)
        (item_dir / "body_chrome.html").write_text("ok", encoding="utf-8")
        (item_dir / "capture_chrome.pcap").write_bytes(b"pcap")
        (item_dir / "capture_chrome.tsv").write_text("row", encoding="utf-8")
        (item_dir / "capture_chrome_flows").mkdir()
        (item_dir / "capture_chrome_flows" / "flow.tsv").write_text(
            "flow", encoding="utf-8"
        )
        (item_dir / "capture_chrome_inferred").mkdir()
        (item_dir / "capture_chrome_inferred" / "result.json").write_text(
            "{}", encoding="utf-8"
        )

        manifest = self.manager._build_manifest(task_dir, payload, [])

        self.assertEqual(manifest["status"], "SUCCEEDED")
        self.assertNotIn("files", manifest)
        self.assertEqual(manifest["storage"]["file_count"], 5)
        self.assertGreater(manifest["storage"]["total_bytes"], 0)

    def test_manifest_returns_status_for_each_url(self) -> None:
        payload = self.payload("multi-url-test-001")
        payload["items"].append(
            {
                "id": "2",
                "name": "Second",
                "url": "https://example.org/",
            }
        )
        task_dir = self.config.tasks_dir / payload["task_id"]

        first_dir = task_dir / "fetch_output" / "1-wiki-Example"
        first_dir.mkdir(parents=True)
        (first_dir / "body_chrome.html").write_text("ok", encoding="utf-8")
        (first_dir / "capture_chrome.pcap").write_bytes(b"pcap")
        (first_dir / "capture_chrome.tsv").write_text("row", encoding="utf-8")
        (first_dir / "capture_chrome_flows").mkdir()
        (first_dir / "capture_chrome_flows" / "flow.tsv").write_text(
            "flow", encoding="utf-8"
        )
        (first_dir / "capture_chrome_inferred").mkdir()
        (first_dir / "capture_chrome_inferred" / "result.json").write_text(
            "{}", encoding="utf-8"
        )

        second_dir = task_dir / "fetch_output" / "2-wiki-Second"
        second_dir.mkdir(parents=True)
        (second_dir / "body_chrome.html").write_text("ok", encoding="utf-8")
        (second_dir / "capture_chrome.pcap").write_bytes(b"pcap")
        (second_dir / "capture_chrome.tsv").write_text("header", encoding="utf-8")

        manifest = self.manager._build_manifest(task_dir, payload, [])

        self.assertEqual(manifest["status"], "PARTIAL")
        self.assertEqual(manifest["item_summary"]["total"], 2)
        self.assertEqual(
            [item["status"] for item in manifest["items"]],
            ["SUCCEEDED", "PARTIAL"],
        )
        self.assertEqual(
            manifest["items"][0]["browser_statuses"],
            [{"browser": "chrome", "status": "SUCCEEDED"}],
        )


if __name__ == "__main__":
    unittest.main()
