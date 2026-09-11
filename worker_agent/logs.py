"""Bounded reads from the end of a task log."""

from pathlib import Path
from typing import Any


def read_log_tail(path: Path, *, lines: int, max_bytes: int) -> dict[str, Any]:
    """Read at most max_bytes plus one boundary byte, regardless of file size."""
    with path.open("rb") as stream:
        stream.seek(0, 2)
        end = stream.tell()
        start = max(0, end - max_bytes)
        partial_first_line = False
        if start:
            stream.seek(start - 1)
            partial_first_line = stream.read(1) != b"\n"
        else:
            stream.seek(0)
        data = stream.read(end - start)

    rows = data.splitlines(keepends=True)
    truncated = partial_first_line and len(rows) <= lines
    if partial_first_line and len(rows) > 1:
        rows = rows[1:]
    rows = rows[-lines:]
    return {
        "text": b"".join(rows).decode("utf-8", errors="replace"),
        "next_offset": start + len(data),
        "eof": True,
        "tail_lines": lines,
        "line_count": len(rows),
        "truncated": truncated,
    }
