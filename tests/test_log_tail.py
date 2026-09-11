from __future__ import annotations

import io
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from master_server.worker_client import WorkerClient, WorkerRequestError
from worker_agent.logs import read_log_tail


class CountingStream(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)


class LogTailTests(unittest.TestCase):
    def read_tail(self, data, *, lines=10, max_bytes=65536):
        stream = CountingStream(data)
        path = Mock()
        path.open.return_value = stream
        result = read_log_tail(path, lines=lines, max_bytes=max_bytes)
        self.assertTrue(all(size >= 0 for size in stream.read_sizes))
        self.assertLessEqual(sum(stream.read_sizes), max_bytes + 1)
        return result

    def test_large_utf8_log_reads_only_the_tail(self):
        for newline in ("\n", "\r\n"):
            for trailing_newline in (True, False):
                with self.subTest(newline=newline, trailing_newline=trailing_newline):
                    rows = [f"第 {index} 条采集日志" for index in range(20000)]
                    suffix = newline if trailing_newline else ""
                    data = (newline.join(rows) + suffix).encode("utf-8")
                    result = self.read_tail(data)
                    self.assertEqual(result["text"], newline.join(rows[-10:]) + suffix)
                    self.assertEqual(result["line_count"], 10)
                    self.assertEqual(result["next_offset"], len(data))
                    self.assertFalse(result["truncated"])
                    self.assertTrue(result["eof"])

    def test_empty_and_short_logs(self):
        for text in ("", "一行", "一行\n", "一行\n\n最后一行"):
            with self.subTest(text=text):
                result = self.read_tail(text.encode("utf-8"))
                self.assertEqual(result["text"], text)
                self.assertEqual(result["line_count"], len(text.splitlines()))
                self.assertFalse(result["truncated"])

    def test_one_huge_line_is_bounded_and_marked_truncated(self):
        result = self.read_tail(b"x" * 100000, max_bytes=4096)
        self.assertEqual(result["text"], "x" * 4096)
        self.assertEqual(result["line_count"], 1)
        self.assertTrue(result["truncated"])

    def test_partial_first_line_is_omitted_but_exact_boundary_is_retained(self):
        data = b"first\nsecond\nthird\n"
        partial = self.read_tail(data, max_bytes=len(b"ond\nthird\n"))
        self.assertEqual(partial["text"], "third\n")
        self.assertTrue(partial["truncated"])
        exact = self.read_tail(data, max_bytes=len(b"second\nthird\n"))
        self.assertEqual(exact["text"], "second\nthird\n")
        self.assertFalse(exact["truncated"])

    def test_worker_client_forwards_tail_option_and_rejects_old_worker_response(self):
        client = WorkerClient("http://worker.example", "test-token")
        with patch.object(client, "_request", return_value={"tail_lines": 10, "text": "last\n"}) as request:
            self.assertEqual(client.get_log("task-1", tail_lines=10)["text"], "last\n")
            query = parse_qs(urlsplit(request.call_args.args[1]).query)
            self.assertEqual(query["tail_lines"], ["10"])
            self.assertEqual(query["limit"], ["65536"])
        with patch.object(client, "_request", return_value={"text": "first lines"}):
            with self.assertRaisesRegex(WorkerRequestError, "更新 Worker"):
                client.get_log("task-1", tail_lines=10)


if __name__ == "__main__":
    unittest.main()
