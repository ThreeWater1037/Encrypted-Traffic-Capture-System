from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from browser_discovery import discover_browser


class BrowserDiscoveryTests(unittest.TestCase):
    def test_environment_override_has_highest_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "custom-firefox.exe"
            binary.touch()
            with patch("browser_discovery.shutil.which", return_value=None):
                found = discover_browser(
                    "firefox",
                    environ={"FIREFOX_BINARY": str(binary)},
                    platform_name="Windows",
                )
            self.assertEqual(found, str(binary.resolve()))

    def test_windows_program_files_location_is_dynamic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "Mozilla Firefox" / "firefox.exe"
            binary.parent.mkdir()
            binary.touch()
            with (
                patch("browser_discovery.shutil.which", return_value=None),
                patch("browser_discovery._windows_app_path", return_value=None),
            ):
                found = discover_browser(
                    "firefox",
                    environ={"PROGRAMFILES": directory},
                    platform_name="Windows",
                )
            self.assertEqual(found, str(binary.resolve()))

    def test_path_lookup_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "firefox"
            binary.touch()
            with patch(
                "browser_discovery.shutil.which",
                side_effect=lambda name: str(binary) if name == "firefox" else None,
            ):
                found = discover_browser(
                    "firefox", environ={}, platform_name="Linux"
                )
            self.assertEqual(found, str(binary.resolve()))

    def test_unknown_browser_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            discover_browser("safari")


if __name__ == "__main__":
    unittest.main()
