from __future__ import annotations

import unittest

from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions

from browser_proxy import parse_browser_proxy
from wiki_fetcher import (
    _apply_chromium_proxy,
    _apply_firefox_proxy,
    _detect_browser_error_page,
)


class BrowserProxyTests(unittest.TestCase):
    def test_http_proxy_is_applied_to_chromium(self) -> None:
        proxy = parse_browser_proxy("http://127.0.0.1:7890")
        options = ChromeOptions()

        _apply_chromium_proxy(options, proxy)

        self.assertIn(
            "--proxy-server=http://127.0.0.1:7890",
            options.arguments,
        )

    def test_http_proxy_is_applied_to_firefox_http_and_https(self) -> None:
        proxy = parse_browser_proxy("http://192.168.1.2:7890")
        options = FirefoxOptions()

        _apply_firefox_proxy(options, proxy)

        preferences = options.preferences
        self.assertEqual(preferences["network.proxy.type"], 1)
        self.assertEqual(preferences["network.proxy.http"], "192.168.1.2")
        self.assertEqual(preferences["network.proxy.http_port"], 7890)
        self.assertEqual(preferences["network.proxy.ssl"], "192.168.1.2")
        self.assertEqual(preferences["network.proxy.ssl_port"], 7890)

    def test_socks5_proxy_enables_remote_dns_in_firefox(self) -> None:
        proxy = parse_browser_proxy("socks5://127.0.0.1:1080")
        options = FirefoxOptions()

        _apply_firefox_proxy(options, proxy)

        preferences = options.preferences
        self.assertEqual(preferences["network.proxy.socks_version"], 5)
        self.assertTrue(preferences["network.proxy.socks_remote_dns"])

    def test_proxy_without_port_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "主机和端口"):
            parse_browser_proxy("http://127.0.0.1")

    def test_chromium_error_page_is_detected(self) -> None:
        html = """
        <html>
          <div id="main-frame-error">
            <div class="error-code">ERR_TIMED_OUT</div>
          </div>
        </html>
        """

        error = _detect_browser_error_page(
            "https://zh.wikipedia.org/wiki/Test",
            html,
        )

        self.assertEqual(error, "Chromium network error page: ERR_TIMED_OUT")

    def test_normal_page_is_not_reported_as_browser_error(self) -> None:
        error = _detect_browser_error_page(
            "https://zh.wikipedia.org/wiki/Test",
            "<html><title>Test - Wikipedia</title><body>article</body></html>",
        )

        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
