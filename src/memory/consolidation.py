"""Consolidation: pruning, decay, merging and procedural versioning.

Runs offline, after tasks finish. Its job is to stop the store turning into an
append-only pile: expire what has aged out, retire memories that are used but
never help, merge near-duplicates, and keep procedural rules versioned with a
usable rollback.
"""
from __future__ import annotations

from typing import Any, Optional

from src.memory.policies import ConflictResolution
from src.memory.retriever import keyword_similarity
from src.memory.schemas import (
    MemoryKind,
    MemoryRecord,
    ProceduralMemory,
    Provenance,
    utc_now_iso,
)
from src.memory.stores.sqlite_store import SqliteMemoryStore


class MemoryConsolidator:
    def __init__(self, store: SqliteMemoryStore,
                 conflicts: Optional[ConflictResolution] = None) -> None:
        self.store = store
        self.conflicts = conflicts or ConflictResolution()

    # ------------------------------------------------------------------ #
    def expire(self, namespace: str) -> int:
        """Deactivate records past their TTL."""
        expired = 0
        for record in self.store.list_active(namespace, limit=10000):
            if record.is_expired():
                self.store.deactivate(namespace, record.memory_id)
                expired += 1
        return expired

    def retire_unhelpful(self, namespace: str, *, min_uses: int = 5,
                         max_usefulness: float = 0.2) -> list[str]:
        """Retire memories that are retrieved often and never correlate with success.

        This is the counterweight to the whole memory system: if a memory has
        been injected repeatedly and the runs using it keep failing, it is
        costing tokens and possibly causing harm.
        """
        retired: list[str] = []
        for record in self.store.list_active(namespace, limit=10000):
            usefulness = record.usefulness()
            if record.use_count >= min_uses and usefulness is not None and usefulness <= max_usefulness:
                if record.provenance in (Provenance.USER_STATED, Provenance.HUMAN_CORRECTION):
                    continue  # never auto-retire something a person told us
                self.store.deactivate(namespace, record.memory_id)
                retired.append(record.memory_id)
        return retired

    def merge_near_duplicates(self, namespace: str, *, threshold: float = 0.85) -> list[tuple[str, str]]:
        """Collapse records saying the same thing in different words.

        Exact duplicates are caught at write time by `dedup_key`; this catches
        paraphrases that accumulate over many runs.
        """
        merged: list[tuple[str, str]] = []
        for kind in MemoryKind:
            records = [r for r in self.store.list_active(namespace, kind=kind, limit=5000)]
            for i, keeper in enumerate(records):
                if keeper.memory_id in {m[1] for m in merged}:
                    continue
                for other in records[i + 1:]:
                    if other.memory_id in {m[1] for m in merged}:
                        continue
                    if other.subject != keeper.subject:
                        continue
                    if keyword_similarity(keeper.content, other.content) < threshold:
                        continue
                    winner, loser = ((keeper, other) if keeper.confidence >= other.confidence
                                     else (other, keeper))
                    winner.confidence = round(min(0.99, winner.confidence + 0.03), 4)
                    winner.use_count += loser.use_count
                    winner.useful_count += loser.useful_count
                    winner.updated_at = utc_now_iso()
                    self.store.upsert(winner)
                    self.store.deactivate(namespace, loser.memory_id,
                                          superseded_by=winner.memory_id)
                    merged.append((winner.memory_id, loser.memory_id))
        return merged

    def decay_confidence(self, namespace: str, *, half_life_days: float = 180.0,
                         floor: float = 0.2) -> int:
        """Age-decay confidence for machine-derived memories.

        User statements and human corrections do not decay: a stated preference
        does not become less true because time passed.
        """
        touched = 0
        for record in self.store.list_active(namespace, limit=10000):
            if record.provenance in (Provenance.USER_STATED, Provenance.HUMAN_CORRECTION):
                continue
            factor = 0.5 ** (record.age_days() / max(half_life_days, 0.001))
            decayed = round(max(floor, record.confidence * factor), 4)
            if abs(decayed - record.confidence) > 0.001:
                record.confidence = decayed
                record.updated_at = utc_now_iso()
                self.store.upsert(record)
                touched += 1
        return touched

    # ------------------------------------------------------------------ #
    # Procedural rule versioning
    # ------------------------------------------------------------------ #
    def propose_rule(self, namespace: str, rule_id: str, content: str, *,
                     provenance: Provenance, gate_evidence: str = "",
                     subject: str = "") -> ProceduralMemory:
        """Create the next version of a procedural rule as an *unapproved*
        proposal. It does not take effect until `approve_rule` is called."""
        existing = self._active_rule(namespace, rule_id)
        rule = ProceduralMemory(
            namespace=namespace, subject=subject, key=f"rule:{rule_id}",
            rule_id=rule_id, content=content, provenance=provenance,
            rule_version=(existing.rule_version + 1) if existing else 1,
            previous_content=existing.content if existing else "",
            gate_evidence=gate_evidence, approved=False, importance=0.9,
            tags=["procedural", "proposal"])
        rule.ensure_id()
        # Proposals are stored inactive so retrieval cannot pick them up.
        rule.active = False
        self.store.upsert(rule)
        return rule

    def approve_rule(self, namespace: str, memory_id: str, approved_by: str) -> Optional[ProceduralMemory]:
        """Activate a proposed rule and retire the version it replaces."""
        record = self.store.get(namespace, memory_id)
        if record is None:
            return None
        rule = ProceduralMemory.model_validate(record.model_dump())
        if not approved_by:
            raise ValueError("approving a procedural rule requires an approver name")

        previous = self._active_rule(namespace, rule.rule_id)
        rule.approved = True
        rule.approved_by = approved_by
        rule.active = True
        rule.updated_at = utc_now_iso()
        self.store.upsert(rule)
        if previous and previous.memory_id != rule.memory_id:
            self.store.deactivate(namespace, previous.memory_id, superseded_by=rule.memory_id)
        return rule

    def rollback_rule(self, namespace: str, rule_id: str) -> Optional[ProceduralMemory]:
        """Restore the previous version of a rule from its own stored history."""
        current = self._active_rule(namespace, rule_id)
        if current is None or not current.previous_content:
            return None
        restored = ProceduralMemory(
            namespace=namespace, subject=current.subject, key=current.key, rule_id=rule_id,
            content=current.previous_content, provenance=Provenance.HUMAN_CORRECTION,
            rule_version=current.rule_version + 1, previous_content=current.content,
            gate_evidence=f"rollback of version {current.rule_version}",
            approved=True, approved_by="rollback", importance=0.9,
            tags=["procedural", "rollback"])
        restored.memory_id = f"{restored.compute_id()}_v{restored.rule_version}"
        self.store.upsert(restored)
        self.store.deactivate(namespace, current.memory_id, superseded_by=restored.memory_id)
        return restored

    def _active_rule(self, namespace: str, rule_id: str) -> Optional[ProceduralMemory]:
        for record in self.store.find_by_key(namespace, f"rule:{rule_id}", MemoryKind.PROCEDURAL):
            rule = ProceduralMemory.model_validate(record.model_dump())
            if rule.approved and rule.active:
                return rule
        return None

    def active_rules(self, namespace: str) -> list[ProceduralMemory]:
        rules: list[ProceduralMemory] = []
        for record in self.store.list_active(namespace, kind=MemoryKind.PROCEDURAL, limit=500):
            rule = ProceduralMemory.model_validate(record.model_dump())
            if rule.approved:
                rules.append(rule)
        return rules

    # ------------------------------------------------------------------ #
    def run_all(self, namespace: str) -> dict[str, Any]:
        """Full offline maintenance pass."""
        return {
            "namespace": namespace,
            "expired": self.expire(namespace),
            "retired_unhelpful": self.retire_unhelpful(namespace),
            "merged": self.merge_near_duplicates(namespace),
            "decayed": self.decay_confidence(namespace),
            "remaining": self.store.count(namespace),
            "ran_at": utc_now_iso(),
        }
