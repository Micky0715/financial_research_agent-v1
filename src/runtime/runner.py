"""HarnessRunner: wraps the existing pipeline with state, budget, checkpoints,
tracing and a resume path.

It does *not* reimplement the pipeline. `WorkflowOrchestrator.run()` still owns
planning, research, browsing, analysis and report generation. The runner:

- creates and persists a `RunState`;
- binds a `ToolExecutor` so every tool call through `tools/tool_gateway.py` is
  validated, retried, budgeted and traced;
- checkpoints at each phase boundary;
- gates each phase on budget and cancellation, applying the degradation ladder
  instead of running until something breaks;
- harvests the evidence ledger from the sources the pipeline selected;
- writes an explicit stop decision and a `FinalOutcome` that distinguishes
  succeeded / degraded / insufficient / failed - a failed run is never recorded
  as a success.

The `harness=` parameter on `WorkflowOrchestrator.run()` defaults to None, and
with it unset the legacy path is untouched.
"""
from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from src.context.manager import ContextManager, Layer
from src.memory.manager import MemoryManager
from src.observability.events import EventType, stop_decision_event
from src.observability.trace_store import ArtifactStore, TraceStore
from src.runtime.budget import BudgetLimits, BudgetTracker, DegradeAction
from src.runtime.checkpoint import (
    SqliteCheckpointStore,
    SqliteRunStore,
    open_stores,
    plan_resume,
)
from src.runtime.errors import RunCancelledError
from src.runtime.policies import (
    CancellationToken,
    CircuitRegistry,
    ConcurrencyLimiter,
    RetryPolicy,
    SideEffectPolicy,
)
from src.runtime.run_context import bind_run
from src.runtime.state import (
    EvidenceRecord,
    FinalOutcome,
    Phase,
    RunState,
    RunStatus,
    StepStatus,
    StopReason,
    TaskState,
    content_hash,
    summarize,
)
from src.tools.executor import ToolExecutor
from src.tools.registry import ToolRegistry, default_registry

#: Maps the orchestrator's stage labels onto harness phases.
_STAGE_TO_PHASE: dict[str, Phase] = {
    "planning": Phase.PLAN,
    "research": Phase.RESEARCH,
    "browse": Phase.BROWSE,
    "analyze": Phase.ANALYZE,
    "report": Phase.REPORT,
    "evaluate": Phase.EVALUATE,
    "revise": Phase.REVISE,
}


class HarnessConfig:
    """Everything that varies between a smoke run, a full eval and production."""

    def __init__(
        self,
        *,
        base_dir: Optional[Path] = None,
        budget: Optional[BudgetLimits] = None,
        retry: Optional[RetryPolicy] = None,
        max_concurrency: int = 8,
        checkpoint_enabled: bool = True,
        context_budget_tokens: int = 12000,
        namespace: str = "default",
        user_id: str = "local",
        memory_enabled: bool = False,
        memory_use_embeddings: bool = True,
        arm: str = "harness",
    ) -> None:
        from config import config as legacy_config

        self.base_dir = Path(base_dir or (legacy_config.OUTPUT_DIR / "harness"))
        self.budget = budget or BudgetLimits()
        self.retry = retry or RetryPolicy()
        self.max_concurrency = max_concurrency
        self.checkpoint_enabled = checkpoint_enabled
        self.context_budget_tokens = context_budget_tokens
        self.namespace = namespace
        self.user_id = user_id
        self.memory_enabled = memory_enabled
        self.memory_use_embeddings = memory_use_embeddings
        #: Experiment arm label, recorded on every run for A/B attribution.
        self.arm = arm

    def fingerprint(self) -> str:
        """Config hash recorded on every run, so a result can be traced back to
        the exact settings that produced it."""
        from config import config as legacy_config

        return content_hash({
            "arm": self.arm,
            "budget": self.budget.model_dump(mode="json"),
            "retry": self.retry.model_dump(mode="json"),
            "max_concurrency": self.max_concurrency,
            "context_budget_tokens": self.context_budget_tokens,
            "memory_enabled": self.memory_enabled,
            "memory_use_embeddings": self.memory_use_embeddings,
            "model": legacy_config.MODEL_NAME,
            "semantic_ranking": legacy_config.ENABLE_SEMANTIC_RANKING,
            "search_cache": legacy_config.ENABLE_SEARCH_CACHE,
            "use_mcp": legacy_config.USE_MCP_TOOLS,
            "max_browse_candidates": legacy_config.MAX_BROWSE_CANDIDATES,
            "top_k_sources": legacy_config.TOP_K_SOURCES,
        })


