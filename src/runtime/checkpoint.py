"""Persistence: run index, checkpoints and resume decisions.

Storage is SQLite from the standard library. Not Redis/Postgres/Kafka: this is
a single-process research pipeline whose entire state fits in a few hundred KB
per run, and a dependency on a running server would make the project
undemoable. The three stores are interfaces (`RunStore`, `CheckpointStore`,
`ArtifactStore`) so a hosted backend can be dropped in without touching the
runner.

Resume semantics - the part that actually matters:

- A checkpoint is written at every phase boundary and after every research
  sub-task, so at most one phase of work is lost.
- On resume, the *latest* checkpoint whose schema major version matches is
  loaded. A mismatched version is refused, loudly. Silently reinterpreting an
  old checkpoint is how a resume produces a subtly wrong report.
- Steps that already SUCCEEDED and are idempotent are skipped, and their
  recorded output is reused (`STEP_SKIPPED_IDEMPOTENT` in the trace).
- Steps left RUNNING - the process died mid-call, outcome unknown - are
  re-run **only if side-effect free**. A non-idempotent step in unknown state
  is surfaced for human decision rather than guessed at.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol

from src.observability.trace_store import ArtifactStore  # re-exported
from src.runtime.state import (
    SCHEMA_VERSION,
    Phase,
    RunState,
    RunStatus,
    StepRecord,
    StepStatus,
)

__all__ = [
    "ArtifactStore", "RunStore", "CheckpointStore", "SqliteRunStore",
    "SqliteCheckpointStore", "ResumePlan", "plan_resume", "open_stores",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    topic TEXT, report_type TEXT, namespace TEXT, user_id TEXT,
    status TEXT, phase TEXT, stop_reason TEXT,
    config_hash TEXT, schema_version TEXT,
    started_at TEXT, updated_at TEXT, ended_at TEXT,
    parent_run_id TEXT, trace_path TEXT, report_path TEXT,
    summary_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_topic ON runs(topic);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    phase TEXT, schema_version TEXT, created_at TEXT,
    state_json TEXT NOT NULL,
    PRIMARY KEY (run_id, version)
);
CREATE INDEX IF NOT EXISTS idx_ckpt_run ON checkpoints(run_id, version DESC);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the executor and its worker threads share one
    # connection, serialized by the store's own lock.
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


class RunStore(Protocol):
    def upsert(self, state: RunState) -> None: ...
    def get(self, run_id: str) -> Optional[dict[str, Any]]: ...
    def list_runs(self, *, limit: int = 50, status: Optional[str] = None) -> list[dict[str, Any]]: ...


class CheckpointStore(Protocol):
    def save(self, state: RunState) -> int: ...
    def load_latest(self, run_id: str) -> Optional[RunState]: ...
    def versions(self, run_id: str) -> list[int]: ...


class _SqliteBase:
    def __init__(self, db_path: Path, conn: Optional[sqlite3.Connection] = None) -> None:
        self.db_path = Path(db_path)
        self._conn = conn or _connect(self.db_path)
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SqliteRunStore(_SqliteBase):
    """Index of every run: what it was, how it ended, where its artifacts are."""

    def upsert(self, state: RunState) -> None:
        summary = {
            "num_sources": state.outcome.num_sources,
            "quality_score": state.outcome.quality_score,
            "evidence_count": len(state.evidence),
            "step_count": len(state.steps),
            "budget": state.budget.model_dump(mode="json"),
            "degradations": state.outcome.degradations,
            "checkpoint_version": state.checkpoint_version,
        }
        with self._lock:
            self._conn.execute(
                """INSERT INTO runs (run_id, topic, report_type, namespace, user_id, status,
                        phase, stop_reason, config_hash, schema_version, started_at, updated_at,
                        ended_at, parent_run_id, trace_path, report_path, summary_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id) DO UPDATE SET
                        status=excluded.status, phase=excluded.phase,
                        stop_reason=excluded.stop_reason, updated_at=excluded.updated_at,
                        ended_at=excluded.ended_at, trace_path=excluded.trace_path,
                        report_path=excluded.report_path, summary_json=excluded.summary_json""",
                (state.run_id, state.topic, state.report_type, state.namespace, state.user_id,
                 state.status.value, state.phase.value, state.outcome.stop_reason.value,
                 state.config_hash, state.schema_version, state.started_at, state.updated_at,
                 state.ended_at, state.parent_run_id, state.outcome.trace_path,
                 state.outcome.report_path, json.dumps(summary, ensure_ascii=False, default=str)),
            )
            self._conn.commit()

    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def list_runs(self, *, limit: int = 50, status: Optional[str] = None,
                  topic: Optional[str] = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM runs"
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if topic:
            clauses.append("topic=?")
            params.append(topic)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def resumable(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Runs that never reached a terminal status - candidates for --resume."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                (RunStatus.RUNNING.value, limit),
            ).fetchall()
        return [dict(r) for r in rows]


class SqliteCheckpointStore(_SqliteBase):
    """Versioned full-state snapshots, one row per checkpoint."""

    def save(self, state: RunState) -> int:
        payload = state.to_checkpoint()
        version = state.checkpoint_version
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO checkpoints
                   (run_id, version, phase, schema_version, created_at, state_json)
                   VALUES (?,?,?,?,?,?)""",
                (state.run_id, version, state.phase.value, state.schema_version,
                 state.updated_at, json.dumps(payload, ensure_ascii=False, default=str)),
            )
            self._conn.commit()
        return version

    def load_latest(self, run_id: str) -> Optional[RunState]:
        """Newest compatible checkpoint, or None.

        Walks backwards so a single corrupt or version-mismatched tail row does
        not make an otherwise resumable run unrecoverable.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, state_json FROM checkpoints WHERE run_id=? ORDER BY version DESC",
                (run_id,),
            ).fetchall()
        errors: list[str] = []
        for row in rows:
            try:
                return RunState.from_checkpoint(json.loads(row["state_json"]))
            except (ValueError, json.JSONDecodeError) as exc:
                errors.append(f"v{row['version']}: {exc}")
                continue
        if errors:
            raise ValueError(
                f"no compatible checkpoint for run {run_id} "
                f"(runtime schema {SCHEMA_VERSION}); tried: {'; '.join(errors[:3])}"
            )
        return None

    def load_version(self, run_id: str, version: int) -> Optional[RunState]:
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM checkpoints WHERE run_id=? AND version=?",
                (run_id, version),
            ).fetchone()
        return RunState.from_checkpoint(json.loads(row["state_json"])) if row else None

    def versions(self, run_id: str) -> list[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT version FROM checkpoints WHERE run_id=? ORDER BY version", (run_id,),
            ).fetchall()
        return [int(r["version"]) for r in rows]

    def prune(self, run_id: str, keep_last: int = 10) -> int:
        """Drop all but the newest `keep_last` checkpoints for a run."""
        versions = self.versions(run_id)
        if len(versions) <= keep_last:
            return 0
        doomed = versions[: len(versions) - keep_last]
        with self._lock:
            self._conn.executemany(
                "DELETE FROM checkpoints WHERE run_id=? AND version=?",
                [(run_id, v) for v in doomed],
            )
            self._conn.commit()
        return len(doomed)


# --------------------------------------------------------------------------- #
# Resume planning
# --------------------------------------------------------------------------- #
class ResumePlan:
    """What a resume will actually do, computed before anything is executed."""

    def __init__(self, state: RunState) -> None:
        self.state = state
        self.resume_phase: Phase = state.phase
        self.skip_step_keys: set[str] = set()
        self.rerun_step_ids: list[str] = []
        self.needs_human: list[dict[str, Any]] = []
        self.reused_evidence: int = len(state.evidence)

    def describe(self) -> dict[str, Any]:
        return {
            "run_id": self.state.run_id,
            "resume_phase": self.resume_phase.value,
            "checkpoint_version": self.state.checkpoint_version,
            "skippable_steps": len(self.skip_step_keys),
            "steps_to_rerun": len(self.rerun_step_ids),
            "needs_human_decision": self.needs_human,
            "reused_evidence": self.reused_evidence,
            "budget_already_spent": self.state.budget.model_dump(mode="json"),
        }


def plan_resume(state: RunState) -> ResumePlan:
    """Decide, per step, whether to skip, re-run, or escalate.

    Three cases:

    - SUCCEEDED + idempotency key -> skip, reuse the recorded output.
    - RUNNING / PENDING + side-effect free -> safe to re-run.
    - RUNNING + has side effects -> unknown external state. Escalate. This is
      the case a naive resume gets wrong by writing the file twice.
    """
    plan = ResumePlan(state)
    for step in state.steps:
        if step.status == StepStatus.SUCCEEDED and step.idempotency_key:
            plan.skip_step_keys.add(step.idempotency_key)
        elif step.status in (StepStatus.RUNNING, StepStatus.PENDING):
            if step.side_effect_free:
                plan.rerun_step_ids.append(step.step_id)
            else:
                plan.needs_human.append({
                    "step_id": step.step_id, "name": step.name, "phase": step.phase.value,
                    "reason": "non-idempotent step was in-flight when the run stopped; "
                              "outcome unknown, manual confirmation required before retry",
                })
    return plan


def should_skip(state: RunState, idempotency_key: str) -> Optional[StepRecord]:
    """Reusable output for an already-completed idempotent step, else None."""
    return state.completed_step(idempotency_key)


def open_stores(base_dir: Path, db_name: str = "harness.db") -> tuple[SqliteRunStore, SqliteCheckpointStore, ArtifactStore]:
    """Open all three stores against one SQLite file plus an artifact directory."""
    base = Path(base_dir)
    db_path = base / db_name
    conn = _connect(db_path)
    return (
        SqliteRunStore(db_path, conn=conn),
        SqliteCheckpointStore(db_path, conn=conn),
        ArtifactStore(base / "artifacts"),
    )


def iter_checkpoints(store: SqliteCheckpointStore, run_id: str) -> Iterator[RunState]:
    for version in store.versions(run_id):
        state = store.load_version(run_id, version)
        if state is not None:
            yield state
