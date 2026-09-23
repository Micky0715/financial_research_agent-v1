"""Trace event schema and redaction.

One append-only JSONL event stream per run. Events are the ground truth the
whole rest of the system reads: metrics, failure mining, regression gates and
the SFT export all derive from this file and nothing else.

Two hard rules, enforced here rather than left to callers:

1. **Secrets never reach the stream.** `redact()` runs on every payload before
   it is written - not on a best-effort basis at call sites. Keys are matched
   by name (api_key, authorization, cookie, token, ...) and values are matched
   by shape (sk-..., Bearer ..., long hex/base64 blobs).
2. **Hidden reasoning is never captured.** There is no `reasoning` /
   `thinking` / `chain_of_thought` field, and any such key in a payload is
   dropped by `redact()` alongside secrets. Only visible I/O summaries,
   tool calls, state transitions, errors and scores are recorded.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.runtime.state import MAX_SUMMARY_CHARS, new_id, summarize, utc_now

TRACE_SCHEMA_VERSION = "1.0"


class EventType(str, Enum):
    """Every event the harness can emit. Values are persisted - keep stable."""

    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"

    PLAN_CREATED = "plan_created"
    PHASE_STARTED = "phase_started"
    PHASE_COMPLETED = "phase_completed"

    MODEL_CALL_STARTED = "model_call_started"
    MODEL_CALL_COMPLETED = "model_call_completed"
    MODEL_CALL_FAILED = "model_call_failed"

    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_FAILED = "tool_call_failed"
    TOOL_CALL_BLOCKED = "tool_call_blocked"      # policy / budget / circuit
    TOOL_CALL_DEDUPED = "tool_call_deduped"      # served from in-run memo

    RETRY_SCHEDULED = "retry_scheduled"
    CIRCUIT_OPENED = "circuit_opened"
    CIRCUIT_CLOSED = "circuit_closed"
    DEGRADED = "degraded"                        # a fallback path was taken

    BUDGET_UPDATED = "budget_updated"
    BUDGET_EXCEEDED = "budget_exceeded"

    CHECKPOINT_SAVED = "checkpoint_saved"
    CHECKPOINT_RESTORED = "checkpoint_restored"
    STEP_SKIPPED_IDEMPOTENT = "step_skipped_idempotent"

    EVIDENCE_ADDED = "evidence_added"
    CONTEXT_COMPRESSED = "context_compressed"

    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    MEMORY_REJECTED = "memory_rejected"

    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_RESOLVED = "approval_resolved"

    GRADER_RESULT = "grader_result"
    STOP_DECISION = "stop_decision"


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
# Secret detection is *segment-exact*, not substring. Substring matching looked
# fine until it silently redacted `tokens_in` (matched "token") and
# `authority_tier` (matched "auth") - destroying legitimate telemetry and
# evidence metadata. A key is a secret when one of its identifier segments is a
# secret word, or when the whole key matches a compound pattern.
_SECRET_SEGMENTS: frozenset[str] = frozenset({
    "secret", "password", "passwd", "pwd", "token", "authorization", "auth",
    "cookie", "cookies", "credential", "credentials", "bearer", "apikey",
    "sessionid", "signature",
})

_SECRET_COMPOUND_RE = re.compile(
    r"(api[_-]?key|private[_-]?key|access[_-]?key|secret[_-]?key|session[_-]?id|"
    r"set[_-]?cookie|refresh[_-]?token|access[_-]?token|auth[_-]?token)",
    re.I,
)

_SEGMENT_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _is_secret_key(key: str) -> bool:
    """Whether a payload key names a secret.

    `access_token` -> yes (segment "token"). `tokens_in`, `output_tokens`,
    `authority_tier`, `author` -> no.
    """
    if _SECRET_COMPOUND_RE.search(key):
        return True
    segments = {s.lower() for s in _SEGMENT_SPLIT_RE.split(key) if s}
    return bool(segments & _SECRET_SEGMENTS)

#: Keys whose *content* is model-internal reasoning. Dropped outright: this
#: project must not persist chain-of-thought.
_REASONING_KEY_RE = re.compile(
    r"^(reasoning|reasoning_content|thinking|thought|thoughts|chain_of_thought|cot|"
    r"scratchpad|internal_monologue|deliberation)$",
    re.I,
)

_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b", re.I),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
)

REDACTED = "[REDACTED]"
DROPPED = "[DROPPED:reasoning-not-persisted]"


def redact_text(text: str) -> str:
    """Mask secret-shaped substrings inside free text."""
    if not text:
        return text
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact secrets and drop reasoning fields.

    Depth-bounded so a pathological nested payload cannot stall the writer.
    """
    if _depth > 12:
        return "[REDACTED:max-depth]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, val in value.items():
            key_str = str(key)
            if _REASONING_KEY_RE.match(key_str):
                out[key_str] = DROPPED
            elif _is_secret_key(key_str):
                out[key_str] = REDACTED
            else:
                out[key_str] = redact(val, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    return value


# --------------------------------------------------------------------------- #
# Event
# --------------------------------------------------------------------------- #
class TraceEvent(BaseModel):
    """One line in the JSONL stream."""

    schema_version: str = TRACE_SCHEMA_VERSION
    event_id: str = Field(default_factory=lambda: new_id("ev_"))
    ts: str = Field(default_factory=utc_now)
    run_id: str = ""
    #: Monotonic sequence within the run - JSONL order can be trusted, but an
    #: explicit seq survives concatenation and out-of-order collection.
    seq: int = 0

    event_type: EventType
    phase: str = ""
    task_id: str = ""
    step_id: str = ""
    attempt: int = 0

    name: str = ""            # tool name / model name / grader name
    status: str = ""          # ok | error | blocked | skipped
    duration_s: float = 0.0

    error_class: str = ""
    error_message: str = ""

    #: Structured, already-redacted detail. Never contains raw page bodies -
    #: long payloads are externalized and referenced by artifact_id.
    payload: dict[str, Any] = Field(default_factory=dict)
    artifact_id: str = ""

    def redacted(self) -> "TraceEvent":
        clone = self.model_copy(deep=True)
        clone.payload = redact(clone.payload)
        clone.error_message = redact_text(clone.error_message)[:MAX_SUMMARY_CHARS]
        clone.name = redact_text(clone.name)
        return clone


def make_event(event_type: EventType, run_id: str = "", **kwargs: Any) -> TraceEvent:
    """Build an event.

    The payload is left intact here on purpose. Bounding it at this point would
    truncate large values *before* `TraceStore` gets a chance to move them into
    the artifact store, turning "stored elsewhere, retrievable" into "silently
    lost". The store applies the size policy: externalize when an artifact
    store is attached, truncate only when there is nowhere to put the data.
    """
    payload = kwargs.pop("payload", None) or {}
    return TraceEvent(event_type=event_type, run_id=run_id, payload=dict(payload), **kwargs)


def bound_payload(payload: dict[str, Any], limit: int = MAX_SUMMARY_CHARS) -> dict[str, Any]:
    """Last-resort truncation for values that could not be externalized."""
    return {
        k: (summarize(v, limit) if isinstance(v, str) and len(v) > limit else v)
        for k, v in payload.items()
    }


def stop_decision_event(run_id: str, reason: str, detail: str, *,
                        evidence_count: int = 0, rounds_used: int = 0,
                        budget_headroom: Optional[float] = None) -> TraceEvent:
    """Explicit answer to 'why did it stop here instead of searching more'.

    Emitted at every terminal decision point, not only on success, so a run
    that gave up can be told apart from one that had enough.
    """
    return make_event(
        EventType.STOP_DECISION, run_id=run_id, name=reason,
        payload={
            "reason": reason, "detail": detail, "evidence_count": evidence_count,
            "rounds_used": rounds_used, "budget_headroom": budget_headroom,
        },
    )
