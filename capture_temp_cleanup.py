"""Clean known browser temporary directories only while their browsers are idle."""

from __future__ import annotations

import csv
import argparse
import hashlib
import io
import json
import logging
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
_NAME = re.compile(r"wb_(chrome|edge|firefox)_[A-Za-z0-9_-]+")
_BROWSER_TEMP_NAMES = (
    (re.compile(r"rust_mozprofile[A-Za-z0-9_-]{6,}"), {"firefox"}),
    (re.compile(r"\.?com\.google\.Chrome\.chrome_(?:chrome_)?"
                r"(?:url_fetcher_|Unpacker_BeginUnzipping)[._-]?[A-Za-z0-9_-]{6,}"), {"chrome"}),
    (re.compile(r"\.?com\.microsoft\.Edge\.msedge_(?:chrome_)?"
                r"(?:url_fetcher_|Unpacker_BeginUnzipping)[._-]?[A-Za-z0-9_-]{6,}"), {"edge"}),
    # On Windows these Chromium component directories can lack a product namespace.
    (re.compile(r"chrome_(?:url_fetcher_|Unpacker_BeginUnzipping)[._-]?[A-Za-z0-9_-]{6,}"),
     {"chrome", "edge"}),
    (re.compile(r"msedge_(?:chrome_)?(?:url_fetcher_|Unpacker_BeginUnzipping)"
                r"[._-]?[A-Za-z0-9_-]{6,}"), {"edge"}),
)
_PROCESSES = {
    "chrome": {"chrome", "chrome.exe", "chromium", "chromium-browser", "chromium-browse",
               "chromedriver", "chromedriver.exe", "google chrome", "google chrome helper",
               "googleupdate.exe", "googleupdater.exe", "updater", "updater.exe"},
    "edge": {"msedge", "msedge.exe", "msedgedriver", "msedgedriver.exe",
             "microsoft edge", "microsoft edge helper", "microsoftedgeupdate.exe", "updater", "updater.exe"},
    "firefox": {"firefox", "firefox.exe", "firefox-bin", "geckodriver", "geckodriver.exe",
                "plugin-container", "web content", "privileged cont", "isolated web co",
                "socket process", "rdd process", "utility process"},
}
log = logging.getLogger(__name__)


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _browsers_for_name(name: str, *, registered: bool = False) -> set[str]:
    match = _NAME.fullmatch(name) if registered else None
    if match:
        return {match.group(1)}
    for pattern, browsers in _BROWSER_TEMP_NAMES:
        if pattern.fullmatch(name):
            return browsers
    return set()


def _owned_directory(path: Path, temp_root: Path, *, registered: bool = True) -> bool:
    # Never delete the temp root itself, an arbitrary path, or a redirected path.
    return (path.is_absolute() and path.parent == temp_root
            and bool(_browsers_for_name(path.name, registered=registered))
            and path.is_dir() and not _is_link(path) and path.resolve(strict=True) == path
            and (not hasattr(os, "getuid") or path.stat().st_uid == os.getuid()))


def register_retained_directory(path: Path, browser: str) -> None:
    """Called only after capture cleanup; standalone CLI runs need no registry."""
    registry_value = os.environ.get(REGISTRY_ENV)
    if not registry_value or browser not in _PROCESSES:
        return
    temp_root = Path(tempfile.gettempdir()).resolve()
    path = Path(os.path.abspath(path))
    if not _owned_directory(path, temp_root) or browser not in _browsers_for_name(path.name, registered=True):
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
        if any(_is_link(Path(directory) / name) for name in directories):
            return True
        for name in files:
            if _is_link(Path(directory) / name):
                # Firefox on Unix leaves a lock symlink in its temporary profile.
                # rmtree unlinks this leaf; it must never traverse its target.
                if Path(directory) == path and path.name.startswith("rust_mozprofile") and name in {"lock", ".parentlock"}:
                    continue
                return True
    return False


def cleanup_retained_directories(registry: Path, *, temp_root: Path | None = None) -> dict:
    """Clean registered capture dirs and allowlisted browser-generated siblings.

    There is no age threshold: process state, ownership and directory identity
    determine eligibility. Unknown names, links and other users' files are skipped.
    """
    summary = {"deleted": 0, "missing": 0, "skipped": 0, "errors": 0}
    temp_root = (temp_root or Path(tempfile.gettempdir())).resolve(strict=True)
    if temp_root == Path(temp_root.anchor) or not temp_root.is_dir():
        raise ValueError("A filesystem root is not a capture temp directory")
    active = running_capture_browsers()  # Failure aborts this round, never assumes idle.
    handled = set()

    def remove(path, identity, *, registered):
        if (active & _browsers_for_name(path.name, registered=registered)
                or not _owned_directory(path, temp_root, registered=registered)
                or _contains_links(path)):
            summary["skipped"] += 1
            return False
        # Recheck the final absolute path and identity just before recursive deletion.
        info = path.stat()
        if (not _owned_directory(path, temp_root, registered=registered)
                or (info.st_dev, info.st_ino) != identity):
            summary["skipped"] += 1
            return False
        shutil.rmtree(path)
        summary["deleted"] += 1
        log.info("Removed unused browser temporary directory: %s", path)
        return True

    for entry in registry.glob("*.json"):
        try:
            if _is_link(entry):
                summary["skipped"] += 1
                continue
            record = json.loads(entry.read_text(encoding="utf-8"))
            path = Path(record["path"])
            browser = record["browser"]
            if (record.get("version") != 1 or browser not in _PROCESSES
                    or path.parent != temp_root
                    or browser not in _browsers_for_name(path.name, registered=True)):
                summary["skipped"] += 1
                continue
            handled.add(path)
            if not path.exists() and not path.is_symlink():
                entry.unlink()
                summary["missing"] += 1
                continue
            if remove(path, (record["device"], record["inode"]), registered=True):
                entry.unlink()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            summary["errors"] += 1
            log.warning("Retained capture temp cleanup deferred (%s): %s", entry.name, exc)
    # Also cover old Firefox profiles and Chromium download/unpack leftovers that
    # were created by the browser itself and therefore have no project registry.
    for path in temp_root.iterdir():
        if path in handled or not _browsers_for_name(path.name):
            continue
        try:
            if not _owned_directory(path, temp_root, registered=False):
                summary["skipped"] += 1
                continue
            info = path.stat()
            remove(path, (info.st_dev, info.st_ino), registered=False)
        except OSError as exc:
            summary["errors"] += 1
            log.warning("Browser temporary directory cleanup deferred (%s): %s", path.name, exc)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-config", type=Path, required=True)
    parser.add_argument("--temp-root", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        import yaml
        config = args.worker_config.resolve(strict=True)
        settings = yaml.safe_load(config.read_text(encoding="utf-8-sig")) or {}
        data = Path(os.path.expandvars(settings.get("paths", {}).get("data_dir") or "./worker_data")).expanduser()
        if not data.is_absolute():
            data = config.parent / data
        summary = cleanup_retained_directories(data / "temp_cleanup", temp_root=args.temp_root)
        print(json.dumps(summary), flush=True)
    except Exception as exc:
        # Redeployment may proceed; uncertainty postpones deletion, not startup.
        log.warning("Temporary directory cleanup postponed: %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
