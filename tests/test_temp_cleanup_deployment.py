"""Deployment cleanup sequencing and self-contained source acceptance."""

import base64
import hashlib
import io
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TempCleanupDeploymentTests(unittest.TestCase):
    def test_linux_cleanup_is_after_stop_and_before_restart_for_both_scripts(self):
        for name in ("deploy_worker_root.sh", "deploy_worker_git_root.sh"):
            with self.subTest(script=name):
                source = (ROOT / "deployment" / name).read_text(encoding="utf-8")
                command = re.search(r'^"\$PYTHON" "[^"\n]+/capture_temp_cleanup.py" --worker-config "\$CONFIG_FILE"$',
                                    source, re.M)
                self.assertIsNotNone(command)
                self.assertLess(source.index("systemctl stop traffic-worker"), command.start())
                self.assertLess(source.index("print('Data directory:', data_path)"), command.start())
                self.assertLess(command.end(), source.index("systemctl restart traffic-worker"))
                if name == "deploy_worker_root.sh":
                    self.assertIn('"$TMP_DIR/source/capture_temp_cleanup.py"', command.group())

    def test_windows_cleanup_follows_stop_and_code_update_before_normal_startup(self):
        source = (ROOT / "deployment/deploy_worker_windows.ps1").read_text(encoding="utf-8")
        command = "Invoke-Native $Python @((Join-Path $ProjectDir 'capture_temp_cleanup.py'), '--worker-config', $ConfigFile)"
        self.assertEqual(source.count(command), 1)
        position = source.index(command)
        self.assertLess(source.index("Stop-ScheduledTask -TaskName $TaskName"), position)
        self.assertLess(source.index("'merge','--ff-only','origin/main'"), position)
        self.assertLess(source.index("'prepare',$RuntimeFile"), position)
        self.assertLess(position, source.rindex("Start-ScheduledTask -TaskName $TaskName"))

    def test_embedded_archive_contains_current_cleaner_and_complete_import_dependencies(self):
        source = (ROOT / "deployment/deploy_worker_root.sh").read_text(encoding="utf-8")
        encoded = source.split("__WORKER_SOURCE_ARCHIVE_BELOW__\n", 1)[1].split("__WORKER_SOURCE_ARCHIVE_END__", 1)[0]
        packed = base64.b64decode(encoded)
        expected = re.search(r"SOURCE_ARCHIVE_SHA256=([0-9a-f]{64})", source).group(1)
        self.assertEqual(hashlib.sha256(packed).hexdigest(), expected)
        with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as archive, tempfile.TemporaryDirectory() as tmp:
            for member in archive.getmembers():
                self.assertTrue(member.isfile())
                self.assertFalse(Path(member.name).is_absolute())
                self.assertNotIn("..", Path(member.name).parts)
                data = archive.extractfile(member).read()
                path = Path(tmp) / member.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            for name in ("capture_temp_cleanup.py", "wiki_fetcher.py", "worker_agent/task_runner.py"):
                self.assertEqual((Path(tmp) / name).read_text(encoding="utf-8"), (ROOT / name).read_text(encoding="utf-8"))
            # Import only the extracted source in a fresh isolated interpreter,
            # catching missing new browser/cleanup dependencies in the uploadable archive.
            result = subprocess.run([sys.executable, "-I", "-c",
                "import sys; sys.path.insert(0,sys.argv[1]); import wiki_fetcher,worker_agent.app,capture_temp_cleanup",
                tmp], capture_output=True, text=True, errors="replace", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
