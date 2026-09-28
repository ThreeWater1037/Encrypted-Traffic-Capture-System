"""Feed systemd only after a real HTTP request traverses the Worker pool."""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
from urllib.request import ProxyHandler, build_opener

log = logging.getLogger(__name__)


class ServiceWatchdog:
    def __init__(self, address: str, url: str, interval: float, timeout: float):
        self.address = address
        self.url = url
        self.interval = interval
        self.timeout = timeout
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="http-watchdog", daemon=True)
        self.opener = build_opener(ProxyHandler({}))

    @classmethod
    def from_environment(cls, host: str, port: int):
        address = os.environ.get("NOTIFY_SOCKET")
        budget = int(os.environ.get("WATCHDOG_USEC", "0")) / 1_000_000
        pid = int(os.environ.get("WATCHDOG_PID", str(os.getpid())))
        if not address or budget <= 0 or pid != os.getpid() or not hasattr(socket, "AF_UNIX"):
            return None
        local = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
        if ":" in local:
            local = f"[{local}]"
        return cls(address, f"http://{local}:{port}/api/v1/health",
                   min(30.0, budget / 4), min(5.0, budget / 8))

    def check_once(self) -> bool:
        try:
            with self.opener.open(self.url, timeout=self.timeout) as response:
                if response.status != 200 or json.loads(response.read(65536)).get("status") != "ok":
                    return False
            address = "\0" + self.address[1:] if self.address.startswith("@") else self.address
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
                channel.settimeout(self.timeout)
                channel.sendto(b"WATCHDOG=1", address)
            return True
        except Exception:
            log.warning("Worker HTTP health check failed; withholding systemd watchdog heartbeat",
                        exc_info=True)
            return False

    def _run(self):
        while not self.stopping.is_set():
            self.check_once()
            self.stopping.wait(self.interval)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopping.set()
        self.thread.join(timeout=self.timeout + 1)
