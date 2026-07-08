"""Base class providing a uniform execution interface, retries, logging and LLM access."""
import json
import re
import time
from typing import Any, Optional

from config import config
from schemas.task import Task, TaskResult
from utils.logger import logger


class BaseAgent:
    """Common scaffolding all agents share: retrying run(), step logging, LLM calls."""

    name: str = "base_agent"
    description: str = "Base agent"
    tools: list[str] = []

    def __init__(self) -> None:
        self.trace_steps: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Execution
    # ------------------------------------------------------------------ #
    def run(self, task: Task, context: dict[str, Any]) -> TaskResult:
        """Execute `task` with retries, catching all exceptions into TaskResult.

        Subclasses implement `execute()`, not `run()`. This wraps execute()
        with timing, retry-on-failure and error capture so a single agent
        failure never crashes the orchestrator.
        """
        start = time.perf_counter()
        attempts = 0
        last_error: Optional[str] = None
        last_partial_result: dict[str, Any] = {}

        while attempts <= task.max_retries:
            attempts += 1
            try:
                result_data = self.execute(task, context)
                elapsed = round(time.perf_counter() - start, 4)
                self.log_step(task, success=True, detail=f"attempt {attempts} ok", duration=elapsed)
                return TaskResult(
                    task_id=task.task_id,
                    task_type=task.task_type,
                    success=True,
                    result=result_data,
                    execution_time=elapsed,
                    metadata={"attempts": attempts, "agent": self.name},
                )
            except Exception as exc:  # noqa: BLE001 - agents must never crash the run
                last_error = self.handle_error(task, exc, attempts)
                # Agents may attach diagnostics to an exception via a
                # `partial_result` dict attribute (e.g. BrowserAgent's
                # browser_metrics) so trace/orchestrator can still see what
                # happened even though the task ultimately failed.
                last_partial_result = getattr(exc, "partial_result", None) or last_partial_result
                # An agent can mark an exception `non_retryable = True` when
                # retrying would just redo identical work with an identical
                # outcome (e.g. BrowserAgent re-fetching the same URL batch
                # that already came back with zero relevant sources) - retry
                # budget is for transient failures, not deterministic ones.
                if getattr(exc, "non_retryable", False):
                    break
                if attempts <= task.max_retries:
                    continue

        elapsed = round(time.perf_counter() - start, 4)
        self.log_step(task, success=False, detail=last_error or "unknown error", duration=elapsed)
        return TaskResult(
            task_id=task.task_id,
            task_type=task.task_type,
            success=False,
            result=last_partial_result,
            error=last_error,
            execution_time=elapsed,
            metadata={"attempts": attempts, "agent": self.name},
        )

    def execute(self, task: Task, context: dict[str, Any]) -> dict[str, Any]:
        """Perform the agent's actual work. Must be overridden by subclasses."""
        raise NotImplementedError

    # ------------------------------------------------------------------ #
    # Logging / error handling
    # ------------------------------------------------------------------ #
    def log_step(self, task: Task, success: bool, detail: str, duration: float) -> None:
        """Record a step both to loguru and to this agent's in-memory trace buffer."""
        level = "info" if success else "warning"
        getattr(logger, level)(
            f"[{self.name}] task={task.task_id} type={task.task_type} "
            f"success={success} duration={duration}s detail={detail}"
        )
        self.trace_steps.append(
            {
                "agent": self.name,
                "task_id": task.task_id,
                "task_type": task.task_type,
                "success": success,
                "detail": detail,
                "duration": duration,
            }
        )

    def handle_error(self, task: Task, exc: Exception, attempt: int) -> str:
        """Log an exception raised during execute() and return its message."""
        msg = f"{type(exc).__name__}: {exc}"
        logger.warning(f"[{self.name}] task={task.task_id} attempt={attempt} failed: {msg}")
        return msg

    # ------------------------------------------------------------------ #
    # Shared LLM helper (via LiteLLM so provider is swappable through .env)
    # ------------------------------------------------------------------ #
    @staticmethod
    def call_llm(prompt: str, system: str = "", temperature: float = 0.3) -> str:
        """Call the configured LLM through LiteLLM and return the raw text response."""
        import litellm

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = litellm.completion(
            model=config.MODEL_NAME,
            messages=messages,
            temperature=temperature,
            timeout=60,
            api_base=config.OPENAI_API_BASE or None,
        )
        return response["choices"][0]["message"]["content"] or ""

    @staticmethod
    def parse_json_response(text: str) -> Optional[Any]:
        """Best-effort extraction of a JSON object/array from an LLM response.

        Handles responses wrapped in markdown code fences or with stray prose
        around the JSON payload. Returns None if nothing parseable is found.
        """
        if not text:
            return None

        candidate = text.strip()
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", candidate)
        if fence_match:
            candidate = fence_match.group(1).strip()

        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        first_obj = re.search(r"[\[{][\s\S]*[\]}]", candidate)
        if first_obj:
            try:
                return json.loads(first_obj.group(0))
            except json.JSONDecodeError:
                return None
        return None
