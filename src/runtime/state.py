"""Serializable run state for the agent harness.

Everything the harness needs to answer "what happened, and where do I resume?"
lives in `RunState`. It is a Pydantic model so it round-trips to JSON without
custom encoders, and it carries `schema_version` so a checkpoint written by an
older build is *rejected* rather than silently misread.

Design constraints that shaped this file:

- **No raw content.** Page text, prompts and model completions never enter the
  state. Only bounded summaries plus a pointer into the ArtifactStore. A run
  state that embedded source bodies would be megabytes per checkpoint (the
  legacy TraceLog has exactly that problem).
- **No hidden reasoning.** `input_summary` / `output_summary` are truncated
  I/O digests. Chain-of-thought is never captured, by construction.
- **Idempotency is a first-class field.** Resume decides what to skip using
  `StepRecord.idempotency_key` + status, not by re-deriving intent.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

#: Bump when a field is removed or its meaning changes. Checkpoints whose
#: major version differs are refused by CheckpointStore.load().
SCHEMA_VERSION = "1.0"

#: Hard cap on any summary string entering the state/trace. Keeps checkpoints
#: small and makes accidental content capture impossible rather than unlikely.
MAX_SUMMARY_CHARS = 2000


def utc_now() -> str:
    """ISO-8601 UTC timestamp with second precision (stable across platforms)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:12]
    return f"{prefix}{raw}" if prefix else raw


