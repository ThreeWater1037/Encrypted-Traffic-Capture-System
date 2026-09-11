"""Regression checks for live progress across capture updates and resumes."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from master_server.config import MasterConfig
from master_server.dispatcher import JobDispatcher
from wiki_fetcher import UrlEntry, WikiFetcher
from worker_agent.app import create_app
from worker_agent.config import WorkerConfig
from worker_agent.task_runner import TaskManager
from worker_agent.task_store import TaskStore


class CaptureProgressWriterTests(unittest.TestCase):
    def test_captured_and_skipped_urls_publish_progress_with_one_run_id(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            fetcher = WikiFetcher(output, ["chrome"], True)
            snapshots = []

            def entries():
                for position in range(1, 4):
                    yield UrlEntry(str(position), "Example", "https://example.com/")
                    # Inspect while the batch is running, before its final write.
                    snapshots.append(json.loads((output / "capture_progress.json").read_text()))

            with (
                patch.object(fetcher, "_run_entry", side_effect=[(1, 0, 0), (0, 1, 0), (0, 1, 0)]),
                patch("wiki_fetcher.time.sleep"),
            ):
                self.assertTrue(fetcher.run(entries(), total=3))

            self.assertEqual([s["last_processed_position"] for s in snapshots], [1, 2, 3])
            self.assertEqual([s["skipped_units_from_checkpoint"] for s in snapshots], [0, 1, 2])
            self.assertTrue(all(s["completed_units_this_run"] == 1 for s in snapshots))
            self.assertTrue(all(s["version"] == 2 for s in snapshots))
            self.assertEqual({s["run_id"] for s in snapshots}, {fetcher.progress_run_id})
            final = json.loads((output / "capture_progress.json").read_text())
            self.assertEqual(final["status"], "finished")
            self.assertEqual(final["run_id"], fetcher.progress_run_id)
            restarted = WikiFetcher(output, ["chrome"], True)
            self.assertNotEqual(restarted.progress_run_id, fetcher.progress_run_id)


class CaptureProgressSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        config = WorkerConfig(
            worker_id="worker-test", host="127.0.0.1", port=5100, token="test-token",
            project_root=Path(__file__).resolve().parents[1],
            python_executable=Path(sys.executable), data_dir=root / "worker",
        )
        config.prepare()
        self.store = TaskStore(config.database_path)
        self.manager = TaskManager(config, self.store, autostart=False)
        self.addCleanup(self.manager.shutdown)
        app = create_app(config, store=self.store, manager=self.manager)
        app.testing = True
        self.client = app.test_client()
        self.task_id = "progress-5000"
        self.items = [
            {"id": str(i), "name": "Example", "url": f"https://example.com/{i}"}
            for i in range(1, 5001)
        ]
        self.manager.submit({
            "task_id": self.task_id, "items": self.items, "browsers": ["chrome"],
            "pcap": True, "analysis": {"steps": []},
        })
        self.store.update_task(self.task_id, started_at="2026-09-11T10:00:00+00:00")
        self.output = config.tasks_dir / self.task_id / "fetch_output"
        self.output.mkdir()
        self.progress_path = self.output / "capture_progress.json"
        # Representative real checkpoints beyond the old 4000 boundary.
        # Other items deliberately lack checkpoints and must not be reported complete.
        for position in (4001, 4989, 5000):
            item = self.items[position - 1]
            directory = self.output / f"{position}-wiki-Example"
            directory.mkdir()
            (directory / "capture_chrome.pcap").write_bytes(b"pcap")
            (directory / "tls_keys_chrome.log").write_bytes(b"key")
            (directory / "capture_chrome.complete.json").write_text(json.dumps({
                "item_id": item["id"], "url": item["url"], "browser": "chrome",
                "artifacts": {"capture_chrome.pcap": 4, "tls_keys_chrome.log": 3},
            }), encoding="utf-8")
        self.master_store = MagicMock()
        self.master_store.get_job_status.return_value = None
        self.dispatcher = JobDispatcher(
            MasterConfig(host="127.0.0.1", port=5200, token="", data_dir=root / "master"),
            self.master_store, autostart=False,
        )
        self.addCleanup(self.dispatcher.shutdown)

    def read_page(self, *, run_id=None, after_position=0, limit=1000):
        query = {"after_position": after_position, "limit": limit}
        if run_id is not None:
            query["run_id"] = run_id
        response = self.client.get(
            f"/api/v1/tasks/{self.task_id}/progress", query_string=query,
            headers={"Authorization": "Bearer test-token"},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_master_catches_up_while_5000_url_snapshot_keeps_changing(self):
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                self.master_store.reset_mock()
                requests = []
                snapshot = {"version": 1 if legacy else 2, "total_urls": 5000}
                if not legacy:
                    snapshot["run_id"] = "capture-run-1"

                def get_progress(task_id, **kwargs):
                    self.assertEqual(task_id, self.task_id)
                    requests.append(kwargs["after_position"])
                    self.assertLessEqual(len(requests), 6, "Pagination restarted instead of catching up")
                    # Capture advances during every page of the initial catch-up.
                    snapshot["last_processed_position"] = 4984 + len(requests)
                    snapshot["updated_at"] = f"2026-09-11T20:56:{len(requests):02d}"
                    self.progress_path.write_text(json.dumps(snapshot), encoding="utf-8")
                    return self.read_page(**kwargs)

                run_id, position = self.dispatcher._sync_worker_capture_progress(
                    SimpleNamespace(get_capture_progress=get_progress), self.task_id,
                    "job-test", "worker-test", run_id=None, position=0,
                )
                self.assertEqual(requests, [0, 1000, 2000, 3000, 4000])
                self.assertEqual(position, 4989)
                units = self.master_store.update_worker_capture_progress.call_args.args[2]
                self.assertEqual([unit["item_id"] for unit in units], ["4001", "4989"])

                snapshot.update(last_processed_position=5000, updated_at="2026-09-11T20:58:00")
                self.progress_path.write_text(json.dumps(snapshot), encoding="utf-8")
                tail = self.read_page(run_id=run_id, after_position=position)
                self.assertEqual(tail["run_id"], run_id)
                self.assertEqual(tail["next_position"], 5000)
                self.assertFalse(tail["has_more"])
                self.assertEqual([unit["item_id"] for unit in tail["units"]], ["5000"])

    def test_real_run_change_resets_pagination_for_both_formats(self):
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                self.store.update_task(self.task_id, started_at="2026-09-11T10:00:00+00:00")
                snapshot = {"total_urls": 5000, "last_processed_position": 4989}
                if not legacy:
                    snapshot["run_id"] = "old-run"
                self.progress_path.write_text(json.dumps(snapshot), encoding="utf-8")
                first = self.read_page()
                if legacy:
                    self.store.update_task(self.task_id, started_at="2026-09-11T11:00:00+00:00")
                else:
                    snapshot["run_id"] = "new-run"
                    self.progress_path.write_text(json.dumps(snapshot), encoding="utf-8")
                resumed = self.read_page(run_id=first["run_id"], after_position=4000)
                self.assertNotEqual(resumed["run_id"], first["run_id"])
                self.assertEqual(resumed["next_position"], 1000)
                self.assertTrue(resumed["has_more"])


if __name__ == "__main__":
    unittest.main()
