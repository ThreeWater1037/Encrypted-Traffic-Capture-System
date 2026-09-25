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

    def run_timeline(self, timeline, *, timeout=5.0, recheck_at=None, **tracker_options):
        driver, policy = self.make_policy()
        if tracker_options.get("completion_policy") == "target_document":
            policy.reject_cache_hits = False
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
            tracker = NetworkIdleTracker(driver, timeout=timeout, **tracker_options)
            result = tracker.wait()
            if recheck_at is not None:
                clock[0] = recheck_at
                result = tracker.wait()
        json.dumps(result)
        return result, clock[0]

    @staticmethod
    def response(method="responseCompleted", request_id="r", **kwargs):
        return bidi(method, request_id, response={"status": 200, "fromCache": False, "mimeType": "text/plain"}, **kwargs)

    def test_target_completion_with_hung_fetch_uses_shared_three_second_budget(self):
        doc_request = {"request": "doc", "url": "https://example.com/", "destination": "document"}
        result, elapsed = self.run_timeline([(0, [
            bidi("beforeRequestSent", request=doc_request),
            bidi("responseStarted", request=doc_request, response={"status": 200, "fromCache": False}),
            bidi("responseCompleted", request=doc_request, response={"status": 200, "fromCache": False}),
            bidi("beforeRequestSent", "hung")])], completion_policy="target_document")
        self.assertLess(elapsed, 3.06)
        self.assertEqual(result["target_document"]["type"], "Document")
        self.assertEqual(result["pending_count"], 1)
        self.assertEqual(result["completion_reason"], "resource_stall")

    def test_target_http_error_is_not_hidden_by_relaxed_policy(self):
        doc_request = {"request": "doc", "url": "https://example.com/", "destination": "document"}
        from selenium.common.exceptions import WebDriverException
        with self.assertRaisesRegex(WebDriverException, "Target document failed"):
            self.run_timeline([(0, [bidi("beforeRequestSent", request=doc_request),
                bidi("responseStarted", request=doc_request, response={"status": 503}),
                bidi("responseCompleted", request=doc_request, response={"status": 503})])],
                completion_policy="target_document")

    def test_target_policy_keeps_optional_cache_as_evidence(self):
        doc = {"request": "doc", "url": "https://example.com/", "destination": "document"}
        timeline = [(0, [bidi("beforeRequestSent", request=doc),
            bidi("responseStarted", request=doc, response={"status": 200, "fromCache": False}),
            bidi("responseCompleted", request=doc, response={"status": 200, "fromCache": False}),
            bidi("beforeRequestSent", "image"),
            bidi("responseStarted", "image", response={"status": 200, "fromCache": True}),
            bidi("responseCompleted", "image", response={"status": 200, "fromCache": True})])]
        result, _ = self.run_timeline(timeline, completion_policy="target_document")
        self.assertEqual(len(result["cache_hit_requests"]), 1)
        self.assertEqual(result["target_document"]["status"], 200)

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
            with self.assertRaisesRegex(CachePolicyError, "https://example.com/a"):
                policy.check()
            self.assertTrue(policy.snapshot()["cache_hits"])

    @staticmethod
    def image_events(request_id, *, cached=False, context="main", url="https://example.com/image.png",
                     status=200, size=100, mime="image/png", destination="image"):
        response = {"status": status, "fromCache": cached, "mimeType": mime, "bytesReceived": size}
        return [bidi(method, request_id, context=context, url=url,
                     request={"request": request_id, "url": url, "method": "GET", "destination": destination},
                     **({"response": response} if method != "beforeRequestSent" else {}))
                for method in ("beforeRequestSent", "responseStarted", "responseCompleted")]

    def test_same_document_image_reuse_keeps_evidence_and_passes_shared_ledger(self):
        result, _ = self.run_timeline([(0, self.image_events("download")),
                                      (.1, self.image_events("reuse", cached=True))])
        self.assertEqual(result["cache_hit_requests"], [])
        self.assertEqual(result["finished_count"], 2)
        reuse, = result["same_document_image_reuses"]
        self.assertTrue(reuse["from_disk_cache"])
        self.assertEqual(reuse["same_document_image_reuse"]["source_request_id"], "download:0")

    def test_image_reuse_requires_completed_uncached_image_in_same_document(self):
        for case in ("missing", "unfinished", "failed", "zero_bytes", "not_image", "different_url",
                     "different_context", "no_context", "reset", "navigation", "destroyed", "http304",
                     "fetch_not_image", "old_response_after_navigation", "previous_capture", "fetch_error"):
            with self.subTest(case=case):
                _, policy = self.make_policy()
                source = self.image_events("download", status=500 if case == "failed" else 200,
                                           size=0 if case == "zero_bytes" else 100,
                                           mime="text/plain" if case == "not_image" else "image/png")
                if case == "unfinished":
                    source = source[:2]
                if case == "missing":
                    source = []
                if case == "fetch_error":
                    source.insert(2, bidi("fetchError", "download", url="https://example.com/image.png"))
                for event in source:
                    policy._handle_event(event)
                if case == "reset":
                    policy.reset_observation()
                if case == "previous_capture":
                    _, policy = self.make_policy()
                if case in {"navigation", "destroyed", "old_response_after_navigation"}:
                    policy._handle_event({"method": "browsingContext." + (
                        "contextDestroyed" if case == "destroyed" else "navigationStarted"),
                        "params": {"context": "main", "url": "https://example.com/"}})
                    if case == "old_response_after_navigation":
                        policy._handle_event(source[-1])
                for event in self.image_events("reuse", cached=True,
                        url="https://example.com/other.png" if case == "different_url" else "https://example.com/image.png",
                        context={"different_context": "other", "no_context": None}.get(case, "main"),
                        status=304 if case == "http304" else 200,
                        destination="" if case == "fetch_not_image" else "image"):
                    policy._handle_event(event)
                with self.assertRaises(CachePolicyError):
                    policy.check()
                self.assertEqual(policy.snapshot()["same_document_image_reuses"], [])

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
             patch("wiki_fetcher.FirefoxService"), patch("wiki_fetcher.CaptureFirefox") as constructor, \
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
