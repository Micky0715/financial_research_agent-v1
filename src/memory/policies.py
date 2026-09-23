"""Memory write and retrieval policies.

These are the rules that decide what is *allowed* to be remembered and what is
worth injecting. They are separated from the manager so they can be unit-tested
directly and swapped per deployment.

The write policy is deliberately strict. Most memory systems fail not because
retrieval is bad but because they accumulate confident nonsense: the model's own
performance self-assessment, a fact scraped from a page that was trying to
manipulate it, a preference inferred from one ambiguous message. Each of those
has an explicit rejection reason here.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.memory.schemas import (
    PROCEDURAL_ALLOWED_PROVENANCE,
    MemoryKind,
    MemoryRecord,
    Provenance,
    WriteRejection,
)

#: Phrases that mark a candidate as the model grading itself. An agent noting
#: "this run went well" is not evidence and must not become memory.
_SELF_ASSESSMENT_RE = re.compile(
    r"(我(的)?(表现|回答|分析)(很好|不错|准确|正确)|本次(运行|分析)(很好|成功|准确)|"
    r"效果(良好|不错)|做得(很)?好|表现优秀|"
    r"i (did|performed) (well|great)|my (answer|analysis) (was|is) (correct|good))",
    re.I)

#: Content that must never be persisted, regardless of provenance.
_SENSITIVE_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|Bearer\s+[A-Za-z0-9._\-]{16,}|"
    r"\b\d{17}[\dXx]\b|"                        # PRC national ID
    r"\b\d{16,19}\b(?=.*(卡号|银行|card))|"      # card number in a banking context
    r"(密码|password)\s*[:：=]\s*\S+|"
    r"\b1[3-9]\d{9}\b(?=.*(手机|电话|phone)))",
    re.I)


class WritePolicy(BaseModel):
    """What may be written, and as what."""

    min_confidence: float = 0.3
    min_content_chars: int = 8
    max_content_chars: int = 600
    #: Episodic memories require a verified outcome, not the model's opinion.
    require_verified_outcome_for_episodic: bool = True
    #: Default TTLs in days. Procedural/user-stated facts get no expiry.
    ttl_days_by_kind: dict[str, Optional[float]] = Field(
        default_factory=lambda: {"semantic": 365.0, "episodic": 120.0, "procedural": None})

    def check(self, record: MemoryRecord, *,
              injection_suspected: bool = False) -> tuple[bool, Optional[WriteRejection], str]:
        """Returns (allowed, rejection, reason)."""
        content = (record.content or "").strip()
        if not content:
            return False, WriteRejection.EMPTY, "candidate has no content"
        if len(content) < self.min_content_chars:
            return False, WriteRejection.SCHEMA_INVALID, f"content shorter than {self.min_content_chars} chars"
        if len(content) > self.max_content_chars:
            return False, WriteRejection.SCHEMA_INVALID, f"content longer than {self.max_content_chars} chars"
        if not record.namespace:
            return False, WriteRejection.NAMESPACE_MISMATCH, "record has no namespace"

        if _SENSITIVE_RE.search(content) or _SENSITIVE_RE.search(str(record.structured)):
            return False, WriteRejection.SENSITIVE_CONTENT, "candidate contains credential or PII-like content"

        if injection_suspected or record.provenance is Provenance.EXTERNAL_CONTENT:
            if record.kind is MemoryKind.PROCEDURAL:
                return (False, WriteRejection.INJECTION_SUSPECTED,
                        "content derived from fetched pages may never become a procedural rule")

        if _SELF_ASSESSMENT_RE.search(content):
            return (False, WriteRejection.UNVERIFIED_SELF_ASSESSMENT,
                    "model self-assessment is not evidence and is not stored")

        if record.kind is MemoryKind.EPISODIC and self.require_verified_outcome_for_episodic:
            if record.provenance not in (Provenance.VERIFIED_OUTCOME, Provenance.HUMAN_CORRECTION,
                                         Provenance.RUN_OBSERVATION):
                return (False, WriteRejection.UNTRUSTED_PROVENANCE,
                        f"episodic memory requires a verified or mechanically observed outcome, "
                        f"got provenance={record.provenance.value}")

        if record.kind is MemoryKind.PROCEDURAL and record.provenance not in PROCEDURAL_ALLOWED_PROVENANCE:
            return (False, WriteRejection.UNTRUSTED_PROVENANCE,
                    f"procedural rules require provenance in "
                    f"{sorted(p.value for p in PROCEDURAL_ALLOWED_PROVENANCE)}, "
                    f"got {record.provenance.value}")

        if record.confidence < self.min_confidence:
            return (False, WriteRejection.LOW_CONFIDENCE,
                    f"confidence {record.confidence} below threshold {self.min_confidence}")

        return True, None, "accepted"

    def default_ttl_days(self, record: MemoryRecord) -> Optional[float]:
        if record.provenance is Provenance.USER_STATED:
            return None
        return self.ttl_days_by_kind.get(record.kind.value)


class RetrievalPolicy(BaseModel):
    """How candidates are scored and how many are injected."""

    #: Weights over the scoring components; they need not sum to 1.
    weight_similarity: float = 0.40
    weight_recency: float = 0.15
    weight_importance: float = 0.15
    weight_usage: float = 0.10
    weight_confidence: float = 0.20

    #: A candidate below this final score is not injected at all. The point of
    #: the floor: injecting weak matches costs tokens and adds noise, and an
    #: empty memory block is better than a misleading one.
    min_score: float = 0.35
    #: Independent relevance gate. The composite score alone is not enough: a
    #: high-importance, high-confidence memory about photovoltaics scored 0.41
    #: against a macro-policy query purely on its non-similarity components, so
    #: it cleared `min_score` while being completely off-topic. Relevance is a
    #: precondition, not one weighted vote among several.
    min_similarity: float = 0.12
    #: Never inject more than this many, whatever the budget allows.
    max_items: int = 6
    #: Token ceiling for the memory block, independent of the context budget.
    max_tokens: int = 800
    #: Half-life for the recency term, in days.
    recency_half_life_days: float = 45.0
    #: Expired memories are never returned.
    drop_expired: bool = True

    def score(self, similarity: float, record: MemoryRecord, age_days: float) -> tuple[float, dict[str, float]]:
        recency = 0.5 ** (age_days / max(self.recency_half_life_days, 0.001))
        usage = min(1.0, record.use_count / 10.0)
        # A memory that has been used and never helped is actively penalised,
        # otherwise a bad memory survives forever on its recency alone.
        usefulness = record.usefulness()
        if usefulness is not None and record.use_count >= 3:
            usage *= max(0.1, usefulness)

        components = {
            "similarity": round(similarity, 4),
            "recency": round(recency, 4),
            "importance": round(record.importance, 4),
            "usage": round(usage, 4),
            "confidence": round(record.confidence, 4),
        }
        total = (self.weight_similarity * similarity
                 + self.weight_recency * recency
                 + self.weight_importance * record.importance
                 + self.weight_usage * usage
                 + self.weight_confidence * record.confidence)
        weight_sum = (self.weight_similarity + self.weight_recency + self.weight_importance
                      + self.weight_usage + self.weight_confidence)
        return round(total / weight_sum if weight_sum else 0.0, 4), components


class ConflictResolution(BaseModel):
    """How to resolve two memories that claim different things about one key."""

    #: A newer record wins only if its confidence is at least this fraction of
    #: the incumbent's. Otherwise the older, better-supported record stands -
    #: "newest wins" alone lets one weak inference overwrite a user statement.
    min_confidence_ratio: float = 0.8
    #: A user statement or human correction always wins.
    human_input_always_wins: bool = True

    def resolve(self, incumbent: MemoryRecord, candidate: MemoryRecord) -> tuple[str, str]:
        """Returns (winner, reason) where winner is 'candidate' or 'incumbent'."""
        if self.human_input_always_wins:
            human = (Provenance.USER_STATED, Provenance.HUMAN_CORRECTION)
            if candidate.provenance in human and incumbent.provenance not in human:
                return "candidate", "human input supersedes machine-derived memory"
            if incumbent.provenance in human and candidate.provenance not in human:
                return "incumbent", "existing memory came from the user and is not overwritten by inference"

        if candidate.confidence >= incumbent.confidence * self.min_confidence_ratio:
            return "candidate", (f"newer record ({candidate.updated_at}) with comparable confidence "
                                 f"{candidate.confidence} vs {incumbent.confidence}")
        return "incumbent", (f"incumbent confidence {incumbent.confidence} materially exceeds "
                             f"candidate {candidate.confidence}; keeping the better-supported record")


def is_self_assessment(text: str) -> bool:
    return bool(_SELF_ASSESSMENT_RE.search(text or ""))


def contains_sensitive(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text or ""))
