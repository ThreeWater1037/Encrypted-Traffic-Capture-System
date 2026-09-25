import subprocess
import unittest
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import WebDriverException
from browser_service import ProcessWaitShutdown, TimedChromeService, TimedEdgeService, TimedFirefoxService


class FakeService(ProcessWaitShutdown):
    port = 12345
    _owns_log_output = False


class BrowserServiceTests(unittest.TestCase):
    def service(self, *, exited=False, waits=None):
        service = FakeService()
        process = service.process = MagicMock()
        process.stdin = process.stdout = process.stderr = None
        self.running = not exited
        process.poll.side_effect = lambda:None if self.running else 0
        outcomes = list(waits or [None])
        def wait(timeout):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            self.running = False
            return 0
        process.wait.side_effect = wait
        return service

    @patch("browser_service.request.build_opener")
    def test_natural_exit_uses_process_wait_and_is_idempotent(self, opener):
        service = self.service()
        service.stop()
        service.stop()
        opener.return_value.open.assert_called_once_with("http://127.0.0.1:12345/shutdown",timeout=1.0)
        service.process.wait.assert_called_once_with(timeout=3.0)
        service.process.terminate.assert_not_called()
        self.assertFalse(service.shutdown_details["forced"])

    @patch("browser_service.request.build_opener")
    def test_already_exited_does_not_probe_closed_port(self, opener):
        service = self.service(exited=True)
        service.stop()
        opener.assert_not_called()
        service.process.wait.assert_not_called()

    @patch("browser_service.request.build_opener")
    def test_firefox_stops_owned_process_without_unsupported_shutdown_endpoint(self, opener):
        service = self.service()
        service.has_shutdown_endpoint = TimedFirefoxService.has_shutdown_endpoint
        service.stop()
        opener.assert_not_called()
        service.process.terminate.assert_called_once()
        self.assertIsNone(service.shutdown_details["error"])

    def test_firefox_unexpected_exit_is_not_hidden_as_requested_termination(self):
        service = self.service(exited=True)
        service.has_shutdown_endpoint = False
        service.process.poll.side_effect = None
        service.process.poll.return_value = 2
        with self.assertRaisesRegex(WebDriverException, "exited with code 2"):
            service.stop()
        service.process.terminate.assert_not_called()

    @patch("browser_service.request.build_opener")
    def test_closed_shutdown_socket_with_completed_exit_is_success(self, opener):
        opener.return_value.open.side_effect = OSError("connection closed")
        service = self.service()
        service.stop()
        self.assertIsNone(service.shutdown_details["error"])
        self.assertIn("connection closed",service.shutdown_details["shutdown_request_note"])

    @patch("browser_service.request.build_opener")
    def test_forced_exit_is_a_warning_only_after_confirmed_stop_for_all_browsers(self, opener):
        for cls in (TimedChromeService, TimedEdgeService, TimedFirefoxService):
            for needs_kill in (False, True):
                with self.subTest(browser=cls.__name__, needs_kill=needs_kill):
                    waits = [subprocess.TimeoutExpired("driver", 3)]
                    if needs_kill:
                        waits.append(subprocess.TimeoutExpired("driver", 2))
                    service = self.service(waits=waits + [None])
                    service.has_shutdown_endpoint = cls.has_shutdown_endpoint
                    service.process.poll.side_effect = lambda: None if self.running else -9
                    service.stop()
                    self.assertTrue(service.shutdown_details["forced"])
                    self.assertTrue(service.shutdown_details["stopped"])
                    self.assertIsNone(service.shutdown_details["error"])
                    self.assertEqual(len(service.shutdown_details["warnings"]), 1)
                    self.assertEqual(service.process.wait.call_count, 3 if needs_kill else 2)
                    self.assertEqual(service.process.kill.call_count, int(needs_kill))
                    service.stop()  # No second termination after the confirmed stop.
                    self.assertEqual(service.process.wait.call_count, 3 if needs_kill else 2)

    @patch("browser_service.request.build_opener")
    def test_failed_force_stop_remains_error_for_all_browsers(self, opener):
        for cls in (TimedChromeService, TimedEdgeService, TimedFirefoxService):
            for outcome in (subprocess.TimeoutExpired("driver", 2), PermissionError("kill denied")):
                with self.subTest(browser=cls.__name__, outcome=outcome):
                    service = self.service(waits=[subprocess.TimeoutExpired("driver", 3),
                                                  subprocess.TimeoutExpired("driver", 2), outcome])
                    service.has_shutdown_endpoint = cls.has_shutdown_endpoint
                    with self.assertRaises(WebDriverException):
                        service.stop()
                    self.assertFalse(service.shutdown_details["stopped"])
                    self.assertTrue(service.shutdown_details["error"])
                    self.assertFalse(service._capture_stop_done)


if __name__ == "__main__":
    unittest.main()