class WorkflowHooks:
    """Callbacks the orchestrator invokes when a harness is attached.

    Kept as a plain class (not a Protocol implementation detail) so the
    orchestrator's import stays cheap and optional.
    """

    def __init__(self, runner: "HarnessRunner") -> None:
        self.runner = runner

    def phase_start(self, stage: str, context: dict[str, Any]) -> None:
        self.runner.begin_phase(_STAGE_TO_PHASE.get(stage, Phase.INIT), stage)

    def phase_end(self, stage: str, context: dict[str, Any], success: bool,
                  error: Optional[str] = None) -> None:
        self.runner.end_phase(_STAGE_TO_PHASE.get(stage, Phase.INIT), stage, context,
                              success, error)

    def gate(self, stage: str) -> tuple[bool, str]:
        """Whether to run this stage at all. False means budget/cancel stop."""
        return self.runner.gate_phase(_STAGE_TO_PHASE.get(stage, Phase.INIT), stage)

    def plan_created(self, task_types: list[str], planner_metrics: dict[str, Any]) -> None:
        trace = self.runner.trace
        if trace is None:
            return
        trace.event(EventType.PLAN_CREATED, phase=Phase.PLAN.value,
                    payload={"tasks": task_types, "planner_metrics": planner_metrics,
                             "note": "plan is normalized to the fixed 5-stage pipeline by "
                                     "agents/planning_agent.normalize_plan; the LLM plan does not "
                                     "control the execution path"})


