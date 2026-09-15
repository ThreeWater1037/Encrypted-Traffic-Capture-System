from __future__ import annotations

import subprocess
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from selenium import webdriver
from selenium.common.exceptions import TimeoutException

from wiki_fetcher import PacketCapture, UrlEntry, WikiFetcher, _entry_slug, _prepare_navigation


class _ExitedProcess:
    def poll(self) -> int:
        return 2


class PacketCaptureTests(unittest.TestCase):
    def test_hit_legacy_resources_only_blocked_on_exact_hit_host(self) -> None:
        driver = MagicMock(spec=webdriver.Chrome)
        _prepare_navigation(driver, "https://today.hit.edu.cn/article/1266")
        driver.execute_cdp_cmd.assert_any_call("Network.setBlockedURLs", {
            "urls": ["http://myweb.hit.edu.cn/*", "https://myweb.hit.edu.cn/*",
                     "http://today2.hit.edu.cn/*", "https://today2.hit.edu.cn/*"],
        })
        driver.set_page_load_timeout.assert_called_once_with(90)

    def test_other_sites_keep_all_image_sources_enabled(self) -> None:
        for url in (
            "https://en.wikipedia.org/wiki/Computer",
            "https://example.com/?next=https://today.hit.edu.cn/",
            "https://today.hit.edu.cn.example.com/",
        ):
            with self.subTest(url=url):
                driver = MagicMock(spec=webdriver.Chrome)
                _prepare_navigation(driver, url)
                self.assertEqual(driver.execute_cdp_cmd.call_args_list, [
                    call("Network.enable", {}),
                    call("Network.setCacheDisabled", {"cacheDisabled": True}),
                    call("Network.setBypassServiceWorker", {"bypass": True}),
                ])
                driver.get_log.assert_called_once_with("performance")

    def test_navigation_timeout_stops_capture_before_quit_and_is_not_success(self) -> None:
        events = []
        driver = MagicMock(spec=webdriver.Chrome)
        driver.command_executor = MagicMock()
        driver.get.side_effect = TimeoutException("page load timed out")
        driver.quit.side_effect = lambda: events.append("quit")
        capture = MagicMock()
        capture.stop.side_effect = lambda: events.append("stop")
        builder = MagicMock()
        builder.build.return_value = driver
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("wiki_fetcher.PacketCapture", return_value=capture),
            patch.dict("wiki_fetcher.AVAILABLE_DRIVERS", {"chrome": builder}),
        ):
            fetcher = WikiFetcher(Path(temp_dir), ["chrome"], True)
            entry = UrlEntry(id="1", name="Slow", url="https://example.com/")
            item_dir = fetcher._url_dir("slow")
            record = fetcher._fetch_with(entry.url, "chrome", item_dir)
            self.assertIn("page load timed out", record.error)
            self.assertFalse(fetcher._mark_complete(entry, item_dir, "chrome", record))
        self.assertEqual(events, ["stop", "quit"])

    def test_wiki_uses_normal_wait_without_hit_policy_or_skip_report(self) -> None:
        driver = MagicMock(spec=webdriver.Chrome)
        driver.command_executor = MagicMock()
        driver.current_url = "https://en.wikipedia.org/wiki/Test"
        driver.title = "Test"
        driver.page_source = "<html><body>Wiki</body></html>"
        builder = MagicMock()
        builder.build.return_value = driver
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict("wiki_fetcher.AVAILABLE_DRIVERS", {"chrome": builder}), \
             patch("wiki_fetcher.wait_for_resources") as resource_wait, \
             patch.object(WikiFetcher, "_wait_for_normal_page", return_value={}) as normal_wait, \
             patch("wiki_fetcher.time.sleep"):
            fetcher = WikiFetcher(Path(tmp), ["chrome"], False)
            item_dir = fetcher._url_dir("wiki")
            result = fetcher._fetch_with(driver.current_url, "chrome", item_dir)
            self.assertIsNone(result.error)
            self.assertEqual(builder.build.call_args.kwargs, {})
            normal_wait.assert_called_once_with(driver)
            resource_wait.assert_not_called()
            self.assertFalse((item_dir / "resource_status_chrome.json").exists())
            self.assertFalse(any(call.args[0] == "Network.setExtraHTTPHeaders"
                                 for call in driver.execute_cdp_cmd.call_args_list))

    def test_network_wait_uses_configurable_quiet_window(self) -> None:
        driver = MagicMock(spec=webdriver.Edge)
        with tempfile.TemporaryDirectory() as tmp, \
             patch("wiki_fetcher.NetworkIdleTracker") as tracker_type:
            tracker_type.return_value.wait.return_value = {"pending_count": 0}
            fetcher = WikiFetcher(Path(tmp), ["edge"], False, network_idle_seconds=0.5)
            self.assertEqual(fetcher._wait_for_normal_page(driver), {"pending_count": 0})
            tracker_type.assert_called_once_with(driver, idle_seconds=0.5)
            tracker_type.return_value.wait.assert_called_once_with()

    def test_failed_network_wait_persists_pending_urls_without_checkpoint(self) -> None:
        driver = MagicMock(spec=webdriver.Edge)
        driver.command_executor = MagicMock()
        builder = MagicMock()
        builder.build.return_value = driver
        failure = TimeoutException("network still pending")
        failure.network_idle_summary = {
            "pending_count": 1, "failed_requests": [],
            "pending_requests": [{"url": "https://example.com/slow.js"}],
        }
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict("wiki_fetcher.AVAILABLE_DRIVERS", {"edge": builder}), \
             patch("wiki_fetcher.NetworkIdleTracker") as tracker_type:
            tracker_type.return_value.wait.side_effect = failure
            fetcher = WikiFetcher(Path(tmp), ["edge"], False)
            item_dir = fetcher._url_dir("failed")
            result = fetcher._fetch_with("https://example.com/", "edge", item_dir)
            self.assertIn("network still pending", result.error)
            status = json.loads((item_dir / "network_status_edge.json").read_text("utf-8"))
            self.assertEqual(status["network_summary"]["pending_requests"][0]["url"],
                             "https://example.com/slow.js")
            self.assertFalse(fetcher._mark_complete(
                UrlEntry("1", "failed", "https://example.com/"), item_dir, "edge", result))

    def test_nonfinite_or_invalid_wait_configuration_is_rejected(self) -> None:
        for options in ({"network_idle_seconds": 0}, {"network_idle_seconds": float("nan")},
                        {"interval_seconds": -1}, {"interval_seconds": float("inf")}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                WikiFetcher(Path("unused"), ["edge"], False, **options)

    def test_cached_resource_cannot_be_marked_as_completed_capture(self):
        driver = MagicMock(spec=webdriver.Edge)
        driver.command_executor = MagicMock()
        driver.current_url = "https://example.com/"
        driver.title = "Example"
        driver.page_source = "<html>Example</html>"
        builder = MagicMock()
        builder.build.return_value = driver
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict("wiki_fetcher.AVAILABLE_DRIVERS", {"edge":builder}), \
             patch.object(WikiFetcher, "_wait_for_normal_page", return_value={
                 "cache_hit_requests":[{"url":"https://example.com/a.js", "served_from_cache":True}]}):
            fetcher = WikiFetcher(Path(tmp), ["edge"], False)
            directory = fetcher._url_dir("cached")
            record = fetcher._fetch_with(driver.current_url, "edge", directory)
            self.assertIn("cache use detected", record.error)
            self.assertFalse(fetcher._mark_complete(UrlEntry("1", "cached", driver.current_url),
                                                   directory, "edge", record))
            driver.quit.assert_called_once()

    def test_partial_capture_checkpoint_keeps_recapture_flag(self) -> None:
        from wiki_fetcher import SessionRecord
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = WikiFetcher(Path(tmp), ["chrome"], True)
            entry = UrlEntry(id="3", name="HIT", url="https://today.hit.edu.cn/article/1266")
            item_dir = fetcher._url_dir("hit")
            for path in fetcher._expected_artifacts(item_dir, "chrome"):
                path.write_bytes(b"fixture")
            skipped = [{"url": "https://today2.hit.edu.cn/old.png",
                        "reason": "isolated_legacy_host"}]
            record = SessionRecord(browser="Chrome", url=entry.url, timestamp="now",
                final_url=entry.url, page_title="HIT", html_length=1, response_time_ms=1,
                content_hash="hash", cookies=[], key_log_path=None, pcap_path=None,
                skipped_resources=skipped)
            self.assertTrue(fetcher._mark_complete(entry, item_dir, "chrome", record))
            marker = json.loads(fetcher._completion_marker_path(item_dir, "chrome").read_text("utf-8"))
            self.assertTrue(marker["needs_recapture"])
            self.assertEqual(marker["resource_status"], "partial")
            self.assertEqual(marker["skipped_resources"], skipped)

    def test_entry_slug_removes_windows_unsafe_trailing_dots_and_spaces(self) -> None:
        entry = UrlEntry(
            id="132",
            name="总务处/... ",
            url="https://example.com/",
        )

        self.assertEqual(_entry_slug(entry), "132-wiki-总务处／")

    def test_entry_slug_uses_fallback_when_name_is_only_dots(self) -> None:
        entry = UrlEntry(id="1", name="...", url="https://example.com/")

        self.assertEqual(_entry_slug(entry), "1-wiki-item")

    def test_linux_tshark_uses_only_any_interface(self) -> None:
        with (
            patch("wiki_fetcher.platform.system", return_value="Linux"),
            patch("wiki_fetcher.subprocess.check_output") as check_output,
        ):
            interfaces = PacketCapture._list_interfaces_tshark("/usr/bin/tshark")

        self.assertEqual(interfaces, ["any"])
        check_output.assert_not_called()

    def test_linux_tshark_command_does_not_include_pseudo_interfaces(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("wiki_fetcher.platform.system", return_value="Linux"),
        ):
            pcap_path = Path(temp_dir) / "capture.pcap"
            capture = PacketCapture(pcap_path)
            command = capture._build_cmd("tshark", "/usr/bin/tshark")

        self.assertEqual(
            command,
            [
                "/usr/bin/tshark",
                "-q",
                "--log-level",
                "warning",
                "--log-debug",
                "Main",
                "-i",
                "any",
                "-w",
                str(pcap_path),
            ],
        )

    def test_windows_tshark_retries_after_timeout(self) -> None:
        tshark_output = (
            "1. \\Device\\NPF_{ONE} (Ethernet)\n"
            "2. \\Device\\NPF_{TWO} (Wi-Fi)\n"
            "3. ciscodump (Cisco remote capture)\n"
        )
        with (
            patch("wiki_fetcher.platform.system", return_value="Windows"),
            patch(
                "wiki_fetcher.subprocess.check_output",
                side_effect=[
                    subprocess.TimeoutExpired("tshark -D", 15),
                    tshark_output,
                ],
            ) as check_output,
            patch("wiki_fetcher.time.sleep") as sleep,
        ):
            interfaces = PacketCapture._list_interfaces_tshark(
                r"C:\Program Files\Wireshark\tshark.exe"
            )

        self.assertEqual(
            interfaces,
            [r"\Device\NPF_{ONE}", r"\Device\NPF_{TWO}"],
        )
        self.assertEqual(check_output.call_count, 2)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(check_output.call_args.kwargs["timeout"], 15)

    def test_windows_tshark_failure_never_uses_macos_interfaces(self) -> None:
        with (
            patch("wiki_fetcher.platform.system", return_value="Windows"),
            patch(
                "wiki_fetcher.subprocess.check_output",
                side_effect=subprocess.TimeoutExpired("tshark -D", 15),
            ) as check_output,
            patch("wiki_fetcher.time.sleep"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "tshark interface detection failed on Windows",
            ):
                PacketCapture._list_interfaces_tshark(
                    r"C:\Program Files\Wireshark\tshark.exe"
                )

        self.assertEqual(check_output.call_count, 2)

    def test_macos_tshark_keeps_platform_specific_fallback(self) -> None:
        with (
            patch("wiki_fetcher.platform.system", return_value="Darwin"),
            patch("wiki_fetcher.subprocess.check_output", return_value=""),
        ):
            interfaces = PacketCapture._list_interfaces_tshark(
                "/Applications/Wireshark.app/Contents/MacOS/tshark"
            )

        self.assertEqual(interfaces, ["en0", "lo0"])

    def test_tshark_stderr_is_drained_and_early_exit_fails_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            capture = PacketCapture(Path(temp_dir) / "capture.pcap")
            process = _ExitedProcess()
            with (
                patch.object(
                    PacketCapture,
                    "_find_tool",
                    return_value=("tshark", "/usr/bin/tshark"),
                ),
                patch.object(
                    PacketCapture,
                    "_build_cmd",
                    return_value=["/usr/bin/tshark", "-i", "any"],
                ),
                patch("wiki_fetcher.subprocess.Popen", return_value=process) as popen,
                patch("wiki_fetcher.CaptureReadinessMonitor") as monitor,
                self.assertLogs("wiki_fetcher", level="WARNING") as captured_logs,
            ):
                monitor.return_value.wait_ready.side_effect = RuntimeError("exited before capture became ready")
                with self.assertRaisesRegex(RuntimeError, "exited before capture became ready"):
                    capture.start()

        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertIn(
            "capture became ready",
            "\n".join(captured_logs.output),
        )

    def test_readiness_failure_prevents_browser_start_and_checkpoint(self):
        builder = MagicMock()
        capture = MagicMock()
        capture.start.side_effect = RuntimeError("capture readiness timeout")
        capture.stop.return_value = None
        capture.summary = {"error": "capture readiness timeout"}
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict("wiki_fetcher.AVAILABLE_DRIVERS", {"edge":builder}), \
             patch("wiki_fetcher.PacketCapture", return_value=capture):
            fetcher = WikiFetcher(Path(tmp),["edge"],True)
            directory = fetcher._url_dir("failed")
            record = fetcher._fetch_with("https://example.com/","edge",directory)
            builder.build.assert_not_called()
            self.assertIn("readiness timeout",record.error)
            self.assertFalse(fetcher._mark_complete(UrlEntry("1","test",record.url),directory,"edge",record))

    def test_metadata_recheck_waits_again_before_capture_stop(self):
        events = []
        driver = MagicMock(spec=webdriver.Edge)
        driver.command_executor = MagicMock()
        driver.current_url = "https://example.com/"
        driver.title = "Example"
        driver.page_source = "<html>Example</html>"
        driver.quit.side_effect = lambda:events.append("quit")
        builder = MagicMock()
        builder.build.return_value = driver
        capture = MagicMock()
        capture.summary = {}
        capture.stop.side_effect = lambda:(events.append("stop") or Path("capture.pcap"))
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict("wiki_fetcher.AVAILABLE_DRIVERS",{"edge":builder}), \
             patch("wiki_fetcher.PacketCapture",return_value=capture), \
             patch("wiki_fetcher.NetworkIdleTracker") as tracker_type:
            tracker_type.return_value.wait.side_effect = lambda:(events.append("wait") or {"pending_count":0})
            fetcher = WikiFetcher(Path(tmp),["edge"],True)
            record = fetcher._fetch_with(driver.current_url,"edge",fetcher._url_dir("page"))
            self.assertIsNone(record.error)
            self.assertEqual(events,["wait","wait","stop","quit"])

    def test_forced_or_structurally_invalid_capture_is_not_saved(self):
        for forced in (False, True):
            with self.subTest(forced=forced), tempfile.TemporaryDirectory() as tmp:
                c = PacketCapture(Path(tmp)/"capture.pcap")
                c._ready = True
                c._proc = MagicMock()
                c._proc.poll.side_effect = [None, 0]
                if forced:
                    c._proc.send_signal.side_effect = OSError("cannot signal")
                with patch("wiki_fetcher.validate_capture_file",side_effect=ValueError("truncated block")):
                    self.assertIsNone(c.stop())
                    self.assertTrue(c.summary.get("error"))

    def test_linux_tcpdump_fallback_uses_any_interface(self) -> None:
        with patch("wiki_fetcher.platform.system", return_value="Linux"):
            self.assertEqual(PacketCapture._default_iface_tcpdump(), "any")

    def test_structurally_valid_file_does_not_hide_capture_shutdown_failure(self):
        cases = ((2, True, 0), (0, False, 0), (0, True, 1))
        for exit_code, drained, truncated in cases:
            with self.subTest(exit_code=exit_code, drained=drained, truncated=truncated), \
                 tempfile.TemporaryDirectory() as tmp:
                capture = PacketCapture(Path(tmp) / "capture.pcap")
                capture._ready = True
                capture._proc = MagicMock()
                capture._proc.poll.side_effect = [None, exit_code]
                capture._monitor = MagicMock()
                capture._monitor.join.return_value = drained
                file_summary = {"structure_valid": True, "packet_count": 10,
                                "truncated_packet_count": truncated}
                with patch("wiki_fetcher.validate_capture_file", return_value=file_summary):
                    self.assertIsNone(capture.stop())
                self.assertTrue(capture.summary["error"])

    def test_successful_capture_stop_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.pcap"
            path.write_bytes(b"validated fixture")
            capture = PacketCapture(path)
            capture._ready = True
            process = capture._proc = MagicMock()
            process.poll.side_effect = [None, 0]
            capture._monitor = MagicMock()
            capture._monitor.join.return_value = True
            with patch("wiki_fetcher.validate_capture_file", return_value={
                    "structure_valid": True, "packet_count": 10,
                    "truncated_packet_count": 0}) as validate:
                self.assertEqual(capture.stop(), path)
                self.assertEqual(capture.stop(), path)
            validate.assert_called_once()
            process.send_signal.assert_called_once()
            capture._monitor.process.stderr.close.assert_called_once()

    def test_nonempty_partial_files_are_not_a_valid_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fetcher = WikiFetcher(Path(temp_dir), ["chrome"], True)
            entry = UrlEntry(id="1", name="Example", url="https://example.com/")
            item_dir = fetcher._url_dir("1-wiki-Example")
            fetcher._key_log_path(item_dir, "chrome").write_text(
                "CLIENT_RANDOM key", encoding="utf-8"
            )
            fetcher._pcap_path(item_dir, "chrome").write_bytes(b"partial-pcap")

            self.assertFalse(fetcher._checkpoint_valid(entry, item_dir, "chrome"))

    def test_valid_checkpoint_requires_matching_url_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fetcher = WikiFetcher(Path(temp_dir), ["chrome"], True)
            entry = UrlEntry(id="1", name="Example", url="https://example.com/")
            item_dir = fetcher._url_dir("1-wiki-Example")
            fetcher._key_log_path(item_dir, "chrome").write_text(
                "CLIENT_RANDOM key", encoding="utf-8"
            )
            fetcher._pcap_path(item_dir, "chrome").write_bytes(b"pcap")
            fetcher._completion_marker_path(item_dir, "chrome").write_text(
                json.dumps(
                    {
                        "item_id": "1",
                        "url": "https://example.com/",
                        "browser": "chrome",
                        "artifacts": {
                            "tls_keys_chrome.log": len("CLIENT_RANDOM key"),
                            "capture_chrome.pcap": len(b"pcap"),
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(fetcher._checkpoint_valid(entry, item_dir, "chrome"))


if __name__ == "__main__":
    unittest.main()
