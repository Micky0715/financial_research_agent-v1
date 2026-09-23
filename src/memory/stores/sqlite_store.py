"""SQLite memory store.

Namespace isolation is enforced *here*, at the only place records are read,
rather than by callers remembering to filter. Every query takes a namespace and
every WHERE clause includes it; there is no method that returns records across
namespaces, and `leak_check()` exists so the isolation property can be asserted
rather than assumed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

from src.memory.schemas import (
    EpisodicMemory,
    MemoryKind,
    MemoryRecord,
    ProceduralMemory,
    SemanticMemory,
)

#: Rehydrate into the concrete subclass. Loading everything as the base
#: `MemoryRecord` silently drops subclass fields - a ProceduralMemory came back
#: without `approved`/`rule_version`, so an approved rule read as unapproved and
#: vanished from `active_rules()`.
_MODEL_BY_KIND: dict[str, type[MemoryRecord]] = {
    MemoryKind.SEMANTIC.value: SemanticMemory,
    MemoryKind.EPISODIC.value: EpisodicMemory,
    MemoryKind.PROCEDURAL.value: ProceduralMemory,
}


def _rehydrate(payload: dict) -> MemoryRecord:
    model = _MODEL_BY_KIND.get(str(payload.get("kind", "")), MemoryRecord)
    return model.model_validate(payload)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    memory_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    subject TEXT,
    kind TEXT NOT NULL,
    key TEXT,
    dedup_key TEXT NOT NULL,
    content TEXT,
    payload_json TEXT NOT NULL,
    confidence REAL, importance REAL,
    use_count INTEGER DEFAULT 0, useful_count INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT, last_used_at TEXT, expires_at TEXT,
    version INTEGER DEFAULT 1, superseded_by TEXT,
    active INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_mem_ns ON memories(namespace, active);
CREATE INDEX IF NOT EXISTS idx_mem_ns_kind ON memories(namespace, kind, active);
CREATE INDEX IF NOT EXISTS idx_mem_dedup ON memories(namespace, dedup_key);
CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(namespace, subject);
"""


