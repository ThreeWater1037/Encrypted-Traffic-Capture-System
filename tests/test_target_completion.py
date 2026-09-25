"""Target success is independent of stalled optional resources, never of HTML."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.remote.command import Command

from browser_loading import NetworkIdleTracker
from browser_request_policy import (
    FORBES_ANALYTICS_URL, blocked_request_reason, cdp_block_patterns, bidi_block_patterns,
)
from browser_session import BoundedCommands
from tests.test_network_idle import FakeDriver, request, event, finished
from wiki_fetcher import WikiFetcher, UrlEntry


def document(status=200, request_id="doc", **kwargs):
    return [request(request_id, 0, url="https://example.com/", type="Document", **kwargs),
            event("Network.responseReceived", 0, requestId=request_id, response={"status": status}),
            finished(request_id, 0)]


class TargetCompletionTests(unittest.TestCase):
    def run_target(self, timeline, **kwargs):
        clock = [0.0]
        driver = FakeDriver(clock, timeline)
        with patch("browser_loading.time.monotonic", side_effect=lambda: clock[0]), \
             patch("browser_loading.time.sleep", side_effect=lambda t: clock.__setitem__(0, clock[0] + t)):
            tracker = NetworkIdleTracker(driver, completion_policy="target_document", **kwargs)
            result = tracker.wait()
            before = clock[0]
            tracker.wait()  # Metadata recheck must reuse the resource deadline.
            self.assertLess(clock[0] - before, .06)
        return result, clock[0]

    def test_hung_analytics_is_partial_success_after_three_seconds(self):
        result, elapsed = self.run_target([(0, document() + [request("ga", 0, type="Fetch")])])
        self.assertLess(elapsed, 3.06)
        self.assertGreaterEqual(elapsed, 3)
        self.assertEqual(result["completion_reason"], "resource_stall")
        self.assertEqual(result["pending_count"], 1)
        self.assertFalse(result["network_complete"])
        self.assertEqual(result["target_document"]["state"], "finished")

    def test_active_optional_download_has_ten_second_budget(self):
        timeline = [(0, document() + [request("download", 0)])]
        timeline += [(i / 2, [event("Network.dataReceived", i / 2, requestId="download")])
                     for i in range(1, 25)]
        result, elapsed = self.run_target(timeline)
        self.assertLess(elapsed, 10.06)
        self.assertEqual(result["completion_reason"], "resource_timeout")

    def test_slow_but_finishing_resource_is_captured_completely(self):
        result, elapsed = self.run_target([(0, document() + [request("image", 0)]),
                                          (2, [finished("image", 2)])])
        self.assertGreaterEqual(elapsed, 2.5)
        self.assertTrue(result["network_complete"])

    def test_missing_incomplete_or_failed_target_never_becomes_success(self):
        cases = [[], [request("doc", 0, type="Document")], document(500),
                 document(304), document(200, frameId="child"),
                 [request("doc", 0, type="Document"),
                  event("Network.loadingFailed", 0, requestId="doc", errorText="net::ERR_NAME_NOT_RESOLVED")]]
        for events in cases:
            with self.subTest(events=events), self.assertRaises(WebDriverException):
                self.run_target([(0, events)], timeout=.2)

    def test_target_cache_is_rejected_but_optional_cache_is_diagnostic(self):
        events = document()
        events.insert(2, event("Network.requestServedFromCache", requestId="doc"))
        with self.assertRaisesRegex(WebDriverException, "Target document failed"):
            self.run_target([(0, events)])
        result, _ = self.run_target([(0, document() + [request("cached-image", 0),
            event("Network.requestServedFromCache", requestId="cached-image"), finished("cached-image", 0)])])
        self.assertEqual(result["target_document"]["status"], 200)
        self.assertEqual(len(result["cache_hit_requests"]), 1)

    def test_redirect_requires_final_main_document_completion(self):
        first = request("doc", 0, type="Document", url="https://example.com/old")
        final = request("doc", .1, type="Document", url="https://example.com/final",
                        redirectResponse={"status": 302})
        result, _ = self.run_target([(0, [first]), (.1, [final,
            event("Network.responseReceived", .1, requestId="doc", response={"status": 200}),
            finished("doc", .1)])])
        self.assertEqual(result["target_document"]["url"], "https://example.com/final")
        self.assertEqual(result["redirect_count"], 1)

    def test_navigation_and_main_body_share_deadline(self):
        with self.assertRaises(TimeoutException) as caught:
            self.run_target([(0, [request("doc", 0, type="Document")])], deadline=.2)
        self.assertLess(caught.exception.network_idle_summary["wait_seconds"], .26)

    def test_partial_success_commits_checkpoint_for_each_browser(self):
        for browser, cls in (("chrome", webdriver.Chrome), ("edge", webdriver.Edge),
                             ("firefox", webdriver.Firefox)):
            with self.subTest(browser=browser), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                driver = MagicMock(spec=cls)
                driver.command_executor = MagicMock()
                driver.current_url, driver.title, driver.page_source = "https://example.com/", "OK", "<html>OK</html>"
                builder = MagicMock()
                builder.build.return_value = driver
                summary = {"completion_policy": "target_document", "resource_status": "partial",
                           "pending_count": 1, "pending_requests": [{"url": FORBES_ANALYTICS_URL}],
                           "warnings": ["optional request pending"]}
                fetcher = WikiFetcher(root, [browser], True)
                (root / f"tls_keys_{browser}.log").write_text("test-key")
                pcap = root / f"capture_{browser}.pcap"
                pcap.write_bytes(b"test-pcap")
                capture = MagicMock(summary={})
                capture.stop.return_value = pcap
                with patch.dict("wiki_fetcher.AVAILABLE_DRIVERS", {browser: builder}), \
                     patch("wiki_fetcher.PacketCapture", return_value=capture), \
                     patch.object(fetcher, "_wait_for_normal_page", return_value=summary):
                    record = fetcher._fetch_with(driver.current_url, browser, root)
                self.assertIsNone(record.error)
                entry = UrlEntry("1", "test", driver.current_url)
                self.assertTrue(fetcher._mark_complete(entry, root, browser, record))
                marker = json.loads((root / f"capture_{browser}.complete.json").read_text())
                self.assertEqual(marker["resource_status"], "partial")
                self.assertEqual(marker["completion_policy"], "target_document")
                pcap.unlink()
                self.assertFalse(fetcher._mark_complete(entry, root, browser, record))

    def test_analytics_query_rule_does_not_match_other_endpoints(self):
        rules = [FORBES_ANALYTICS_URL]
        for suffix in ("", "?v=2&en=page_view"):
            self.assertEqual(blocked_request_reason(FORBES_ANALYTICS_URL + suffix, rules),
                             "forbes_analytics_exclusion")
        for url in (FORBES_ANALYTICS_URL + "-other", FORBES_ANALYTICS_URL + "/extra",
                    "https://www.google-analytics.com.evil.test/g/collect?v=2",
                    "https://example.com/?next=" + FORBES_ANALYTICS_URL):
            self.assertIsNone(blocked_request_reason(url, rules))
        self.assertEqual(len(cdp_block_patterns(rules)), 2)
        self.assertEqual(bidi_block_patterns(rules)[0]["pathname"], "/g/collect")

    def test_cleanup_warning_preserves_checkpoint_but_real_failures_do_not(self):
        for browser, cls in (("chrome", webdriver.Chrome), ("edge", webdriver.Edge),
                             ("firefox", webdriver.Firefox)):
            for failure in (None, "navigation", "driver", "keylog", "pcap"):
                with self.subTest(browser=browser, failure=failure), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    profile = root / "profile"
                    profile.mkdir()
                    driver = MagicMock(spec=cls)
                    driver.command_executor = MagicMock()
                    driver.current_url, driver.title, driver.page_source = "https://example.com/", "OK", "<html>OK</html>"
                    details = {"forced": True, "stopped": failure != "driver", "exit_code": -9,
                               "warnings": ["Driver service required forced termination"],
                               "error": "Driver service is still running" if failure == "driver" else None}
                    driver.service = SimpleNamespace(shutdown_details=details)
                    if failure == "driver":
                        driver.quit.side_effect = WebDriverException(details["error"])
                    if failure == "navigation":
                        driver.get.side_effect = TimeoutException("navigation timed out")
                    builder = MagicMock()
                    builder.build.return_value = driver
                    (root / f"tls_keys_{browser}.log").write_text("" if failure == "keylog" else "test-key")
                    pcap = root / f"capture_{browser}.pcap"
                    pcap.write_bytes(b"test-pcap")
                    capture = MagicMock(summary={"error": "Invalid PCAP"} if failure == "pcap" else {})
                    capture.stop.return_value = None if failure == "pcap" else pcap
                    fetcher = WikiFetcher(root, [browser], True)
                    summary = {"completion_policy": "target_document", "resource_status": "complete"}
                    with patch.dict("wiki_fetcher.AVAILABLE_DRIVERS", {browser: builder}), \
                         patch("wiki_fetcher.tempfile.mkdtemp", return_value=str(profile)), \
                         patch("wiki_fetcher.PacketCapture", return_value=capture), \
                         patch.object(fetcher, "_wait_for_normal_page", return_value=summary), \
                         self.assertLogs("wiki_fetcher", level="WARNING") as logs:
                        record = fetcher._fetch_with(driver.current_url, browser, root)
                    self.assertTrue(any("Cleanup warning:" in line for line in logs.output))
                    if failure in ("driver", "navigation", "pcap"):
                        self.assertIsNotNone(record.error)
                        self.assertTrue(any("Capture failed:" in line for line in logs.output))
                    else:
                        self.assertIsNone(record.error)
                    self.assertEqual(record.cleanup_summary["retained_profile"], str(profile))
                    entry = UrlEntry("1", "test", driver.current_url)
                    self.assertEqual(fetcher._mark_complete(entry, root, browser, record), failure is None)
                    status = json.loads((root / f"network_status_{browser}.json").read_text())
                    self.assertEqual(status["cleanup_summary"]["warnings"], details["warnings"])
                    if failure is None:
                        marker = json.loads((root / f"capture_{browser}.complete.json").read_text())
                        self.assertEqual(marker["cleanup_summary"]["warnings"], details["warnings"])

    def test_startup_navigation_metadata_and_quit_use_distinct_budgets(self):
        class Base:
            def execute(self, command, params=None):
                return self.command_executor.client_config.timeout

        class Driver(BoundedCommands, Base):
            pass

        driver = Driver()
        driver.command_executor = SimpleNamespace(client_config=SimpleNamespace(timeout=120),
                                                   _conn=SimpleNamespace(connection_pool_kw={}))
        for command, expected in ((Command.NEW_SESSION, 30), (Command.GET, 35),
                                  (Command.GET_TITLE, 10), (Command.QUIT, 5)):
            self.assertEqual(driver.execute(command), expected)
        self.assertEqual(driver.command_executor._conn.connection_pool_kw["retries"], 0)
