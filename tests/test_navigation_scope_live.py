"""Real local browser events replayed late to reproduce startup-iframe contamination."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import browser_loading
from browser_loading import NetworkIdleTracker
from wiki_fetcher import AVAILABLE_DRIVERS, _prepare_navigation


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/parent":
            body = b'<iframe id="child" src="/child"></iframe>'
        elif self.path == "/child":
            body = b'<script>fetch("/unfinished").catch(()=>{});</script>'
        elif self.path == "/target":
            body = b'<script>fetch("/target-fetch").then(r=>r.text());</script>'
        else:
            body = b'complete'
        body = b'<!doctype html><link rel="icon" href="data:,"><body>' + body
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            if self.path == "/unfinished":
                self.wfile.write(body[:1]);self.wfile.flush()
                self.server.request_started.set()
                self.server.release.wait(15)
                self.wfile.write(body[1:])
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@unittest.skipUnless(os.environ.get("RUN_NAVIGATION_SCOPE_LIVE") == "1", "opt-in real browser")
class NavigationScopeLiveTests(unittest.TestCase):
    def setUp(self):
        self.browser = os.environ.get("WIKI_TEST_BROWSER", "chrome")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.release = threading.Event()
        self.server.request_started = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.temp = tempfile.TemporaryDirectory(prefix="navigation-scope-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.driver = AVAILABLE_DRIVERS[self.browser].build(root/"keys.log",root)
        self.addCleanup(self.driver.quit)
        self.addCleanup(self.driver._capture_cache_policy.close)
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.server.release.set()
        self.server.shutdown();self.server.server_close();self.thread.join(3)

    def open_parent(self):
        _prepare_navigation(self.driver,self.base+"/parent")
        self.driver.get(self.base+"/parent")
        self.assertTrue(self.server.request_started.wait(5))

    def test_late_startup_iframe_events_do_not_taint_target(self):
        self.open_parent()
        adapter = vars(self.driver).get("_capture_firefox_network")
        tree = adapter.frame_tree() if adapter else self.driver.execute_cdp_cmd("Page.getFrameTree",{})["frameTree"]
        main = tree["frame"]["id"]
        saved = []
        for entry in browser_loading._performance_events(self.driver):
            m=json.loads(entry["message"])["message"];p=m.get("params",{})
            if ((m["method"]=="Network.requestWillBeSent" and p.get("frameId")!=main)
                    or (m["method"]=="Page.frameAttached" and p.get("frameId")!=main)
                    or (m["method"]=="Page.frameNavigated" and p.get("frame",{}).get("id")!=main)):
                saved.append(entry)
        self.assertTrue(any("/unfinished" in e["message"] for e in saved),saved)
        _prepare_navigation(self.driver,self.base+"/target")
        self.driver.get(self.base+"/target")
        original = browser_loading._performance_events
        def delayed(driver):
            result = original(driver)
            result.extend(saved);saved.clear()
            return result
        with patch("browser_loading._performance_events",side_effect=delayed):
            tracker=NetworkIdleTracker(self.driver,completion_policy="target_document",timeout=6)
            result=tracker.wait()
            result=tracker.wait()
        self.assertEqual(result["resource_status"],"complete",result)
        self.assertTrue(any("/unfinished" in r["url"] for r in result["prior_navigation_requests"]))
        fetch=[r for r in result["requests"] if r["url"].endswith("/target-fetch")]
        self.assertEqual(len(fetch),1,result)
        self.assertEqual(fetch[0]["state"],"finished")
        print(json.dumps({"browser":self.browser,"resource_status":result["resource_status"],
                          "prior_requests":len(result["prior_navigation_requests"])},ensure_ascii=True))

    def test_real_target_iframe_interruption_still_marks_partial(self):
        self.open_parent()
        self.driver.execute_script("document.getElementById('child').remove()")
        result=NetworkIdleTracker(self.driver,completion_policy="target_document",timeout=6).wait()
        self.assertEqual(result["resource_status"],"partial",result)
        self.assertTrue(result["detached_requests"] or result["failed_requests"],result)
        self.assertFalse(any(r["url"].endswith("/unfinished")
                             for r in result["prior_navigation_requests"]))
