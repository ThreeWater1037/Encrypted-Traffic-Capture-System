"""Register retained capture directories and remove expired, idle ones only."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time


REGISTRY_ENV = "CAPTURE_TEMP_CLEANUP_REGISTRY"
CLEANUP_INTERVAL_SECONDS = 3600
RETENTION_SECONDS = 24 * 3600
_NAME = re.compile(r"wb_(chrome|edge|firefox)_[A-Za-z0-9_-]+")
_PROCESSES = {
    "chrome": {"chrome", "chrome.exe", "chromium", "chromium-browser", "chromium-browse",
               "chromedriver", "chromedriver.exe", "google chrome", "google chrome helper"},
    "edge": {"msedge", "msedge.exe", "msedgedriver", "msedgedriver.exe",
             "microsoft edge", "microsoft edge helper"},
    "firefox": {"firefox", "firefox.exe", "firefox-bin", "geckodriver", "geckodriver.exe",
                "plugin-container", "web content", "privileged cont", "isolated web co",
                "socket process", "rdd process", "utility process"},
}
log = logging.getLogger(__name__)


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _owned_directory(path: Path, temp_root: Path) -> bool:
    # Never delete the temp root itself, an arbitrary path, or a redirected path.
    return (path.is_absolute() and path.parent == temp_root and _NAME.fullmatch(path.name) is not None
            and path.is_dir() and not _is_link(path) and path.resolve(strict=True) == path)


def register_retained_directory(path: Path, browser: str) -> None:
    """Called only after capture cleanup; standalone CLI runs need no registry."""
    registry_value = os.environ.get(REGISTRY_ENV)
    if not registry_value or browser not in _PROCESSES:
        return
    temp_root = Path(tempfile.gettempdir()).resolve()
    path = Path(os.path.abspath(path))
    if not _owned_directory(path, temp_root) or not path.name.startswith(f"wb_{browser}_"):
        raise ValueError("Refusing to register a directory outside the capture temp scope")
    info = path.stat()
    registry = Path(registry_value)
    registry.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(str(path).encode()).hexdigest() + ".json"
    target = registry / name
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "version": 1, "path": str(path), "browser": browser,
        "retained_at": time.time(), "device": info.st_dev, "inode": info.st_ino,
    }), encoding="utf-8")
    temporary.replace(target)


def running_capture_browsers() -> set[str]:
    """Conservatively skip a browser's directories while any instance is alive."""
    if os.name == "nt":
        command = [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/tasklist.exe"),
                   "/FO", "CSV", "/NH"]
    else:
        command = ["ps", "-A", "-o", "comm="]
    result = subprocess.run(command, capture_output=True, text=True, errors="replace",
                            timeout=10, check=True)
    if not result.stdout.strip():
        raise RuntimeError("Process listing is empty; postpone temp cleanup")
    if os.name == "nt":
        names = {row[0].lower() for row in csv.reader(io.StringIO(result.stdout))
                 if len(row) >= 2 and row[1].isdigit()}
        if not names:
            raise RuntimeError("Unrecognized process listing; postpone temp cleanup")
    else:
        names = {Path(line.strip()).name.lower() for line in result.stdout.splitlines() if line.strip()}
    return {browser for browser, candidates in _PROCESSES.items() if names & candidates}


def _contains_links(path: Path) -> bool:
    def fail(error):
        raise error
    for directory, directories, files in os.walk(path, followlinks=False, onerror=fail):
        if any(_is_link(Path(directory) / name) for name in directories + files):
            return True
    return False


def cleanup_retained_directories(registry: Path, *, now: float | None = None) -> dict:
    """Only remove registered direct children of this service's own temp root."""
    summary = {"deleted": 0, "missing": 0, "skipped": 0, "errors": 0}
    if not registry.is_dir():
        return summary
    now = time.time() if now is None else now
    temp_root = Path(tempfile.gettempdir()).resolve()
    active = running_capture_browsers()  # Failure aborts this round, never assumes idle.
    for entry in registry.glob("*.json"):
        try:
            if _is_link(entry):
                summary["skipped"] += 1
                continue
            record = json.loads(entry.read_text(encoding="utf-8"))
            path = Path(record["path"])
            browser = record["browser"]
            age = now - float(record["retained_at"])
            if (record.get("version") != 1 or browser not in _PROCESSES
                    or not math.isfinite(age) or age < RETENTION_SECONDS or browser in active
                    or path.parent != temp_root or not path.name.startswith(f"wb_{browser}_")
                    or _NAME.fullmatch(path.name) is None):
                summary["skipped"] += 1
                continue
            if not path.exists() and not path.is_symlink():
                entry.unlink()
                summary["missing"] += 1
                continue
            if not _owned_directory(path, temp_root):
                summary["skipped"] += 1
                continue
            info = path.stat()
            if ((info.st_dev, info.st_ino) != (record["device"], record["inode"])
                    or _contains_links(path)):
                summary["skipped"] += 1
                continue
            # Resolve/check the final target again immediately before recursive deletion.
            if not _owned_directory(path, temp_root):
                summary["skipped"] += 1
                continue
            shutil.rmtree(path)
            entry.unlink()
            summary["deleted"] += 1
            log.info("Removed expired capture temporary directory: %s", path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            summary["errors"] += 1
            log.warning("Retained capture temp cleanup deferred (%s): %s", entry.name, exc)
    return summary
