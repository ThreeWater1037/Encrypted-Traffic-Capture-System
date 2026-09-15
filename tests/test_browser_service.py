import subprocess
import unittest
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import WebDriverException
from browser_service import ProcessWaitShutdown


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
    def test_closed_shutdown_socket_with_completed_exit_is_success(self, opener):
        opener.return_value.open.side_effect = OSError("connection closed")
        service = self.service()
        service.stop()
        self.assertIsNone(service.shutdown_details["error"])
        self.assertIn("connection closed",service.shutdown_details["shutdown_request_note"])

    @patch("browser_service.request.build_opener")
    def test_unresponsive_service_is_bounded_and_not_silently_successful(self, opener):
        service = self.service(waits=[subprocess.TimeoutExpired("driver",3),
                                      subprocess.TimeoutExpired("driver",2),None])
        with self.assertRaises(WebDriverException):
            service.stop()
        self.assertTrue(service.shutdown_details["forced"])
        self.assertEqual(service.process.wait.call_count,3)
        service.process.terminate.assert_called_once()
        service.process.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
