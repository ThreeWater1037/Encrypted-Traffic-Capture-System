from __future__ import annotations

import subprocess
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from wiki_fetcher import PacketCapture, UrlEntry, WikiFetcher, _entry_slug


class _ExitedProcess:
    def poll(self) -> int:
        return 2


class PacketCaptureTests(unittest.TestCase):
    def test_entry_slug_removes_windows_unsafe_trailing_dots_and_spaces(self) -> None:
        entry = UrlEntry(
            id="132",
            name="总务处/... ",
            url="https://example.com/",
        )

        self.assertEqual(_entry_slug(entry), "132-wiki-总务处／")

    def test_entry_slug_uses_fallback_when_name_is_only_dots(self) -> None:
        entry = UrlEntry(id="1", name="...", url="https://example.com/")

        self.assertEqual(_entry_slug(entry), "1-wiki-item")

    def test_linux_tshark_uses_only_any_interface(self) -> None:
        with (
            patch("wiki_fetcher.platform.system", return_value="Linux"),
            patch("wiki_fetcher.subprocess.check_output") as check_output,
        ):
            interfaces = PacketCapture._list_interfaces_tshark("/usr/bin/tshark")

        self.assertEqual(interfaces, ["any"])
        check_output.assert_not_called()

    def test_linux_tshark_command_does_not_include_pseudo_interfaces(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch("wiki_fetcher.platform.system", return_value="Linux"),
        ):
            pcap_path = Path(temp_dir) / "capture.pcap"
            capture = PacketCapture(pcap_path)
            command = capture._build_cmd("tshark", "/usr/bin/tshark")

        self.assertEqual(
            command,
            [
                "/usr/bin/tshark",
                "-q",
                "-i",
                "any",
                "-w",
                str(pcap_path),
            ],
        )

    def test_windows_tshark_retries_after_timeout(self) -> None:
        tshark_output = (
            "1. \\Device\\NPF_{ONE} (Ethernet)\n"
            "2. \\Device\\NPF_{TWO} (Wi-Fi)\n"
            "3. ciscodump (Cisco remote capture)\n"
        )
        with (
            patch("wiki_fetcher.platform.system", return_value="Windows"),
            patch(
                "wiki_fetcher.subprocess.check_output",
                side_effect=[
                    subprocess.TimeoutExpired("tshark -D", 15),
                    tshark_output,
                ],
            ) as check_output,
            patch("wiki_fetcher.time.sleep") as sleep,
        ):
            interfaces = PacketCapture._list_interfaces_tshark(
                r"C:\Program Files\Wireshark\tshark.exe"
            )

        self.assertEqual(
            interfaces,
            [r"\Device\NPF_{ONE}", r"\Device\NPF_{TWO}"],
        )
        self.assertEqual(check_output.call_count, 2)
        sleep.assert_called_once_with(0.5)
        self.assertEqual(check_output.call_args.kwargs["timeout"], 15)

    def test_windows_tshark_failure_never_uses_macos_interfaces(self) -> None:
        with (
            patch("wiki_fetcher.platform.system", return_value="Windows"),
            patch(
                "wiki_fetcher.subprocess.check_output",
                side_effect=subprocess.TimeoutExpired("tshark -D", 15),
            ) as check_output,
            patch("wiki_fetcher.time.sleep"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "tshark interface detection failed on Windows",
            ):
                PacketCapture._list_interfaces_tshark(
                    r"C:\Program Files\Wireshark\tshark.exe"
                )

        self.assertEqual(check_output.call_count, 2)

    def test_macos_tshark_keeps_platform_specific_fallback(self) -> None:
        with (
            patch("wiki_fetcher.platform.system", return_value="Darwin"),
            patch("wiki_fetcher.subprocess.check_output", return_value=""),
        ):
            interfaces = PacketCapture._list_interfaces_tshark(
                "/Applications/Wireshark.app/Contents/MacOS/tshark"
            )

        self.assertEqual(interfaces, ["en0", "lo0"])

    def test_tshark_errors_are_inherited_and_early_exit_is_logged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            capture = PacketCapture(Path(temp_dir) / "capture.pcap")
            process = _ExitedProcess()
            with (
                patch.object(
                    PacketCapture,
                    "_find_tool",
                    return_value=("tshark", "/usr/bin/tshark"),
                ),
                patch.object(
                    PacketCapture,
                    "_build_cmd",
                    return_value=["/usr/bin/tshark", "-i", "any"],
                ),
                patch("wiki_fetcher.subprocess.Popen", return_value=process) as popen,
                patch("wiki_fetcher.time.sleep"),
                self.assertLogs("wiki_fetcher", level="WARNING") as captured_logs,
            ):
                capture.start()

        self.assertIs(popen.call_args.kwargs["stderr"], None)
        self.assertIn(
            "exited before capture started",
            "\n".join(captured_logs.output),
        )

    def test_linux_tcpdump_fallback_uses_any_interface(self) -> None:
        with patch("wiki_fetcher.platform.system", return_value="Linux"):
            self.assertEqual(PacketCapture._default_iface_tcpdump(), "any")

    def test_nonempty_partial_files_are_not_a_valid_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fetcher = WikiFetcher(Path(temp_dir), ["chrome"], True)
            entry = UrlEntry(id="1", name="Example", url="https://example.com/")
            item_dir = fetcher._url_dir("1-wiki-Example")
            fetcher._key_log_path(item_dir, "chrome").write_text(
                "CLIENT_RANDOM key", encoding="utf-8"
            )
            fetcher._pcap_path(item_dir, "chrome").write_bytes(b"partial-pcap")

            self.assertFalse(fetcher._checkpoint_valid(entry, item_dir, "chrome"))

    def test_valid_checkpoint_requires_matching_url_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fetcher = WikiFetcher(Path(temp_dir), ["chrome"], True)
            entry = UrlEntry(id="1", name="Example", url="https://example.com/")
            item_dir = fetcher._url_dir("1-wiki-Example")
            fetcher._key_log_path(item_dir, "chrome").write_text(
                "CLIENT_RANDOM key", encoding="utf-8"
            )
            fetcher._pcap_path(item_dir, "chrome").write_bytes(b"pcap")
            fetcher._completion_marker_path(item_dir, "chrome").write_text(
                json.dumps(
                    {
                        "item_id": "1",
                        "url": "https://example.com/",
                        "browser": "chrome",
                        "artifacts": {
                            "tls_keys_chrome.log": len("CLIENT_RANDOM key"),
                            "capture_chrome.pcap": len(b"pcap"),
                        },
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(fetcher._checkpoint_valid(entry, item_dir, "chrome"))


if __name__ == "__main__":
    unittest.main()
