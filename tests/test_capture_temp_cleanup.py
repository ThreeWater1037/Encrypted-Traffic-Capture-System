import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from capture_temp_cleanup import (
    REGISTRY_ENV, cleanup_retained_directories,
    register_retained_directory, running_capture_browsers,
    main,
)


class CaptureTempCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.registry = self.root / "registry"
        self.now = 200000.0
        self.addCleanup(patch.stopall)
        patch("capture_temp_cleanup.tempfile.gettempdir", return_value=str(self.root)).start()
        patch.dict(os.environ, {REGISTRY_ENV: str(self.registry)}).start()
        self.running = patch("capture_temp_cleanup.running_capture_browsers", return_value=set()).start()

    def register(self, browser="firefox", *, age=0):
        path = Path(tempfile.mkdtemp(prefix=f"wb_{browser}_", dir=self.root))
        (path / "keys.log").write_text("fixture")
        with patch("capture_temp_cleanup.time.time", return_value=self.now - age):
            register_retained_directory(path, browser)
        entry = next(e for e in self.registry.glob("*.json")
                     if json.loads(e.read_text())["path"] == str(path))
        return path, entry

    def clean(self):
        return cleanup_retained_directories(self.registry)

    def test_registered_idle_directories_deleted_immediately_for_all_browsers(self):
        for browser in ("chrome", "edge", "firefox"):
            old, entry = self.register(browser)
            recent, _ = self.register(browser, age=0)
            unknown = self.root / f"wb_{browser}_unregistered"
            unknown.mkdir()
            self.assertEqual(self.clean()["deleted"], 2)
            self.assertFalse(old.exists())
            self.assertFalse(entry.exists())
            self.assertFalse(recent.exists())
            self.assertTrue(unknown.exists())

    def test_running_browsers_are_skipped_and_later_retried(self):
        path, entry = self.register()
        self.running.return_value = {"firefox"}
        self.assertEqual(self.clean()["skipped"], 1)
        self.assertTrue(path.exists())
        self.running.return_value = set()
        self.assertEqual(self.clean()["deleted"], 1)
        self.assertFalse(entry.exists())

    def test_process_inspection_failure_never_deletes(self):
        path, _ = self.register()
        self.running.side_effect = OSError("process listing denied")
        with self.assertRaises(OSError):
            self.clean()
        self.assertTrue(path.exists())

    def test_locked_directory_is_retained_and_next_round_retries(self):
        path, entry = self.register()
        with patch("capture_temp_cleanup.shutil.rmtree", side_effect=PermissionError("in use")):
            self.assertEqual(self.clean()["errors"], 1)
        self.assertTrue(entry.exists())
        self.assertTrue(path.exists())
        self.assertEqual(self.clean()["deleted"], 1)

    @unittest.skipUnless(os.name == "nt", "Windows file sharing lock")
    def test_real_windows_open_file_defers_cleanup_until_handle_closes(self):
        path, entry = self.register()
        with (path / "keys.log").open("rb"):
            self.assertEqual(self.clean()["errors"], 1)
            self.assertTrue(path.exists())
            self.assertTrue(entry.exists())
        self.assertEqual(self.clean()["deleted"], 1)
        self.assertFalse(path.exists())

    def test_arbitrary_paths_and_wrong_identity_are_never_deleted(self):
        path, entry = self.register()
        original = json.loads(entry.read_text())
        for update in ({"path": str(self.root)}, {"path": str(self.registry)},
                       {"path": str(self.root.parent / path.name)}, {"inode": -1},
                       {"browser": "chrome"}, {"version": 999}):
            with self.subTest(update=update):
                entry.write_text(json.dumps({**original, **update}))
                self.assertEqual(self.clean()["deleted"], 0)
                self.assertTrue(path.exists())

    def test_linked_tree_is_not_removed(self):
        path, _ = self.register()
        with patch("capture_temp_cleanup._contains_links", return_value=True):
            self.assertEqual(self.clean()["skipped"], 1)
        self.assertTrue(path.exists())

    def test_missing_directory_drops_only_registry_entry(self):
        path, entry = self.register()
        (path / "keys.log").unlink()
        path.rmdir()
        self.assertEqual(self.clean()["missing"], 1)
        self.assertFalse(entry.exists())

    def test_bad_entry_does_not_prevent_other_cleanup(self):
        path, _ = self.register()
        (self.registry / "broken.json").write_text("{")
        summary = self.clean()
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["deleted"], 1)
        self.assertFalse(path.exists())

    def test_registration_rejects_non_capture_paths(self):
        for path in (self.root, self.root / "output"):
            path.mkdir(exist_ok=True)
            with self.assertRaises(ValueError):
                register_retained_directory(path, "firefox")

    def test_cli_without_registry_does_not_register(self):
        with patch.dict(os.environ, {}, clear=True):
            register_retained_directory(self.root, "firefox")
        self.assertFalse(self.registry.exists())

    def test_screenshot_browser_directories_are_cleaned_without_age_or_registration(self):
        names = ["rust_mozprofile9CyUIY",
                 "com.google.Chrome.chrome_chrome_url_fetcher_.ABC123",
                 "com.google.Chrome.chrome_chrome_Unpacker_BeginUnzipping.ABC123",
                 "com.microsoft.Edge.msedge_chrome_Unpacker_BeginUnzipping.ABC123",
                 "com.microsoft.Edge.msedge_url_fetcher_.ABC123",
                 "chrome_url_fetcher_ABC123", "chrome_Unpacker_BeginUnzippingABC123",
                 "msedge_url_fetcher_ABC123"]
        for name in names:
            path = self.root / name
            path.mkdir()
            (path / "fresh-file").write_text("just created")
        self.assertFalse(self.registry.exists())
        self.assertEqual(self.clean()["deleted"], len(names))
        self.assertTrue(all(not (self.root / name).exists() for name in names))

    def test_unknown_files_directories_and_nested_candidates_are_preserved(self):
        for name in ("other-cache", "rust_mozprofile", "com.google.Chrome.downloads",
                     "com.microsoft.Edge.msedge_url_fetcher_", "capture_firefox.pcap"):
            (self.root / name).mkdir()
        nested = self.root / "other-cache" / "rust_mozprofileABC123"
        nested.mkdir()
        named_file = self.root / "rust_mozprofileABC123"
        named_file.write_text("not a directory")
        self.assertEqual(self.clean()["deleted"], 0)
        self.assertTrue(nested.exists())
        self.assertTrue(named_file.is_file())

    def test_busy_browser_and_updater_prevent_legacy_directory_deletion(self):
        names = {"rust_mozprofileABC123": {"firefox"},
                 "com.google.Chrome.chrome_chrome_url_fetcher_.ABC123": {"chrome"},
                 "com.microsoft.Edge.msedge_url_fetcher_.ABC123": {"edge"},
                 "chrome_url_fetcher_ABC123": {"chrome", "edge"}}
        for name, browsers in names.items():
            path = self.root / name
            path.mkdir()
            for browser in browsers:
                self.running.return_value = {browser}
                self.assertEqual(self.clean()["deleted"], 0)
                self.assertTrue(path.exists())
            self.running.return_value = set()
            self.assertEqual(self.clean()["deleted"], 1)

    def test_registered_firefox_profile_retains_identity_protection(self):
        path = self.root / "rust_mozprofileABC123"
        path.mkdir()
        register_retained_directory(path, "firefox")
        entry = next(self.registry.glob("*.json"))
        record = json.loads(entry.read_text())
        entry.write_text(json.dumps({**record, "inode": -1}))
        self.assertEqual(self.clean()["deleted"], 0)
        self.assertTrue(path.exists(), "Legacy scanning must not bypass a failed registry identity check")

    def test_other_unix_users_directories_are_not_deleted(self):
        path = self.root / "rust_mozprofileABC123"
        path.mkdir()
        with patch("capture_temp_cleanup.os.getuid", return_value=path.stat().st_uid + 1, create=True):
            self.assertEqual(self.clean()["deleted"], 0)
        self.assertTrue(path.exists())

    def test_legacy_links_are_skipped_but_firefox_lock_leaf_is_safe(self):
        path = self.root / "rust_mozprofileABC123"
        path.mkdir()
        outside = self.root / "keep.txt"
        outside.write_text("keep")
        try:
            (path / "lock").symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        self.assertEqual(self.clean()["deleted"], 1)
        self.assertEqual(outside.read_text(), "keep")
        path.mkdir()
        (path / "other-link").symlink_to(outside)
        self.assertEqual(self.clean()["deleted"], 0)
        self.assertTrue(path.exists())

    def test_deploy_cli_cleans_configured_temp_root_and_preserves_outputs(self):
        config = self.root / "worker.yaml"
        config.write_text("paths:\n  data_dir: data\n")
        output = self.root / "data" / "tasks"
        output.mkdir(parents=True)
        (output / "capture.pcap").write_text("keep")
        profile = self.root / "rust_mozprofileABC123"
        profile.mkdir()
        with patch("sys.argv", ["cleanup", "--worker-config", str(config), "--temp-root", str(self.root)]):
            self.assertEqual(main(), 0)
        self.assertFalse(profile.exists())
        self.assertEqual((output / "capture.pcap").read_text(), "keep")

    def test_filesystem_root_is_never_accepted(self):
        with self.assertRaises(ValueError):
            cleanup_retained_directories(self.registry, temp_root=Path(self.root.anchor))

    def test_process_names_cover_three_browsers_and_drivers(self):
        output = ('"firefox.exe","1"\n"msedgedriver.exe","2"\n"chrome.exe","3"'
                  if os.name == "nt" else "firefox\nmsedgedriver\nchrome\n")
        with patch("capture_temp_cleanup.subprocess.run") as run:
            run.return_value.stdout = output
            # Call the original function imported before the idle-process mock.
            self.assertEqual(running_capture_browsers(), {"chrome", "edge", "firefox"})
            run.return_value.stdout = ""
            with self.assertRaises(RuntimeError):
                running_capture_browsers()


if __name__ == "__main__":
    unittest.main()
