"""Front-end data contracts and abnormal Worker completion regression coverage."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tests import test_master_server as master_fixtures
from tests import test_worker_agent as worker_fixtures
from worker_agent.task_runner import TaskTimedOutError


class FrontendContractTests(unittest.TestCase):
    def setUp(self):
        self.master = master_fixtures.MasterServerTests()
        self.master.setUp()
        self.addCleanup(self.master.tearDown)
        self.worker = worker_fixtures.WorkerAgentApiTests()
        self.worker.setUp()
        self.addCleanup(self.worker.tearDown)

    def create_job(self, job_id):
        payload = self.master.payload(job_id)
        payload["analysis"] = {"steps": []}
        payload["items"].append({"id": "3", "name": "Missing", "url": "https://example.net/"})
        self.master.store.create_job(payload)
        return payload

    def test_partial_artifacts_survive_retry_exhaustion_and_timeout(self):
        for failure in ("retry", "timeout"):
            with self.subTest(failure=failure):
                job_id = f"mixed-{failure}"
                payload = self.create_job(job_id)
                task_id = self.master.store.get_worker_task_id(job_id, "worker-local")
                request = self.worker.payload(task_id)
                request["items"] = payload["items"]
                request["analysis"]["steps"] = []
                self.worker.manager.submit(request)
                task_dir = self.worker.config.tasks_dir / task_id
                for item in request["items"][:2]:
                    directory = task_dir / "fetch_output" / f"{item['id']}-wiki-{item['name']}"
                    directory.mkdir(parents=True)
                    (directory / "capture_chrome.pcap").write_bytes(b"fixture pcap")
                    (directory / "tls_keys_chrome.log").write_text("fixture keylog")
                    (directory / "capture_chrome.complete.json").write_text(json.dumps({"item_id": item["id"]}))
                command = {"return_value": 2} if failure == "retry" else {"side_effect": TaskTimedOutError("test timeout")}
                with patch.object(self.worker.manager, "_run_command", **command), patch.object(self.worker.manager, "_wait_interruptibly"):
                    self.worker.manager._run_task(self.worker.store.get_task(task_id))
                task = self.worker.store.get_task(task_id)
                self.assertEqual(task["status"], "PARTIAL")
                self.assertTrue(task["error"])
                self.assertTrue((task_dir / "manifest.json").is_file())
                self.master.dispatcher._apply_worker_result(job_id, "worker-local", task)
                self.master.dispatcher._sync_job_status(job_id, finished=True)
                job = self.master.client.get(f"/api/v1/jobs/{job_id}?unit=url").get_json()
                self.assertEqual(job["status"], "PARTIAL")
                self.assertEqual([item["status"] for item in job["items"]], ["SUCCEEDED", "SUCCEEDED", "FAILED"])
                self.assertEqual(job["summary"]["succeeded"], 2)
                self.assertEqual(job["summary"]["failed"], 1)
                self.assertTrue(job["executions"][0]["result"]["artifacts"]["pcap"])
                self.assertTrue(job["executions"][0]["result"]["batch_error"])

    def test_failed_worker_without_manifest_keeps_checkpoint_and_validated_result(self):
        job_id = "missing-manifest"
        self.create_job(job_id)
        checkpoint = {"item_id": "1", "browser": "chrome", "status": "CAPTURED", "artifacts": {"pcap": {"path": "capture.pcap"}}}
        self.master.store.update_worker_capture_progress(job_id, "worker-local", [checkpoint])
        validated = {"item_id": "2", "browser": "chrome", "status": "SUCCEEDED", "checks": {"checkpoint": True}}
        self.master.store.update_execution_result(job_id, "worker-local", "2", "chrome", status="SUCCEEDED", result=validated)
        for _ in range(2):
            self.master.dispatcher._apply_worker_result(job_id, "worker-local", {"status": "FAILED", "error": "manifest unavailable"})
            self.master.dispatcher._sync_job_status(job_id, finished=True)
            job = self.master.client.get(f"/api/v1/jobs/{job_id}?unit=url").get_json()
            self.assertEqual([item["status"] for item in job["items"]], ["PARTIAL", "SUCCEEDED", "FAILED"])
            self.assertEqual(job["executions"][0]["result"], checkpoint)
            self.assertEqual(job["executions"][1]["result"], validated)
        captured = self.master.client.get(f"/api/v1/jobs/{job_id}?unit=url&status=CAPTURED").get_json()
        self.assertEqual(captured["page"]["total"], 2)

    def test_explicit_unit_failure_overrides_checkpoint_fallback(self):
        job_id = "explicit-result"
        self.create_job(job_id)
        self.master.store.update_worker_capture_progress(job_id, "worker-local", [{"item_id": "1", "browser": "chrome", "status": "CAPTURED"}])
        self.master.store.update_worker_results(job_id, "worker-local", top_status="FAILED", error="batch error", units=[{
            "item_id": "1", "browser": "chrome", "status": "FAILED", "checks": {"pcap": False}, "error": "invalid pcap",
        }])
        unit = self.master.store.get_job(job_id)["executions"][0]
        self.assertEqual(unit["status"], "FAILED")
        self.assertEqual(unit["error"], "invalid pcap")
        self.assertFalse(unit["result"]["checks"]["pcap"])

    def test_dispatcher_failure_also_preserves_completed_capture(self):
        job_id = "dispatcher-failure"
        self.create_job(job_id)
        checkpoint = {"item_id": "1", "browser": "chrome", "status": "CAPTURED"}
        self.master.store.update_worker_capture_progress(job_id, "worker-local", [checkpoint])
        self.master.store.update_worker_executions(job_id, "worker-local", status="FAILED", error="dispatcher exception")
        unit = self.master.store.get_job(job_id)["executions"][0]
        self.assertEqual(unit["status"], "PARTIAL")
        self.assertEqual(unit["result"], checkpoint)

    def test_history_paging_filter_and_running_count_include_older_jobs(self):
        for index in range(103):
            job_id = f"history-{index:03}"
            self.master.store.create_job(self.master.payload(job_id))
            if index:
                self.master.store.update_job(job_id, status="SUCCEEDED")
        ids = []
        for offset in range(0, 103, 20):
            response = self.master.client.get(f"/api/v1/jobs?offset={offset}&limit=20").get_json()
            self.assertEqual(response["page"]["total"], 103)
            self.assertEqual(response["running_count"], 1)
            ids.extend(job["job_id"] for job in response["jobs"])
        self.assertEqual(len(set(ids)), 103)
        response = self.master.client.get("/api/v1/jobs?status=CREATED&query=HISTORY-000&offset=999").get_json()
        self.assertEqual(response["page"]["offset"], 0)
        self.assertEqual(response["page"]["total"], 1)
        self.assertEqual(response["jobs"][0]["job_id"], "history-000")
        self.assertEqual(self.master.client.get("/api/v1/jobs?offset=invalid").status_code, 400)

    def test_capture_evidence_remains_filterable_after_stage_change_and_cancel(self):
        job_id = "capture-evidence"
        self.create_job(job_id)
        self.master.store.update_worker_capture_progress(job_id, "worker-local", [{"item_id": "1", "browser": "chrome", "status": "CAPTURED"}])
        self.master.dispatcher._apply_worker_progress(job_id, "worker-local", {"status": "ANALYZING"})
        for stopped in (False, True):
            if stopped:
                self.master.store.update_worker_results(job_id, "worker-local", top_status="CANCELED", units=[], error=None)
            response = self.master.client.get(f"/api/v1/jobs/{job_id}?unit=url&status=CAPTURED").get_json()
            self.assertEqual(response["page"]["total"], 1)
            self.assertEqual(response["items"][0]["id"], "1")

    def test_manifest_inspection_failure_preserves_original_error(self):
        request = self.worker.payload("manifest-unavailable")
        self.worker.manager.submit(request)
        task_dir = self.worker.config.tasks_dir / request["task_id"]
        with patch.object(self.worker.manager, "_build_manifest", side_effect=OSError("disk error")):
            self.worker.manager._finish_failed_task(task_dir, request, "original failure", "FAILED")
        task = self.worker.store.get_task(request["task_id"])
        self.assertEqual(task["status"], "FAILED")
        self.assertIn("original failure", task["error"])
        self.assertIn("disk error", task["error"])


if __name__ == "__main__":
    unittest.main()
