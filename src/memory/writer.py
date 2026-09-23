"""Candidate extraction and the write path.

Extraction is **deterministic and post-hoc**, not an LLM pass over every
message. Two reasons: an extra model call per turn is a real cost for a
speculative benefit, and a model asked "what should I remember?" mid-task
reliably answers with its own impressions - the class of memory this system
explicitly refuses to store.

So candidates come from things that actually happened and can be checked:
- which retrieval strategy produced usable sources (from the trace);
- which domains failed to fetch, and how (from tool failure events);
- which recovery path worked after a failure;
- the resolved entity/symbol for a subject (from entity validation);
- explicit user statements, when the caller passes them in.

Everything goes through `WritePolicy`, then dedup, then conflict resolution.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from src.memory.policies import ConflictResolution, WritePolicy
from src.memory.schemas import (
    PROVENANCE_CONFIDENCE,
    EpisodicMemory,
    MemoryKind,
    MemoryRecord,
    Provenance,
    SemanticMemory,
    WriteRejection,
    WriteResult,
    utc_now_iso,
)
from src.memory.stores.sqlite_store import SqliteMemoryStore


def _domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""


class MemoryWriter:
    """Validates, dedups and persists memory candidates."""

    def __init__(self, store: SqliteMemoryStore, *, policy: Optional[WritePolicy] = None,
                 conflicts: Optional[ConflictResolution] = None) -> None:
        self.store = store
        self.policy = policy or WritePolicy()
        self.conflicts = conflicts or ConflictResolution()

    # ------------------------------------------------------------------ #
    def write(self, record: MemoryRecord, *, injection_suspected: bool = False) -> WriteResult:
        """Single entry point for persisting a memory."""
        record.namespace = record.namespace or "default"

        allowed, rejection, reason = self.policy.check(
            record, injection_suspected=injection_suspected)
        if not allowed:
            return WriteResult(written=False, action="rejected", rejection=rejection, reason=reason)

        if record.expires_at is None:
            if (ttl := self.policy.default_ttl_days(record)) is not None:
                record.set_ttl(ttl)

        record.ensure_id()
        record.updated_at = utc_now_iso()

        existing = self.store.find_by_dedup_key(record.namespace, record.dedup_key())
        if existing is not None:
            return self._merge(existing, record)

        # Same key, different content -> a factual conflict, not a duplicate.
        if record.key:
            rivals = [r for r in self.store.find_by_key(record.namespace, record.key,
                                                        record.kind, record.subject)
                      if r.dedup_key() != record.dedup_key()]
            if rivals:
                return self._resolve_conflict(rivals[0], record)

        self.store.upsert(record)
        return WriteResult(written=True, memory_id=record.memory_id, action="created",
                           reason="new memory stored")

    # ------------------------------------------------------------------ #
    def _merge(self, existing: MemoryRecord, candidate: MemoryRecord) -> WriteResult:
        """Same memory seen again: reinforce rather than duplicate."""
        existing.confidence = round(min(0.99, max(existing.confidence, candidate.confidence)
                                        + 0.05), 4)
        existing.importance = max(existing.importance, candidate.importance)
        existing.updated_at = utc_now_iso()
        if candidate.expires_at:
            existing.expires_at = candidate.expires_at  # observed again: extend life
        existing.tags = sorted(set(existing.tags) | set(candidate.tags))
        self.store.upsert(existing)
        return WriteResult(written=True, memory_id=existing.memory_id, action="merged",
                           rejection=WriteRejection.DUPLICATE,
                           reason="duplicate of an existing memory; confidence reinforced instead")

    def _resolve_conflict(self, incumbent: MemoryRecord, candidate: MemoryRecord) -> WriteResult:
        winner, reason = self.conflicts.resolve(incumbent, candidate)
        if winner == "incumbent":
            return WriteResult(written=False, memory_id=incumbent.memory_id, action="rejected",
                               rejection=WriteRejection.CONFLICTS_WITH_NEWER, reason=reason)

        candidate.version = incumbent.version + 1
        self.store.upsert(candidate)
        self.store.deactivate(incumbent.namespace, incumbent.memory_id,
                              superseded_by=candidate.memory_id)
        return WriteResult(written=True, memory_id=candidate.memory_id, action="superseded",
                           reason=f"supersedes {incumbent.memory_id}: {reason}")

    def write_many(self, records: Iterable[MemoryRecord]) -> list[WriteResult]:
        return [self.write(record) for record in records]

    # ------------------------------------------------------------------ #
    # Candidate extraction from a finished run
    # ------------------------------------------------------------------ #
    def extract_candidates(self, *, namespace: str, subject: str, run_id: str,
                           events: list[dict[str, Any]], succeeded: bool,
                           entity_validation: Optional[dict[str, Any]] = None,
                           user_statements: Optional[list[str]] = None) -> list[MemoryRecord]:
        """Derive memory candidates from a completed run's trace.

        Only mechanically observable facts. Nothing here asks a model what it
        thinks it learned.
        """
        candidates: list[MemoryRecord] = []

        # 1. Explicit user statements -> semantic memory, highest trust.
        for statement in (user_statements or []):
            candidates.append(SemanticMemory(
                namespace=namespace, subject=subject,
                key=f"user_preference:{abs(hash(statement)) % 10**8}",
                content=statement.strip(), provenance=Provenance.USER_STATED,
                importance=0.9, source_run_id=run_id, tags=["preference"]))

        # 2. Resolved entity identity -> semantic memory (company aliases).
        if entity_validation and entity_validation.get("validation_status") == "verified":
            symbol = entity_validation.get("symbol") or ""
            name = entity_validation.get("entity_name") or subject
            if symbol and name:
                candidates.append(SemanticMemory(
                    namespace=namespace, subject=subject, key=f"symbol:{name}",
                    content=f"「{name}」对应的 A 股证券代码是 {symbol}（实体校验通过）。",
                    structured={"entity": name, "symbol": symbol},
                    provenance=Provenance.VERIFIED_OUTCOME, importance=0.8,
                    source_run_id=run_id, tags=["alias", "symbol"]))

        # 3. Domains that consistently failed -> episodic memory.
        failures: dict[str, list[str]] = defaultdict(list)
        for event in events:
            if event.get("event_type") != "tool_call_failed":
                continue
            url = (event.get("payload", {}) or {}).get("url", "")
            if not url:
                continue
            if domain := _domain(url):
                failures[domain].append(event.get("error_class", "UNKNOWN"))
        for domain, classes in failures.items():
            if len(classes) < 2:
                continue  # one failure is noise, not a pattern
            dominant = max(set(classes), key=classes.count)
            candidates.append(EpisodicMemory(
                namespace=namespace, subject="", key=f"unreliable_domain:{domain}",
                content=f"域名 {domain} 在抓取时反复失败（{len(classes)} 次，主要错误 {dominant}），"
                        f"后续检索可降低其优先级。",
                structured={"domain": domain, "failures": len(classes), "error_class": dominant},
                provenance=Provenance.RUN_OBSERVATION, importance=0.6,
                source_run_id=run_id, task_signature=f"fetch:{domain}",
                action_taken="read_webpage/read_pdf", outcome=f"failed x{len(classes)}",
                outcome_verified_by="trace_events", tags=["domain", "reliability"]))

        # 4. Query templates that produced usable sources -> episodic, and only
        #    when the run actually succeeded.
        if succeeded:
            completed_search_steps = {
                e.get("step_id")
                for e in events
                if e.get("event_type") == "tool_call_completed"
                and e.get("name") == "web_search"
                and e.get("step_id")
            }
            productive = [
                (e.get("payload", {}) or {}).get("arguments", {}).get("query", "")
                for e in events
                if e.get("event_type") == "tool_call_started"
                and e.get("name") == "web_search"
                and e.get("step_id") in completed_search_steps
            ]
            productive = [q for q in productive if q]
            if productive:
                shapes = sorted({q.replace(subject, "{subject}") for q in productive})[:3]
                candidates.append(EpisodicMemory(
                    namespace=namespace, subject=subject,
                    key=f"query_strategy:{subject or 'general'}",
                    content=f"针对「{subject}」这类主题，以下检索式产出了可用来源："
                            + "；".join(shapes),
                    structured={"query_shapes": shapes},
                    provenance=Provenance.VERIFIED_OUTCOME, importance=0.7,
                    source_run_id=run_id, task_signature=f"research:{subject}",
                    action_taken="web_search", outcome="produced usable sources",
                    outcome_verified_by="run_outcome", tags=["retrieval_strategy"]))

        # 5. Recovery paths that worked -> episodic.
        failed_steps = {e.get("step_id") for e in events
                        if e.get("event_type") == "tool_call_failed" and e.get("step_id")}
        for event in events:
            if (event.get("event_type") == "tool_call_completed"
                    and event.get("step_id") in failed_steps):
                tool = event.get("name", "")
                candidates.append(EpisodicMemory(
                    namespace=namespace, subject="", key=f"recovery:{tool}",
                    content=f"工具 {tool} 失败后经退避重试恢复成功，"
                            f"该类瞬时故障值得重试而非直接降级。",
                    structured={"tool": tool},
                    provenance=Provenance.RUN_OBSERVATION, importance=0.55,
                    source_run_id=run_id, task_signature=f"recover:{tool}",
                    action_taken="retry with backoff", outcome="succeeded",
                    outcome_verified_by="trace_events", tags=["recovery"]))
                break

        return candidates

    def persist_run_memories(self, **kwargs: Any) -> list[WriteResult]:
        """Extract and write in one call. Intended to run *after* a task ends,
        asynchronously - never inline per message."""
        return self.write_many(self.extract_candidates(**kwargs))
