"""Windows reader contention must not abort checkpoint/resume publication."""

import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from wiki_fetcher import SessionRecord, UrlEntry, WikiFetcher, _replace_capture_file


class AtomicCaptureWriteTests(unittest.TestCase):
    def test_transient_windows_errors_keep_old_json_until_replacement(self):
        for code in (5, 32, 33):
            with self.subTest(winerror=code), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "progress.json"
                source = Path(tmp) / "progress.json.tmp"
                target.write_text('{"position": 1}')
                source.write_text('{"position": 2}')
                error = PermissionError(errno.EACCES, "held by reader")
                error.winerror = code
                original = Path.replace
                attempts = []

                def replace(path, destination):
                    attempts.append(1)
                    if len(attempts) < 4:
                        self.assertEqual(json.loads(target.read_text())["position"], 1)
                        raise error
                    return original(path, destination)

                with patch.object(Path, "replace", replace), patch("wiki_fetcher.time.sleep"):
                    _replace_capture_file(source, target)
                self.assertEqual(json.loads(target.read_text())["position"], 2)
                self.assertFalse(source.exists())

    def test_persistent_lock_is_bounded_and_preserves_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, source = Path(tmp) / "old", Path(tmp) / "new"
            target.write_bytes(b"old"); source.write_bytes(b"new")
            error = PermissionError(errno.EACCES, "permanent denial")
            error.winerror = 5
            with patch.object(Path, "replace", side_effect=error) as replace, \
                 patch("wiki_fetcher.time.sleep") as sleep:
                with self.assertRaises(PermissionError):
                    _replace_capture_file(source, target)
            self.assertEqual(replace.call_count, 20)
            self.assertLessEqual(sum(c.args[0] for c in sleep.call_args_list), 5)
            self.assertEqual(target.read_bytes(), b"old")
            self.assertEqual(source.read_bytes(), b"new")

    def test_other_io_errors_are_not_retried(self):
        for error in (OSError(errno.ENOSPC, "full"), PermissionError(errno.EACCES, "denied")):
            with self.subTest(error=error), patch.object(Path, "replace", side_effect=error), \
                 patch("wiki_fetcher.time.sleep") as sleep:
                with self.assertRaises(OSError):
                    _replace_capture_file(Path("new"), Path("old"))
                sleep.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "requires real Windows file sharing")
    def test_three_browser_progress_and_checkpoints_survive_real_reader(self):
        for browser in ("chrome", "edge", "firefox"):
            with self.subTest(browser=browser), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fetcher = WikiFetcher(root, [browser], True)
                entry = UrlEntry("1", "example", "https://example.com/")
                record = SessionRecord(browser, entry.url, "now", entry.url, "OK", 1,
                                       1, "hash", [], None, None)
                (root / f"tls_keys_{browser}.log").write_bytes(b"key")
                (root / f"capture_{browser}.pcap").write_bytes(b"pcap")
                progress = root / "capture_progress.json"
                marker = root / f"capture_{browser}.complete.json"

                def write_progress():
                    fetcher._write_progress(position=1, total=1, entry=entry,
                                            completed_units=1, skipped_units=0,
                                            incomplete_units=0)

                for target, write in ((progress, write_progress),
                                      (marker, lambda: fetcher._mark_complete(entry, root, browser, record))):
                    target.write_text('{"old": true}')
                    reader = target.open("rb")
                    release = threading.Timer(0.15, reader.close)
                    release.start()
                    try:
                        write()
                    finally:
                        release.join(timeout=2)
                        reader.close()
                    self.assertEqual(json.loads(target.read_text())["run_id"], fetcher.progress_run_id)
                self.assertTrue(fetcher._checkpoint_valid(entry, root, browser))


if __name__ == "__main__":
    unittest.main()
