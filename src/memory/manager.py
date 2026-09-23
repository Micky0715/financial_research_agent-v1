"""MemoryManager: the single façade the harness talks to.

Responsibilities:
- retrieve memories for a run and emit `memory_read` events;
- record which memories were injected, so usefulness can be attributed after
  the run ends;
- write candidates *after* the task completes, emitting `memory_write` /
  `memory_rejected`;
- expose the user-facing view/delete operations.

Namespace is fixed at construction. Every store call goes through it, so a
manager built for one tenant cannot read or write another's memories.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from src.memory.consolidation import MemoryConsolidator
from src.memory.policies import ConflictResolution, RetrievalPolicy, WritePolicy
from src.memory.retriever import MemoryRetriever
from src.memory.schemas import (
    MemoryKind,
    MemoryRecord,
    Provenance,
    WriteResult,
    utc_now_iso,
)
from src.memory.stores.sqlite_store import SqliteMemoryStore
from src.memory.writer import MemoryWriter
from src.observability.events import EventType


class MemoryManager:
    """Per-namespace memory façade."""

    def __init__(self, *, namespace: str = "default", db_path: Optional[Path] = None,
                 store: Optional[SqliteMemoryStore] = None,
                 write_policy: Optional[WritePolicy] = None,
                 retrieval_policy: Optional[RetrievalPolicy] = None,
                 conflicts: Optional[ConflictResolution] = None,
                 enabled: bool = True, use_embeddings: bool = True,
                 trace: Any = None) -> None:
        if store is None:
            if db_path is None:
                from config import config

                db_path = config.OUTPUT_DIR / "harness" / "memory.db"
            store = SqliteMemoryStore(db_path)
        self.namespace = namespace
        self.store = store
        self.enabled = enabled
        self.trace = trace
        self.writer = MemoryWriter(store, policy=write_policy, conflicts=conflicts)
        self.retriever = MemoryRetriever(store, policy=retrieval_policy,
                                         use_embeddings=use_embeddings)
        self.consolidator = MemoryConsolidator(store, conflicts=conflicts)
        #: Memory ids injected into the current run, for post-hoc attribution.
        self.injected_ids: list[str] = []

    # ------------------------------------------------------------------ #
    def _emit(self, event_type: EventType, **payload: Any) -> None:
        if self.trace is not None:
            self.trace.event(event_type, payload=payload)

    # ------------------------------------------------------------------ #
    # Read path
    # ------------------------------------------------------------------ #
    def retrieve_for_run(self, *, query: str, subject: str = "",
                         kinds: Optional[list[MemoryKind]] = None) -> list[dict[str, Any]]:
        """Memories to inject into this run. Empty list when memory is off or
        nothing clears the relevance floor."""
        if not self.enabled:
            self._emit(EventType.MEMORY_READ, retrieved=0, reason="memory disabled for this arm")
            return []

        items, diagnostics = self.retriever.retrieve_for_injection(
            namespace=self.namespace, query=query, subject=subject, kinds=kinds)
        self.injected_ids = [i["memory_id"] for i in items]
        self._emit(EventType.MEMORY_READ, **diagnostics)
        return items

    def active_rules_text(self) -> list[str]:
        """Approved procedural rules, as instruction lines."""
        return [r.content for r in self.consolidator.active_rules(self.namespace)]

    # ------------------------------------------------------------------ #
    # Write path
    # ------------------------------------------------------------------ #
    def write(self, record: MemoryRecord, *, injection_suspected: bool = False) -> WriteResult:
        record.namespace = self.namespace  # never trust a caller-supplied namespace
        result = self.writer.write(record, injection_suspected=injection_suspected)
        if result.written:
            self._emit(EventType.MEMORY_WRITE, memory_id=result.memory_id,
                       action=result.action, kind=record.kind.value, reason=result.reason)
        else:
            self._emit(EventType.MEMORY_REJECTED, kind=record.kind.value,
                       rejection=result.rejection.value if result.rejection else "",
                       reason=result.reason)
        return result

    def learn_from_run(self, *, run_id: str, subject: str, events: list[dict[str, Any]],
                       succeeded: bool, entity_validation: Optional[dict[str, Any]] = None,
                       user_statements: Optional[list[str]] = None) -> dict[str, Any]:
        """Post-task extraction and write. Never called inline during a task.

        Also attributes usefulness: the memories injected into this run get
        their `use_count` incremented, and `useful_count` too when the run
        succeeded. That is what makes "did memory help?" answerable later.
        """
        if not self.enabled:
            return {"enabled": False, "written": 0}

        if self.injected_ids:
            self.store.record_usage(self.namespace, self.injected_ids,
                                    useful=succeeded, used_at=utc_now_iso())

        candidates = self.writer.extract_candidates(
            namespace=self.namespace, subject=subject, run_id=run_id, events=events,
            succeeded=succeeded, entity_validation=entity_validation,
            user_statements=user_statements)

        # A run whose fetched content tripped the injection scanner cannot
        # promote anything into procedural memory.
        injection_suspected = any(
            e.get("event_type") == "tool_call_blocked"
            and "injection" in (e.get("error_message") or "") for e in events)

        results = [self.write(c, injection_suspected=injection_suspected) for c in candidates]
        return {
            "enabled": True,
            "candidates": len(candidates),
            "written": sum(1 for r in results if r.written),
            "rejected": [{"rejection": r.rejection.value if r.rejection else "", "reason": r.reason}
                         for r in results if not r.written],
            "attributed_usage": len(self.injected_ids),
            "injection_suspected": injection_suspected,
        }

    # ------------------------------------------------------------------ #
    # User-facing controls
    # ------------------------------------------------------------------ #
    def list_memories(self, *, kind: Optional[MemoryKind] = None,
                      limit: int = 200) -> list[dict[str, Any]]:
        """Everything this namespace holds, for user inspection."""
        return [
            {"memory_id": r.memory_id, "kind": r.kind.value, "subject": r.subject,
             "content": r.content, "provenance": r.provenance.value,
             "confidence": r.confidence, "importance": r.importance,
             "use_count": r.use_count, "usefulness": r.usefulness(),
             "created_at": r.created_at, "expires_at": r.expires_at}
            for r in self.store.list_active(self.namespace, kind=kind, limit=limit)
        ]

    def delete_memory(self, memory_id: str) -> bool:
        """A user deleting their own memory. Hard delete, not a flag."""
        deleted = self.store.delete(self.namespace, memory_id)
        self._emit(EventType.MEMORY_WRITE, action="deleted", memory_id=memory_id,
                   reason="user requested deletion")
        return deleted

    def forget_all(self) -> int:
        count = self.store.delete_namespace(self.namespace)
        self._emit(EventType.MEMORY_WRITE, action="deleted_all", reason="user cleared namespace",
                   count=count)
        return count

    def correct(self, memory_id: str, new_content: str, corrected_by: str) -> WriteResult:
        """Human correction: supersedes the old record with a high-trust one."""
        existing = self.store.get(self.namespace, memory_id)
        if existing is None:
            return WriteResult(written=False, action="rejected", reason="memory not found")
        corrected = existing.model_copy(deep=True)
        corrected.content = new_content
        corrected.provenance = Provenance.HUMAN_CORRECTION
        corrected.confidence = 0.95
        corrected.version = existing.version + 1
        corrected.memory_id = ""
        corrected.updated_at = utc_now_iso()
        corrected.ensure_id()
        result = self.write(corrected)
        if result.written and corrected.memory_id != memory_id:
            self.store.deactivate(self.namespace, memory_id, superseded_by=corrected.memory_id)
        result.reason = f"corrected by {corrected_by}: {result.reason}"
        return result

    def maintain(self) -> dict[str, Any]:
        return self.consolidator.run_all(self.namespace)

    def stats(self) -> dict[str, Any]:
        return self.store.stats(self.namespace)
