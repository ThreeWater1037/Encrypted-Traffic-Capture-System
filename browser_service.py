"""Bounded local browser service shutdown with process-based completion."""

import subprocess
import sys
import time
from urllib import request

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.firefox.service import Service as FirefoxService
from process_ownership import LINUX, LinuxProcessOwner


class ProcessWaitShutdown:
    """Stop the owned driver and its Linux browser family."""

    shutdown_request_timeout = 1.0
    shutdown_process_timeout = 3.0
    has_shutdown_endpoint = True

    def _start_process(self, path):
        if LINUX:
            if sys.version_info < (3, 11):
                raise RuntimeError("Linux browser process cleanup requires Python 3.11 or newer")
            self._capture_process_owner = LinuxProcessOwner("CAPTURE_BROWSER_OWNER")
            self.env = self._capture_process_owner.environment(self.env)
            # Keep the task's session, but isolate this driver's process group.
            # No preexec_fn: browser launches can happen in threaded Workers.
            self.popen_kw["process_group"] = 0
        result = super()._start_process(path)
        if LINUX:
            self._capture_process_owner.bind(self.process)
        return result

    def stop(self):
        if getattr(self, "_capture_stop_done", False):
            return
        started = time.perf_counter()
        details = {"forced": False, "error": None, "warnings": [], "timings": {}}
        termination_requested = False
        self.shutdown_details = details
        process = getattr(self, "process", None)
        try:
            if process is not None and process.poll() is None:
                phase = time.perf_counter()
                try:
                    # Locally created Chromium drivers listen on IPv4 loopback.
                    # Bypass proxy environment and localhost IPv6 retry delays.
                    if self.has_shutdown_endpoint:
                        opener = request.build_opener(request.ProxyHandler({}))
                        with opener.open(f"http://127.0.0.1:{self.port}/shutdown",
                                         timeout=self.shutdown_request_timeout):
                            pass
                    else:
                        # GeckoDriver has no /shutdown endpoint and can stay
                        # listening after DELETE /session. Terminate our owned
                        # driver process after Firefox.quit has ended the session.
                        process.terminate()
                        termination_requested = True
                        details["stop_method"] = "terminate_owned_driver"
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
                    details["warnings"].append(
                        "Driver service did not exit after shutdown; forced termination required"
                    )
                    process.terminate()
                    termination_requested = True
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
                elif details["exit_code"] != 0 and not termination_requested and not details["error"]:
                    details["error"] = f"Driver service exited with code {details['exit_code']}"
        except Exception as exc:
            details["error"] = str(exc)
        finally:
            owner = vars(self).get("_capture_process_owner")
            family_stopped = True
            if owner is not None:
                try:
                    family = details["process_family"] = owner.terminate()
                    family_stopped = not family["remaining_pids"]
                    if family["signaled_pids"]:
                        details["forced"] = True
                        details["warnings"].append("Remaining owned browser processes required termination")
                    if not family_stopped:
                        details["error"] = f"Owned browser processes still running: {family['remaining_pids']}"
                    if process is not None and process.poll() is None and family_stopped:
                        process.wait(timeout=2.0)
                except Exception as exc:
                    family_stopped = False
                    details["error"] = f"Owned browser cleanup failed: {exc}"
            stopped = (process is None or process.poll() is not None) and family_stopped
            details["stopped"] = stopped
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


class TimedFirefoxService(ProcessWaitShutdown, FirefoxService):
    has_shutdown_endpoint = False
