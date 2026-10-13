import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from process_ownership import LINUX, LinuxProcessOwner


class OwnershipSafetyTests(unittest.TestCase):
    def test_pid_reuse_is_not_signaled(self):
        with patch("process_ownership._identity", return_value="new-start"), \
             patch("process_ownership.os.kill") as kill, \
             patch("process_ownership.os.pidfd_open", create=True, return_value=99), \
             patch("process_ownership.signal.pidfd_send_signal", create=True) as send, \
             patch("process_ownership.os.close"):
            LinuxProcessOwner._signal(43210, "old-start", signal.SIGTERM)
        kill.assert_not_called()
        send.assert_not_called()


@unittest.skipUnless(LINUX, "Linux /proc ownership integration")
class OwnershipIntegrationTests(unittest.TestCase):
    def test_task_session_cleans_separate_browser_group_after_environment_is_erased(self):
        owner = LinuxProcessOwner("CAPTURE_TEST_OWNER")
        try:
            with tempfile.TemporaryDirectory() as directory:
                ready = Path(directory) / "ready"
                child_code = (
                    "import os,signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                    f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
                )
                parent_code = (
                    "import subprocess,sys; "
                    f"subprocess.Popen([sys.executable,'-c',{child_code!r}], "
                    "env={},process_group=0,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
                )
                parent = subprocess.Popen([sys.executable, "-c", parent_code],
                                          env=owner.environment(os.environ), start_new_session=True)
                owner.bind(parent, session=True)
                parent.wait(timeout=10)
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists())
                child_pid = int(ready.read_text())
                self.assertIn(child_pid, owner.members())
                result = owner.terminate(grace=0.1, kill_wait=3)
                self.assertEqual(result["remaining_pids"], [])
                self.assertIn(child_pid, result["signaled_pids"])
        finally:
            owner.terminate(grace=0.1)

    def test_reparented_detached_child_is_killed_without_touching_other_work(self):
        owner = LinuxProcessOwner("CAPTURE_TEST_OWNER")
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with tempfile.TemporaryDirectory() as directory:
                artifact = Path(directory) / "capture.pcap"
                artifact.write_bytes(b"existing-capture-do-not-change")
                ready = Path(directory) / "ready"
                child_code = (
                    "import os,signal,time; from pathlib import Path; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)"
                )
                parent_code = (
                    "import subprocess,sys; "
                    f"subprocess.Popen([sys.executable,'-c',{child_code!r}], "
                    "start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
                )
                parent = subprocess.Popen([sys.executable, "-c", parent_code],
                                          env=owner.environment(os.environ), start_new_session=True)
                parent.wait(timeout=10)
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists(), "child did not become ready")
                child_pid = int(ready.read_text())
                self.assertIn(child_pid, owner.members())
                details = owner.terminate(grace=0.1, kill_wait=3)
                self.assertTrue(details["forced"])
                self.assertEqual(details["remaining_pids"], [])
                self.assertIn(child_pid, details["signaled_pids"])
                self.assertIsNone(unrelated.poll())
                self.assertEqual(artifact.read_bytes(), b"existing-capture-do-not-change")
                self.assertEqual(owner.terminate()["signaled_pids"], [])
        finally:
            owner.terminate(grace=0.1)
            unrelated.terminate()
            unrelated.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
