from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wiki_fetcher import PacketCapture


class _ExitedProcess:
    def poll(self) -> int:
        return 2


class PacketCaptureTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
