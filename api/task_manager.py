"""In-memory task manager for the FastAPI service (v4 stage J).

**已知限制**（写进 docs/deployment.md）：单进程内存任务队列，不做持久化、
不做分布式；服务重启后所有任务状态丢失。这是明确声明的当前限制，不是缺陷。

`WorkflowOrchestrator.run()` 本身是同步阻塞调用（LLM/网络/AkShare 请求），
所以每个任务在线程池里跑，FastAPI 的 asyncio 事件循环不被阻塞；
`max_workers` 就是并发任务数限制。
"""
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from config import config
from orchestrator.workflow import WorkflowOrchestrator
from schemas.request import ResearchRequest
from src.runtime.runner import HarnessConfig, HarnessRunner
from utils.logger import logger

_TERMINAL_STATUSES = {"succeeded", "failed", "degraded"}


@dataclass
class TaskRecord:
    task_id: str
    status: str = "queued"
    progress: float = 0.0
    current_stage: str = ""
    error: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    topic: str = ""
    report_type: str = ""
    export_formats: list = field(default_factory=list)
    enable_memory: bool = False
    export_paths: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


class TaskManager:
    """Bounded in-memory task queue + worker pool."""

    def __init__(self, max_concurrent: int = 2, task_timeout_seconds: int = 900) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent, thread_name_prefix="report-worker")
        self._task_timeout_seconds = task_timeout_seconds

    def submit(self, api_request: Any) -> str:
        task_id = uuid.uuid4().hex[:12]
        record = TaskRecord(
            task_id=task_id, created_at=datetime.now().isoformat(timespec="seconds"),
            topic=api_request.topic, report_type=api_request.report_type,
            export_formats=list(api_request.export_formats), enable_memory=api_request.enable_memory,
        )
        with self._lock:
            self._tasks[task_id] = record
        self._executor.submit(self._run_guarded, task_id, api_request)
        return task_id

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._tasks.get(task_id)

    def _update(self, task_id: str, **kwargs) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if record:
                for k, v in kwargs.items():
                    setattr(record, k, v)

    def _run_guarded(self, task_id: str, api_request: Any) -> None:
        """Wrap _run so a worker-thread crash can never leave a task stuck
        in 'running' forever, and enforce a hard wall-clock timeout."""
        result_holder: dict[str, Any] = {}

        def target() -> None:
            try:
                self._run(task_id, api_request)
            except Exception as exc:  # noqa: BLE001 - last-resort guard
                logger.error(f"task {task_id} worker crashed: {exc!r}\n{traceback.format_exc()}")
                self._update(task_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                            finished_at=datetime.now().isoformat(timespec="seconds"))
            finally:
                result_holder["done"] = True

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=self._task_timeout_seconds)
        if not result_holder.get("done"):
            self._update(task_id, status="failed",
                         error=f"task exceeded {self._task_timeout_seconds}s timeout",
                         finished_at=datetime.now().isoformat(timespec="seconds"))
            logger.warning(f"task {task_id} timed out after {self._task_timeout_seconds}s (worker thread abandoned)")

    def _run(self, task_id: str, api_request: Any) -> None:
        self._update(task_id, status="running", started_at=datetime.now().isoformat(timespec="seconds"))

        def on_progress(step_index: int, total_steps: int, label: str) -> None:
            self._update(task_id, progress=round(step_index / max(total_steps, 1), 2), current_stage=label)

        request = ResearchRequest(
            topic=api_request.topic, report_type=api_request.report_type,
            requirements=api_request.requirements, output_format=api_request.output_format,
            max_sources=api_request.max_sources, local_files=api_request.local_files,
            enable_revision=api_request.enable_revision,
        )
        orchestrator = WorkflowOrchestrator()
        if config.USE_AGENT_HARNESS:
            runner = HarnessRunner(HarnessConfig(
                max_concurrency=config.HARNESS_MAX_CONCURRENCY,
                context_budget_tokens=config.HARNESS_CONTEXT_BUDGET_TOKENS,
                memory_enabled=api_request.enable_memory,
                namespace="api_local",
                user_id="api_local",
                arm="production_api",
            ))
            result = runner.run(request, orchestrator=orchestrator, on_progress=on_progress)
        else:
            result = orchestrator.run(request, on_progress=on_progress)

        evaluation = result.get("evaluation", {}) or {}
        num_sources = result.get("num_sources", 0)
        warnings: list[str] = []
        harness_status = result.get("harness_status")
        if harness_status in ("failed", "cancelled", "insufficient"):
            status = "failed"
            error = result.get("harness_stop_reason") or "harness_failed"
            if evaluation.get("entity_validation_failed"):
                error = "insufficient_entity_evidence"
        elif harness_status == "degraded":
            status, error = "degraded", None
            warnings.extend(result.get("harness_degradations") or [])
        elif evaluation.get("entity_validation_failed"):
            status, error = "failed", "insufficient_entity_evidence"
        elif num_sources == 0:
            status, error = "failed", "no usable sources found"
        else:
            status, error = "succeeded", None
            if evaluation.get("diagnostics", {}).get("unsupported_unlisted_company"):
                status = "degraded"
                warnings.append("unsupported_unlisted_company: 公开资料摘要，非正式上市公司研报")
            if (evaluation.get("overall_score") is not None
                    and evaluation.get("overall_score", 1.0) < 0.4):
                status = "degraded"
                warnings.append(f"overall_score={evaluation.get('overall_score')} 偏低")

        export_paths: dict[str, Any] = {}
        if status in ("succeeded", "degraded") and api_request.export_formats and result.get("run_id"):
            try:
                from scripts.export_reports import export_run_result

                exported = export_run_result(result["run_id"])
                if exported:
                    if "docx" in api_request.export_formats:
                        export_paths["docx"] = exported["docx"]
                    if "pdf" in api_request.export_formats:
                        export_paths["pdf"] = exported["pdf"]
            except Exception as exc:  # noqa: BLE001 - export is an enhancement, never blocks the task
                warnings.append(f"export failed (degraded): {exc!r}")
                logger.warning(f"task {task_id} export failed: {exc!r}")

        if (not config.USE_AGENT_HARNESS and status in ("succeeded", "degraded")
                and api_request.enable_memory):
            try:
                from memory.report_memory import build_index

                build_index()
            except Exception as exc:  # noqa: BLE001 - memory indexing is opt-in and best-effort
                warnings.append(f"memory index rebuild failed (degraded): {exc!r}")
                logger.warning(f"task {task_id} memory index failed: {exc!r}")

        self._update(task_id, status=status, progress=1.0, result=result, error=error,
                     export_paths=export_paths, warnings=warnings,
                     finished_at=datetime.now().isoformat(timespec="seconds"))


task_manager = TaskManager(
    max_concurrent=config.API_MAX_CONCURRENT_TASKS,
    task_timeout_seconds=config.API_TASK_TIMEOUT_SECONDS,
)
