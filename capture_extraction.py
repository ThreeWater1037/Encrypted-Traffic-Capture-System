"""Durable per-browser extraction queue and verified PCAP retirement.

Capture checkpoints are the durable queue: a restart visits them again. Receipts
bind the source checkpoint and every result file before deletion is permitted.
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

LOG = logging.getLogger(__name__)
BROWSERS = ("chrome", "edge", "firefox")
ROOT = Path(__file__).resolve().parent


def sync_directory(path):
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def save_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(10):
            try:
                os.replace(temporary, path)
                sync_directory(path.parent)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(.05)
    finally:
        temporary.unlink(missing_ok=True)


def signature(path):
    path = Path(path)
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"Input changed while reading: {path}")
    return {"bytes": after.st_size, "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest()}


def output_signature(path):
    # Persist generated data before recording the durable deletion permission.
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())
    return signature(path)


def receipt_path(directory, browser):
    return Path(directory) / f"extraction_{browser}.json"


def verified_receipt(directory, browser, *, full=False):
    """Fast reads for progress; cryptographic validation before source deletion."""
    directory = Path(directory).resolve()
    try:
        receipt = json.loads(receipt_path(directory, browser).read_text(encoding="utf-8"))
        if (receipt["version"] != 1 or receipt["status"] not in ("verified", "succeeded")
                or receipt["browser"] != browser):
            return None
        marker = directory / f"capture_{browser}.complete.json"
        if signature(marker) != receipt["checkpoint"]:
            return None
        if not receipt["outputs"] or not receipt["flow_verified"] or not receipt["tls_verified"]:
            return None
        for relative, expected in receipt["outputs"].items():
            path = (directory / relative).resolve()
            if not path.is_relative_to(directory) or path == directory or not path.is_file():
                return None
            if path.stat().st_size != expected["bytes"]:
                return None
            if full and signature(path)["sha256"] != expected["sha256"]:
                return None
        return receipt
    except (OSError, ValueError, KeyError, TypeError):
        return None


def retired_capture(directory, browser):
    receipt = verified_receipt(directory, browser)
    return bool(receipt and receipt["delete_requested"] and
                not (Path(directory) / f"capture_{browser}.pcap").exists())


def retire_pcap(directory, browser, receipt):
    directory = Path(directory).resolve()
    if not verified_receipt(directory, browser, full=True):
        raise RuntimeError("Result verification failed; original PCAP retained")
    pcap = directory / f"capture_{browser}.pcap"
    if receipt["delete_requested"]:
        if pcap.exists():
            if pcap.is_symlink() or signature(pcap) != receipt["source"]:
                raise RuntimeError("Source changed; refusing PCAP deletion")
            pcap.unlink()
        receipt["pcap_deleted"] = True
    receipt.update(status="succeeded", finished_at=time.time())
    save_json(receipt_path(directory, browser), receipt)
    return receipt


def storage_guard(directory):
    if shutil.disk_usage(directory).free < 8 * 1024**3:
        raise RuntimeError("Extraction requires 8 GiB free space; PCAP retained")
    if hasattr(os, "statvfs") and os.statvfs(directory).f_favail < 100000:
        raise RuntimeError("Extraction requires 100000 free inodes; PCAP retained")
    if sys.platform.startswith("linux"):
        memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        if int(memory["MemAvailable"].split()[0]) < 512 * 1024:
            raise RuntimeError("Extraction requires 512 MiB available memory; PCAP retained")


def lower_priority():
    """Applied only in the analysis process, inherited by its TShark children."""
    if sys.platform.startswith("linux"):
        os.nice(15)
        cpus = os.sched_getaffinity(0)
        os.sched_setaffinity(0, {max(cpus)})
        subprocess.run(["ionice", "-c", "3", "-p", str(os.getpid())], check=True)
    elif os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        if not kernel.SetPriorityClass(kernel.GetCurrentProcess(), 0x00004000):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        raise RuntimeError("Background extraction priority is supported on Linux and Windows only")


@contextmanager
def unit_lock(directory, browser):
    with (Path(directory) / f"extraction_{browser}.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.write(b"0")
            stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def extract_one(directory, browser, delete_pcap=False):
    if browser not in BROWSERS:
        raise ValueError("Unsupported extraction browser")
    with unit_lock(directory, browser):
        return _extract_one(directory, browser, delete_pcap)


def _extract_one(directory, browser, delete_pcap=False):
    """Flow sequences first, TLS features second; failed attempts remain intact."""
    if browser not in BROWSERS:
        raise ValueError("Unsupported extraction browser")
    directory = Path(directory).resolve()
    previous = verified_receipt(directory, browser, full=True)
    if previous:
        # A crash after verification or unlink is recoverable without recapture.
        # The current invocation controls deletion of an existing source. Keep
        # the retirement marker for a source already removed before a crash.
        previous["delete_requested"] = bool(delete_pcap or
            (previous["delete_requested"] and not (directory / f"capture_{browser}.pcap").exists()))
        save_json(receipt_path(directory, browser), previous)
        return retire_pcap(directory, browser, previous)
    pcap = directory / f"capture_{browser}.pcap"
    keylog = directory / f"tls_keys_{browser}.log"
    marker = directory / f"capture_{browser}.complete.json"
    receipt = {"version": 1, "browser": browser, "status": "running",
               "started_at": time.time(), "delete_requested": bool(delete_pcap),
               "pcap_deleted": False, "flow_verified": False, "tls_verified": False}
    output = directory / "flow_extraction" / browser / ("attempt-" + uuid.uuid4().hex)
    try:
        storage_guard(directory)
        checkpoint = json.loads(marker.read_text(encoding="utf-8"))
        if checkpoint.get("browser") != browser:
            raise RuntimeError("Capture checkpoint browser mismatch")
        if checkpoint.get("capture_summary", {}).get("packets_dropped", 0):
            raise RuntimeError("Capture reported packet loss; retaining PCAP for investigation")
        for source in (pcap, keylog):
            if source.is_symlink() or source.stat().st_size != checkpoint["artifacts"][source.name]:
                raise RuntimeError(f"Capture is not finalized: {source}")
        receipt.update(source=signature(pcap), keylog=signature(keylog), checkpoint=signature(marker))
        output.mkdir(parents=True, exist_ok=False)
        receipt["result_dir"] = output.relative_to(directory).as_posix()
        save_json(receipt_path(directory, browser), receipt)
        from analysis_scripts.result_layout import create_inputs
        inputs = create_inputs(output, pcap)
        scripts = ROOT / "analysis_scripts"
        stages = [
            ["extract_features.py", str(pcap), "--output", str(inputs / f"capture_{browser}.tsv")],
            ["classify_packets.py", str(pcap), str(keylog), "--flow-tsv", str(inputs / f"capture_{browser}.tsv"), "--output-dir", str(inputs / f"capture_{browser}_flows")],
            ["infer_packets.py", str(pcap), "--flow-tsv", str(inputs / f"capture_{browser}.tsv"), "--output-dir", str(inputs / f"capture_{browser}_inferred")],
            ["extract_header_seqs.py", str(inputs), "--browser", browser, "--protocol", "both", "--allow-unpaired", "--output-dir", str(output / "sequences")],
            ["verify_sequences.py", str(output), browser, str(pcap)],
            ["extract_tls.py", str(pcap), "-o", str(output / "tls" / f"capture_{browser}.jsonl")],
        ]
        for index, stage in enumerate(stages):
            storage_guard(directory)
            receipt["stage"] = stage[0]
            save_json(receipt_path(directory, browser), receipt)
            with (output / f"{index + 1:02d}_{stage[0]}.log").open("wb") as log:
                subprocess.run([sys.executable, str(scripts / stage[0]), *stage[1:]],
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=900,
                               env={**os.environ, "PYTHONOPTIMIZE": "0"})
        validation = json.loads((output / "verification.json").read_text())
        if not validation.get("tcp_flags_verified") or not validation.get("packet_rows_per_mode"):
            raise RuntimeError("No verified flow packets; original PCAP retained")
        tls = output / "tls" / f"capture_{browser}.jsonl"
        manifest = json.loads(tls.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        lines = 0
        with tls.open(encoding="utf-8") as stream:
            for line in stream:
                json.loads(line)
                lines += 1
        if (not manifest.get("complete") or lines == 0 or
                lines != manifest["stats"]["output_flows"] or
                manifest["stats"].get("capture_truncated_packets", 0)):
            raise RuntimeError("TLS result incomplete/empty; original PCAP retained")
        for source, recorded in ((pcap, "source"), (keylog, "keylog"), (marker, "checkpoint")):
            if signature(source) != receipt[recorded]:
                raise RuntimeError("Capture inputs changed during extraction")
        receipt.update(outputs={p.relative_to(directory).as_posix(): output_signature(p)
                                for p in output.rglob("*") if p.is_file()},
                       flow_verified=True, tls_verified=True, status="verified", stage="verified")
        for folder in sorted((p for p in output.rglob("*") if p.is_dir()),
                             key=lambda p: len(p.parts), reverse=True):
            sync_directory(folder)
        for folder in (output, output.parent, output.parent.parent):
            sync_directory(folder)
        save_json(receipt_path(directory, browser), receipt)
        return retire_pcap(directory, browser, receipt)
    except Exception as exc:
        if receipt.get("status") in ("verified", "succeeded") and not pcap.exists():
            # Keep the durable verified receipt if final publication failed after
            # unlink. It is the recovery record for that transaction.
            raise
        receipt.update(status="failed", error=f"{type(exc).__name__}: {exc}", finished_at=time.time())
        save_json(receipt_path(directory, browser), receipt)
        raise


class ExtractionQueue:
    """One low-priority child at a time; only paths are queued, never PCAP bytes."""
    def __init__(self, output_dir, *, delete_pcap=False):
        self.output_dir = Path(output_dir)
        self.delete_pcap = delete_pcap
        self.queue = queue.Queue()
        self.seen = set()
        self.failures = []
        self.finished = 0
        self.stop = threading.Event()
        self.draining = False
        self.error = None
        self._progress()
        self.thread = threading.Thread(target=self._consume, name="capture-extraction", daemon=True)
        self.thread.start()

    def enqueue(self, directory, browser):
        item = (str(Path(directory).resolve()), browser)
        if item not in self.seen:
            self.seen.add(item)
            self.queue.put(item)

    def _progress(self, **extra):
        save_json(self.output_dir / "extraction_progress.json",
                  dict(updated_at=time.time(), draining=self.draining, queued=self.queue.qsize(),
                       finished=self.finished, failed=len(self.failures), **extra))

    def _consume(self):
        try:
            while not self.stop.is_set():
                item = self.queue.get()
                try:
                    if item is None:
                        return
                    directory, browser = item
                    self._progress(current=directory, browser=browser)
                    command = [sys.executable, str(ROOT / "capture_extraction.py"), directory, browser]
                    if self.delete_pcap:
                        command.append("--delete-pcap")
                    from process_ownership import LINUX, LinuxProcessOwner
                    owner = LinuxProcessOwner("EXTRACTION_OWNER") if LINUX else None
                    env = owner.environment(os.environ.copy()) if owner else os.environ.copy()
                    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
                    with (Path(directory) / f"extraction_{browser}.log").open("ab") as log:
                        process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                                                   **({"process_group": 0} if LINUX else {}))
                        if owner:
                            owner.bind(process)
                        try:
                            started = time.monotonic()
                            heartbeat = started
                            while process.poll() is None:
                                if self.stop.is_set() or time.monotonic() - started > 6000:
                                    if os.name == "nt":
                                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                                    elif owner:
                                        owner.terminate()
                                    process.wait(timeout=15)
                                    break
                                if LINUX and not self.draining:
                                    # Bound the whole owned analysis process group to
                                    # 25% wall-clock duty on one CPU during capture.
                                    # nice alone cannot reserve capture CPU capacity.
                                    self.stop.wait(.1)
                                    if process.poll() is None:
                                        try:
                                            os.killpg(process.pid, signal.SIGSTOP)
                                            self.stop.wait(.3)
                                        except ProcessLookupError:
                                            pass
                                        finally:
                                            try:
                                                os.killpg(process.pid, signal.SIGCONT)
                                            except ProcessLookupError:
                                                pass
                                else:
                                    self.stop.wait(.5)
                                if time.monotonic() - heartbeat >= 2:
                                    self._progress(current=directory, browser=browser)
                                    heartbeat = time.monotonic()
                        finally:
                            if owner:
                                owner.terminate()
                    if process.returncode != 0 or not verified_receipt(directory, browser):
                        self.failures.append(item)
                        LOG.error("Extraction failed; retaining PCAP: %s/%s", directory, browser)
                    self.finished += 1
                    self._progress()
                finally:
                    self.queue.task_done()
        except BaseException as exc:
            self.error = exc
            LOG.exception("Extraction queue stopped")

    def close(self, *, cancel=False):
        self.draining = True
        self._progress()
        if cancel:
            self.stop.set()
        self.queue.put(None)
        self.thread.join()
        self._progress(done=True, error=str(self.error) if self.error else None)
        return not self.failures and self.error is None and not cancel


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("browser", choices=BROWSERS)
    parser.add_argument("--delete-pcap", action="store_true")
    args = parser.parse_args()
    lower_priority()
    extract_one(args.directory, args.browser, args.delete_pcap)
