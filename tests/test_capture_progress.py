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

    def test_windows_replace_conflict_does_not_reset_live_progress(self):
        snapshot = {"run_id": "stable-run", "last_processed_position": 5000,
                    "total_urls": 5000}
        self.progress_path.write_text(json.dumps(snapshot), encoding="utf-8")
        original = Path.read_text
        for code in (2, 5, 32, 33):
            with self.subTest(winerror=code):
                attempts = []
                error = OSError("temporarily unavailable during replacement")
                error.winerror = code

                def read(path, *args, **kwargs):
                    if path == self.progress_path:
                        attempts.append(1)
                        if len(attempts) < 3:
                            raise error
                    return original(path, *args, **kwargs)

                with patch.object(Path, "read_text", read), patch("worker_agent.task_runner.time.sleep"):
                    page = self.read_page(run_id="stable-run", after_position=4989)
                self.assertEqual(page["run_id"], "stable-run")
                self.assertEqual(page["observed_position"], 5000)
                self.assertEqual(page["next_position"], 5000)
                self.assertEqual([u["item_id"] for u in page["units"]], ["5000"])

    def test_persistently_unreadable_progress_still_returns_bounded_fallback(self):
        error = PermissionError("still locked")
        error.winerror = 5
        with patch.object(Path, "read_text", side_effect=error) as read, \
             patch("worker_agent.task_runner.time.sleep") as sleep:
            page = self.read_page()
        self.assertEqual(read.call_count, 6)
        self.assertLessEqual(sum(c.args[0] for c in sleep.call_args_list), 0.25)
        self.assertIsNone(page["run_id"])
        self.assertEqual(page["units"], [])

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


class CaptureProgressLookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.output = root / "lookup" / "fetch_output"
        self.output.mkdir(parents=True)
        self.items = [
            {"id": str(i), "name": "Example", "url": f"https://example.com/{i}"}
            for i in range(100)
        ]
        self.items.append({"id": "a-wiki-b", "name": "old-wiki-name", "url": "https://example.com/special"})
        self.browsers = ["chrome", "edge", "firefox"]
        self.manager = object.__new__(TaskManager)
        self.manager.config = SimpleNamespace(tasks_dir=root)
        self.manager.store = SimpleNamespace(
            get_task_status=lambda _: {"status": "CAPTURING"},
            get_task=lambda _: {"request": {"items": self.items, "browsers": self.browsers}},
        )
        (self.output / "capture_progress.json").write_text(json.dumps({
            "run_id": "lookup-run", "last_processed_position": len(self.items),
            "total_urls": len(self.items),
        }), encoding="utf-8")
        for item in self.items:
            for browser in self.browsers:
                self.write_checkpoint(item, browser)

    def write_checkpoint(self, item, browser, suffix="renamed-wiki-entry"):
        directory = self.output / f"{item['id']}-wiki-{suffix}"
        directory.mkdir(exist_ok=True)
        sizes = {f"capture_{browser}.pcap": 4, f"tls_keys_{browser}.log": 3}
        for name, size in sizes.items():
            (directory / name).write_bytes(b"x" * size)
        marker = directory / f"capture_{browser}.complete.json"
        marker.write_text(json.dumps({
            "item_id": item["id"], "url": item["url"], "browser": browser,
            "artifacts": sizes,
        }), encoding="utf-8")
        return marker

    def test_large_three_browser_page_enumerates_directory_once(self):
        # Directory count must not multiply filesystem enumeration by page size
        # or browser count. Avoid timing assertions that depend on the host.
        for i in range(1000):
            (self.output / f"unrelated-{i}-wiki-other").mkdir()
        scans = []
        original = Path.iterdir

        def tracked(path):
            scans.append(path)
            return original(path)

        with patch.object(Path, "iterdir", tracked), patch.object(
            Path, "glob", side_effect=AssertionError("Repeated glob scan")
        ):
            result = self.manager.capture_progress("lookup")
        self.assertEqual(scans, [self.output])
        self.assertEqual(len(result["units"]), len(self.items) * 3)
        self.assertEqual(
            [(u["item_id"], u["browser"]) for u in result["units"]],
            [(item["id"], browser) for item in self.items for browser in self.browsers],
        )

    def test_pagination_and_caught_up_poll_keep_the_same_contract(self):
        first = self.manager.capture_progress("lookup", limit=2)
        self.assertEqual(first["next_position"], 2)
        self.assertTrue(first["has_more"])
        second = self.manager.capture_progress("lookup", run_id="lookup-run", after_position=2, limit=2)
        self.assertEqual({u["item_id"] for u in second["units"]}, {"2", "3"})
        with patch.object(Path, "iterdir", side_effect=AssertionError("Caught-up poll must not scan")):
            caught_up = self.manager.capture_progress("lookup", run_id="lookup-run", after_position=len(self.items))
        self.assertEqual(caught_up["units"], [])

    def test_stale_corrupt_and_incomplete_checkpoints_are_not_reported(self):
        for i, browser in enumerate(self.browsers):
            item = self.items[i]
            marker = self.write_checkpoint(item, browser)
            data = json.loads(marker.read_text())
            if browser == "chrome":
                data["url"] = "https://example.com/stale"
                marker.write_text(json.dumps(data))
            elif browser == "edge":
                (marker.parent / "capture_edge.pcap").write_bytes(b"truncated")
            else:
                marker.write_text("[]")
        result = self.manager.capture_progress("lookup")
        pairs = {(u["item_id"], u["browser"]) for u in result["units"]}
        for i, browser in enumerate(self.browsers):
            self.assertNotIn((str(i), browser), pairs)
        self.assertEqual(len(pairs), len(self.items) * 3 - 3)

    def test_new_checkpoint_is_visible_on_next_poll(self):
        item = self.items[-1]
        marker = self.write_checkpoint(item, "firefox")
        marker.unlink()
        first = self.manager.capture_progress("lookup")
        self.write_checkpoint(item, "firefox", suffix="new-location")
        second = self.manager.capture_progress("lookup")
        self.assertEqual(len(second["units"]), len(first["units"]) + 1)
        self.assertTrue(any(u["item_id"] == item["id"] and u["browser"] == "firefox" for u in second["units"]))


if __name__ == "__main__":
    unittest.main()
