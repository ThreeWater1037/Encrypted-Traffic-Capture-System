import json
import unittest
from unittest.mock import patch

from selenium.common.exceptions import TimeoutException

from browser_loading import wait_for_resources


def event(method, **params):
    return {"message": json.dumps({"message": {"method": method, "params": params}})}


def request(request_id, url="https://example.com/image.png", **kwargs):
    return event("Network.requestWillBeSent", requestId=request_id,
                 loaderId="page", frameId="main", type="Image", request={"url": url}, **kwargs)


class FakeDriver:
    def __init__(self, clock, timeline):
        self.clock, self.timeline, self.commands = clock, list(timeline), []

    def get_log(self, name):
        result = []
        while self.timeline and self.timeline[0][0] <= self.clock[0]:
            result.extend(self.timeline.pop(0)[1])
        return result

    def execute_cdp_cmd(self, name, params):
        self.commands.append(name)
        return {"frameTree": {"frame": {"id": "main", "loaderId": "page"}}}


class ResourceWaitTests(unittest.TestCase):
    def run_wait(self, timeline):
        clock = [0.0]
        driver = FakeDriver(clock, timeline)
        def sleep(seconds):
            clock[0] += seconds
        with patch("browser_loading.time.monotonic", side_effect=lambda: clock[0]), \
             patch("browser_loading.time.sleep", side_effect=sleep):
            result = wait_for_resources(driver)
        return result, driver, clock[0]

    def test_unknown_host_stalls_after_five_seconds(self):
        result, driver, duration = self.run_wait([(0, [request("1")])])
        self.assertEqual(result[0]["reason"], "no_progress_for_5_seconds")
        self.assertIn("Page.stopLoading", driver.commands)
        self.assertGreaterEqual(duration, 5)
        self.assertLess(duration, 5.3)

    def test_streaming_image_over_five_seconds_is_preserved(self):
        timeline = [(0, [request("1"), request("stalled")])]
        for second in (2, 4, 6, 8):
            timeline.append((second, [event("Network.dataReceived", requestId="1", dataLength=10)]))
        timeline.append((9, [event("Network.loadingFinished", requestId="1")]))
        result, driver, duration = self.run_wait(timeline)
        self.assertEqual(len(result), 1)
        self.assertGreaterEqual(duration, 9)
        self.assertIn("Page.stopLoading", driver.commands)

    def test_completed_image_does_not_trigger_stop(self):
        result, driver, duration = self.run_wait([
            (0, [request("1")]),
            (4, [event("Network.loadingFinished", requestId="1")]),
        ])
        self.assertEqual(result, [])
        self.assertNotIn("Page.stopLoading", driver.commands)

    def test_blocked_legacy_url_is_retained_for_recapture(self):
        url = "https://myweb.hit.edu.cn/old.png"
        result, _, _ = self.run_wait([(0, [request("1", url), event(
            "Network.loadingFailed", requestId="1", blockedReason="inspector")])])
        self.assertEqual(result[0]["url"], url)
        self.assertEqual(result[0]["reason"], "isolated_legacy_host")

    def test_continuous_download_hits_safety_limit_as_failure(self):
        timeline = [(0, [request("1")])]
        timeline.extend((second, [event("Network.dataReceived", requestId="1")])
                        for second in range(1, 100))
        with self.assertRaises(TimeoutException):
            self.run_wait(timeline)