class HarnessRunner:
    """One instance per run."""

    def __init__(self, config: Optional[HarnessConfig] = None,
                 registry: Optional[ToolRegistry] = None,
                 run_store: Optional[SqliteRunStore] = None,
                 checkpoint_store: Optional[SqliteCheckpointStore] = None,
                 artifacts: Optional[ArtifactStore] = None) -> None:
        self.config = config or HarnessConfig()
        self.config.base_dir.mkdir(parents=True, exist_ok=True)

        if run_store is None or checkpoint_store is None or artifacts is None:
            built_runs, built_ckpt, built_artifacts = open_stores(self.config.base_dir)
            run_store = run_store or built_runs
            checkpoint_store = checkpoint_store or built_ckpt
            artifacts = artifacts or built_artifacts
        self.run_store = run_store
        self.checkpoint_store = checkpoint_store
        self.artifacts = artifacts
        self.registry = registry or default_registry()

        self.state: Optional[RunState] = None
        self.trace: Optional[TraceStore] = None
        self.executor: Optional[ToolExecutor] = None
        self.budget: Optional[BudgetTracker] = None
        self.memory_manager: Optional[MemoryManager] = None
        self._memory_items: list[dict[str, Any]] = []
        self._memory_summary: dict[str, Any] = {"enabled": False, "written": 0}
        self.cancellation = CancellationToken()
        self.circuits = CircuitRegistry()
        self._phase_started_at: dict[str, float] = {}
        self._degradations: list[str] = []
        self._run_started_at = 0.0
        self._stop_reason: Optional[StopReason] = None
        self._stop_detail = ""

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def _trace_path(self, run_id: str) -> Path:
        directory = self.config.base_dir / "traces"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{run_id}.jsonl"

    def prepare(self, request: Any, *, resume_from: Optional[str] = None) -> RunState:
        """Create (or restore) the run state and open all runtime services."""
        if resume_from:
            restored = self.checkpoint_store.load_latest(resume_from)
            if restored is None:
                raise ValueError(f"no checkpoint found for run {resume_from!r}")
            plan = plan_resume(restored)
            state = restored
            state.parent_run_id = resume_from
            state.status = RunStatus.RUNNING
        else:
            plan = None
            state = RunState(
                topic=getattr(request, "topic", ""),
                report_type=getattr(request, "report_type", ""),
                requirements=list(getattr(request, "requirements", []) or []),
                namespace=self.config.namespace,
                user_id=self.config.user_id,
                config_hash=self.config.fingerprint(),
            )

        self.state = state
        self.trace = TraceStore(self._trace_path(state.run_id), artifacts=self.artifacts,
                                run_id=state.run_id)
        self.budget = BudgetTracker(self.config.budget, state.budget)
        self.executor = ToolExecutor(
            run_state=state, trace=self.trace, registry=self.registry, budget=self.budget,
            artifacts=self.artifacts, retry_policy=self.config.retry, circuits=self.circuits,
            limiter=ConcurrencyLimiter(max_global=self.config.max_concurrency),
            side_effects=SideEffectPolicy(), cancellation=self.cancellation,
        )

        self.trace.event(EventType.RUN_STARTED, phase=state.phase.value, payload={
            "topic": state.topic, "report_type": state.report_type,
            "requirements": state.requirements, "arm": self.config.arm,
            "config_hash": state.config_hash, "namespace": state.namespace,
            "budget_limits": self.config.budget.model_dump(mode="json"),
            "resumed_from": resume_from or "",
            "tool_catalog": [t["name"] for t in self.registry.catalog()],
        })
        if self.config.memory_enabled:
            self.memory_manager = MemoryManager(
                namespace=self.config.namespace,
                db_path=self.config.base_dir / "memory.db",
                enabled=True,
                use_embeddings=self.config.memory_use_embeddings,
                trace=self.trace,
            )
            self._memory_items = self.memory_manager.retrieve_for_run(
                query=state.topic,
                subject=state.topic,
            )
        if plan is not None:
            # Applying the plan, not just reporting it: without this the memo
            # starts empty and every completed step is re-executed.
            seeded = self.executor.seed_completed_steps(state)
            self.trace.event(EventType.CHECKPOINT_RESTORED, phase=state.phase.value,
                             payload={**plan.describe(), **seeded})
        self.run_store.upsert(state)
        return state

    # ------------------------------------------------------------------ #
    # Phase gating / checkpointing
    # ------------------------------------------------------------------ #
    def begin_phase(self, phase: Phase, stage: str) -> None:
        assert self.state is not None and self.trace is not None
        self.state.set_phase(phase)
        self._phase_started_at[stage] = time.perf_counter()
        self.trace.event(EventType.PHASE_STARTED, phase=phase.value, name=stage,
                         payload={"budget_headroom": self.budget.headroom() if self.budget else 1.0})

    def end_phase(self, phase: Phase, stage: str, context: dict[str, Any],
                  success: bool, error: Optional[str] = None) -> None:
        assert self.state is not None and self.trace is not None and self.budget is not None
        elapsed = round(time.perf_counter() - self._phase_started_at.get(stage, time.perf_counter()), 4)
        self.budget.set_elapsed(time.perf_counter() - self._run_started_at)

        self.state.tasks.append(TaskState(
            task_id=stage, task_type=stage, error=error or "",
            status=StepStatus.SUCCEEDED if success else StepStatus.FAILED,
        ))

        if stage == "browse" and success:
            self.harvest_evidence(context)
            plan_steps = [
                getattr(task, "task_type", str(task))
                for task in (context.get("tasks") or [])
            ]
            rendered, diagnostics = self.build_context(
                plan_steps=plan_steps,
                memory_items=self._memory_items,
            )
            context["harness_context"] = rendered
            context["harness_context_diagnostics"] = diagnostics

        self.trace.event(EventType.PHASE_COMPLETED, phase=phase.value, name=stage,
                         status="ok" if success else "error", duration_s=elapsed,
                         error_message=(error or "")[:500],
                         payload={"success": success,
                                  "budget": self.budget.state.model_dump(mode="json")})
        self.checkpoint(f"after_{stage}")

    def gate_phase(self, phase: Phase, stage: str) -> tuple[bool, str]:
        """Budget/cancellation gate applied before a stage runs."""
        assert self.budget is not None and self.trace is not None
        if self.cancellation.cancelled:
            self._stop_reason = StopReason.CANCELLED
            self._stop_detail = self.cancellation.reason
            return False, self.cancellation.reason

        has_evidence = bool(self.state and self.state.evidence)
        decision = self.budget.recommend(has_evidence=has_evidence)
        if decision.action in (DegradeAction.CONTINUE, DegradeAction.NARROW_SEARCH,
                               DegradeAction.REDUCE_PARALLELISM, DegradeAction.DOWNGRADE_MODEL):
            if decision.action != DegradeAction.CONTINUE:
                self.note_degradation(decision.action.value, decision.reason)
            return True, ""

        if decision.action == DegradeAction.SKIP_OPTIONAL and stage in ("analyze", "report", "evaluate"):
            # Optional enrichment is skipped inside the agents; the core stages
            # still run, otherwise there is no report at all.
            self.note_degradation(decision.action.value, decision.reason)
            return True, ""

        if stage in ("research", "browse") and decision.action in (
                DegradeAction.FINALIZE_WITH_CURRENT, DegradeAction.SKIP_OPTIONAL):
            self.note_degradation(decision.action.value, decision.reason)
            self._stop_reason = StopReason.BUDGET_EXHAUSTED
            self._stop_detail = decision.reason
            return False, decision.reason

        if decision.action in (DegradeAction.RETURN_INSUFFICIENT, DegradeAction.ABORT):
            self._stop_reason = StopReason.BUDGET_EXHAUSTED
            self._stop_detail = decision.reason
            self.trace.event(EventType.BUDGET_EXCEEDED, phase=phase.value, name=stage,
                             payload={"dimension": decision.dimension, "reason": decision.reason,
                                      "utilization": decision.utilization})
            return False, decision.reason
        return True, ""

    def note_degradation(self, kind: str, reason: str) -> None:
        assert self.trace is not None
        label = f"{kind}: {reason}"
        if label in self._degradations:
            return
        self._degradations.append(label)
        self.trace.event(EventType.DEGRADED, name=kind, payload={"reason": reason})

    def checkpoint(self, label: str) -> int:
        assert self.state is not None and self.trace is not None
        if not self.config.checkpoint_enabled:
            return self.state.checkpoint_version
        version = self.checkpoint_store.save(self.state)
        self.run_store.upsert(self.state)
        self.trace.event(EventType.CHECKPOINT_SAVED, phase=self.state.phase.value, name=label,
                         payload={"version": version, "evidence": len(self.state.evidence),
                                  "steps": len(self.state.steps)})
        return version

    # ------------------------------------------------------------------ #
    # Evidence
    # ------------------------------------------------------------------ #
    def harvest_evidence(self, context: dict[str, Any]) -> int:
        """Turn the pipeline's selected sources into ledger entries.

        One entry per source (title + bounded excerpt), which is what report
        citations resolve against. Numeric evidence is added later by the
        grounding grader, which knows which numbers actually appear.
        """
        assert self.state is not None and self.trace is not None
        sources = (context.get("sources") or {}).get("sources", [])
        added = 0
        for source in sources:
            if not isinstance(source, dict):
                continue
            record = EvidenceRecord(
                claim=f"来源可用于支撑「{self.state.topic}」相关论述",
                source_id=source.get("source_id", ""),
                url=source.get("url", ""),
                title=(source.get("title") or "")[:200],
                origin=source.get("source_type", "web"),
                authority_tier=source.get("authority_tier", ""),
                excerpt=(source.get("content") or "")[:400],
                confidence=float(source.get("score", 0.0) or 0.0),
            )
            if self.state.add_evidence(record):
                added += 1
        if added:
            self.trace.event(EventType.EVIDENCE_ADDED, phase=self.state.phase.value,
                             payload={"added": added, "total": len(self.state.evidence)})
        return added

    def build_context(self, *, plan_steps: Optional[list[str]] = None,
                      open_questions: Optional[list[str]] = None,
                      memory_items: Optional[list[dict[str, Any]]] = None) -> tuple[str, dict[str, Any]]:
        """Assemble layered context and record what compression did."""
        assert self.state is not None and self.trace is not None and self.budget is not None
        manager = ContextManager(budget_tokens=self.config.context_budget_tokens)
        manager.add_goal(self.state.topic, self.state.report_type, self.state.requirements)
        if plan_steps:
            manager.add_plan(plan_steps, current=self.state.phase.value)
        manager.add_stage_state(self.state.phase.value, self.budget.headroom(), self._degradations)
        manager.add_open_questions(open_questions or [])
        manager.add_evidence(self.state.evidence)
        for step in self.state.tool_steps()[-8:]:
            manager.add_tool_result(step.name, step.output_summary, artifact_id=step.artifact_id)
        if memory_items:
            manager.add_memory(memory_items)

        rendered, report = manager.assemble()
        self.trace.event(EventType.CONTEXT_COMPRESSED, phase=self.state.phase.value,
                         payload=report.model_dump(mode="json"))
        return rendered, report.model_dump(mode="json")

    # ------------------------------------------------------------------ #
    # Main entry point
    # ------------------------------------------------------------------ #
    def run(self, request: Any, *, orchestrator: Any = None,
            resume_from: Optional[str] = None,
            on_progress: Optional[Callable[[int, int, str], None]] = None) -> dict[str, Any]:
        """Execute one full pipeline run under the harness."""
        from orchestrator.workflow import WorkflowOrchestrator

        state = self.prepare(request, resume_from=resume_from)
        orchestrator = orchestrator or WorkflowOrchestrator()
        hooks = WorkflowHooks(self)
        self._run_started_at = time.perf_counter()
        result: dict[str, Any] = {}
        error_text = ""
        original_requirements = list(getattr(request, "requirements", []) or [])

        if self.memory_manager is not None:
            memory_hints = [
                f"Memory hint ({item.get('kind', 'unknown')}): {item.get('content', '')}"
                for item in self._memory_items
                if item.get("content")
            ]
            memory_hints.extend(
                f"Approved procedural rule: {rule}"
                for rule in self.memory_manager.active_rules_text()
            )
            if memory_hints:
                request.requirements = original_requirements + memory_hints

        try:
            with bind_run(self.executor):  # type: ignore[arg-type]
                result = orchestrator.run(request, on_progress=on_progress, harness=hooks)
        except RunCancelledError as exc:
            error_text = str(exc)
            self._stop_reason = StopReason.CANCELLED
            self._stop_detail = error_text
        except Exception as exc:  # noqa: BLE001 - a crash must still produce a truthful record
            error_text = f"{type(exc).__name__}: {exc}"
            self._stop_reason = StopReason.FATAL_ERROR
            self._stop_detail = error_text
            assert self.trace is not None
            self.trace.event(EventType.RUN_FAILED, phase=state.phase.value,
                             error_message=error_text[:1000],
                             payload={"traceback": summarize(traceback.format_exc(), 1500)})
        finally:
            request.requirements = original_requirements
            self.budget.set_elapsed(time.perf_counter() - self._run_started_at)  # type: ignore[union-attr]

        outcome = self._build_outcome(result, error_text)
        state.finish(outcome)
        self.checkpoint("final")

        assert self.trace is not None
        self.trace.emit(stop_decision_event(
            state.run_id, outcome.stop_reason.value, self._stop_detail or outcome.summary,
            evidence_count=len(state.evidence),
            rounds_used=state.budget.supplementary_rounds,
            budget_headroom=self.budget.headroom() if self.budget else None))
        if self.memory_manager is not None:
            self._memory_summary = self.memory_manager.learn_from_run(
                run_id=state.run_id,
                subject=state.topic,
                events=TraceStore.read(self._trace_path(state.run_id)),
                succeeded=outcome.is_success,
                entity_validation=(result.get("entity_validation")
                                   or (result.get("evaluation") or {}).get("entity_validation")),
                user_statements=original_requirements,
            )
        self.trace.event(
            EventType.RUN_COMPLETED if outcome.is_success else EventType.RUN_FAILED,
            phase=Phase.DONE.value, status="ok" if outcome.is_success else "error",
            duration_s=round(time.perf_counter() - self._run_started_at, 4),
            payload={"is_success": outcome.is_success, "status": outcome.status.value,
                     "stop_reason": outcome.stop_reason.value,
                     "duration_s": round(time.perf_counter() - self._run_started_at, 4),
                     "num_sources": outcome.num_sources, "quality_score": outcome.quality_score,
                     "degradations": outcome.degradations,
                     "budget": self.budget.snapshot() if self.budget else {},
                     "executor": self.executor.snapshot() if self.executor else {}})
        self.trace.close()
        self.run_store.upsert(state)
        if self.executor:
            self.executor.close()

        return {
            **result,
            "harness_run_id": state.run_id,
            "harness_trace_path": str(self._trace_path(state.run_id)),
            "harness_status": outcome.status.value,
            "harness_stop_reason": outcome.stop_reason.value,
            "harness_budget": self.budget.snapshot() if self.budget else {},
            "harness_degradations": outcome.degradations,
            "harness_evidence_count": len(state.evidence),
            "harness_memory": self._memory_summary,
        }

    def _build_outcome(self, result: dict[str, Any], error_text: str) -> FinalOutcome:
        """Derive the terminal verdict from what actually happened.

        The ordering matters: a fatal error outranks a written report, and a
        report written with zero sources is INSUFFICIENT, never SUCCEEDED.
        """
        assert self.state is not None
        evaluation = result.get("evaluation") or {}
        num_sources = int(result.get("num_sources", 0) or 0)
        quality = evaluation.get("overall_score")
        trace_path = str(self._trace_path(self.state.run_id))

        runtime_degradations = list(self._degradations)
        # A failed optional page among several usable pages is normal retrieval
        # noise and must not downgrade an otherwise complete report.  Failures
        # of the explicitly requested structured/PDF evidence paths do change
        # the delivered evidence set and therefore produce a truthful
        # ``degraded`` outcome.
        failed_tools = sorted({
            step.name for step in self.state.tool_steps()
            if step.status == StepStatus.FAILED
            and step.name in {"fetch_financial_snapshot", "read_pdf"}
        })
        runtime_degradations.extend(
            f"tool_failed: {name}" for name in failed_tools
            if f"tool_failed: {name}" not in runtime_degradations
        )
        open_circuits = self.circuits.open_keys()
        if open_circuits:
            runtime_degradations.append(f"circuit_open: {open_circuits}")

        outcome = FinalOutcome(
            report_path=str(result.get("report_path", "")),
            evaluation_path=str(result.get("evaluation_path", "")),
            sources_path=str(result.get("sources_path", "")),
            trace_path=trace_path,
            num_sources=num_sources,
            quality_score=float(quality) if isinstance(quality, (int, float)) else None,
            degradations=runtime_degradations,
        )

        if error_text and self._stop_reason == StopReason.CANCELLED:
            outcome.status = RunStatus.CANCELLED
            outcome.stop_reason = StopReason.CANCELLED
            outcome.summary = f"run cancelled: {error_text}"
            outcome.errors.append({"error": error_text})
        elif error_text:
            outcome.status = RunStatus.FAILED
            outcome.stop_reason = StopReason.FATAL_ERROR
            outcome.summary = f"run failed: {error_text}"
            outcome.errors.append({"error": error_text})
        elif evaluation.get("entity_validation_failed"):
            outcome.status = RunStatus.INSUFFICIENT
            outcome.stop_reason = StopReason.ENTITY_UNVERIFIED
            outcome.summary = "研究主体未通过实体真实性校验，已在分析阶段前终止"
        elif num_sources == 0:
            outcome.status = RunStatus.INSUFFICIENT
            outcome.stop_reason = (self._stop_reason or StopReason.NO_USABLE_SOURCES)
            outcome.summary = "没有可用来源，未生成正式报告"
        elif self._stop_reason == StopReason.BUDGET_EXHAUSTED:
            outcome.status = RunStatus.DEGRADED
            outcome.stop_reason = StopReason.BUDGET_EXHAUSTED
            outcome.summary = f"预算耗尽，用已有证据收口：{self._stop_detail}"
        elif outcome.degradations:
            outcome.status = RunStatus.DEGRADED
            outcome.stop_reason = StopReason.ENOUGH_EVIDENCE
            outcome.summary = f"报告已生成，但触发了 {len(outcome.degradations)} 项降级"
        else:
            outcome.status = RunStatus.SUCCEEDED
            outcome.stop_reason = StopReason.COMPLETED
            outcome.summary = f"报告已生成，{num_sources} 个来源"

        return outcome

    def cancel(self, reason: str = "cancelled by operator") -> None:
        self.cancellation.cancel(reason)
        if self.trace:
            self.trace.event(EventType.RUN_CANCELLED, payload={"reason": reason})
