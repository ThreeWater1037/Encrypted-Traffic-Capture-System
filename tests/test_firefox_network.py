"""Firefox protocol normalization and common capture contract regressions."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import TimeoutException

from browser_cache import CachePolicyError
from browser_firefox import FirefoxNetworkPolicy
from browser_loading import NetworkIdleTracker
from wiki_fetcher import FirefoxDriver, WikiFetcher, _prepare_navigation


def bidi(method, request_id="r", *, context="main", hop=0, url="https://example.com/a", **extra):
    params = {"context": context, "redirectCount": hop,
              "request": {"request": request_id, "url": url, "destination": "", "initiatorType": "fetch"}}
    params.update(extra)
    return {"method": "network." + method, "params": params}


class FirefoxNetworkTests(unittest.TestCase):
    def make_policy(self):
        driver = MagicMock()
        policy = FirefoxNetworkPolicy(driver)
        driver._capture_firefox_network = policy
        policy._connected = True
        policy._request = MagicMock(return_value={})
        policy.frame_tree = lambda: {"frame": {"id": "main", "url": "https://example.com/"}}
        return driver, policy

    def run_timeline(self, timeline, *, timeout=5.0, recheck_at=None):
        driver, policy = self.make_policy()
        clock = [0.0]
        schedule = list(timeline)
        original = policy.network_events

        def events():
            while schedule and schedule[0][0] <= clock[0] + 1e-9:
                _, messages = schedule.pop(0)
                for message in messages:
                    policy._handle_event(message)
            return original()

        policy.network_events = events
        with patch("browser_loading.time.monotonic", side_effect=lambda: clock[0]), \
             patch("browser_loading.time.sleep", side_effect=lambda t: clock.__setitem__(0, clock[0] + t)):
            tracker = NetworkIdleTracker(driver, timeout=timeout)
            result = tracker.wait()
            if recheck_at is not None:
                clock[0] = recheck_at
                result = tracker.wait()
        json.dumps(result)
        return result, clock[0]

    @staticmethod
    def response(method="responseCompleted", request_id="r", **kwargs):
        return bidi(method, request_id, response={"status": 200, "fromCache": False, "mimeType": "text/plain"}, **kwargs)

    def test_pending_body_waits_for_completion_and_quiet(self):
        result, elapsed = self.run_timeline([(0, [bidi("beforeRequestSent"), self.response("responseStarted")]),
                                             (2, [self.response()])])
        self.assertGreaterEqual(elapsed, 2.5)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["clock_source"], "bidi_monotonic_receipt")

    def test_metadata_recheck_includes_new_fetch(self):
        result, elapsed = self.run_timeline([(0, [bidi("beforeRequestSent"), self.response()]),
            (.6, [bidi("beforeRequestSent", "late")]), (1.8, [self.response(request_id="late")])], recheck_at=.7)
        self.assertGreaterEqual(elapsed, 2.3)
        self.assertEqual(result["finished_count"], 2)

    def test_metadata_recheck_keeps_original_deadline(self):
        with self.assertRaises(TimeoutException) as caught:
            self.run_timeline([(0, [bidi("beforeRequestSent"), self.response()]),
                (.6, [bidi("beforeRequestSent", "late")])], timeout=1, recheck_at=.7)
        self.assertEqual(caught.exception.network_idle_summary["pending_count"], 1)
        self.assertLess(caught.exception.network_idle_summary["wait_seconds"], 1.06)

    def test_worker_without_context_is_observed(self):
        result, _ = self.run_timeline([(0, [bidi("beforeRequestSent", context=None),
                                            self.response(context=None)])])
        self.assertEqual(result["finished_count"], 1)

    def test_redirect_hops_with_same_id_are_independent(self):
        result, _ = self.run_timeline([(0, [bidi("beforeRequestSent"),
            bidi("responseCompleted", response={"status": 302}),
            bidi("beforeRequestSent", hop=1), self.response(hop=1)])])
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["redirect_count"], 1)
        self.assertEqual(result["pending_count"], 0)

    def test_eventsource_does_not_block(self):
        result, _ = self.run_timeline([(0, [bidi("beforeRequestSent"),
            bidi("responseStarted", response={"status": 200, "mimeType": "text/event-stream"})])])
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["requests"][0]["state"], "ignored_long_lived")

    def test_frame_removal_preserves_unfinished_evidence(self):
        result, _ = self.run_timeline([(0, [
            {"method": "browsingContext.contextCreated", "params": {"context": "child", "parent": "main"}},
            bidi("beforeRequestSent", context="child")]),
            (.2, [{"method": "browsingContext.contextDestroyed", "params": {"context": "child"}}])])
        self.assertEqual(len(result["detached_requests"]), 1)
        self.assertEqual(result["pending_count"], 0)

    def test_cache_or_304_cannot_be_silently_accepted(self):
        for response in ({"status": 200, "fromCache": True}, {"status": 304}):
            _, policy = self.make_policy()
            policy._handle_event(bidi("responseStarted", response=response))
            with self.assertRaises(CachePolicyError):
                policy.check()
            self.assertTrue(policy.snapshot()["cache_hits"])

    def test_intercept_is_queued_off_receiver_and_preserves_reason(self):
        _, policy = self.make_policy()
        policy._handle_event(bidi("beforeRequestSent", isBlocked=True))
        self.assertEqual(policy._initializations.get_nowait(), "r")
        policy._request.assert_not_called()
        policy._handle_event(bidi("fetchError", errorText="NS_ERROR_ABORT"))
        self.assertEqual(policy.network_events()[-1]["params"]["blockedReason"], "inspector")

    def test_disconnected_policy_fails_closed(self):
        _, policy = self.make_policy()
        policy._connected = False
        with self.assertRaises(CachePolicyError):
            policy.check()

    def test_remote_debugger_endpoint_is_rejected(self):
        driver, policy = self.make_policy()
        driver.capabilities = {"webSocketUrl": "ws://example.com:1234/session"}
        with self.assertRaises(CachePolicyError):
            policy._debugger_url()

    def test_navigation_applies_legacy_blocking_only_for_hit(self):
        driver, policy = self.make_policy()
        policy.reset_observation = MagicMock()
        policy.block_hosts = MagicMock()
        _prepare_navigation(driver, "https://example.com/")
        policy.block_hosts.assert_not_called()
        _prepare_navigation(driver, "https://today.hit.edu.cn/test")
        policy.block_hosts.assert_called_once_with({"today2.hit.edu.cn", "myweb.hit.edu.cn"})

    def test_builder_enables_bidi_and_disables_service_workers(self):
        with patch.object(FirefoxDriver, "_find_binary", return_value="/firefox"), \
             patch("wiki_fetcher._resolve_driver_path", return_value="/gecko"), \
             patch("wiki_fetcher.FirefoxService"), patch("wiki_fetcher.webdriver.Firefox") as constructor, \
             patch("wiki_fetcher._initialize_firefox_network") as initialize:
            FirefoxDriver().build(Path("/keys"), Path("/profile"))
        options = constructor.call_args.kwargs["options"]
        self.assertTrue(options.to_capabilities()["webSocketUrl"])
        self.assertFalse(options.preferences["dom.serviceWorkers.enabled"])
        initialize.assert_called_once_with(constructor.return_value)

    def test_failed_firefox_navigation_still_writes_network_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = WikiFetcher(Path(tmp), ["firefox"], False)
            with patch("wiki_fetcher.AVAILABLE_DRIVERS") as drivers:
                drivers.__getitem__.return_value.name = "Firefox (Gecko)"
                drivers.__getitem__.return_value.build.side_effect = RuntimeError("BiDi unavailable")
                record = fetcher._fetch_with("https://example.com/", "firefox", Path(tmp))
            self.assertIn("BiDi unavailable", record.error)
            self.assertTrue((Path(tmp) / "network_status_firefox.json").exists())

    def test_firefox_legacy_exclusion_writes_partial_recapture_assessment(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetcher = WikiFetcher(Path(tmp), ["firefox"], False)
            driver = MagicMock()
            driver.current_url = "https://today.hit.edu.cn/article"
            driver.title = "Article"
            driver.page_source = "<html>article</html>"
            summary = {"failed_requests": [{"url": "http://today2.hit.edu.cn/a.png", "blockedReason": "inspector"}]}
            with patch("wiki_fetcher.AVAILABLE_DRIVERS") as drivers, \
                 patch("wiki_fetcher._prepare_navigation"), \
                 patch.object(fetcher, "_wait_for_normal_page", return_value=summary):
                drivers.__getitem__.return_value.name = "Firefox (Gecko)"
                drivers.__getitem__.return_value.build.return_value = driver
                record = fetcher._fetch_with(driver.current_url, "firefox", Path(tmp))
            self.assertIsNone(record.error)
            assessment = json.loads((Path(tmp) / "resource_status_firefox.json").read_text(encoding="utf-8"))
            self.assertTrue(assessment["needs_recapture"])
            self.assertEqual(assessment["resource_status"], "partial")
            self.assertEqual(assessment["skipped_resources"][0]["reason"], "isolated_legacy_host")
            self.assertTrue((Path(tmp) / "pages_needing_recapture.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
