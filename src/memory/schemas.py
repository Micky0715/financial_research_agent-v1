"""Memory record types.

Three kinds, because they have genuinely different lifecycles and different
failure modes. Collapsing them into "put the conversation in a vector DB" is
what makes memory systems accumulate stale, unverified noise:

- **Semantic** - stable facts about the user and the domain (report
  preferences, watched metrics, company aliases, accounting conventions,
  trusted sources). Slow-changing; conflicts are resolved by recency plus
  confidence.
- **Episodic** - what happened on a specific past task, and only where the
  outcome was *verified*: a retrieval strategy that demonstrably worked, a site
  that reliably blocks scraping, a recovery path that succeeded. A model's own
  opinion of its performance is explicitly not admissible (`WriteRejection.
  UNVERIFIED_SELF_ASSESSMENT`).
- **Procedural** - versioned rules the system follows: report format, source
  priority, stop conditions, number-grounding rules. These change behaviour, so
  they are versioned, require provenance, and can be rolled back. Nothing
  derived from fetched web content may ever become procedural memory.

Every record carries a namespace. Isolation is enforced at the store layer, not
by convention, and there is a test for cross-namespace leakage.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

MEMORY_SCHEMA_VERSION = "1.0"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return _now().isoformat(timespec="seconds")


class MemoryKind(str, Enum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


class Provenance(str, Enum):
    """Where a memory came from. Drives what it is allowed to become."""

    USER_STATED = "user_stated"              # the user said it explicitly
    HUMAN_CORRECTION = "human_correction"    # a person fixed a past mistake
    VERIFIED_OUTCOME = "verified_outcome"    # a grader/deterministic check confirmed it
    RUN_OBSERVATION = "run_observation"      # observed mechanically during a run
    MODEL_INFERENCE = "model_inference"      # the model concluded it - weakest
    EXTERNAL_CONTENT = "external_content"    # derived from a fetched page - untrusted


#: Only these may become procedural rules. External content and unverified
#: model inference must never change how the system behaves.
PROCEDURAL_ALLOWED_PROVENANCE: frozenset[Provenance] = frozenset({
    Provenance.USER_STATED, Provenance.HUMAN_CORRECTION, Provenance.VERIFIED_OUTCOME,
})

#: Baseline trust by provenance, used as the starting confidence.
PROVENANCE_CONFIDENCE: dict[Provenance, float] = {
    Provenance.USER_STATED: 0.95,
    Provenance.HUMAN_CORRECTION: 0.95,
    Provenance.VERIFIED_OUTCOME: 0.85,
    Provenance.RUN_OBSERVATION: 0.6,
    Provenance.MODEL_INFERENCE: 0.35,
    Provenance.EXTERNAL_CONTENT: 0.2,
}


class MemoryRecord(BaseModel):
    """One memory entry."""

    schema_version: str = MEMORY_SCHEMA_VERSION
    memory_id: str = ""
    namespace: str = "default"       # tenant/user isolation key
    subject: str = ""                # company/industry/topic this is about, "" = global
    kind: MemoryKind = MemoryKind.SEMANTIC

    key: str = ""                    # canonical slug, used for conflict detection
    content: str = ""                # the memory itself, in one or two sentences
    structured: dict[str, Any] = Field(default_factory=dict)

    provenance: Provenance = Provenance.MODEL_INFERENCE
    source_run_id: str = ""
    source_reference: str = ""

    confidence: float = 0.5
    importance: float = 0.5
    #: Times this memory was retrieved and injected into a run.
    use_count: int = 0
    #: Times a run that used it succeeded (feeds Memory Usefulness).
    useful_count: int = 0

    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    last_used_at: str = ""
    #: Absolute expiry. None = no expiry (procedural rules, user preferences).
    expires_at: Optional[str] = None

    version: int = 1
    superseded_by: str = ""
    active: bool = True
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _default_confidence_from_provenance(self) -> "MemoryRecord":
        """Derive confidence from provenance unless the caller set it explicitly.

        Checked via `model_fields_set` rather than truthiness: the field default
        is 0.5, so `if not confidence` never fires and every record - including
        a user's own stated preference - would sit at the generic default.
        """
        if "confidence" not in self.model_fields_set:
            object.__setattr__(self, "confidence",
                               PROVENANCE_CONFIDENCE.get(self.provenance, 0.5))
        return self

    # ------------------------------------------------------------------ #
    def compute_id(self) -> str:
        """Stable id derived from namespace, kind, key **and** content.

        Content must be part of it for the same reason it is part of
        `dedup_key`: with the id keyed on `key` alone, a correction and the
        record it corrects shared one id, so writing the correction overwrote
        the incumbent row and the subsequent `deactivate(incumbent)` then
        retired the correction itself - the memory disappeared entirely.
        """
        return "mem_" + self.dedup_key()

    def ensure_id(self) -> str:
        if not self.memory_id:
            self.memory_id = self.compute_id()
        return self.memory_id

    def dedup_key(self) -> str:
        """Two records with the same dedup key are the same memory.

        Content is part of the key, not just `key`. Hashing `key or content`
        made "茅台代码是600519" and "茅台代码是000001" collide under the shared
        key `symbol:茅台`, so a contradicting claim was merged in as a duplicate
        and the conflict resolver never ran - a wrong fact could quietly
        reinforce a right one. Same key + different content is a *conflict*;
        only identical content is a duplicate.
        """
        normalized = "".join((self.content or "").split()).lower()
        return hashlib.sha256(
            f"{self.namespace}|{self.subject}|{self.kind.value}|{self.key}|{normalized}".encode("utf-8")
        ).hexdigest()[:16]

    def is_expired(self, now: Optional[datetime] = None) -> bool:
        if not self.expires_at:
            return False
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return (now or _now()) >= expiry

    def age_days(self, now: Optional[datetime] = None) -> float:
        try:
            created = datetime.fromisoformat(self.created_at)
        except ValueError:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return max(0.0, ((now or _now()) - created).total_seconds() / 86400.0)

    def usefulness(self) -> Optional[float]:
        """Share of runs that used this memory and succeeded. None if unused."""
        return round(self.useful_count / self.use_count, 4) if self.use_count else None

    def set_ttl(self, days: float) -> "MemoryRecord":
        self.expires_at = (_now() + timedelta(days=days)).isoformat(timespec="seconds")
        return self


class SemanticMemory(MemoryRecord):
    kind: MemoryKind = MemoryKind.SEMANTIC


class EpisodicMemory(MemoryRecord):
    kind: MemoryKind = MemoryKind.EPISODIC
    #: What the task was, what was done, and what the verified outcome was.
    task_signature: str = ""
    action_taken: str = ""
    outcome: str = ""
    outcome_verified_by: str = ""    # grader name / human name


class ProceduralMemory(MemoryRecord):
    kind: MemoryKind = MemoryKind.PROCEDURAL
    rule_id: str = ""
    rule_version: int = 1
    #: The previous version's content, so a rollback needs no external history.
    previous_content: str = ""
    #: Evidence that this rule change passed the regression gate.
    gate_evidence: str = ""
    #: Procedural memories are proposals until a human or gate approves them.
    approved: bool = False
    approved_by: str = ""


class WriteRejection(str, Enum):
    """Why a candidate memory was refused. Recorded in the trace."""

    DUPLICATE = "duplicate"
    SCHEMA_INVALID = "schema_invalid"
    SENSITIVE_CONTENT = "sensitive_content"
    UNVERIFIED_SELF_ASSESSMENT = "unverified_self_assessment"
    UNTRUSTED_PROVENANCE = "untrusted_provenance"
    INJECTION_SUSPECTED = "injection_suspected"
    LOW_CONFIDENCE = "low_confidence"
    CONFLICTS_WITH_NEWER = "conflicts_with_newer"
    NAMESPACE_MISMATCH = "namespace_mismatch"
    EMPTY = "empty"


class WriteResult(BaseModel):
    written: bool = False
    memory_id: str = ""
    action: str = ""            # created | updated | merged | rejected | superseded
    rejection: Optional[WriteRejection] = None
    reason: str = ""


class RetrievalResult(BaseModel):
    record: MemoryRecord
    score: float = 0.0
    components: dict[str, float] = Field(default_factory=dict)

    def to_injection(self) -> dict[str, Any]:
        """Compact form handed to the context manager."""
        return {
            "memory_id": self.record.memory_id,
            "kind": self.record.kind.value,
            "content": self.record.content,
            "importance": self.record.importance,
            "confidence": self.record.confidence,
            "score": self.score,
            "provenance": self.record.provenance.value,
        }
