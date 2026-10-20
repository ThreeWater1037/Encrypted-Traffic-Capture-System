"""Wikipedia uses current capture with scoped language; legacy stays parked."""
import hashlib
import wiki_fetcher
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import call, patch, MagicMock
from selenium import webdriver
import wiki_legacy_2874751 as legacy
from wiki_fetcher import WikiFetcher, UrlEntry, _is_wikipedia_url


class WikipediaPacingTests(unittest.TestCase):
    def test_legacy_source_is_exact_commit_except_six_language_lines(self):
        source = Path(legacy.__file__).read_text(encoding='utf-8')
        removals = {
            '        opts.add_argument("--lang=zh-CN")\n': 2,
            '        opts.add_experimental_option("prefs", {"intl.accept_languages": "zh-CN"})\n': 2,
            '        opts.set_preference("intl.accept_languages", "zh-CN")\n': 1,
            '        opts.set_preference("intl.locale.requested", "zh-CN")\n': 1,
        }
        for line, count in removals.items():
            self.assertEqual(source.count(line), count)
            source = source.replace(line, '')
        self.assertEqual(hashlib.sha256(source.encode()).hexdigest(),
                         'aaa68b8e95f54580f6affb4cd18fc1946f1823b4ae57f2fb064a26c7a427d711')

    def test_domain_boundary(self):
        for url in ('https://wikipedia.org/', 'https://zh.wikipedia.org/wiki/A',
                    'https://EN.M.WIKIPEDIA.ORG./wiki/A'):
            self.assertTrue(_is_wikipedia_url(url), url)
        for url in ('https://wikipedia.org.example.com/', 'https://notwikipedia.org/',
                    'https://example.com/wiki/A', 'https://example.com/?next=https://wikipedia.org',
                    'https://wikipedia.org@example.com/', 'https://today.hit.edu.cn/',
                    'https://www.forbeschina.com/', 'https://www.wikimedia.org/'):
            self.assertFalse(_is_wikipedia_url(url), url)

    def test_current_builders_scope_language_and_keep_fast_options(self):
        for browser in ('chrome', 'edge', 'firefox'):
            for language in (None, 'zh-CN'):
                cls=browser.capitalize()
                builder=wiki_fetcher.AVAILABLE_DRIVERS[browser]
                with self.subTest(browser=browser,language=language), \
                     patch.object(builder,'_find_binary',return_value='/browser'), \
                     patch('wiki_fetcher._resolve_driver_path',return_value='/driver'), \
                     patch('wiki_fetcher.'+cls+'Service'), \
                     patch('wiki_fetcher.Capture'+cls) as factory, \
                     patch('wiki_fetcher._initialize_chromium_network',side_effect=lambda d:d), \
                     patch('wiki_fetcher._initialize_firefox_network',side_effect=lambda d:d), patch.dict(os.environ):
                    builder.build(Path('/keys'),Path('/profile'),language=language)
                    opts=factory.call_args.kwargs['options']
                    self.assertEqual(opts.page_load_strategy,'eager')
                    if browser=='firefox':
                        self.assertEqual(opts.preferences.get('intl.accept_languages'),language)
                        self.assertEqual(opts.preferences.get('intl.locale.requested'),language)
                        self.assertFalse(opts.preferences['dom.serviceWorkers.enabled'])
                        self.assertTrue(opts.capabilities['webSocketUrl'])
                    else:
                        self.assertEqual(opts.experimental_options.get('prefs',{}).get('intl.accept_languages'),language)
                        self.assertEqual('--lang=zh-CN' in opts.arguments,language=='zh-CN')

    def test_wiki_and_other_sites_use_current_capture_for_all_browsers(self):
        for browser in ('chrome','edge','firefox'):
            for url,kwargs in [('https://zh.wikipedia.org/wiki/A',{'language':'zh-CN'}),
                               ('https://example.com/',{})]:
                driver=MagicMock(spec=getattr(webdriver,browser.capitalize()))
                driver.command_executor=MagicMock();driver.capabilities={}
                driver.current_url=url;driver.title='Test';driver.page_source='<html>Test</html>'
                builder=MagicMock();builder.build.return_value=driver
                with self.subTest(browser=browser,url=url), tempfile.TemporaryDirectory() as tmp, \
                     patch.dict(wiki_fetcher.AVAILABLE_DRIVERS,{browser:builder}), \
                     patch('wiki_fetcher._prepare_navigation'), \
                     patch.object(WikiFetcher,'_wait_for_normal_page',return_value={'completion_policy':'target_document'}), \
                     patch.object(legacy.WikiFetcher,'_fetch_with',side_effect=AssertionError('Parked legacy called')):
                    root=Path(tmp);record=WikiFetcher(root,[browser],False)._fetch_with(url,browser,root)
                    self.assertIsNone(record.error,record.error)
                    self.assertEqual(builder.build.call_args.kwargs,kwargs)
                    self.assertEqual(record.network_summary['completion_policy'],'target_document')
                    self.assertTrue((root/f'network_status_{browser}.json').exists())

    def test_runtime_has_no_legacy_import_or_routing(self):
        source=Path(wiki_fetcher.__file__).read_text(encoding='utf-8')
        self.assertNotIn('wiki_legacy_2874751',source)
        self.assertFalse(hasattr(WikiFetcher,'_legacy_wiki'))

    def test_mixed_batch_uses_configured_current_interval(self):
        entries=[UrlEntry(str(i),'test',url) for i,url in enumerate([
            'https://zh.wikipedia.org/wiki/A','https://example.com/',
            'https://en.wikipedia.org/wiki/B','https://today.hit.edu.cn/','https://www.forbeschina.com/'])]
        self.assertEqual(wiki_fetcher.INTERVAL_BETWEEN_URLS,1.0)
        self.assertEqual(wiki_fetcher.NETWORK_IDLE_SECONDS,0.5)
        for interval in (1,5):
            with tempfile.TemporaryDirectory() as tmp:
                fetcher=WikiFetcher(Path(tmp),['firefox'],False,interval_seconds=interval)
                with patch.object(fetcher,'_run_entry',side_effect=[(1,0,0),(1,0,0),(0,1,0),(0,0,1),(1,0,0)]), \
                     patch('wiki_fetcher.time.sleep') as sleep:
                    self.assertFalse(fetcher.run(entries,total=len(entries)))
                self.assertEqual(sleep.call_args_list,[call(interval),call(interval),call(interval)])
