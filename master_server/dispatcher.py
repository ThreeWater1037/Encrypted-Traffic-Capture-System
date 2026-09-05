"""主控任务分发、Worker 轮询和结果聚合。"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

from .config import MasterConfig
from .store import MasterStore, TERMINAL_STATUSES, utc_now
from .worker_client import WorkerClient, WorkerRequestError


ClientFactory = Callable[[dict[str, Any]], WorkerClient]
PROGRESS_COMPLETED_STATUSES = TERMINAL_STATUSES | {"CAPTURED"}


class QueueFullError(RuntimeError):
    """主控本地待分发任务队列已满。"""


def public_machine(machine: dict[str, Any]) -> dict[str, Any]:
    """移除 Worker Token，避免密钥进入浏览器。"""
    hidden = dict(machine)
    hidden.pop("token", None)
    hidden["token_configured"] = bool(machine.get("token"))
    return hidden


def summarize_job(job: dict[str, Any]) -> dict[str, Any]:
    """生成前端需要的进度、URL 汇总和目标汇总。"""
    if isinstance(job.get("execution_counts"), dict):
        public = {
            key: value
            for key, value in job.items()
            if key not in {"execution_counts", "source_path"}
        }
        counts = job["execution_counts"]
        total = sum(counts.values())
        terminal_count = sum(
            count for status, count in counts.items() if status in PROGRESS_COMPLETED_STATUSES
        )
        public["summary"] = {
            "total": total,
            "completed": terminal_count,
            "progress": round(terminal_count * 100 / total) if total else 0,
            "succeeded": counts.get("SUCCEEDED", 0),
            "partial": counts.get("PARTIAL", 0),
            "failed": counts.get("FAILED", 0) + counts.get("INTERRUPTED", 0),
            "canceled": counts.get("CANCELED", 0),
            "statuses": counts,
        }
        executions = job.get("executions", [])
        if executions:
            item_groups: dict[str, list[dict[str, Any]]] = {}
            target_groups: dict[str, list[dict[str, Any]]] = {}
            for execution in executions:
                item_groups.setdefault(execution["item_id"], []).append(execution)
                target_groups.setdefault(execution["machine_id"], []).append(execution)
            public["items"] = []
            for item_id, units in item_groups.items():
                first = units[0]
                public["items"].append(
                    {
                        "id": item_id,
                        "name": first["item_name"],
                        "url": first["url"],
                        "status": _aggregate_execution_statuses(
                            [unit["status"] for unit in units]
                        ),
                        "executions": units,
                    }
                )
            public["targets"] = []
            for machine_id, units in target_groups.items():
                first = units[0]
                public["targets"].append(
                    {
                        "machine_id": machine_id,
                        "machine_name": first.get("machine_name", machine_id),
                        "machine_url": first.get("machine_url"),
                        "worker_task_id": first.get("worker_task_id"),
                        "status": _aggregate_execution_statuses(
                            [unit["status"] for unit in units]
                        ),
                    }
                )
        return public

    public = {key: value for key, value in job.items() if key != "source_path"}
    executions = job.get("executions", [])
    counts: dict[str, int] = {}
    for execution in executions:
        counts[execution["status"]] = counts.get(execution["status"], 0) + 1
    terminal_count = sum(
        count for status, count in counts.items() if status in PROGRESS_COMPLETED_STATUSES
    )
    total = len(executions)
    public["summary"] = {
        "total": total,
        "completed": terminal_count,
        "progress": round(terminal_count * 100 / total) if total else 0,
        "succeeded": counts.get("SUCCEEDED", 0),
        "partial": counts.get("PARTIAL", 0),
        "failed": counts.get("FAILED", 0) + counts.get("INTERRUPTED", 0),
        "canceled": counts.get("CANCELED", 0),
        "statuses": counts,
    }

    item_groups: dict[str, list[dict[str, Any]]] = {}
    target_groups: dict[str, list[dict[str, Any]]] = {}
    for execution in executions:
        item_groups.setdefault(execution["item_id"], []).append(execution)
        target_groups.setdefault(execution["machine_id"], []).append(execution)

    public["items"] = []
    for item in job["request"]["items"]:
        units = item_groups.get(item["id"], [])
        public["items"].append(
            {
                **item,
                "status": _aggregate_execution_statuses(
                    [unit["status"] for unit in units]
                ),
                "executions": units,
            }
        )

    public["targets"] = []
    for target in job["request"]["targets"]:
        units = target_groups.get(target["machine_id"], [])
        first = units[0] if units else {}
        public["targets"].append(
            {
                **target,
                "machine_name": first.get("machine_name", target["machine_id"]),
                "machine_url": first.get("machine_url"),
                "worker_task_id": first.get("worker_task_id"),
                "status": _aggregate_execution_statuses(
                    [unit["status"] for unit in units]
                ),
            }
        )
    return public


def _aggregate_execution_statuses(statuses: list[str]) -> str:
    if not statuses:
        return "CREATED"
    if any(status not in TERMINAL_STATUSES for status in statuses):
        for preferred in (
            "ANALYZING",
            "CAPTURING",
            "CAPTURED",
            "VALIDATING",
            "PREPARING",
            "RESUMING",
            "QUEUED",
            "WAITING_FOR_WORKER",
            "DISPATCHING",
            "CREATED",
        ):
            if preferred in statuses:
                return preferred
        return "RUNNING"
    if all(status == "SUCCEEDED" for status in statuses):
        return "SUCCEEDED"
    if all(status == "CANCELED" for status in statuses):
        return "CANCELED"
    if any(status in {"SUCCEEDED", "PARTIAL"} for status in statuses):
        return "PARTIAL"
    return "FAILED"


class JobDispatcher:
    """单主控队列；一个任务中的不同机器会并行分发。"""

    def __init__(
        self,
        config: MasterConfig,
        store: MasterStore,
        *,
        client_factory: ClientFactory | None = None,
        autostart: bool = True,
    ):
        self.config = config
        self.store = store
        self._queue: queue.Queue[str | None] = queue.Queue(config.max_queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client_factory = client_factory or self._default_client
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        if autostart:
            self.start()

    def _default_client(self, machine: dict[str, Any]) -> WorkerClient:
        return WorkerClient(
            machine["base_url"],
            machine["token"],
            timeout=self.config.worker_request_timeout,
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._loop, name="master-dispatcher", daemon=True
        )
        self._thread.start()
        for job_id in self.store.incomplete_job_ids():
            self.enqueue(job_id)

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout)

    def enqueue(self, job_id: str) -> None:
        with self._lock:
            if job_id in self._queued:
                return
            try:
                self._queue.put_nowait(job_id)
            except queue.Full as exc:
                raise QueueFullError("主控任务队列已满") from exc
            self._queued.add(job_id)

    def cancel(self, job_id: str) -> bool:
        return self.store.request_cancel(job_id)

    def probe(self, machine_id: str) -> dict[str, Any]:
        machine = self.store.get_machine(machine_id)
        if machine is None:
            raise KeyError(machine_id)
        client = self._client_factory(machine)
        try:
            health = client.health()
            capabilities = client.capabilities()
            status = "BUSY" if health.get("busy") else "ONLINE"
            self.store.update_machine_probe(
                machine_id,
                status=status,
                health=health,
                capabilities=capabilities,
            )
        except WorkerRequestError as exc:
            self.store.update_machine_probe(
                machine_id, status="OFFLINE", error=str(exc)
            )
        return public_machine(self.store.get_machine(machine_id) or {})

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None:
                break
            try:
                self._run_job(job_id)
            finally:
                with self._lock:
                    self._queued.discard(job_id)
                self._queue.task_done()

    def _run_job(self, job_id: str) -> None:
        job = self.store.get_job_control(job_id)
        if job is None or job["status"] in TERMINAL_STATUSES:
            return
        if job["cancel_requested"]:
            for target in job["request"]["targets"]:
                self.store.update_worker_executions(
                    job_id, target["machine_id"], status="CANCELED"
                )
            self._sync_job_status(job_id)
            return

        self.store.update_job(
            job_id,
            status="DISPATCHING",
            stage="DISPATCHING",
            started_at=job["started_at"] or utc_now(),
            error=None,
        )
        targets = job["request"]["targets"]
        with ThreadPoolExecutor(max_workers=max(1, len(targets))) as executor:
            futures = {
                executor.submit(
                    self._run_target,
                    job_id,
                    target,
                    job["request"],
                    job.get("resume_token"),
                ): target
                for target in targets
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    target = futures[future]
                    self.store.update_worker_executions(
                        job_id,
                        target["machine_id"],
                        status="FAILED",
                        error=f"主控内部调度错误：{type(exc).__name__}: {exc}",
                    )
                self._sync_job_status(job_id)
        if not self._stop.is_set():
            self._sync_job_status(job_id, finished=True)

    def _run_target(
        self,
        job_id: str,
        target: dict[str, Any],
        request_data: dict[str, Any],
        resume_token: str | None,
    ) -> None:
        machine_id = target["machine_id"]
        machine = self.store.get_machine(machine_id)
        if machine is None or not machine["enabled"]:
            message = "目标机器不存在或已停用"
            self.store.update_worker_executions(
                job_id, machine_id, status="FAILED", error=message
            )
            return
        client = self._client_factory(machine)
        worker_task_id = MasterStore.worker_task_id(job_id, machine_id)
        task: dict[str, Any] | None = None
        last_progress: tuple[str, str, str | None] | None = None
        progress_run_id: str | None = None
        progress_position = 0

        while task is None and not self._stop.is_set():
            job = self.store.get_job_status(job_id)
            if job is None:
                return
            if job["cancel_requested"]:
                self.store.update_worker_executions(
                    job_id, machine_id, status="CANCELED"
                )
                return
            try:
                health = client.health()
                capabilities = client.capabilities()
                available = {
                    item["name"] for item in capabilities.get("browsers", [])
                }
                missing = sorted(set(target["browsers"]) - available)
                if missing:
                    self.store.update_worker_executions(
                        job_id,
                        machine_id,
                        status="FAILED",
                        error=f"Worker 缺少所选浏览器：{', '.join(missing)}",
                    )
                    return
                self.store.update_machine_probe(
                    machine_id,
                    status="BUSY" if health.get("busy") else "ONLINE",
                    health=health,
                    capabilities=capabilities,
                )
                task = client.submit_task(
                    {
                        "task_id": worker_task_id,
                        "items": request_data["items"],
                        "browsers": target["browsers"],
                        "pcap": request_data["pcap"],
                        "outputs": request_data.get(
                            "outputs", {"html": False, "reports": False}
                        ),
                        "analysis": request_data["analysis"],
                    }
                )
                if resume_token:
                    task = client.resume_task(worker_task_id, resume_token)
            except WorkerRequestError as exc:
                message = str(exc)
                self.store.update_machine_probe(
                    machine_id, status="OFFLINE", error=message
                )
                progress = ("WAITING_FOR_WORKER", "WAITING_FOR_WORKER", message)
                if progress != last_progress:
                    self.store.update_worker_executions(
                        job_id,
                        machine_id,
                        status=progress[0],
                        stage=progress[1],
                        error=progress[2],
                    )
                    self._sync_job_status(job_id)
                    last_progress = progress
                if self._stop.wait(self.config.poll_interval):
                    return

        if task is None:
            return

        cancel_sent = False
        while task.get("status") not in TERMINAL_STATUSES:
            progress = (
                str(task.get("status") or "RUNNING"),
                str(task.get("stage") or task.get("status") or "RUNNING"),
                task.get("error"),
            )
            if progress != last_progress:
                self._apply_worker_progress(job_id, machine_id, task)
                last_progress = progress
            if progress[1] in {"QUEUED", "PREPARING", "RESUMING", "CAPTURING"}:
                try:
                    progress_run_id, progress_position = self._sync_worker_capture_progress(
                        client,
                        worker_task_id,
                        job_id,
                        machine_id,
                        run_id=progress_run_id,
                        position=progress_position,
                    )
                except WorkerRequestError:
                    # 任务状态轮询仍是主链路；短暂的进度端点失败交给下一轮补齐。
                    pass
            if self._stop.wait(self.config.poll_interval):
                return
            current = self.store.get_job_status(job_id)
            if current is None:
                return
            try:
                if current["cancel_requested"] and not cancel_sent:
                    task = client.cancel_task(worker_task_id)
                    cancel_sent = True
                else:
                    task = client.get_task(worker_task_id)
            except WorkerRequestError as exc:
                message = str(exc)
                self.store.update_machine_probe(
                    machine_id, status="OFFLINE", error=message
                )
                waiting = ("WAITING_FOR_WORKER", "WAITING_FOR_WORKER", message)
                if waiting != last_progress:
                    self.store.update_worker_executions(
                        job_id,
                        machine_id,
                        status=waiting[0],
                        stage=waiting[1],
                        error=waiting[2],
                    )
                    self._sync_job_status(job_id)
                    last_progress = waiting
                continue

        while not isinstance(task.get("result"), dict) and not self._stop.is_set():
            try:
                result_response = client.get_result(worker_task_id)
                task = {
                    **task,
                    "result": result_response.get("result"),
                    "error": result_response.get("error") or task.get("error"),
                }
                break
            except WorkerRequestError as exc:
                message = str(exc)
                self.store.update_machine_probe(
                    machine_id, status="OFFLINE", error=message
                )
                waiting = ("WAITING_FOR_WORKER", "WAITING_FOR_WORKER", message)
                if waiting != last_progress:
                    self.store.update_worker_executions(
                        job_id,
                        machine_id,
                        status=waiting[0],
                        stage=waiting[1],
                        error=waiting[2],
                    )
                    self._sync_job_status(job_id)
                    last_progress = waiting
                if self._stop.wait(self.config.poll_interval):
                    return

        if self._stop.is_set():
            return
        self._apply_worker_result(job_id, machine_id, task)

    def _sync_worker_capture_progress(
        self,
        client: WorkerClient,
        worker_task_id: str,
        job_id: str,
        machine_id: str,
        *,
        run_id: str | None,
        position: int,
    ) -> tuple[str | None, int]:
        """分页拉取 Worker 原子检查点，并增量更新执行矩阵。"""
        while True:
            snapshot = client.get_capture_progress(
                worker_task_id,
                run_id=run_id,
                after_position=position,
                limit=1000,
            )
            snapshot_run_id = snapshot.get("run_id")
            if isinstance(snapshot_run_id, str) and snapshot_run_id:
                if snapshot_run_id != run_id:
                    position = 0
                run_id = snapshot_run_id
            units = snapshot.get("units")
            valid_units = (
                [unit for unit in units if isinstance(unit, dict)]
                if isinstance(units, list)
                else []
            )
            if valid_units:
                self.store.update_worker_capture_progress(
                    job_id, machine_id, valid_units
                )
                self._sync_job_status(job_id)
            next_position = snapshot.get("next_position")
            if not isinstance(next_position, int) or isinstance(next_position, bool):
                break
            previous_position = position
            position = max(0, next_position)
            if not snapshot.get("has_more") or position <= previous_position:
                break
        return run_id, position

    def _apply_worker_progress(
        self, job_id: str, machine_id: str, task: dict[str, Any]
    ) -> None:
        status = str(task.get("status") or "RUNNING")
        stage = str(task.get("stage") or status)
        self.store.update_worker_executions(
            job_id,
            machine_id,
            status=status,
            stage=stage,
            error=task.get("error"),
        )
        self._sync_job_status(job_id)

    def _apply_worker_result(
        self, job_id: str, machine_id: str, task: dict[str, Any]
    ) -> None:
        top_status = str(task.get("status") or "FAILED")
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        units = result.get("units", []) if isinstance(result, dict) else []
        if top_status not in TERMINAL_STATUSES:
            top_status = "FAILED"
        self.store.update_worker_results(
            job_id,
            machine_id,
            top_status=top_status,
            units=[unit for unit in units if isinstance(unit, dict)],
            error=task.get("error"),
        )

    def _sync_job_status(self, job_id: str, *, finished: bool = False) -> None:
        job = self.store.get_job_status(job_id)
        if job is None:
            return
        counts = self.store.execution_status_counts(job_id)
        statuses = list(counts)
        aggregated = _aggregate_execution_statuses(statuses)
        if aggregated in TERMINAL_STATUSES:
            self.store.update_job(
                job_id,
                status=aggregated,
                stage="DONE",
                finished_at=job["finished_at"] or utc_now(),
            )
            return
        active_status = "RUNNING" if aggregated not in {"CREATED", "DISPATCHING"} else aggregated
        self.store.update_job(job_id, status=active_status, stage=aggregated)
        if finished:
            # 防止未知 Worker 状态导致任务永久停留在运行中。
            self.store.update_job(
                job_id,
                status="FAILED",
                stage="DONE",
                error="所有分发线程已结束，但仍存在非终态执行记录",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
