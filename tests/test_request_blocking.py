"""Site boundaries, protocol coverage, and explicit exclusion diagnostics."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from selenium import webdriver

from browser_cache import CachePolicyError, ChromiumCachePolicy
from browser_firefox import FirefoxNetworkPolicy
from browser_request_policy import FORBES_RECAPTCHA_URL, FORBES_ANALYTICS_URL, blocked_urls_for_page, bidi_block_patterns
from wiki_fetcher import WikiFetcher, _prepare_navigation


class RequestBlockingTests(unittest.TestCase):
    def test_scope_uses_hostname_not_substring(self):
        for url in ("https://forbeschina.com/a", "https://www.forbeschina.com/a",
                    "https://news.forbeschina.com/a", "https://WWW.FORBESCHINA.COM/a"):
            self.assertEqual(blocked_urls_for_page(url), [FORBES_RECAPTCHA_URL, FORBES_ANALYTICS_URL])
        for url in ("https://forbeschina.com.evil.test/", "https://evilforbeschina.com/",
                    "https://example.com/?next=https://forbeschina.com/",
                    "https://forbeschina.com@evil.test/"):
            self.assertEqual(blocked_urls_for_page(url), [])

    def test_all_browsers_apply_and_clear_exact_rule_before_navigation(self):
        for browser in (webdriver.Chrome, webdriver.Edge, webdriver.Firefox):
            with self.subTest(browser=browser):
                driver = MagicMock(spec=browser)
                policy = MagicMock()
                driver._capture_cache_policy = policy
                if browser is webdriver.Firefox:
                    driver._capture_firefox_network = policy
                _prepare_navigation(driver, "https://www.forbeschina.com/article")
                policy.set_blocked_urls.assert_called_once_with([FORBES_RECAPTCHA_URL, FORBES_ANALYTICS_URL])
                self.assertEqual(driver._capture_blocked_urls, [FORBES_RECAPTCHA_URL, FORBES_ANALYTICS_URL])
                driver.get.assert_not_called()
                _prepare_navigation(driver, "https://example.com/")
                policy.set_blocked_urls.assert_called_with([])
                self.assertEqual(driver._capture_blocked_urls, [])

    def test_bidi_analytics_port_is_nonempty_and_keeps_default_port_scope(self):
        for url, expected in (("https://www.google-analytics.com/g/collect", "443"),
                              ("http://localhost/g/collect", "80"),
                              ("https://localhost:8443/g/collect", "8443"),
                              ("http://localhost:8123/g/collect", "8123")):
            with self.subTest(url=url), patch("browser_request_policy.FORBES_ANALYTICS_URL", url):
                pattern = bidi_block_patterns([url])[0]
                self.assertEqual(pattern["port"], expected)
                self.assertNotIn("search", pattern)
                self.assertEqual(pattern["pathname"], "/g/collect")

    def test_bidi_uses_exact_url_and_removes_intercept(self):
        policy = FirefoxNetworkPolicy(MagicMock())
        policy._request = MagicMock(return_value={"intercept": "exact"})
        policy.set_blocked_urls([FORBES_RECAPTCHA_URL])
        policy._request.assert_called_with("network.addIntercept", {
            "phases": ["beforeRequestSent"],
            "urlPatterns": [{"type": "string", "pattern": FORBES_RECAPTCHA_URL}]})
        policy.set_blocked_urls([])
        policy._request.assert_called_with("network.removeIntercept", {"intercept": "exact"})
        self.assertEqual(policy._intercepts, [])

    def test_cdp_updates_existing_and_future_child_targets_before_resume(self):
        policy = ChromiumCachePolicy(MagicMock())
        policy._connected = True
        policy._request = MagicMock(return_value={})
        for sid in ("existing", "detached"):
            policy._targets[sid] = {"initialized": True, "detached": sid == "detached", "type": "page"}
        policy.set_blocked_urls([FORBES_RECAPTCHA_URL])
        policy._request.assert_called_once_with(
            "Fetch.enable", {"patterns": [{"urlPattern": FORBES_RECAPTCHA_URL,
                                           "requestStage": "Request"}]}, "existing")
        policy._handle_event({"method": "Target.attachedToTarget", "params": {
            "sessionId": "new", "waitingForDebugger": True,
            "targetInfo": {"targetId": "frame", "type": "iframe", "url": ""}}})
        policy._initializations.put(None)
        policy._initialize_loop()
        methods = [c.args[0] for c in policy._request.call_args_list if c.args[-1] == "new"]
        self.assertLess(methods.index("Fetch.enable"), methods.index("Runtime.runIfWaitingForDebugger"))

    def test_cdp_intercept_double_checks_url_and_never_blocks_receiver(self):
        policy = ChromiumCachePolicy(MagicMock())
        policy._blocked_urls = [FORBES_RECAPTCHA_URL]
        policy._request = MagicMock()
        for index, url in enumerate((FORBES_RECAPTCHA_URL, FORBES_RECAPTCHA_URL + "?keep=1",
                                     FORBES_RECAPTCHA_URL + "-other")):
            policy._handle_event({"method": "Fetch.requestPaused", "sessionId": "page", "params": {
                "requestId": str(index), "request": {"url": url}}})
        policy._request.assert_not_called()
        policy._initializations.put(None)
        policy._initialize_loop()
        self.assertEqual([c.args[0] for c in policy._request.call_args_list],
                         ["Fetch.failRequest", "Fetch.continueRequest", "Fetch.continueRequest"])

    def test_rejected_rule_fails_capture_and_is_persisted(self):
        for browser in ("chrome", "edge", "firefox"):
            with self.subTest(browser=browser), tempfile.TemporaryDirectory() as tmp:
                driver = MagicMock(spec=getattr(webdriver, browser.capitalize() if browser != "firefox" else "Firefox"))
                policy = MagicMock()
                policy.set_blocked_urls.side_effect = CachePolicyError("rule rejected")
                driver._capture_cache_policy = policy
                if browser == "firefox":
                    driver._capture_firefox_network = policy
                policy.snapshot.return_value = {}
                with patch("wiki_fetcher.AVAILABLE_DRIVERS") as builders:
                    builders.__getitem__.return_value.name = browser
                    builders.__getitem__.return_value.build.return_value = driver
                    record = WikiFetcher(Path(tmp), [browser], False)._fetch_with(
                        "https://www.forbeschina.com/article", browser, Path(tmp))
                self.assertIn("rule rejected", record.error)
                driver.get.assert_not_called()
                status = json.loads((Path(tmp) / f"network_status_{browser}.json").read_text(encoding="utf-8"))
                self.assertEqual(status["request_blocking"]["urls"], [FORBES_RECAPTCHA_URL, FORBES_ANALYTICS_URL])


if __name__ == "__main__":
    unittest.main()
