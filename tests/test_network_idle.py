import json
import unittest
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import TimeoutException, WebDriverException

from browser_loading import NetworkIdleTracker, wait_for_network_idle


def event(method, timestamp=None, **params):
    if timestamp is not None:
        params["timestamp"] = 100.0 + timestamp
    return {"message": json.dumps({"message": {"method": method, "params": params}})}


def request(request_id, timestamp, url="https://example.com/image.png", **kwargs):
    params = dict(requestId=request_id, loaderId="page", frameId="main",
                  type="Image", request={"url": url})
    params.update(kwargs)
    return event("Network.requestWillBeSent", timestamp, **params)


def finished(request_id, timestamp):
    return event("Network.loadingFinished", timestamp, requestId=request_id,
                 encodedDataLength=100)


class FakeDriver:
    def __init__(self, clock, timeline, metrics=True):
        self.clock, self.timeline = clock, list(timeline)
        self.commands, self.metrics = [], metrics

    def get_log(self, name):
        assert name == "performance"
        result = []
        while self.timeline and self.timeline[0][0] <= self.clock[0] + 1e-9:
            result.extend(self.timeline.pop(0)[1])
        return result

    def execute_cdp_cmd(self, name, params):
        self.commands.append(name)
        if name == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "main", "loaderId": "page",
                                           "url": "https://example.com/"}}}
        if name == "Performance.getMetrics":
            if not self.metrics:
                raise WebDriverException("metrics unavailable")
            return {"metrics": [{"name": "Timestamp", "value": 100.0 + self.clock[0]}]}
        return {}


