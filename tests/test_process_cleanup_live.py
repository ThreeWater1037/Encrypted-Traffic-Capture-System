"""Opt-in Linux browser lifecycle checks; RUN_PROCESS_CLEANUP_LIVE=1.

Uses separate test profiles and never accesses existing capture outputs.
"""

import os
from pathlib import Path
import tempfile
import unittest
from selenium.common.exceptions import WebDriverException

from process_ownership import LINUX, _identity
from wiki_fetcher import ChromeDriver, EdgeDriver, FirefoxDriver


@unittest.skipUnless(LINUX and os.getenv("RUN_PROCESS_CLEANUP_LIVE") == "1",
                     "opt-in real Linux Chrome/Edge/Firefox cleanup")
class ProcessCleanupLiveTests(unittest.TestCase):
    def test_normal_quit_and_driver_crash_for_each_browser(self):
        for builder in (ChromeDriver, EdgeDriver, FirefoxDriver):
            for crash in (False, True):
                with self.subTest(browser=builder.__name__, driver_crash=crash):
                    with tempfile.TemporaryDirectory(prefix="cleanup_validation_") as directory:
                        root = Path(directory)
                        profile = root / "profile"
                        profile.mkdir()
                        driver = None
                        try:
                            driver = builder().build(root / "keys.log", profile)
                            owner = driver.service._capture_process_owner
                            original_processes = owner.members()
                            self.assertGreaterEqual(len(original_processes), 2)
                            driver.get("data:text/html,<title>cleanup validation</title>")
                            self.assertEqual(driver.title, "cleanup validation")
                            policy = vars(driver).get("_capture_cache_policy")
                            if policy is not None:
                                policy.close()
                            if crash:
                                driver.service.process.kill()
                                driver.service.process.wait(timeout=5)
                                # Browser remains independently alive after its
                                # driver is lost, exactly the production failure.
                                with self.assertRaisesRegex(WebDriverException, "exited with code"):
                                    driver.service.stop()
                            else:
                                driver.quit()
                            self.assertEqual(owner.members(), {})
                            for pid, identity in original_processes.items():
                                self.assertNotEqual(_identity(pid), identity, f"browser process {pid} survived")
                            self.assertTrue(driver.service.shutdown_details["stopped"])
                            print("CLEANUP_LIVE", builder.__name__, "driver_crash" if crash else "normal", "PASS", flush=True)
                        finally:
                            if driver is not None:
                                driver.service._capture_process_owner.terminate()
                            os.environ.pop("SSLKEYLOGFILE", None)


if __name__ == "__main__":
    unittest.main()
