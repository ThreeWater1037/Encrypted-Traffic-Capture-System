"""串行任务队列与抓取/分析子进程编排。

一台子机器同一时刻只运行一个抓取任务，避免多个 TShark 和浏览器实例争用网卡、
CPU 与磁盘。执行器负责状态流转、取消、超时、日志和最终产物校验。
"""

from __future__ import annotations

import json
import os
import platform
import queue
import shlex
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import WorkerConfig
from .task_store import TERMINAL_STATUSES, TaskStore, utc_now


class QueueFullError(RuntimeError):
    """任务队列达到配置容量。"""

    pass


class TaskConflictError(RuntimeError):
    """task_id 与已有数据库记录或本地目录发生冲突。"""

    pass


class TaskCanceledError(RuntimeError):
    """任务在执行阶段收到取消请求。"""

    pass


class TaskTimedOutError(RuntimeError):
    """任务超过整条流水线的总超时时间。"""

    pass


class TaskManager:
    """管理单消费者队列，并把任务映射为现有命令行流水线。"""

    def __init__(self, config: WorkerConfig, store: TaskStore, *, autostart: bool = True):
        """恢复异常中断状态，并按需启动后台执行线程。"""
        self.config = config
        self.store = store
        self._queue: queue.Queue[str | None] = queue.Queue(config.max_queue_size)
        self._stop_event = threading.Event()
        self._active_lock = threading.Lock()
        self._active_task_id: str | None = None
        self._active_process: subprocess.Popen[bytes] | None = None
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="worker-task-runner",
            daemon=True,
        )
        self.store.mark_incomplete_interrupted()
        if autostart:
            self.start()

    def start(self) -> None:
        """启动唯一的后台消费者线程。"""
        if not self._thread.is_alive():
            self._thread.start()

    def shutdown(self, timeout: float = 5.0) -> None:
        """停止接收执行工作，并终止仍在运行的子进程树。"""
        self._stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self.cancel_active_process()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def active_task_id(self) -> str | None:
        """线程安全地返回当前正在执行的 task_id。"""
        with self._active_lock:
            return self._active_task_id

    @property
    def queue_size(self) -> int:
        """返回尚未被消费者取出的任务数量。"""
        return self._queue.qsize()

    def submit(self, request_data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """以 task_id 实现幂等提交，并创建专属任务目录。"""
        task_id = request_data["task_id"]
        existing = self.store.get_task(task_id)
        if existing:
            return existing, False
        if self._queue.full():
            raise QueueFullError("Worker 本地任务队列已满")

        task_dir = self.config.tasks_dir / task_id
        if task_dir.exists():
            raise TaskConflictError(
                f"任务目录已经存在但数据库中没有对应记录：{task_id}"
            )
        if not self.store.create_task(request_data):
            existing = self.store.get_task(task_id)
            if existing:
                return existing, False
            raise TaskConflictError(f"任务 ID 冲突：{task_id}")

        try:
            task_dir.mkdir(parents=True, exist_ok=False)
            self._queue.put_nowait(task_id)
        except Exception as exc:
            self.store.update_task(
                task_id,
                status="FAILED",
                stage="SUBMIT_FAILED",
                error=str(exc),
                finished_at=utc_now(),
            )
            raise
        return self.store.get_task(task_id) or {}, True

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        """取消排队任务，或请求终止正在运行的任务进程树。"""
        task = self.store.get_task(task_id)
        if task is None:
            return None
        if task["status"] in TERMINAL_STATUSES:
            return task

        self.store.request_cancel(task_id)
        if task["status"] == "QUEUED":
            self.store.update_task(
                task_id,
                status="CANCELED",
                stage="CANCELED",
                error="任务在排队阶段被取消",
                finished_at=utc_now(),
            )
        elif self.active_task_id == task_id:
            self.store.update_task(task_id, status="CANCELING", stage="CANCELING")
            self.cancel_active_process()
        return self.store.get_task(task_id)

    def cancel_active_process(self) -> None:
        """关闭当前活跃子进程，供取消和服务退出复用。"""
        with self._active_lock:
            process = self._active_process
        if process and process.poll() is None:
            self._terminate_process_tree(process)

    def _worker_loop(self) -> None:
        """串行消费队列；单个任务失败不会终止整个 Worker。"""
        while not self._stop_event.is_set():
            try:
                task_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if task_id is None:
                self._queue.task_done()
                return
            try:
                task = self.store.get_task(task_id)
                if not task or task["status"] in TERMINAL_STATUSES:
                    continue
                if self.store.is_cancel_requested(task_id):
                    self.store.update_task(
                        task_id,
                        status="CANCELED",
                        stage="CANCELED",
                        finished_at=utc_now(),
                    )
                    continue
                with self._active_lock:
                    self._active_task_id = task_id
                self._run_task(task)
            finally:
                with self._active_lock:
                    self._active_task_id = None
                    self._active_process = None
                self._queue.task_done()

    def _run_task(self, task: dict[str, Any]) -> None:
        """执行 PREPARING→CAPTURING→ANALYZING→VALIDATING 状态机。"""
        task_id = task["task_id"]
        request_data = task["request"]
        task_dir = self.config.tasks_dir / task_id
        log_path = task_dir / "worker.log"
        deadline = time.monotonic() + self.config.task_timeout_seconds
        errors: list[str] = []

        try:
            self.store.update_task(
                task_id,
                status="PREPARING",
                stage="PREPARING",
                started_at=utc_now(),
                error=None,
            )
            self._write_task_inputs(task_dir, request_data)
            self._append_log(
                log_path,
                f"Worker={self.config.worker_id} task={task_id} "
                f"platform={platform.platform()} no_cache=true",
            )

            output_dir = task_dir / "fetch_output"
            capture_command = [
                str(self.config.python_executable),
                str(self.config.project_root / "wiki_fetcher.py"),
                "--input",
                str(task_dir / "input.tsv"),
                "--output-dir",
                str(output_dir),
                "--browsers",
                *request_data["browsers"],
            ]
            if request_data["pcap"]:
                capture_command.append("--pcap")

            self.store.update_task(
                task_id, status="CAPTURING", stage="CAPTURING"
            )
            capture_code = self._run_command(
                task_id, capture_command, log_path, deadline
            )
            if capture_code != 0:
                errors.append(f"wiki_fetcher.py 退出码：{capture_code}")

            steps = request_data["analysis"]["steps"]
            if steps and capture_code == 0:
                analysis_command = [
                    str(self.config.python_executable),
                    str(self.config.project_root / "batch_process.py"),
                    str(output_dir),
                    "--only",
                    *steps,
                    "--jobs",
                    "1",
                ]
                if request_data["analysis"]["with_coframe"]:
                    analysis_command.append("--with-coframe")
                suffixes = request_data["analysis"]["sni_suffixes"]
                if suffixes:
                    analysis_command.extend(["--sni-suffix", *suffixes])

                self.store.update_task(
                    task_id, status="ANALYZING", stage="ANALYZING"
                )
                analysis_code = self._run_command(
                    task_id, analysis_command, log_path, deadline
                )
                if analysis_code != 0:
                    errors.append(f"batch_process.py 退出码：{analysis_code}")

            self._raise_if_canceled(task_id)
            self.store.update_task(
                task_id, status="VALIDATING", stage="VALIDATING"
            )
            manifest = self._build_manifest(task_dir, request_data, errors)
            (task_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            final_status = manifest["status"]
            final_error = "; ".join(errors) if errors else None
            self.store.update_task(
                task_id,
                status=final_status,
                stage="DONE",
                result_json=manifest,
                error=final_error,
                pid=None,
                finished_at=utc_now(),
            )
            self._append_log(
                log_path,
                f"task={task_id} finished status={final_status}",
            )
        except TaskCanceledError:
            self.store.update_task(
                task_id,
                status="CANCELED",
                stage="CANCELED",
                error="任务已取消",
                pid=None,
                finished_at=utc_now(),
            )
            self._append_log(log_path, f"task={task_id} canceled")
        except TaskTimedOutError as exc:
            self.store.update_task(
                task_id,
                status="FAILED",
                stage="TIMED_OUT",
                error=str(exc),
                pid=None,
                finished_at=utc_now(),
            )
            self._append_log(log_path, f"task={task_id} timed out: {exc}")
        except Exception as exc:
            self.store.update_task(
                task_id,
                status="FAILED",
                stage="FAILED",
                error=f"{type(exc).__name__}: {exc}",
                pid=None,
                finished_at=utc_now(),
            )
            self._append_log(
                log_path,
                f"task={task_id} failed: {type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _write_task_inputs(task_dir: Path, request_data: dict[str, Any]) -> None:
        """落盘规范化请求，并生成旧抓取脚本所需的 input.tsv。"""
        (task_dir / "request.json").write_text(
            json.dumps(request_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        lines = [
            f"{item['id']}\t{item['name']}\t{item['url']}"
            for item in request_data["items"]
        ]
        (task_dir / "input.tsv").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

    def _run_command(
        self,
        task_id: str,
        command: list[str],
        log_path: Path,
        deadline: float,
    ) -> int:
        """在剩余超时内运行命令，并持续响应数据库中的取消标记。"""
        self._raise_if_canceled(task_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TaskTimedOutError(
                f"任务超过 {self.config.task_timeout_seconds} 秒限制"
            )

        self._append_log(log_path, "$ " + shlex.join(command))
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        # 浏览器代理只传给采集子进程使用，不改变主控访问 Worker 的网络路径。
        if self.config.proxy_url:
            environment["BROWSER_PROXY_URL"] = self.config.proxy_url
        else:
            environment.pop("BROWSER_PROXY_URL", None)
        # webdriver-manager 的驱动二进制属于运行依赖，不是网页实验缓存。
        # 固定放到 WORKER_DATA_DIR/.wdm，避免服务账户无法写用户主目录。
        environment.pop("WDM_LOCAL", None)
        environment["WDM_CACHE_DIR"] = str(self.config.data_dir)
        popen_kwargs: dict[str, Any] = {
            "cwd": self.config.data_dir,
            "env": environment,
            "stdin": subprocess.DEVNULL,
            "stdout": None,
            "stderr": subprocess.STDOUT,
            "shell": False,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        with log_path.open("ab", buffering=0) as log_file:
            popen_kwargs["stdout"] = log_file
            process = subprocess.Popen(command, **popen_kwargs)

            with self._active_lock:
                self._active_process = process
            self.store.update_task(task_id, pid=process.pid)

            try:
                while True:
                    return_code = process.poll()
                    if return_code is not None:
                        return return_code
                    if self.store.is_cancel_requested(task_id):
                        self._terminate_process_tree(process)
                        raise TaskCanceledError()
                    if time.monotonic() >= deadline:
                        self._terminate_process_tree(process)
                        raise TaskTimedOutError(
                            f"任务超过 {self.config.task_timeout_seconds} 秒限制"
                        )
                    time.sleep(0.5)
            finally:
                with self._active_lock:
                    if self._active_process is process:
                        self._active_process = None
                self.store.update_task(task_id, pid=None)

    def _raise_if_canceled(self, task_id: str) -> None:
        """把持久化取消标记转换为控制流异常。"""
        if self.store.is_cancel_requested(task_id):
            raise TaskCanceledError()

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
        """按操作系统终止整个子进程树，防止遗留浏览器或 TShark。"""
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
            else:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            try:
                process.kill()
            except OSError:
                pass

    @staticmethod
    def _append_log(log_path: Path, message: str) -> None:
        """以 UTC 时间前缀追加 Worker 自身日志。"""
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat()
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{timestamp}] {message}\n")

    def _build_manifest(
        self,
        task_dir: Path,
        request_data: dict[str, Any],
        process_errors: list[str],
    ) -> dict[str, Any]:
        """根据真实产物而非脚本退出码，生成 URL 和浏览器两级结果。"""
        output_dir = task_dir / "fetch_output"
        units: list[dict[str, Any]] = []

        for item in request_data["items"]:
            candidates = sorted(output_dir.glob(f"{item['id']}-wiki-*"))
            item_dir = candidates[0] if candidates else None
            for browser in request_data["browsers"]:
                checks: dict[str, bool] = {}
                artifacts: dict[str, dict[str, Any] | None] = {}

                body = item_dir / f"body_{browser}.html" if item_dir else None
                checks["body"] = self._file_has_data(body)
                artifacts["body"] = self._artifact_info(task_dir, body)

                if request_data["pcap"]:
                    pcap = item_dir / f"capture_{browser}.pcap" if item_dir else None
                    checks["pcap"] = self._file_has_data(pcap)
                    artifacts["pcap"] = self._artifact_info(task_dir, pcap)

                for step in request_data["analysis"]["steps"]:
                    if step == "extract":
                        path = item_dir / f"capture_{browser}.tsv" if item_dir else None
                        checks["extract"] = self._file_has_data(path)
                        artifacts["extract"] = self._artifact_info(task_dir, path)
                    elif step == "classify":
                        path = item_dir / f"capture_{browser}_flows" if item_dir else None
                        checks["classify"] = self._directory_has_files(path)
                        artifacts["classify"] = self._artifact_info(task_dir, path)
                    elif step == "infer":
                        path = item_dir / f"capture_{browser}_inferred" if item_dir else None
                        checks["infer"] = self._directory_has_files(path)
                        artifacts["infer"] = self._artifact_info(task_dir, path)

                has_any = any(value is not None for value in artifacts.values())
                if checks and all(checks.values()):
                    status = "SUCCEEDED"
                elif has_any:
                    status = "PARTIAL"
                else:
                    status = "FAILED"
                units.append(
                    {
                        "item_id": item["id"],
                        "name": item["name"],
                        "url": item["url"],
                        "browser": browser,
                        "status": status,
                        "checks": checks,
                        "artifacts": artifacts,
                    }
                )

        items: list[dict[str, Any]] = []
        for item in request_data["items"]:
            item_units = [unit for unit in units if unit["item_id"] == item["id"]]
            item_status = self._aggregate_statuses(
                [unit["status"] for unit in item_units]
            )
            items.append(
                {
                    "item_id": item["id"],
                    "name": item["name"],
                    "url": item["url"],
                    "status": item_status,
                    "browser_statuses": [
                        {
                            "browser": unit["browser"],
                            "status": unit["status"],
                        }
                        for unit in item_units
                    ],
                }
            )

        succeeded = sum(unit["status"] == "SUCCEEDED" for unit in units)
        partial = sum(unit["status"] == "PARTIAL" for unit in units)
        failed = sum(unit["status"] == "FAILED" for unit in units)
        item_succeeded = sum(item["status"] == "SUCCEEDED" for item in items)
        item_partial = sum(item["status"] == "PARTIAL" for item in items)
        item_failed = sum(item["status"] == "FAILED" for item in items)
        final_status = self._aggregate_statuses(
            [item["status"] for item in items]
        )

        file_count = 0
        total_bytes = 0
        for path in sorted(task_dir.rglob("*")):
            if path.is_file():
                file_count += 1
                total_bytes += path.stat().st_size

        return {
            "task_id": request_data["task_id"],
            "worker_id": self.config.worker_id,
            "status": final_status,
            "generated_at": utc_now(),
            "summary": {
                "total": len(units),
                "succeeded": succeeded,
                "partial": partial,
                "failed": failed,
            },
            "item_summary": {
                "total": len(items),
                "succeeded": item_succeeded,
                "partial": item_partial,
                "failed": item_failed,
            },
            "cache_policy": {
                "enabled": False,
                "fresh_browser_profile": True,
                "reuse_existing_results": False,
                "supported_browsers": list(self.config.allowed_browsers),
            },
            "process_errors": list(process_errors),
            "items": items,
            "units": units,
            "storage": {
                "task_dir": f"tasks/{request_data['task_id']}",
                "file_count": file_count,
                "total_bytes": total_bytes,
            },
        }

    @staticmethod
    def _aggregate_statuses(statuses: list[str]) -> str:
        """按全成功、部分有效、全失败三档汇总子状态。"""
        if statuses and all(status == "SUCCEEDED" for status in statuses):
            return "SUCCEEDED"
        if any(status in {"SUCCEEDED", "PARTIAL"} for status in statuses):
            return "PARTIAL"
        return "FAILED"

    @staticmethod
    def _file_has_data(path: Path | None) -> bool:
        """判断预期文件是否存在且非空。"""
        return bool(path and path.is_file() and path.stat().st_size > 0)

    @staticmethod
    def _directory_has_files(path: Path | None) -> bool:
        """判断结果目录中是否至少存在一个文件。"""
        return bool(path and path.is_dir() and any(item.is_file() for item in path.rglob("*")))

    @staticmethod
    def _artifact_info(task_dir: Path, path: Path | None) -> dict[str, Any] | None:
        """返回相对任务目录的产物元数据，避免 API 暴露绝对路径。"""
        if path is None or not path.exists():
            return None
        if path.is_file():
            size = path.stat().st_size
            kind = "file"
        else:
            size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            kind = "directory"
        return {
            "path": path.relative_to(task_dir).as_posix(),
            "kind": kind,
            "size": size,
        }