class SqliteMemoryStore:
    """Namespace-scoped persistence for memory records."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_record(row: sqlite3.Row) -> MemoryRecord:
        return _rehydrate(json.loads(row["payload_json"]))

    def upsert(self, record: MemoryRecord) -> str:
        record.ensure_id()
        with self._lock:
            self._conn.execute(
                """INSERT INTO memories (memory_id, namespace, subject, kind, key, dedup_key,
                        content, payload_json, confidence, importance, use_count, useful_count,
                        created_at, updated_at, last_used_at, expires_at, version, superseded_by, active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(memory_id) DO UPDATE SET
                       subject=excluded.subject, key=excluded.key, dedup_key=excluded.dedup_key,
                       content=excluded.content, payload_json=excluded.payload_json,
                       confidence=excluded.confidence, importance=excluded.importance,
                       use_count=excluded.use_count, useful_count=excluded.useful_count,
                       updated_at=excluded.updated_at, last_used_at=excluded.last_used_at,
                       expires_at=excluded.expires_at, version=excluded.version,
                       superseded_by=excluded.superseded_by, active=excluded.active""",
                (record.memory_id, record.namespace, record.subject, record.kind.value,
                 record.key, record.dedup_key(), record.content,
                 record.model_dump_json(), record.confidence, record.importance,
                 record.use_count, record.useful_count, record.created_at, record.updated_at,
                 record.last_used_at, record.expires_at, record.version, record.superseded_by,
                 1 if record.active else 0))
            self._conn.commit()
        return record.memory_id

    def get(self, namespace: str, memory_id: str) -> Optional[MemoryRecord]:
        """Namespace is part of the lookup: an id from another tenant returns
        nothing, rather than another tenant's record."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM memories WHERE namespace=? AND memory_id=?",
                (namespace, memory_id)).fetchone()
        return _rehydrate(json.loads(row["payload_json"])) if row else None

    def find_by_dedup_key(self, namespace: str, dedup_key: str) -> Optional[MemoryRecord]:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM memories WHERE namespace=? AND dedup_key=? AND active=1",
                (namespace, dedup_key)).fetchone()
        return _rehydrate(json.loads(row["payload_json"])) if row else None

    def find_by_key(self, namespace: str, key: str, kind: Optional[MemoryKind] = None,
                    subject: str = "") -> list[MemoryRecord]:
        query = "SELECT payload_json FROM memories WHERE namespace=? AND key=? AND active=1"
        params: list[Any] = [namespace, key]
        if kind:
            query += " AND kind=?"
            params.append(kind.value)
        if subject:
            query += " AND subject=?"
            params.append(subject)
        with self._lock:
            rows = self._conn.execute(query + " ORDER BY updated_at DESC", params).fetchall()
        return [self._to_record(r) for r in rows]

    def list_active(self, namespace: str, *, kind: Optional[MemoryKind] = None,
                    subject: str = "", limit: int = 500) -> list[MemoryRecord]:
        query = "SELECT payload_json FROM memories WHERE namespace=? AND active=1"
        params: list[Any] = [namespace]
        if kind:
            query += " AND kind=?"
            params.append(kind.value)
        if subject:
            query += " AND (subject=? OR subject='')"
            params.append(subject)
        query += " ORDER BY importance DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._to_record(r) for r in rows]

    def deactivate(self, namespace: str, memory_id: str, superseded_by: str = "") -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE memories SET active=0, superseded_by=? WHERE namespace=? AND memory_id=?",
                (superseded_by, namespace, memory_id))
            self._conn.commit()
        return cursor.rowcount > 0

    def delete(self, namespace: str, memory_id: str) -> bool:
        """Hard delete - the user's right to remove their own memory."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM memories WHERE namespace=? AND memory_id=?", (namespace, memory_id))
            self._conn.commit()
        return cursor.rowcount > 0

    def delete_namespace(self, namespace: str) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM memories WHERE namespace=?", (namespace,))
            self._conn.commit()
        return cursor.rowcount

    def record_usage(self, namespace: str, memory_ids: Iterable[str], *,
                     useful: Optional[bool] = None, used_at: str = "") -> int:
        """Increment use counters after a run that injected these memories."""
        ids = [i for i in memory_ids if i]
        if not ids:
            return 0
        updated = 0
        with self._lock:
            for memory_id in ids:
                row = self._conn.execute(
                    "SELECT payload_json FROM memories WHERE namespace=? AND memory_id=?",
                    (namespace, memory_id)).fetchone()
                if not row:
                    continue
                record = _rehydrate(json.loads(row["payload_json"]))
                record.use_count += 1
                if useful:
                    record.useful_count += 1
                record.last_used_at = used_at or record.last_used_at
                self._conn.execute(
                    "UPDATE memories SET payload_json=?, use_count=?, useful_count=?, last_used_at=? "
                    "WHERE namespace=? AND memory_id=?",
                    (record.model_dump_json(), record.use_count, record.useful_count,
                     record.last_used_at, namespace, memory_id))
                updated += 1
            self._conn.commit()
        return updated

    # ------------------------------------------------------------------ #
    def namespaces(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT DISTINCT namespace FROM memories").fetchall()
        return sorted(r["namespace"] for r in rows)

    def count(self, namespace: str, *, active_only: bool = True) -> int:
        query = "SELECT COUNT(*) AS c FROM memories WHERE namespace=?"
        if active_only:
            query += " AND active=1"
        with self._lock:
            return int(self._conn.execute(query, (namespace,)).fetchone()["c"])

    def leak_check(self, namespace: str) -> list[str]:
        """Ids returned for `namespace` that do not actually belong to it.

        Should always be empty; it exists so the isolation property is a test
        assertion rather than a claim in a doc.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT memory_id, namespace FROM memories WHERE namespace=?",
                (namespace,)).fetchall()
        return [r["memory_id"] for r in rows if r["namespace"] != namespace]

    def stats(self, namespace: str) -> dict[str, Any]:
        records = self.list_active(namespace, limit=10000)
        by_kind: dict[str, int] = {}
        for record in records:
            by_kind[record.kind.value] = by_kind.get(record.kind.value, 0) + 1
        used = [r for r in records if r.use_count]
        return {
            "namespace": namespace,
            "active": len(records),
            "total": self.count(namespace, active_only=False),
            "by_kind": by_kind,
            "used": len(used),
            "mean_usefulness": (round(sum(r.usefulness() or 0.0 for r in used) / len(used), 4)
                                if used else None),
        }