class NetworkIdleTests(unittest.TestCase):
    def test_child_target_finished_event_completes_request_without_double_counting(self):
        clock = [0.0]
        main = request("document", 0, type="Document")
        driver = FakeDriver(clock, [(0, [main])])
        policy = driver._capture_cache_policy = MagicMock()
        policy.drain_events.side_effect = [
            [json.loads(e["message"])["message"] for e in (main, finished("document", 0))],
        ] + [[]] * 20
        with patch("browser_loading.time.monotonic", side_effect=lambda:clock[0]), \
             patch("browser_loading.time.sleep", side_effect=lambda t:clock.__setitem__(0,clock[0]+t)):
            result = wait_for_network_idle(driver)
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["finished_count"], 1)
        self.assertEqual(result["pending_count"], 0)
        self.assert_duration(clock[0], 0.5)

    def test_cache_event_is_retained_as_audit_evidence(self):
        result, _, _ = self.run_wait([(0, [request("cached", 0),
            event("Network.requestServedFromCache", requestId="cached"),
            finished("cached", 0)])])
        self.assertEqual(len(result["cache_hit_requests"]), 1)
        self.assertTrue(result["cache_hit_requests"][0]["served_from_cache"])

    def test_two_cdp_sessions_do_not_double_count_blocked_request_with_timestamp_jitter(self):
        clock = [0.0]
        url = "http://today2.hit.edu.cn/legacy.png"
        first = [request("31108.6", 0, url=url),
                 event("Network.loadingFailed", 0.000016, requestId="31108.6",
                       errorText="", canceled=False, blockedReason="inspector")]
        duplicate = [request("31108.6", 0.000018, url=url),
                     event("Network.loadingFailed", 0.000024, requestId="31108.6",
                           errorText="", canceled=False, blockedReason="inspector")]
        driver = FakeDriver(clock, [(0, first)])
        policy = driver._capture_cache_policy = MagicMock()
        policy.drain_events.side_effect = [
            [json.loads(e["message"])["message"] for e in duplicate],
        ] + [[]] * 20
        with patch("browser_loading.time.monotonic", side_effect=lambda: clock[0]), \
             patch("browser_loading.time.sleep", side_effect=lambda t: clock.__setitem__(0, clock[0] + t)):
            result = wait_for_network_idle(driver)
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(len(result["failed_requests"]), 1)
        self.assertEqual(result["failed_requests"][0]["request_id"], "31108.6")
        self.assertEqual(result["failed_requests"][0]["blockedReason"], "inspector")
        self.assertEqual(result["requests"][0]["started_timestamp"], 100.0)

    def test_related_worker_without_frame_id_is_included(self):
        clock = [0.0]
        driver = FakeDriver(clock, [])
        policy = driver._capture_cache_policy = MagicMock()
        policy.snapshot.return_value = {"targets":[{"session_id":"worker-session", "type":"worker"}]}
        messages = [json.loads(e["message"])["message"] for e in (
            request("worker-fetch", 0, frameId=None, loaderId="", type="Fetch"),
            finished("worker-fetch", 0))]
        for message in messages:
            message["sessionId"] = "worker-session"
        policy.drain_events.side_effect = [messages] + [[]] * 20
        with patch("browser_loading.time.monotonic", side_effect=lambda:clock[0]), \
             patch("browser_loading.time.sleep", side_effect=lambda t:clock.__setitem__(0,clock[0]+t)):
            result = wait_for_network_idle(driver)
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["finished_count"], 1)
        self.assertEqual(result["pending_count"], 0)

    def run_wait(self, timeline, *, metrics=True, **kwargs):
        clock = [0.0]
        driver = FakeDriver(clock, timeline, metrics)

        def sleep(seconds):
            clock[0] += seconds

        with patch("browser_loading.time.monotonic", side_effect=lambda: clock[0]), \
             patch("browser_loading.time.sleep", side_effect=sleep):
            result = wait_for_network_idle(driver, **kwargs)
        json.dumps(result)  # The ledger is written to disk by the fetcher.
        return result, driver, clock[0]

    def assert_duration(self, actual, expected):
        self.assertGreaterEqual(actual + 1e-8, expected)
        self.assertLess(actual, expected + 0.052)

    def test_recheck_reuses_completed_window_without_another_sleep(self):
        clock = [0.0]
        driver = FakeDriver(clock, [(0, [request("page", 0), finished("page", 0)])])
        with patch("browser_loading.time.monotonic", side_effect=lambda:clock[0]), \
             patch("browser_loading.time.sleep", side_effect=lambda t:clock.__setitem__(0,clock[0]+t)):
            tracker = NetworkIdleTracker(driver)
            tracker.wait()
            clock[0] += 0.1  # metadata work
            before = clock[0]
            result = tracker.wait()
        self.assertEqual(clock[0], before)
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(driver.commands.count("Page.getFrameTree"), 1)

    def test_recheck_includes_requests_started_during_metadata(self):
        clock = [0.0]
        driver = FakeDriver(clock, [
            (0, [request("page", 0), finished("page", 0)]),
            (0.6, [request("late", 0.6, type="Fetch")]),
            (1.8, [finished("late", 1.8)]),
        ])
        with patch("browser_loading.time.monotonic", side_effect=lambda:clock[0]), \
             patch("browser_loading.time.sleep", side_effect=lambda t:clock.__setitem__(0,clock[0]+t)):
            tracker = NetworkIdleTracker(driver)
            tracker.wait()
            clock[0] = 0.7
            result = tracker.wait()
        self.assert_duration(clock[0], 2.3)
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["pending_count"], 0)

    def test_recheck_does_not_restart_deadline(self):
        clock = [0.0]
        driver = FakeDriver(clock, [(0,[request("page",0),finished("page",0)]),
                                     (0.6,[request("late",0.6)])])
        with patch("browser_loading.time.monotonic", side_effect=lambda:clock[0]), \
             patch("browser_loading.time.sleep", side_effect=lambda t:clock.__setitem__(0,clock[0]+t)):
            tracker = NetworkIdleTracker(driver, timeout=1.0)
            tracker.wait()
            clock[0] = 0.7
            with self.assertRaises(TimeoutException):
                tracker.wait()
        self.assertLess(clock[0], 1.052)

    def test_waits_500_ms_after_completion(self):
        result, driver, duration = self.run_wait([(0, [request("a", 0), finished("a", 0)])])
        self.assert_duration(duration, 0.5)
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["finished_count"], 1)
        self.assertEqual(result["pending_count"], 0)
        self.assertNotIn("Page.stopLoading", driver.commands)

    def test_navigation_time_already_quiet_counts(self):
        result, _, duration = self.run_wait([(0, [request("a", -1), finished("a", -0.3)])])
        self.assert_duration(duration, 0.2)
        self.assertEqual(result["clock_source"], "cdp_monotonic")

    def test_pending_download_survives_silence_longer_than_500_ms(self):
        _, _, duration = self.run_wait([
            (0, [request("slow", 0)]),
            (2.0, [finished("slow", 2.0)]),
        ])
        self.assert_duration(duration, 2.5)

    def test_later_xhr_resets_quiet_interval(self):
        result, _, duration = self.run_wait([
            (0, [request("page", 0, type="Document"), finished("page", 0)]),
            (0.45, [request("xhr", 0.45, type="XHR")]),
            (0.6, [finished("xhr", 0.6)]),
        ])
        self.assert_duration(duration, 1.1)
        self.assertEqual(result["request_count"], 2)

    def test_failed_fetch_finishes_and_preserves_error(self):
        result, _, duration = self.run_wait([
            (0, [request("fetch", 0, type="Fetch")]),
            (0.2, [event("Network.loadingFailed", 0.2, requestId="fetch",
                         errorText="net::ERR_FAILED", corsErrorStatus={
                             "corsError": "HeaderDisallowedByPreflightResponse"})]),
        ])
        self.assert_duration(duration, 0.7)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual(result["failed_requests"][0]["url"], "https://example.com/image.png")
        self.assertEqual(result["failed_requests"][0]["error"], "net::ERR_FAILED")
        self.assertIn("corsErrorStatus", result["failed_requests"][0])

    def test_background_page_and_previous_loader_do_not_block_or_reset(self):
        result, _, duration = self.run_wait([
            (0, [request("page", 0), finished("page", 0),
                 request("old", 0, loaderId="new-tab")]),
            (0.4, [request("background", 0.4, frameId="other", loaderId="other"),
                   event("Network.dataReceived", 0.4, requestId="old")]),
        ])
        self.assert_duration(duration, 0.5)
        self.assertEqual(result["request_count"], 1)

    def test_websocket_and_eventsource_streams_do_not_block(self):
        result, _, duration = self.run_wait([(0, [
            request("page", 0), finished("page", 0),
            request("sse", 0, type="EventSource"),
            request("ws", 0, type="WebSocket", url="wss://example.com/socket"),
        ])])
        self.assert_duration(duration, 0.5)
        self.assertEqual(result["request_count"], 1)

    def test_eventsource_identified_by_response_is_removed_from_pending(self):
        result, _, duration = self.run_wait([
            (0, [request("sse", 0, type="Other")]),
            (0.1, [event("Network.responseReceived", 0.1, requestId="sse",
                         type="EventSource", response={"status": 200})]),
        ])
        self.assert_duration(duration, 0.6)
        self.assertEqual(result["requests"][0]["state"], "ignored_long_lived")
        self.assertEqual(result["pending_count"], 0)

    def test_cache_hit_stays_pending_until_loading_finished(self):
        result, _, duration = self.run_wait([
            (0, [request("cached", 0), event("Network.requestServedFromCache",
                                           requestId="cached")]),
            (1.0, [finished("cached", 1.0)]),
        ])
        self.assert_duration(duration, 1.5)
        self.assertEqual(result["finished_count"], 1)

    def test_redirect_chain_is_one_pending_request_and_two_audited_hops(self):
        result, _, duration = self.run_wait([
            (0, [request("doc", 0, url="https://example.com/old", type="Document")]),
            (0.1, [request("doc", 0.1, url="https://example.com/new", type="Document",
                           redirectResponse={"status": 302})]),
            (0.2, [finished("doc", 0.2)]),
        ])
        self.assert_duration(duration, 0.7)
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(result["finished_count"], 2)
        self.assertEqual(result["redirect_count"], 1)
        self.assertEqual(result["requests"][0]["status"], 302)

    def test_same_url_redirects_keep_each_hop_despite_reusing_request_id(self):
        url = "https://example.com/repeated-redirect"
        result, _, duration = self.run_wait([
            (0, [request("doc", 0, url=url, type="Document")]),
            (0.1, [request("doc", 0.1, url=url, type="Document",
                           redirectResponse={"status": 302, "url": url})]),
            (0.2, [request("doc", 0.2, url=url, type="Document",
                           redirectResponse={"status": 302, "url": url})]),
            (0.3, [finished("doc", 0.3)]),
        ])
        self.assert_duration(duration, 0.8)
        self.assertEqual(result["request_count"], 3)
        self.assertEqual(result["redirect_count"], 2)
        self.assertEqual(result["finished_count"], 3)
        self.assertEqual(result["pending_count"], 0)
        self.assertEqual([r["state"] for r in result["requests"]],
                         ["redirected", "redirected", "finished"])

    def test_iframe_and_nested_frame_requests_are_included_and_removed_on_detach(self):
        result, _, duration = self.run_wait([
            (0, [event("Page.frameAttached", frameId="child", parentFrameId="main"),
                 event("Page.frameNavigated", frame={"id": "child", "parentId": "main",
                                                     "loaderId": "child-page"}),
                 event("Page.frameAttached", frameId="nested", parentFrameId="child"),
                 request("iframe", 0, frameId="child", loaderId="child-page"),
                 request("nested-image", 0, frameId="nested", loaderId="nested-page")]),
            (1.0, [event("Page.frameDetached", frameId="child", reason="remove")]),
        ])
        self.assert_duration(duration, 1.5)
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(len(result["detached_requests"]), 2)

    def test_iframe_process_swap_does_not_discard_pending_download(self):
        result, _, duration = self.run_wait([
            (0, [event("Page.frameAttached", frameId="child", parentFrameId="main"),
                 request("iframe", 0, frameId="child", loaderId="child-page")]),
            (0.1, [event("Page.frameDetached", frameId="child", reason="swap")]),
            (1.0, [finished("iframe", 1.0)]),
        ])
        self.assert_duration(duration, 1.5)
        self.assertEqual(result["finished_count"], 1)
        self.assertEqual(result["detached_requests"], [])

    def test_existing_frame_navigation_is_pending_before_loader_commit(self):
        for frame_id in ("main", "child"):
            with self.subTest(frame_id=frame_id):
                result, _, duration = self.run_wait([
                    (0, [request("page", 0), finished("page", 0),
                         event("Page.frameAttached", frameId="child", parentFrameId="main"),
                         event("Page.frameNavigated", frame={"id": "child", "parentId": "main",
                                                             "loaderId": "old-child"})]),
                    (0.2, [request("new-document", 0.2, type="Document",
                                   frameId=frame_id, loaderId="new-loader")]),
                    (1.4, [event("Page.frameNavigated", frame={"id": frame_id,
                                                              "loaderId": "new-loader"}),
                           finished("new-document", 1.4)]),
                ])
                self.assert_duration(duration, 1.9)
                self.assertEqual(result["request_count"], 2)
                self.assertEqual(result["requests"][-1]["state"], "finished")

    def test_missing_metrics_falls_back_to_conservative_receipt_time(self):
        result, _, duration = self.run_wait(
            [(0, [request("page", -3), finished("page", -2)])], metrics=False)
        self.assert_duration(duration, 0.5)
        self.assertEqual(result["clock_source"], "log_receipt")

    def test_pending_request_hits_bounded_timeout_with_evidence(self):
        with self.assertRaises(TimeoutException) as caught:
            self.run_wait([(0, [request("stalled", 0)])], timeout=0.8)
        summary = caught.exception.network_idle_summary
        self.assertEqual(summary["pending_count"], 1)
        self.assertEqual(summary["pending_requests"][0]["url"], "https://example.com/image.png")
        self.assertLess(summary["wait_seconds"], 0.852)

    def test_continuous_completed_requests_hit_timeout_too(self):
        timeline = [(index / 10, [request(str(index), index / 10),
                                 finished(str(index), index / 10)]) for index in range(12)]
        with self.assertRaises(TimeoutException) as caught:
            self.run_wait(timeline, timeout=1.0)
        self.assertEqual(caught.exception.network_idle_summary["pending_count"], 0)

    def test_empty_target_log_does_not_claim_http_navigation_succeeded(self):
        with self.assertRaises(TimeoutException) as caught:
            self.run_wait([])
        self.assertIn("No target HTTP(S) requests", caught.exception.msg)
        self.assertEqual(caught.exception.network_idle_summary["request_count"], 0)

    def test_rejects_nonpositive_limits(self):
        for kwargs in ({"idle_seconds": 0}, {"timeout": 0}, {"idle_seconds": -1},
                       {"idle_seconds": float("nan")}, {"timeout": float("inf")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_wait([], **kwargs)


if __name__ == "__main__":
    unittest.main()
