import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from capture_temp_cleanup import configure_temp_directory, REGISTRY_ENV
from wiki_fetcher import WikiFetcher, UrlEntry
from worker_agent.config import WorkerConfig
from worker_agent.task_runner import TaskManager
from worker_agent.watchdog import ServiceWatchdog


class WorkerResilienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = WorkerConfig(
            worker_id="resilience-test", host="127.0.0.1", port=5100, token="test",
            project_root=Path(__file__).resolve().parents[1], python_executable=Path(sys.executable),
            data_dir=self.root / "data", temp_dir=self.root / "dedicated-temp", capture_stall_seconds=5,
        )
        self.config.prepare()

    def test_temp_configuration_controls_python_and_browser_environment(self):
        old = tempfile.tempdir
        self.addCleanup(setattr, tempfile, "tempdir", old)
        with patch.dict(os.environ):
            configure_temp_directory(self.config.capture_temp_dir)
            self.assertEqual(Path(tempfile.gettempdir()), self.config.capture_temp_dir)
            for name in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(os.environ[name], str(self.config.capture_temp_dir))
            with tempfile.TemporaryDirectory() as created:
                self.assertEqual(Path(created).parent, self.config.capture_temp_dir)

    def test_temp_root_cannot_overlap_tasks_or_data(self):
        for directory in (self.config.data_dir, self.config.tasks_dir,
                          self.config.tasks_dir / "capture", self.root, Path(self.root.anchor)):
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                replace(self.config, temp_dir=directory).prepare()

    def test_capture_cleans_hourly_between_all_three_browsers(self):
        fetcher = WikiFetcher(self.root / "outputs", ["chrome", "edge", "firefox"], True)
        events, clock = [], [0]
        def clean(*args, **kwargs):
            events.append("clean")
            return {"deleted": 1}
        def capture(url, browser, directory):
            events.append(browser)
            clock[0] += 3601
            return SimpleNamespace(key_log_path="key", pcap_path="pcap")
        with patch.dict(os.environ, {REGISTRY_ENV: str(self.root / "registry")}), \
             patch("capture_temp_cleanup.time.monotonic", side_effect=lambda: clock[0]), \
             patch("capture_temp_cleanup.cleanup_retained_directories", side_effect=clean), \
             patch.object(fetcher, "_fetch_with", side_effect=capture), \
             patch.object(fetcher, "_mark_complete", return_value=True):
            self.assertEqual(fetcher._run_entry(UrlEntry("1", "fixture", "https://example.com/"), 1, 1), (3, 0, 0))
        self.assertEqual(events, ["clean", "chrome", "clean", "edge", "clean", "firefox"])

    def run_child(self, *, moving=False, script="wiki_fetcher.py", disabled=False):
        store = MagicMock()
        store.is_cancel_requested.return_value = False
        manager = TaskManager(replace(self.config, capture_stall_seconds=0) if disabled else self.config,
                              store, autostart=False)
        self.addCleanup(manager.shutdown)
        clock, calls = [0], [0]
        def poll():
            calls[0] += 1
            clock[0] += 4
            return 0 if calls[0] == 4 else None
        with patch("worker_agent.task_runner.subprocess.Popen") as popen, \
             patch("worker_agent.task_runner.time.monotonic", side_effect=lambda: clock[0]), \
             patch("worker_agent.task_runner.time.sleep"), \
             patch.object(manager, "_progress_stamp", side_effect=lambda _: (clock[0], 10) if moving else None), \
             patch.object(manager, "_terminate_process_tree") as terminate:
            popen.return_value.pid = 12345
            popen.return_value.poll.side_effect = poll
            result = manager._run_command("fixture", [sys.executable, script], self.root / "task.log", None)
            return result, terminate.call_count

    def test_stalled_capture_terminates_tree_and_enters_existing_bounded_retry(self):
        self.assertEqual(self.run_child(), (124, 1))
        self.assertIn("no checkpoint progress", (self.root / "task.log").read_text())

    def test_new_progress_resets_stall_deadline(self):
        self.assertEqual(self.run_child(moving=True), (0, 0))

    def test_analysis_and_disabled_stall_guard_are_not_killed(self):
        self.assertEqual(self.run_child(script="batch_process.py"), (0, 0))
        self.assertEqual(self.run_child(disabled=True), (0, 0))


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        # Exercise the Linux notification protocol even on Windows test hosts.
        self.unix_socket = patch("worker_agent.watchdog.socket.AF_UNIX", 1, create=True)
        self.unix_socket.start()
        self.addCleanup(self.unix_socket.stop)

    def test_only_service_owner_enables_watchdog_and_ignores_proxies(self):
        with patch.dict(os.environ, {"NOTIFY_SOCKET": "/run/test-notify", "WATCHDOG_USEC": "180000000",
                                     "WATCHDOG_PID": str(os.getpid())}, clear=True):
            guard = ServiceWatchdog.from_environment("0.0.0.0", 5100)
            self.assertEqual(guard.url, "http://127.0.0.1:5100/api/v1/health")
            self.assertEqual((guard.interval, guard.timeout), (30, 5))
            os.environ["WATCHDOG_PID"] = str(os.getpid() + 1)
            self.assertIsNone(ServiceWatchdog.from_environment("0.0.0.0", 5100))
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(ServiceWatchdog.from_environment("0.0.0.0", 5100))

    def test_only_successful_http_response_feeds_systemd(self):
        guard = ServiceWatchdog("@notify", "http://127.0.0.1/health", 1, 1)
        guard.opener = MagicMock()
        response = guard.opener.open.return_value.__enter__.return_value
        with patch("worker_agent.watchdog.socket.socket") as factory:
            for status, body in ((500, b'{}'), (200, b'{"status":"bad"}'), (200, b'{"status":"ok"}')):
                response.status, response.read.return_value = status, body
                self.assertEqual(guard.check_once(), status == 200 and b'"ok"' in body)
            factory.return_value.__enter__.return_value.sendto.assert_called_once_with(b"WATCHDOG=1", "\0notify")
        guard.opener.open.side_effect = TimeoutError("blocked request pool")
        with patch("worker_agent.watchdog.socket.socket") as factory, self.assertLogs(level="WARNING"):
            self.assertFalse(guard.check_once())
            factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
