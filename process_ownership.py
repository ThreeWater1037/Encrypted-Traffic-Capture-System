"""Linux child ownership survives reparenting and detached browser helpers.

Use dedicated process groups/sessions plus an inherited marker for helpers.
No profile, capture, or checkpoint files are modified by this module.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid


LINUX = sys.platform.startswith("linux")
PROC = Path("/proc")


def _identity(pid: int) -> str | None:
    try:
        fields = (PROC / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        # Zombies no longer consume a browser's memory and cannot be signalled
        # out of existence; only their parent can reap them.
        return None if fields[0] in {"Z", "X"} else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


class LinuxProcessOwner:
    def __init__(self, key: str):
        if not LINUX:
            raise RuntimeError("Linux process ownership requires /proc")
        self.key = key
        self.token = uuid.uuid4().hex
        self._marker = f"{key}={self.token}".encode()
        self._lock = threading.Lock()
        self._binding = None
        self._closed = False

    def environment(self, base) -> dict[str, str]:
        return {**base, self.key: self.token}

    def bind(self, process, *, session: bool = False) -> None:
        """Bind immediately after Popen created an isolated group or session."""
        self._binding = (process.pid, _identity(process.pid),
                         process.pid if session else os.getsid(0), session)

    def members(self) -> dict[int, str]:
        if self._closed:
            return {}
        found = {}
        binding = self._binding
        if binding is not None:
            leader, identity, sid, session = binding
            current = _identity(leader)
            if current is not None and current != identity:
                binding = None  # An already-released group ID was reused.
        for entry in PROC.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                pid = int(entry.name)
                fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
                if fields[0] in {"Z", "X"}:
                    continue
                in_group = binding is not None and int(fields[3]) == binding[2] and (
                    binding[3] or int(fields[2]) == binding[0])
                # Chromium overwrites its environment when changing process
                # titles. The kernel group/session is authoritative even then.
                if in_group or self._marker in (entry / "environ").read_bytes().split(b"\0"):
                    found[pid] = fields[19]
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                # Other users' processes need not expose their environment.
                # An unreadable process belonging to us makes verification fail.
                try:
                    if entry.stat().st_uid == os.geteuid():
                        raise
                except FileNotFoundError:
                    pass
        return found

    @staticmethod
    def _signal(pid: int, identity: str, sig: int) -> None:
        descriptor = None
        try:
            if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
                descriptor = os.pidfd_open(pid)
            if _identity(pid) != identity:
                return  # PID was recycled after the ownership snapshot.
            if descriptor is not None:
                signal.pidfd_send_signal(descriptor, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def terminate(self, grace: float = 3.0, kill_wait: float = 2.0) -> dict:
        """Recheck the whole owned family, even if its original parent exited."""
        with self._lock:
            signaled = set()
            forced = False
            remaining = self.members()
            for sig, budget in ((signal.SIGTERM, grace), (signal.SIGKILL, kill_wait)):
                sent = set()
                deadline = time.monotonic() + budget
                while remaining:
                    for pid, identity in remaining.items():
                        if (pid, identity) not in sent:
                            self._signal(pid, identity, sig)
                            sent.add((pid, identity))
                            signaled.add(pid)
                            forced |= sig == signal.SIGKILL
                    remaining = self.members()
                    if not remaining or time.monotonic() >= deadline:
                        break
                    time.sleep(0.05)
                if not remaining:
                    break
            self._closed = not remaining
            return {"signaled_pids": sorted(signaled), "forced": forced,
                    "remaining_pids": sorted(remaining)}
