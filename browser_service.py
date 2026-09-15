"""Bounded local Chromium service shutdown with process-based completion."""

import subprocess
import time
from urllib import request

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService


class ProcessWaitShutdown:
    """Stop only this service's child; do not poll a closed HTTP port."""

    shutdown_request_timeout = 1.0
    shutdown_process_timeout = 3.0

    def stop(self):
        if getattr(self, "_capture_stop_done", False):
            return
        started = time.perf_counter()
        details = {"forced": False, "error": None, "timings": {}}
        self.shutdown_details = details
        process = getattr(self, "process", None)
        try:
            if process is not None and process.poll() is None:
                phase = time.perf_counter()
                try:
                    # Locally created Chromium drivers listen on IPv4 loopback.
                    # Bypass proxy environment and localhost IPv6 retry delays.
                    opener = request.build_opener(request.ProxyHandler({}))
                    with opener.open(f"http://127.0.0.1:{self.port}/shutdown",
                                     timeout=self.shutdown_request_timeout):
                        pass
                except Exception as exc:
                    # A process exiting while replying may close the socket.
                    # Its process handle below determines actual completion.
                    details["shutdown_request_note"] = str(exc)
                details["timings"]["shutdown_request"] = time.perf_counter() - phase
                phase = time.perf_counter()
                try:
                    process.wait(timeout=self.shutdown_process_timeout)
                except subprocess.TimeoutExpired:
                    details["forced"] = True
                    details["error"] = "Driver service did not exit after shutdown"
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2.0)
                details["timings"]["process_exit"] = time.perf_counter() - phase
            if process is not None:
                details["exit_code"] = process.poll()
                if details["exit_code"] is None:
                    details["error"] = "Driver service is still running"
                elif details["exit_code"] != 0 and not details["error"]:
                    details["error"] = f"Driver service exited with code {details['exit_code']}"
        except Exception as exc:
            details["error"] = str(exc)
        finally:
            stopped = process is None or process.poll() is not None
            self._capture_stop_done = stopped
            if stopped and process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass
            if stopped and getattr(self, "_owns_log_output", False):
                try:
                    self.log_output.close()
                except (OSError, ValueError):
                    pass
            details["timings"]["total"] = time.perf_counter() - started
        if details["error"]:
            raise WebDriverException(details["error"])


class TimedChromeService(ProcessWaitShutdown, ChromeService):
    pass


class TimedEdgeService(ProcessWaitShutdown, EdgeService):
    pass
