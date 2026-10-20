"""Bot identity is opt-in, scoped to Wiki sessions, and equivalent in all browsers."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from selenium import webdriver
import wiki_fetcher as wf


class WikipediaBotIdentityTests(unittest.TestCase):
    def test_contact_is_not_invented_and_invalid_headers_fail(self):
        for contact in ("", "  "):
            with patch.dict(os.environ, WIKIPEDIA_BOT_CONTACT=contact):
                self.assertIsNone(wf._wikipedia_bot_user_agent())
        for contact in ("x\r\nX-Test: injected", "邮箱@example.com", "foo(bar)"):
            with patch.dict(os.environ, WIKIPEDIA_BOT_CONTACT=contact):
                with self.assertRaises(ValueError):
                    wf._wikipedia_bot_user_agent()
        with patch.dict(os.environ, WIKIPEDIA_BOT_CONTACT="operator@example.invalid"):
            self.assertEqual(wf._wikipedia_bot_user_agent(),
                             "EncryptedTrafficCaptureBot/1.0 (operator@example.invalid)")

    def test_all_browser_options(self):
        ua = "EncryptedTrafficCaptureBot/1.0 (operator@example.invalid)"
        for browser in ("chrome", "edge", "firefox"):
            for value in (None, ua):
                builder = wf.AVAILABLE_DRIVERS[browser]
                cls = browser.capitalize()
                with self.subTest(browser=browser, ua=value), \
                     patch.object(builder, "_find_binary", return_value="/browser"), \
                     patch("wiki_fetcher._resolve_driver_path", return_value="/driver"), \
                     patch("wiki_fetcher."+cls+"Service"), \
                     patch("wiki_fetcher.Capture"+cls) as factory, \
                     patch("wiki_fetcher._initialize_chromium_network", side_effect=lambda d:d), \
                     patch("wiki_fetcher._initialize_firefox_network", side_effect=lambda d:d), \
                     patch.dict(os.environ):
                    builder.build(Path("/keys"), Path("/profile"), language="zh-CN", user_agent=value)
                    opts = factory.call_args.kwargs["options"]
                    if browser == "firefox":
                        self.assertEqual(opts.preferences.get("general.useragent.override"), value)
                        self.assertFalse(opts.preferences["dom.serviceWorkers.enabled"])
                    else:
                        self.assertEqual([x for x in opts.arguments if x.startswith("--user-agent=")],
                                         [] if value is None else ["--user-agent="+value])
                    self.assertEqual(opts.page_load_strategy, "eager")

    def test_wiki_dispatch_only_even_when_configured(self):
        contact = "operator@example.invalid"
        for browser in ("chrome", "edge", "firefox"):
            for url, expected in [
                ("https://zh.wikipedia.org/wiki/A", {"language":"zh-CN",
                 "user_agent":f"EncryptedTrafficCaptureBot/1.0 ({contact})"}),
                ("https://example.com/", {}),
                ("https://wikipedia.org.example.com/", {}),
            ]:
                driver = MagicMock(spec=getattr(webdriver, browser.capitalize()))
                driver.command_executor = MagicMock()
                driver.capabilities = {}
                driver.current_url = url
                driver.title = "Test"
                driver.page_source = "<html>Test</html>"
                builder = MagicMock()
                builder.build.return_value = driver
                with self.subTest(browser=browser,url=url), tempfile.TemporaryDirectory() as tmp, \
                     patch.dict(os.environ, WIKIPEDIA_BOT_CONTACT=contact), \
                     patch.dict(wf.AVAILABLE_DRIVERS, {browser:builder}), \
                     patch("wiki_fetcher._prepare_navigation"), \
                     patch.object(wf.WikiFetcher, "_wait_for_normal_page",
                                  return_value={"completion_policy":"target_document"}):
                    root = Path(tmp)
                    result = wf.WikiFetcher(root,[browser],False)._fetch_with(url,browser,root)
                    self.assertIsNone(result.error, result.error)
                    self.assertEqual(builder.build.call_args.kwargs, expected)
