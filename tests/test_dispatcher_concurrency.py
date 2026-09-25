from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from werkzeug.serving import WSGIRequestHandler, make_server

from master_server.dispatcher import JobDispatcher, QueueFullError
from master_server.worker_client import WorkerClient, WorkerRequestError
from tests import test_master_server as master_fixtures
from tests import test_worker_agent as worker_fixtures


class ControlledWorker:
    """Keep captures running until the test releases them; never block an HTTP poll."""

    def __init__(self):
        self.requests = {}
        self.submissions = []
        self.completed = set()
        self.canceled = set()
        self.resumes = []
        self.offline = False
        self.probed = threading.Event()

    def health(self):
        self.probed.set()
        if self.offline:
            raise WorkerRequestError("offline fixture")
        return {"busy": bool(set(self.requests) - self.completed - self.canceled)}

    def capabilities(self):
        return {"browsers": [{"name": name} for name in ("chrome", "edge", "firefox")]}

    def submit_task(self, payload):
        self.requests[payload["task_id"]] = payload
        self.submissions.append(payload["task_id"])
        return self.get_task(payload["task_id"])

    def get_task(self, task_id):
        status = "CANCELED" if task_id in self.canceled else (
            "SUCCEEDED" if task_id in self.completed else "CAPTURING"
        )
        request = self.requests[task_id]
        return {
            "task_id": task_id, "status": status, "stage": status,
            "result": {"units": [
                {"item_id": item["id"], "browser": browser, "status": status}
                for item in request["items"] for browser in request["browsers"]
            ]} if status != "CAPTURING" else None,
        }

    def get_capture_progress(self, task_id, **kwargs):
        return {"units": [], "next_position": 0, "has_more": False}

    def cancel_task(self, task_id):
        self.canceled.add(task_id)
        return self.get_task(task_id)

    def resume_task(self, task_id, token):
        self.resumes.append((task_id, token))
        self.canceled.discard(task_id)
        self.completed.discard(task_id)
        return self.get_task(task_id)

    def get_log(self, task_id, **kwargs):
        if task_id not in self.requests:
            raise WorkerRequestError("Worker 返回 HTTP 404：任务不存在", status_code=404)
        return {"text": f"capture {task_id}", "tail_lines": 10, "line_count": 1}


class DispatcherConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.master = master_fixtures.MasterServerTests()
        self.master.setUp()
        self.addCleanup(self.master.tearDown)
        self.workers = {name: ControlledWorker() for name in ("worker-local", "second")}
        response = self.master.client.post("/api/v1/machines", json={
            "machine_id": "second", "name": "Second", "base_url": "http://second.invalid",
            "token": "fixture", "enabled": True,
        })
        self.assertEqual(response.status_code, 201)
        self.master.dispatcher._client_factory = lambda machine: self.workers[machine["machine_id"]]

    def eventually(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Timed out waiting for dispatcher state")
            time.sleep(0.01)

    def submit(self, job_id, *machines):
        payload = self.master.payload(job_id)
        payload["targets"] = [
            {"machine_id": machine, "browsers": ["chrome", "edge", "firefox"]}
            for machine in machines
        ]
        self.assertEqual(self.master.client.post("/api/v1/jobs", json=payload).status_code, 202)

    def task_id(self, job_id, machine="worker-local"):
        return self.master.store.get_worker_task_id(job_id, machine)

    def status(self, job_id):
        return self.master.store.get_job_status(job_id)["status"]

    def wait_submitted(self, job_id, machine="worker-local"):
        self.eventually(lambda: self.task_id(job_id, machine) in self.workers[machine].requests)

    def test_independent_workers_capture_and_read_logs_concurrently_for_all_browsers(self):
        self.master.dispatcher.start()
        self.submit("first", "worker-local")
        self.wait_submitted("first")
        self.submit("second-job", "second")
        self.wait_submitted("second-job", "second")
        for job_id, machine in (("first", "worker-local"), ("second-job", "second")):
            worker = self.workers[machine]
            with patch("master_server.app.WorkerClient", return_value=worker):
                entry = self.master.client.get(f"/api/v1/jobs/{job_id}/logs?tail_lines=10").get_json()["logs"][0]
            self.assertNotIn("error", entry)
            self.assertIn(self.task_id(job_id, machine), entry["text"])
            self.assertEqual(worker.requests[self.task_id(job_id, machine)]["browsers"], ["chrome", "edge", "firefox"])
            worker.completed.add(self.task_id(job_id, machine))
        self.eventually(lambda: all(self.status(job) == "SUCCEEDED" for job in ("first", "second-job")))
        for job in ("first", "second-job"):
            self.assertEqual(self.master.store.execution_status_counts(job), {"SUCCEEDED": 6})

    def test_same_worker_is_fifo_and_pending_logs_do_not_call_worker(self):
        self.master.dispatcher.start()
        for job in ("first", "next", "last"):
            self.submit(job, "worker-local")
        self.wait_submitted("first")
        self.eventually(lambda: len(self.master.dispatcher._futures) == 3)
        worker = self.workers["worker-local"]
        self.assertEqual(worker.submissions, [self.task_id("first")])
        with patch("master_server.app.WorkerClient") as client:
            entry = self.master.client.get("/api/v1/jobs/next/logs?tail_lines=10").get_json()["logs"][0]
            client.assert_not_called()
        self.assertNotIn("error", entry)
        self.assertIn("等待", entry["text"])
        self.assertEqual(entry["tail_lines"], 10)
        for job in ("first", "next", "last"):
            self.wait_submitted(job)
            worker.completed.add(self.task_id(job))
            self.eventually(lambda: self.status(job) == "SUCCEEDED")
        self.assertEqual(worker.submissions, [self.task_id(job) for job in ("first", "next", "last")])

    def test_slow_target_in_multi_worker_job_does_not_hold_completed_worker(self):
        self.master.dispatcher.start()
        self.submit("multi", "worker-local", "second")
        self.wait_submitted("multi")
        self.wait_submitted("multi", "second")
        self.submit("next-second", "second")
        self.workers["second"].completed.add(self.task_id("multi", "second"))
        self.wait_submitted("next-second", "second")
        self.assertNotIn(self.task_id("multi"), self.workers["worker-local"].completed)
        self.assertNotIn(self.status("multi"), ("SUCCEEDED", "FAILED"))

    def test_offline_worker_does_not_block_other_worker(self):
        self.workers["worker-local"].offline = True
        self.master.dispatcher.start()
        self.submit("offline", "worker-local")
        self.assertTrue(self.workers["worker-local"].probed.wait(5))
        self.submit("online", "second")
        self.wait_submitted("online", "second")
        self.assertEqual(self.master.store.worker_execution_statuses("offline", "worker-local"), {"WAITING_FOR_WORKER"})

    def test_cancel_pending_and_running_then_resume_keeps_worker_serial(self):
        self.master.dispatcher.start()
        self.submit("running", "worker-local")
        self.wait_submitted("running")
        self.submit("pending", "worker-local")
        self.eventually(lambda: "pending" in self.master.dispatcher._futures)
        self.master.client.post("/api/v1/jobs/pending/cancel")
        self.eventually(lambda: self.status("pending") == "CANCELED")
        self.assertNotIn(self.task_id("pending"), self.workers["worker-local"].requests)
        response = self.master.client.post("/api/v1/jobs/pending/resume")
        self.assertEqual(response.status_code, 202)
        self.eventually(lambda: "pending" in self.master.dispatcher._futures)
        self.assertNotIn(self.task_id("pending"), self.workers["worker-local"].requests)
        self.master.client.post("/api/v1/jobs/running/cancel")
        self.eventually(lambda: self.status("running") == "CANCELED")
        self.wait_submitted("pending")
        self.eventually(lambda: bool(self.workers["worker-local"].resumes))
        token = self.master.store.get_job_control("pending")["resume_token"]
        self.assertEqual(self.workers["worker-local"].resumes, [(self.task_id("pending"), token)])

    def test_shutdown_and_recovery_keep_pending_jobs_and_order_above_capacity(self):
        self.master.dispatcher.start()
        for job, machine in (("active", "worker-local"), ("pending", "worker-local"), ("other", "second")):
            self.submit(job, machine)
        self.wait_submitted("active")
        self.wait_submitted("other", "second")
        self.eventually(lambda: "pending" in self.master.dispatcher._futures)
        self.master.dispatcher.shutdown()
        self.assertEqual(self.master.store.worker_execution_statuses("pending", "worker-local"), {"CREATED"})
        self.assertFalse(self.workers["worker-local"].canceled)
        recovered = JobDispatcher(
            replace(self.master.config, max_queue_size=1), self.master.store,
            client_factory=lambda machine: self.workers[machine["machine_id"]], autostart=False,
        )
        self.addCleanup(recovered.shutdown)
        recovered.start()
        self.eventually(lambda: len(self.workers["second"].submissions) == 2)
        self.assertNotIn(self.task_id("pending"), self.workers["worker-local"].requests)
        with self.assertRaises(QueueFullError):
            recovered.enqueue("over-capacity")
        self.workers["worker-local"].completed.add(self.task_id("active"))
        self.wait_submitted("pending")
        self.assertEqual(self.workers["worker-local"].submissions, [self.task_id("active"), self.task_id("active"), self.task_id("pending")])

    def test_failure_frees_only_its_worker_and_keeps_scheduler_alive(self):
        worker = self.workers["worker-local"]
        submit = worker.submit_task

        def fail_first(payload):
            if payload["task_id"] == self.task_id("broken"):
                raise RuntimeError("fixture failure")
            return submit(payload)

        worker.submit_task = fail_first
        self.master.dispatcher.start()
        self.submit("broken", "worker-local")
        self.submit("healthy", "worker-local")
        self.eventually(lambda: self.status("broken") == "FAILED")
        self.wait_submitted("healthy")

    def test_running_task_404_is_not_hidden_as_queue_message(self):
        self.submit("lost", "worker-local")
        self.master.store.update_worker_executions("lost", "worker-local", status="CAPTURING")
        with patch("master_server.app.WorkerClient", return_value=self.workers["worker-local"]):
            entry = self.master.client.get("/api/v1/jobs/lost/logs?tail_lines=10").get_json()["logs"][0]
        self.assertIn("404", entry["error"])

    def test_full_queue_rejects_resume_without_resetting_terminal_job(self):
        self.master.dispatcher.config = replace(self.master.config, max_queue_size=1)
        self.submit("occupies-queue", "worker-local")
        self.master.store.create_job(self.master.payload("retry-later"))
        self.master.store.update_job("retry-later", status="FAILED", stage="DONE")
        response = self.master.client.post("/api/v1/jobs/retry-later/resume")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.status("retry-later"), "FAILED")

    def test_terminal_status_waits_for_target_cleanup_before_allowing_resume(self):
        written = threading.Event()
        cleanup = threading.Event()
        self.addCleanup(cleanup.set)
        apply_result = self.master.dispatcher._apply_worker_result

        def delayed_cleanup(*args):
            apply_result(*args)
            written.set()
            cleanup.wait(5)

        with patch.object(self.master.dispatcher, "_apply_worker_result", side_effect=delayed_cleanup):
            self.master.dispatcher.start()
            self.submit("finishing", "worker-local")
            self.wait_submitted("finishing")
            self.master.client.post("/api/v1/jobs/finishing/cancel")
            self.assertTrue(written.wait(5))
            self.master.dispatcher._sync_job_status("finishing")
            self.assertNotEqual(self.status("finishing"), "CANCELED")
            self.assertEqual(self.master.client.post("/api/v1/jobs/finishing/resume").status_code, 409)
            cleanup.set()
            self.eventually(lambda: self.status("finishing") == "CANCELED")
        self.assertEqual(self.master.client.post("/api/v1/jobs/finishing/resume").status_code, 202)
        self.eventually(lambda: len(self.workers["worker-local"].submissions) == 2)

    def test_worker_queue_cannot_bypass_master_capacity_and_completion_releases_slot(self):
        self.master.dispatcher.config = replace(self.master.config, max_queue_size=2)
        self.master.dispatcher.start()
        self.submit("first", "worker-local")
        self.wait_submitted("first")
        self.submit("pending", "worker-local")
        self.eventually(lambda: "pending" in self.master.dispatcher._futures)
        with self.assertRaises(QueueFullError):
            self.master.dispatcher.enqueue("overflow")
        self.master.dispatcher.enqueue("pending")  # Idempotent duplicates do not need a slot.
        self.workers["worker-local"].completed.add(self.task_id("first"))
        self.eventually(lambda: self.status("first") == "SUCCEEDED")
        self.submit("new-worker", "second")
        self.wait_submitted("new-worker", "second")

    def test_real_worker_http_queues_and_logs_with_controlled_capture_execution(self):
        # Exercise real Worker HTTP, persistence and TaskManager consumers. Only
        # the expensive capture body is replaced with an event-controlled fixture.
        releases = {name: threading.Event() for name in ("worker-local", "second")}
        workers = {}

        class QuietHandler(WSGIRequestHandler):
            def log_request(self, *args, **kwargs):
                pass

        def capture(worker, gate, task):
            task_id = task["task_id"]
            (worker.config.tasks_dir / task_id / "worker.log").write_bytes(b"capturing\n")
            worker.store.update_task(task_id, status="CAPTURING", stage="CAPTURING")
            if not gate.wait(10):
                worker.store.update_task(task_id, status="FAILED", error="fixture timed out")
                return
            worker.store.update_task(task_id, status="SUCCEEDED", stage="DONE", result_json={"units": [
                {"item_id": item["id"], "browser": browser, "status": "SUCCEEDED"}
                for item in task["request"]["items"] for browser in task["request"]["browsers"]
            ]})

        capability_patch = patch("worker_agent.app.detect_capabilities", return_value={
            "browsers": [{"name": browser} for browser in ("chrome", "edge", "firefox")],
        })
        capability_patch.start()
        self.addCleanup(capability_patch.stop)
        for name, gate in releases.items():
            worker = worker_fixtures.WorkerAgentApiTests()
            worker.setUp()
            workers[name] = worker
            self.addCleanup(worker.tearDown)
            worker.manager._run_task = lambda task, worker=worker, gate=gate: capture(worker, gate, task)
            worker.manager.start()
            server = make_server("127.0.0.1", 0, worker.app, threaded=True, request_handler=QuietHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def close(server=server, thread=thread):
                server.shutdown()
                thread.join(5)
                server.server_close()

            self.addCleanup(close)
            response = self.master.client.post("/api/v1/machines", json={
                "machine_id": name, "name": name,
                "base_url": f"http://127.0.0.1:{server.server_port}", "token": "test-token",
            })
            self.assertEqual(response.status_code, 200)
        self.addCleanup(self.master.dispatcher.shutdown)
        for gate in releases.values():
            self.addCleanup(gate.set)
        self.master.dispatcher._client_factory = lambda machine: WorkerClient(machine["base_url"], machine["token"])
        self.master.dispatcher.start()
        self.submit("http-first", "worker-local")
        self.submit("http-pending", "worker-local")
        self.submit("http-other", "second")
        for job, machine in (("http-first", "worker-local"), ("http-other", "second")):
            self.eventually(lambda: self.master.store.worker_execution_statuses(job, machine) == {"CAPTURING"})
            entry = self.master.client.get(f"/api/v1/jobs/{job}/logs?tail_lines=10").get_json()["logs"][0]
            self.assertEqual(entry["text"], "capturing\n")
            self.assertNotIn("error", entry)
        self.assertIsNone(workers["worker-local"].store.get_task_status(self.task_id("http-pending")))
        releases["worker-local"].set()
        self.eventually(lambda: self.status("http-pending") == "SUCCEEDED")
        self.assertEqual(workers["second"].manager.active_task_id, self.task_id("http-other", "second"))


if __name__ == "__main__":
    unittest.main()