def summarize(value: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    """Render any value as a bounded, single-line-ish summary string.

    Used for every `*_summary` field. Dicts/lists are JSON-encoded so the shape
    stays inspectable; long values are truncated with an explicit marker so a
    reader never mistakes a truncated summary for the whole thing.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def content_hash(value: Any) -> str:
    """Stable short hash of a value, for dedup and idempotency keys."""
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = str(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Phase(str, Enum):
    """Pipeline phase. Mirrors the legacy 5 stages plus harness-level phases."""

    INIT = "init"
    PLAN = "plan"
    RESEARCH = "research"
    BROWSE = "browse"
    EVIDENCE = "evidence"
    ANALYZE = "analyze"
    REPORT = "report"
    EVALUATE = "evaluate"
    REVISE = "revise"
    DONE = "done"


#: Phase order used by resume to decide what is already behind us.
PHASE_ORDER: tuple[Phase, ...] = (
    Phase.INIT, Phase.PLAN, Phase.RESEARCH, Phase.BROWSE, Phase.EVIDENCE,
    Phase.ANALYZE, Phase.REPORT, Phase.EVALUATE, Phase.REVISE, Phase.DONE,
)


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    #: Produced output, but with a declared degradation (fallback path taken,
    #: budget forced an early stop, a data source was unavailable).
    DEGRADED = "degraded"
    FAILED = "failed"
    #: Deliberately stopped short with insufficient evidence. NOT a success.
    INSUFFICIENT = "insufficient"
    CANCELLED = "cancelled"


class StopReason(str, Enum):
    """Why the run stopped here instead of continuing to search.

    This enum is the machine-readable answer to audit question #4 ("为什么在
    这里停止"). Every terminal run carries exactly one.
    """

    COMPLETED = "completed"
    ENOUGH_EVIDENCE = "enough_evidence"
    DIMINISHING_RETURNS = "diminishing_returns"
    MAX_ROUNDS_REACHED = "max_rounds_reached"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_USABLE_SOURCES = "no_usable_sources"
    ENTITY_UNVERIFIED = "entity_unverified"
    TOOL_CIRCUIT_OPEN = "tool_circuit_open"
    CANCELLED = "cancelled"
    FATAL_ERROR = "fatal_error"


# --------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------- #
class AttemptRecord(BaseModel):
    """One physical attempt at a step. A retried step has several of these."""

    attempt: int = Field(..., ge=1)
    started_at: str = Field(default_factory=utc_now)
    ended_at: str = ""
    duration_s: float = 0.0
    status: StepStatus = StepStatus.RUNNING

    # Model call accounting (empty for pure-tool steps)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0

    # Tool call accounting (empty for pure-model steps)
    tool_name: str = ""
    tool_args: dict[str, Any] = Field(default_factory=dict)
    tool_status: str = ""          # "ok" | "error" | "blocked" | "cached"
    from_cache: bool = False

    error_class: str = ""
    error_message: str = ""
    retryable: bool = False
    will_retry: bool = False
    backoff_s: float = 0.0

    def close(self, status: StepStatus) -> "AttemptRecord":
        self.status = status
        self.ended_at = utc_now()
        return self


class StepRecord(BaseModel):
    """One logical step: a tool call, a model call, or a pipeline stage.

    `idempotency_key` is what resume keys off. Two steps with the same key are
    the same work: if one already SUCCEEDED, the other is skipped and its
    recorded output is reused.
    """

    step_id: str = Field(default_factory=lambda: new_id("st_"))
    task_id: str = ""
    phase: Phase = Phase.INIT
    kind: Literal["tool", "model", "stage", "grader", "memory"] = "stage"
    name: str = ""

    status: StepStatus = StepStatus.PENDING
    attempts: list[AttemptRecord] = Field(default_factory=list)

    input_summary: str = ""
    output_summary: str = ""
    #: Where the full payload lives, if it was externalized (ArtifactStore id).
    artifact_id: str = ""

    idempotency_key: str = ""
    #: True when this step has no external side effects, so resume may re-run
    #: it safely if its outcome is unknown.
    side_effect_free: bool = True

    started_at: str = Field(default_factory=utc_now)
    ended_at: str = ""
    duration_s: float = 0.0

    #: Why the harness chose this tool / took this branch. Free text, written
    #: by the caller at the decision point - this is what makes audit question
    #: #1 ("Agent 为什么选择这个工具") answerable after the fact.
    decision_rationale: str = ""

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def last_error_class(self) -> str:
        for attempt in reversed(self.attempts):
            if attempt.error_class:
                return attempt.error_class
        return ""

    @property
    def recovered(self) -> bool:
        """Succeeded after at least one failed attempt (feeds Recovery Rate)."""
        return self.status == StepStatus.SUCCEEDED and any(
            a.status == StepStatus.FAILED for a in self.attempts
        )

    def total_tokens(self) -> tuple[int, int]:
        return (
            sum(a.input_tokens for a in self.attempts),
            sum(a.output_tokens for a in self.attempts),
        )

    def total_cost(self) -> float:
        return round(sum(a.estimated_cost_usd for a in self.attempts), 6)


class EvidenceRecord(BaseModel):
    """One piece of grounded evidence in the ledger.

    The ledger is what the report is allowed to cite. Keeping it separate from
    the raw sources means context compression can drop page bodies while every
    number keeps a resolvable provenance pointer (audit question #7).
    """

    evidence_id: str = Field(default_factory=lambda: new_id("ev_"))
    claim: str = ""                  # the statement this evidence supports
    value: str = ""                  # the specific number/fact, if any
    source_id: str = ""              # legacy Source.source_id, e.g. "s3"
    url: str = ""
    title: str = ""
    #: "web" | "pdf" | "akshare" | "local_file" | "computed" | "assumption"
    origin: str = "web"
    authority_tier: str = ""
    excerpt: str = ""                # bounded quote supporting the claim
    confidence: float = 0.0
    collected_at: str = Field(default_factory=utc_now)
    #: Set for origin="computed"/"assumption": which tool or config produced it.
    derived_from: str = ""

    def dedup_key(self) -> str:
        return content_hash([self.url or self.derived_from, self.value, self.claim[:120]])


class BudgetState(BaseModel):
    """Live budget counters. Limits live in `BudgetLimits` (budget.py)."""

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    model_calls: int = 0
    tool_calls: int = 0
    tool_calls_by_name: dict[str, int] = Field(default_factory=dict)
    supplementary_rounds: int = 0
    #: Set the first time a limit is hit; the run degrades rather than dies.
    exceeded: list[str] = Field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.tokens_in + self.tokens_out


class FinalOutcome(BaseModel):
    """The terminal verdict of a run. A failed run must never be recorded as
    succeeded - `status` and `stop_reason` are written from the actual
    execution result, never from an optimistic default."""

    status: RunStatus = RunStatus.RUNNING
    stop_reason: StopReason = StopReason.COMPLETED
    summary: str = ""
    report_path: str = ""
    evaluation_path: str = ""
    trace_path: str = ""
    sources_path: str = ""
    num_sources: int = 0
    quality_score: Optional[float] = None
    degradations: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return self.status in (RunStatus.SUCCEEDED, RunStatus.DEGRADED)


class TaskState(BaseModel):
    """A sub-task inside a run (one research dimension, one report section)."""

    task_id: str
    task_type: str = ""
    description: str = ""
    status: StepStatus = StepStatus.PENDING
    dependencies: list[str] = Field(default_factory=list)
    step_ids: list[str] = Field(default_factory=list)
    result_summary: str = ""
    error: str = ""
    started_at: str = ""
    ended_at: str = ""


class RunState(BaseModel):
    """Complete, serializable state of one harness run."""

    schema_version: str = SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: new_id())
    parent_run_id: str = ""          # set when resumed from another run

    # Request identity
    topic: str = ""
    report_type: str = ""
    requirements: list[str] = Field(default_factory=list)
    namespace: str = "default"       # memory/tenant isolation key
    user_id: str = "local"
    config_hash: str = ""            # hash of the effective config for this run

    # Progress
    phase: Phase = Phase.INIT
    status: RunStatus = RunStatus.RUNNING
    checkpoint_version: int = 0

    started_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    ended_at: str = ""

    tasks: list[TaskState] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    budget: BudgetState = Field(default_factory=BudgetState)
    outcome: FinalOutcome = Field(default_factory=FinalOutcome)

    #: Small, JSON-safe carry-over between phases (never raw content).
    scratch: dict[str, Any] = Field(default_factory=dict)
    #: Memory entries injected into this run, for A/B attribution.
    memory_injected: list[dict[str, Any]] = Field(default_factory=list)

    # ----------------------------------------------------------------- #
    # Mutation helpers (all bump updated_at so checkpoints stay ordered)
    # ----------------------------------------------------------------- #
    def touch(self) -> None:
        self.updated_at = utc_now()

    def add_step(self, step: StepRecord) -> StepRecord:
        self.steps.append(step)
        self.touch()
        return step

    def find_step(self, idempotency_key: str) -> Optional[StepRecord]:
        """Most recent step with this key, for resume/dedup decisions."""
        if not idempotency_key:
            return None
        for step in reversed(self.steps):
            if step.idempotency_key == idempotency_key:
                return step
        return None

    def completed_step(self, idempotency_key: str) -> Optional[StepRecord]:
        step = self.find_step(idempotency_key)
        return step if step and step.status == StepStatus.SUCCEEDED else None

    def add_evidence(self, record: EvidenceRecord) -> bool:
        """Append unless an equivalent record is already in the ledger.

        Returns True if it was added. Dedup is by (url|derived_from, value,
        claim-prefix) so the same figure cited twice from one page collapses.
        """
        key = record.dedup_key()
        if any(e.dedup_key() == key for e in self.evidence):
            return False
        self.evidence.append(record)
        self.touch()
        return True

    def set_phase(self, phase: Phase) -> None:
        self.phase = phase
        self.touch()

    def finish(self, outcome: FinalOutcome) -> None:
        self.outcome = outcome
        self.status = outcome.status
        self.phase = Phase.DONE
        self.ended_at = utc_now()
        self.touch()

    # ----------------------------------------------------------------- #
    # Derived views
    # ----------------------------------------------------------------- #
    def tool_steps(self) -> list[StepRecord]:
        return [s for s in self.steps if s.kind == "tool"]

    def model_steps(self) -> list[StepRecord]:
        return [s for s in self.steps if s.kind == "model"]

    def redundant_tool_calls(self) -> int:
        """Successful tool steps whose (name, args) repeated an earlier one.

        Counts *waste*, not caching: a cache hit that avoided the network still
        represents a call the agent should not have asked for twice.
        """
        seen: set[str] = set()
        redundant = 0
        for step in self.tool_steps():
            if step.status != StepStatus.SUCCEEDED or not step.idempotency_key:
                continue
            if step.idempotency_key in seen:
                redundant += 1
            else:
                seen.add(step.idempotency_key)
        return redundant

    def recovery_stats(self) -> tuple[int, int]:
        """(steps that failed at least once, of those how many still succeeded)."""
        had_failure = [s for s in self.steps if any(a.status == StepStatus.FAILED for a in s.attempts)]
        recovered = [s for s in had_failure if s.status == StepStatus.SUCCEEDED]
        return len(had_failure), len(recovered)

    def to_checkpoint(self) -> dict[str, Any]:
        self.checkpoint_version += 1
        self.touch()
        return self.model_dump(mode="json")

    @classmethod
    def from_checkpoint(cls, payload: dict[str, Any]) -> "RunState":
        """Rebuild from a checkpoint payload, refusing incompatible schemas."""
        version = str(payload.get("schema_version", ""))
        if version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
            raise ValueError(
                f"checkpoint schema_version={version!r} is incompatible with "
                f"runtime SCHEMA_VERSION={SCHEMA_VERSION!r}; refusing to resume"
            )
        return cls.model_validate(payload)
