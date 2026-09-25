from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wiki_fetcher import (
    ChromeDriver, EdgeDriver, FirefoxDriver, _DriverHttpClient, _resolve_driver_path,
)


class DriverResolutionTests(unittest.TestCase):
    def test_explicit_driver_bypasses_path_and_online_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = Path(tmp) / 'driver.exe'
            driver.write_bytes(b'fixture')
            driver.chmod(0o755)
            for browser, env_name in [('chrome', 'CHROMEDRIVER_PATH'),
                                      ('edge', 'EDGEDRIVER_PATH'),
                                      ('firefox', 'GECKODRIVER_PATH')]:
                with self.subTest(browser=browser), \
                     patch.dict(os.environ, {env_name: str(driver)}), \
                     patch('wiki_fetcher.shutil.which') as which, \
                     patch('wiki_fetcher._driver_cache_manager') as cache:
                    self.assertEqual(_resolve_driver_path(browser), str(driver.resolve()))
                    which.assert_not_called()
                    cache.assert_not_called()

    def test_invalid_explicit_driver_does_not_silently_download(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {'CHROMEDRIVER_PATH': tmp}), \
             patch('wiki_fetcher._driver_cache_manager') as cache:
            with self.assertRaisesRegex(RuntimeError, 'not an executable file'):
                _resolve_driver_path('chrome')
            cache.assert_not_called()

    def test_path_driver_bypasses_online_resolution_for_each_browser(self):
        for browser, name in [('chrome', 'chromedriver'), ('edge', 'msedgedriver'),
                              ('firefox', 'geckodriver')]:
            with self.subTest(browser=browser), patch.dict(os.environ, {}, clear=True), \
                 patch('wiki_fetcher.shutil.which', return_value='/local/' + name) as which, \
                 patch('wiki_fetcher._driver_cache_manager') as cache:
                self.assertEqual(_resolve_driver_path(browser), '/local/' + name)
                which.assert_called_once_with(name)
                cache.assert_not_called()

    def test_online_fallback_uses_cache_and_bounded_http_client(self):
        for browser, manager in [('chrome', 'ChromeDriverManager'),
                                 ('edge', 'EdgeChromiumDriverManager'),
                                 ('firefox', 'GeckoDriverManager')]:
            with self.subTest(browser=browser), patch.dict(os.environ, {}, clear=True), \
                 patch('wiki_fetcher.shutil.which', return_value=None), \
                 patch('wiki_fetcher._driver_cache_manager') as cache, \
                 patch('wiki_fetcher.' + manager) as factory:
                factory.return_value.install.return_value = '/downloaded/driver'
                self.assertEqual(_resolve_driver_path(browser), '/downloaded/driver')
                self.assertIs(factory.call_args.kwargs['cache_manager'], cache.return_value)
                self.assertIsInstance(factory.call_args.kwargs['download_manager'].http_client,
                                      _DriverHttpClient)

    def test_http_timeouts_apply_to_metadata_and_download_requests(self):
        with patch('webdriver_manager.core.http.requests.get') as get:
            get.return_value.status_code = 200
            client = _DriverHttpClient()
            client.get('https://example.com/metadata.json')
            self.assertEqual(get.call_args.kwargs['timeout'], (10, 30))
            client.get('https://example.com/driver.zip', timeout=(2, 5))
            self.assertEqual(get.call_args.kwargs['timeout'], (2, 5))

    def test_browser_builders_pass_resolved_driver_to_selenium(self):
        for browser, builder, service in [('chrome', ChromeDriver, 'ChromeService'),
                                          ('edge', EdgeDriver, 'EdgeService'),
                                          ('firefox', FirefoxDriver, 'FirefoxService')]:
            with self.subTest(browser=browser), \
                 patch.object(builder, '_find_binary', return_value='/browser'), \
                 patch('wiki_fetcher._resolve_driver_path', return_value='/local/driver') as resolve, \
                 patch('wiki_fetcher.' + service) as service_type, \
                 patch('wiki_fetcher.webdriver.' + browser.capitalize()), \
                 patch('wiki_fetcher._initialize_chromium_network'), \
                 patch.dict(os.environ, {}, clear=False):
                builder().build(Path('/keys'), Path('/profile'))
                resolve.assert_called_once_with(browser)
                service_type.assert_called_once_with('/local/driver')


if __name__ == '__main__':
    unittest.main()
