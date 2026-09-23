"""Tool executor: one place where every tool call is validated, guarded,
retried, budgeted, deduped, traced and recorded.

Call path for a single tool invocation:

    call(name, args, rationale)
      1. resolve spec                       -> NOT_FOUND if unknown
      2. cancellation check                 -> POLICY_BLOCKED
      3. coerce + default + validate args   -> INVALID_ARGUMENT (no retry)
      4. side-effect policy                 -> POLICY_BLOCKED (approval/dry-run)
      5. budget pre-flight                  -> BUDGET_EXCEEDED (no retry)
      6. circuit breaker                    -> blocked, try fallback_tool
      7. dedup memo hit                     -> return memoized, count redundant
      8. attempt loop (<= spec.max_attempts)
           concurrency slot -> timeout-guarded handler -> classify outcome
           transient failure -> backoff + retry; anything else -> stop
      9. validate output schema             -> SCHEMA_ERROR
     10. project result: compact -> caller, full -> ArtifactStore
     11. emit events, update budget, append StepRecord

Design notes worth keeping in mind when editing:

- **A tool returning `{"success": false}` is not an exception.** The legacy
  tools signal failure in-band. `_classify_envelope` maps that onto the error
  taxonomy so a 403 fetch becomes NOT_FOUND/AUTH_ERROR rather than a silent ok.
- **Timeouts are enforced with a worker thread, not signals.** `signal.alarm`
  is Unix-only and this project runs on Windows. A timed-out handler thread is
  abandoned (Python cannot kill a thread); the slot is released so the run
  proceeds, and the abandonment is recorded.
- **Retries only on transient classes**, per `errors.RETRYABLE`.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Callable, Optional

from src.observability.events import EventType, make_event
from src.observability.trace_store import ArtifactStore, TraceStore
from src.runtime.budget import BudgetTracker
from src.runtime.errors import (
    ErrorClass,
    HarnessError,
    InvalidArgumentError,
    OutputSchemaError,
    ToolNotFoundError,
    classify_exception,
    is_retryable,
)
from src.runtime.policies import (
    CancellationToken,
    CircuitRegistry,
    ConcurrencyLimiter,
    DuplicateDetector,
    RetryPolicy,
    SideEffectPolicy,
)
from src.runtime.state import (
    AttemptRecord,
    Phase,
    RunState,
    StepRecord,
    StepStatus,
    content_hash,
    summarize,
)
from src.tools.registry import ToolRegistry, default_registry
from src.tools.schemas import ToolResult, ToolSpec

#: Envelope keys the legacy tools use to signal in-band failure.
_ENVELOPE_OK_KEYS = ("success",)


def _classify_envelope(result: Any) -> Optional[ErrorClass]:
    """Detect an in-band failure envelope and classify it.

    Returns None when the result is a success (or is not an envelope at all).
    """
    if not isinstance(result, dict):
        return None
    for key in _ENVELOPE_OK_KEYS:
        if key in result and result[key] is False:
            message = str(result.get("error") or result.get("message") or "")
            if not message:
                return ErrorClass.PERMANENT_FAILURE
            return classify_exception(RuntimeError(message))
    return None


class ToolExecutor:
    """Executes tools against a `RunState`, enforcing every runtime policy.

    One executor per run. Safe to call from several threads (ResearchAgent runs
    searches in a pool): budget and state mutations are serialized by a lock.
    """

    def __init__(
        self,
        *,
        run_state: RunState,
        trace: TraceStore,
        registry: Optional[ToolRegistry] = None,
        budget: Optional[BudgetTracker] = None,
        artifacts: Optional[ArtifactStore] = None,
        retry_policy: Optional[RetryPolicy] = None,
        circuits: Optional[CircuitRegistry] = None,
        limiter: Optional[ConcurrencyLimiter] = None,
        side_effects: Optional[SideEffectPolicy] = None,
        cancellation: Optional[CancellationToken] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.state = run_state
        self.trace = trace
        self.registry = registry or default_registry()
        # One authoritative set of counters: the tracker's state and the run
        # state's budget must be the same object, or a checkpoint records zeros
        # while the tracker enforces real limits.
        self.budget = budget or BudgetTracker(state=run_state.budget)
        run_state.budget = self.budget.state
        self.artifacts = artifacts
        self.retry_policy = retry_policy or RetryPolicy()
        self.circuits = circuits or CircuitRegistry()
        self.limiter = limiter or ConcurrencyLimiter(max_global=8)
        self.side_effects = side_effects or SideEffectPolicy()
        self.cancellation = cancellation or CancellationToken()
        self._sleep = sleep
        self._dedup = DuplicateDetector()
        #: Fingerprints rehydrated from a checkpoint. A hit on one of these is a
        #: resume-skip, not a redundant call by the agent, and the two must not
        #: be reported as the same thing.
        self._resumed_fingerprints: set[str] = set()
        #: fingerprint -> artifact id of the *full* payload. The memo holds the
        #: compact projection, but `tools/tool_gateway._dispatch` hands callers
        #: the full payload; without this the dedup path returned the projection
        #: instead, and BrowserAgent - which reads `parsed["content"]` - saw a
        #: dict with no content key and dropped every source.
        self._memo_artifacts: dict[str, str] = {}
        self._lock = threading.RLock()
        # Dedicated pool for timeout enforcement. Bounded so a storm of hung
        # handlers cannot spawn unbounded threads.
        self._timeout_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tool-timeout")

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        self._timeout_pool.shutdown(wait=False, cancel_futures=True)

    def fingerprint(self, tool_name: str, arguments: dict[str, Any]) -> str:
        return content_hash({"tool": tool_name, "args": arguments})

    def seed_completed_steps(self, state: RunState) -> dict[str, Any]:
        """Rehydrate the skip memo from a restored checkpoint.

        Without this, resume is not resume: the memo starts empty, so every
        already-completed idempotent tool call is issued again. Measured before
        the fix, a run killed after `browse` re-executed *more* tool calls on
        resume than the original run had made.

        Only side-effect-free, idempotent, SUCCEEDED steps whose payload is
        still in the artifact store can be rehydrated. Anything else is left to
        re-run, which is the safe direction. The compact projection is
        recomputed from the stored payload via the tool's own projector, so the
        replayed value is byte-identical to what the first run returned.
        """
        if self.artifacts is None:
            return {"seeded": 0, "reason": "no artifact store"}

        seeded, unrecoverable = 0, 0
        for step in state.steps:
            if step.kind != "tool" or step.status != StepStatus.SUCCEEDED:
                continue
            if not step.idempotency_key or not step.side_effect_free:
                continue
            if not step.artifact_id:
                unrecoverable += 1
                continue
            payload = self.artifacts.get(step.artifact_id)
            if payload is None:
                unrecoverable += 1
                continue
            try:
                spec = self.registry.spec(step.name)
                if not spec.idempotent:
                    continue
                registration = self.registry.get(step.name)
                compact = (registration.projector(payload)[0]
                           if registration.projector is not None else payload)
            except Exception:  # noqa: BLE001 - an unknown tool is simply re-run
                unrecoverable += 1
                continue
            self._dedup.remember(step.idempotency_key, compact)
            self._memo_artifacts[step.idempotency_key] = step.artifact_id
            self._resumed_fingerprints.add(step.idempotency_key)
            seeded += 1
        return {"seeded": seeded, "unrecoverable": unrecoverable}

    # ------------------------------------------------------------------ #
    def call(self, tool_name: str, arguments: Optional[dict[str, Any]] = None, *,
             rationale: str = "", phase: Optional[Phase] = None,
             task_id: str = "", allow_dedup: bool = True) -> ToolResult:
        """Execute one tool call end to end. Never raises for tool failures -
        every outcome comes back as a `ToolResult`."""
        arguments = dict(arguments or {})
        phase = phase or self.state.phase
        step = StepRecord(
            task_id=task_id, phase=phase, kind="tool", name=tool_name,
            input_summary=summarize(arguments), decision_rationale=rationale,
            status=StepStatus.RUNNING,
        )
        started = time.perf_counter()

        try:
            spec = self.registry.spec(tool_name)
        except ToolNotFoundError as exc:
            return self._finish_blocked(step, tool_name, exc.error_class, str(exc), started)

        step.side_effect_free = spec.side_effect_free

        # --- cancellation ------------------------------------------------
        if self.cancellation.cancelled:
            return self._finish_blocked(step, tool_name, ErrorClass.POLICY_BLOCKED,
                                        f"run cancelled: {self.cancellation.reason}", started)

        # --- argument validation ----------------------------------------
        normalized, errors = spec.validate_input(arguments)
        if errors:
            return self._finish_blocked(
                step, tool_name, ErrorClass.INVALID_ARGUMENT,
                "invalid arguments: " + "; ".join(errors[:5]), started,
                payload={"errors": errors[:10], "arguments": summarize(arguments)},
            )
        step.input_summary = summarize(normalized)

        # --- side-effect policy ------------------------------------------
        allowed, reason = self.side_effects.check(tool_name, spec.side_effect_free)
        if not allowed:
            self.trace.event(EventType.APPROVAL_REQUESTED, name=tool_name, step_id=step.step_id,
                             phase=phase.value, payload={"reason": reason,
                                                         "arguments": summarize(normalized)})
            return self._finish_blocked(step, tool_name, ErrorClass.POLICY_BLOCKED, reason, started)

        # --- budget pre-flight -------------------------------------------
        with self._lock:
            affordable, budget_reason = self.budget.can_call_tool(tool_name)
        if not affordable:
            self.trace.event(EventType.BUDGET_EXCEEDED, name=tool_name, step_id=step.step_id,
                             phase=phase.value, error_class=ErrorClass.BUDGET_EXCEEDED.value,
                             payload={"dimension": budget_reason.split()[0], "reason": budget_reason})
            return self._finish_blocked(step, tool_name, ErrorClass.BUDGET_EXCEEDED,
                                        budget_reason, started)

        # --- dedup --------------------------------------------------------
        fingerprint = self.fingerprint(tool_name, normalized)
        step.idempotency_key = fingerprint
        if allow_dedup and spec.idempotent and self._dedup.seen(fingerprint):
            memo = self._dedup.get(fingerprint)
            step.status = StepStatus.SKIPPED
            step.output_summary = summarize(memo)
            step.duration_s = round(time.perf_counter() - started, 4)
            step.ended_at = step.ended_at or step.started_at
            with self._lock:
                self.state.add_step(step)
            # A hit on a checkpoint-seeded fingerprint is work this run is
            # legitimately skipping, not waste the agent created. Reporting it
            # as a redundant call would make every resumed run look wasteful.
            resumed = fingerprint in self._resumed_fingerprints
            self.trace.event(
                EventType.STEP_SKIPPED_IDEMPOTENT if resumed else EventType.TOOL_CALL_DEDUPED,
                name=tool_name, step_id=step.step_id, phase=phase.value,
                status="skipped" if resumed else "deduped",
                payload={"fingerprint": fingerprint, "rationale": rationale,
                         "source": "checkpoint" if resumed else "in-run memo"})
            return ToolResult(tool_name=tool_name, ok=True, content=memo, deduped=True,
                              artifact_id=self._memo_artifacts.get(fingerprint, ""),
                              step_id=step.step_id, duration_s=step.duration_s)

        # --- circuit breaker ---------------------------------------------
        breaker = self.circuits.get(tool_name)
        permitted, circuit_reason = breaker.allow()
        if not permitted:
            if spec.fallback_tool and self.registry.has(spec.fallback_tool):
                self.trace.event(EventType.DEGRADED, name=tool_name, step_id=step.step_id,
                                 phase=phase.value,
                                 payload={"reason": circuit_reason, "fallback": spec.fallback_tool})
                return self.call(spec.fallback_tool, normalized,
                                 rationale=f"fallback for {tool_name}: {circuit_reason}",
                                 phase=phase, task_id=task_id)
            return self._finish_blocked(step, tool_name, ErrorClass.TRANSIENT_NETWORK,
                                        circuit_reason, started)

        # --- attempt loop -------------------------------------------------
        self.trace.event(EventType.TOOL_CALL_STARTED, name=tool_name, step_id=step.step_id,
                         phase=phase.value, task_id=task_id,
                         payload={"arguments": normalized, "rationale": rationale,
                                  "when_to_use": spec.when_to_use[:200]})

        entry = self.registry.get(tool_name)
        last_error_class = ErrorClass.UNKNOWN
        last_error_message = ""

        for attempt_no in range(1, spec.max_attempts + 1):
            if self.cancellation.cancelled:
                last_error_class = ErrorClass.POLICY_BLOCKED
                last_error_message = f"run cancelled: {self.cancellation.reason}"
                break

            attempt = AttemptRecord(attempt=attempt_no, tool_name=tool_name,
                                    tool_args=normalized)
            attempt_started = time.perf_counter()
            raw_result: Any = None
            error_class: Optional[ErrorClass] = None
            error_message = ""

            try:
                with self.limiter.acquire(tool_name, timeout=spec.timeout_s):
                    future = self._timeout_pool.submit(self._invoke, entry.handler, normalized)
                    try:
                        raw_result = future.result(timeout=spec.timeout_s)
                    except FutureTimeoutError:
                        future.cancel()
                        raise TimeoutError(
                            f"{tool_name} exceeded {spec.timeout_s}s; handler thread abandoned"
                        )
                envelope_error = _classify_envelope(raw_result)
                if envelope_error is not None:
                    error_class = envelope_error
                    error_message = str(
                        (raw_result or {}).get("error") or "tool reported success=false")
            except BaseException as exc:  # noqa: BLE001 - classify, never propagate
                error_class = classify_exception(exc)
                error_message = f"{type(exc).__name__}: {exc}"

            attempt.duration_s = round(time.perf_counter() - attempt_started, 4)
            with self._lock:
                self.budget.charge_tool(tool_name)
                self.budget.charge_time(attempt.duration_s, phase.value)

            if error_class is None:
                attempt.tool_status = "ok"
                attempt.close(StepStatus.SUCCEEDED)
                step.attempts.append(attempt)
                breaker.record_success()
                return self._finish_success(step, spec, raw_result, started, phase, task_id,
                                            fingerprint, allow_dedup)

            # failure path
            attempt.tool_status = "error"
            attempt.error_class = error_class.value
            attempt.error_message = error_message[:1000]
            attempt.retryable = is_retryable(error_class)
            attempt.close(StepStatus.FAILED)
            last_error_class, last_error_message = error_class, error_message

            if breaker.record_failure(error_class):
                self.trace.event(EventType.CIRCUIT_OPENED, name=tool_name, step_id=step.step_id,
                                 phase=phase.value, error_class=error_class.value,
                                 payload={"tool": tool_name,
                                          "threshold": breaker.failure_threshold,
                                          "cooldown_s": breaker.cooldown_s})

            will_retry = self.retry_policy.should_retry(error_class, attempt_no) and \
                attempt_no < spec.max_attempts
            attempt.will_retry = will_retry
            if will_retry:
                delay = self.retry_policy.backoff(attempt_no, error_class)
                attempt.backoff_s = delay
                step.attempts.append(attempt)
                self.trace.event(EventType.RETRY_SCHEDULED, name=tool_name, step_id=step.step_id,
                                 phase=phase.value, attempt=attempt_no,
                                 error_class=error_class.value,
                                 payload={"backoff_s": delay, "next_attempt": attempt_no + 1,
                                          "reason": error_message[:200]})
                self._sleep(delay)
                continue

            step.attempts.append(attempt)
            break

        return self._finish_failure(step, tool_name, last_error_class, last_error_message,
                                    started, phase, task_id)

    # ------------------------------------------------------------------ #
    @staticmethod
    def _invoke(handler: Callable[..., Any], arguments: dict[str, Any]) -> Any:
        return handler(**arguments)

    def _finish_success(self, step: StepRecord, spec: ToolSpec, raw_result: Any,
                        started: float, phase: Phase, task_id: str,
                        fingerprint: str, allow_dedup: bool) -> ToolResult:
        registration = self.registry.get(spec.name)
        if registration.projector is not None:
            compact, full = registration.projector(raw_result)
        else:
            compact, full = raw_result, raw_result

        schema_errors = spec.validate_output(compact)
        if schema_errors:
            return self._finish_failure(
                step, spec.name, ErrorClass.SCHEMA_ERROR,
                "output schema violation: " + "; ".join(schema_errors[:5]),
                started, phase, task_id)

        artifact_id = ""
        if self.artifacts is not None and full is not None:
            # Stored even when the projector passed the payload through
            # unchanged (`full is compact`). Resume rehydrates its skip memo
            # from these artifacts, so a tool whose result was never
            # externalized could not be skipped and would be re-called.
            artifact_id = self.artifacts.put(full, kind=f"tool:{spec.name}")

        step.status = StepStatus.SUCCEEDED
        step.output_summary = summarize(compact)
        step.artifact_id = artifact_id
        step.duration_s = round(time.perf_counter() - started, 4)
        step.ended_at = step.attempts[-1].ended_at if step.attempts else step.started_at
        with self._lock:
            self.state.add_step(step)
        if allow_dedup and spec.idempotent:
            self._dedup.remember(fingerprint, compact)
            if artifact_id:
                self._memo_artifacts[fingerprint] = artifact_id

        # External content that tries to address the model is recorded, not
        # silently passed on. The content is still used as evidence - it is
        # wrapped as untrusted data by the projector - but the attempt is now
        # visible in the trace and available to the memory write policy.
        if isinstance(compact, dict) and compact.get("injection_suspected"):
            self.trace.event(EventType.TOOL_CALL_BLOCKED, name=spec.name, step_id=step.step_id,
                             phase=phase.value, status="ok",
                             error_class=ErrorClass.POLICY_BLOCKED.value,
                             error_message="prompt injection patterns detected in fetched content",
                             payload={"url": compact.get("url", ""),
                                      "categories": compact.get("injection_categories", []),
                                      "action": "content wrapped as untrusted data; "
                                                "not eligible for procedural memory"})

        from_cache = bool(isinstance(raw_result, dict) and raw_result.get("from_cache"))
        self.trace.event(EventType.TOOL_CALL_COMPLETED, name=spec.name, step_id=step.step_id,
                         phase=phase.value, task_id=task_id, status="ok",
                         duration_s=step.duration_s, attempt=step.attempt_count,
                         artifact_id=artifact_id,
                         payload={"result_summary": step.output_summary, "from_cache": from_cache,
                                  "attempts": step.attempt_count, "recovered": step.recovered})
        self._emit_budget()
        return ToolResult(tool_name=spec.name, ok=True, content=compact, artifact_id=artifact_id,
                          attempts=step.attempt_count, duration_s=step.duration_s,
                          from_cache=from_cache, step_id=step.step_id)

    def _finish_failure(self, step: StepRecord, tool_name: str, error_class: ErrorClass,
                        message: str, started: float, phase: Phase, task_id: str) -> ToolResult:
        step.status = StepStatus.FAILED
        step.duration_s = round(time.perf_counter() - started, 4)
        step.output_summary = f"failed: {error_class.value}"
        step.ended_at = step.attempts[-1].ended_at if step.attempts else step.started_at
        with self._lock:
            self.state.add_step(step)
        self.trace.event(EventType.TOOL_CALL_FAILED, name=tool_name, step_id=step.step_id,
                         phase=phase.value, task_id=task_id, status="error",
                         duration_s=step.duration_s, attempt=step.attempt_count,
                         error_class=error_class.value, error_message=message[:1000],
                         payload={"attempts": step.attempt_count,
                                  "retryable": is_retryable(error_class)})
        self._emit_budget()
        return ToolResult(tool_name=tool_name, ok=False, error_class=error_class.value,
                          error_message=message[:1000], attempts=step.attempt_count,
                          duration_s=step.duration_s, step_id=step.step_id)

    def _finish_blocked(self, step: StepRecord, tool_name: str, error_class: ErrorClass,
                        reason: str, started: float,
                        payload: Optional[dict[str, Any]] = None) -> ToolResult:
        step.status = StepStatus.FAILED
        step.duration_s = round(time.perf_counter() - started, 4)
        step.output_summary = f"blocked: {error_class.value}"
        with self._lock:
            self.state.add_step(step)
        self.trace.event(EventType.TOOL_CALL_BLOCKED, name=tool_name, step_id=step.step_id,
                         phase=step.phase.value, status="blocked",
                         error_class=error_class.value, error_message=reason[:1000],
                         payload={**(payload or {}), "reason": reason[:500]})
        return ToolResult(tool_name=tool_name, ok=False, error_class=error_class.value,
                          error_message=reason[:1000], blocked_reason=reason[:500],
                          step_id=step.step_id, duration_s=step.duration_s)

    def _emit_budget(self) -> None:
        with self._lock:
            snapshot = self.budget.state.model_dump(mode="json")
            headroom = self.budget.headroom()
        self.trace.event(EventType.BUDGET_UPDATED, payload={**snapshot, "headroom": headroom})

    # ------------------------------------------------------------------ #
    # Model calls go through the same accounting so tokens/cost are recorded
    # ------------------------------------------------------------------ #
    def record_model_call(self, *, model: str, input_text: str = "", output_text: str = "",
                          input_tokens: Optional[int] = None, output_tokens: Optional[int] = None,
                          duration_s: float = 0.0, phase: Optional[Phase] = None,
                          task_id: str = "", rationale: str = "",
                          error: Optional[BaseException] = None) -> StepRecord:
        """Record one model call (token/cost accounting + trace event).

        Token counts come from the provider when available; otherwise they are
        estimated from text length and the event carries `tokens_estimated=True`
        so nobody mistakes an estimate for metered usage.
        """
        from src.runtime.budget import estimate_tokens

        phase = phase or self.state.phase
        estimated = input_tokens is None or output_tokens is None
        tokens_in = input_tokens if input_tokens is not None else estimate_tokens(input_text)
        tokens_out = output_tokens if output_tokens is not None else estimate_tokens(output_text)

        step = StepRecord(task_id=task_id, phase=phase, kind="model", name=model,
                          input_summary=summarize(input_text, 600),
                          output_summary=summarize(output_text, 600),
                          decision_rationale=rationale, duration_s=round(duration_s, 4))
        attempt = AttemptRecord(attempt=1, model=model, input_tokens=tokens_in,
                                output_tokens=tokens_out, duration_s=round(duration_s, 4))

        with self._lock:
            cost = self.budget.charge_model(model, tokens_in, tokens_out)
            self.budget.charge_time(duration_s, phase.value)
        attempt.estimated_cost_usd = cost

        if error is None:
            attempt.close(StepStatus.SUCCEEDED)
            step.status = StepStatus.SUCCEEDED
            step.attempts.append(attempt)
            with self._lock:
                self.state.add_step(step)
            self.trace.event(EventType.MODEL_CALL_COMPLETED, name=model, step_id=step.step_id,
                             phase=phase.value, task_id=task_id, status="ok",
                             duration_s=step.duration_s,
                             payload={"input_tokens": tokens_in, "output_tokens": tokens_out,
                                      "estimated_cost_usd": cost, "tokens_estimated": estimated,
                                      "output_summary": step.output_summary})
        else:
            error_class = classify_exception(error)
            attempt.error_class = error_class.value
            attempt.error_message = f"{type(error).__name__}: {error}"[:1000]
            attempt.close(StepStatus.FAILED)
            step.status = StepStatus.FAILED
            step.attempts.append(attempt)
            with self._lock:
                self.state.add_step(step)
            self.trace.event(EventType.MODEL_CALL_FAILED, name=model, step_id=step.step_id,
                             phase=phase.value, task_id=task_id, status="error",
                             duration_s=step.duration_s, error_class=error_class.value,
                             error_message=attempt.error_message,
                             payload={"tokens_estimated": estimated})
        self._emit_budget()
        return step

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict[str, Any]:
        return {
            "budget": self.budget.snapshot(),
            "circuits": self.circuits.snapshot(),
            "duplicates": self._dedup.snapshot(),
            "duplicate_calls": self._dedup.duplicate_count(),
            "registry": self.registry.stats(),
        }
