"""Version store for improvement proposals.

Records every candidate, its gate result, and its lifecycle state. Nothing here
mutates production configuration: a version is a *file*, and applying it is a
separate, human action. `apply_version()` exists but refuses to run on a
version that has not passed the gate and has not been explicitly approved by a
named person - and even then it only writes an overlay file that a human must
wire in.

Lifecycle:

    proposed --gate passed--> gated --human approval--> approved --applied--> applied
         \\--gate failed--> rejected
    approved/applied --rollback--> rolled_back
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.optimization.candidate_generator import Candidate
from src.optimization.regression_gate import GateResult


class VersionState(str, Enum):
    PROPOSED = "proposed"
    GATED = "gated"            # passed the gate, awaiting human approval
    REJECTED = "rejected"      # failed the gate
    APPROVED = "approved"      # a person signed off
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"


class VersionRecord(BaseModel):
    version: str
    candidate: Candidate
    gate_result: Optional[GateResult] = None
    state: VersionState = VersionState.PROPOSED
    created_at: str = ""
    updated_at: str = ""
    approved_by: str = ""
    approved_at: str = ""
    applied_at: str = ""
    notes: str = ""
    #: The value each overridden setting had before this version was applied,
    #: so a rollback needs nothing but this record.
    previous_values: dict[str, Any] = Field(default_factory=dict)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class VersionStore:
    """Filesystem-backed. One JSON per version plus an index."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"

    # ------------------------------------------------------------------ #
    def _index(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []

    def _write_index(self, entries: list[dict[str, Any]]) -> None:
        self.index_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    def next_version(self) -> str:
        """Sequential `vNNN`, independent of the repo's own release numbering."""
        existing = [e.get("version", "") for e in self._index()]
        numbers = [int(v[1:]) for v in existing if v.startswith("v") and v[1:].isdigit()]
        return f"v{max(numbers, default=0) + 1:03d}"

    def _path(self, version: str) -> Path:
        return self.root / f"{version}.json"

    # ------------------------------------------------------------------ #
    def propose(self, candidate: Candidate, notes: str = "") -> VersionRecord:
        version = self.next_version()
        candidate.finalize(version)
        record = VersionRecord(version=version, candidate=candidate,
                               state=VersionState.PROPOSED,
                               created_at=_now(), updated_at=_now(), notes=notes)
        self._save(record)
        return record

    def record_gate(self, version: str, gate_result: GateResult) -> VersionRecord:
        record = self.get(version)
        if record is None:
            raise ValueError(f"unknown version {version!r}")
        gate_result.version = version
        record.gate_result = gate_result
        record.state = VersionState.GATED if gate_result.passed else VersionState.REJECTED
        record.updated_at = _now()
        self._save(record)
        return record

    def approve(self, version: str, approved_by: str) -> VersionRecord:
        """Human sign-off. Refuses anything that did not pass the gate."""
        record = self.get(version)
        if record is None:
            raise ValueError(f"unknown version {version!r}")
        if not approved_by:
            raise ValueError("approval requires a named approver")
        if record.state is not VersionState.GATED:
            raise ValueError(
                f"version {version} is in state {record.state.value!r}; only a version that "
                "passed the regression gate (state='gated') can be approved")
        record.state = VersionState.APPROVED
        record.approved_by = approved_by
        record.approved_at = _now()
        record.updated_at = _now()
        self._save(record)
        return record

    def apply_version(self, version: str, *, out_dir: Optional[Path] = None) -> Path:
        """Write an *overlay file* for an approved version.

        This is as far as automation goes. The overlay is a JSON file of
        setting overrides; wiring it into the running configuration is a
        deliberate human step, which is why nothing here touches `config.py`,
        `prompts/`, or any registered tool spec.
        """
        record = self.get(version)
        if record is None:
            raise ValueError(f"unknown version {version!r}")
        if record.state is not VersionState.APPROVED:
            raise ValueError(
                f"version {version} is {record.state.value!r}; only an approved version may be "
                "materialised, and even then only as an overlay a human applies")

        out_dir = Path(out_dir or (self.root / "overlays"))
        out_dir.mkdir(parents=True, exist_ok=True)
        overlay_path = out_dir / f"{version}.overlay.json"
        overlay_path.write_text(json.dumps({
            "version": version,
            "candidate_id": record.candidate.candidate_id,
            "change_type": record.candidate.change_type.value,
            "target": record.candidate.target,
            "config_overrides": record.candidate.config_overrides,
            "text_change": {"before": record.candidate.before, "after": record.candidate.after},
            "approved_by": record.approved_by,
            "note": "这是待人工应用的覆盖文件，系统不会自动加载它。",
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        record.state = VersionState.APPLIED
        record.applied_at = _now()
        record.updated_at = _now()
        self._save(record)
        return overlay_path

    def rollback(self, version: str, reason: str = "") -> VersionRecord:
        record = self.get(version)
        if record is None:
            raise ValueError(f"unknown version {version!r}")
        record.state = VersionState.ROLLED_BACK
        record.notes = (record.notes + f"\nrolled back: {reason}").strip()
        record.updated_at = _now()
        self._save(record)
        return record

    # ------------------------------------------------------------------ #
    def get(self, version: str) -> Optional[VersionRecord]:
        path = self._path(version)
        if not path.exists():
            return None
        return VersionRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def list_versions(self, *, state: Optional[VersionState] = None) -> list[VersionRecord]:
        records = [self.get(e["version"]) for e in self._index()]
        records = [r for r in records if r is not None]
        if state:
            records = [r for r in records if r.state is state]
        return sorted(records, key=lambda r: r.version)

    def _save(self, record: VersionRecord) -> None:
        self._path(record.version).write_text(record.model_dump_json(indent=2), encoding="utf-8")
        entries = [e for e in self._index() if e.get("version") != record.version]
        entries.append({
            "version": record.version, "state": record.state.value,
            "candidate_id": record.candidate.candidate_id,
            "target_failure_mode": record.candidate.target_failure_mode.value,
            "change_type": record.candidate.change_type.value,
            "target": record.candidate.target,
            "gate_passed": bool(record.gate_result and record.gate_result.passed),
            "updated_at": record.updated_at,
        })
        self._write_index(sorted(entries, key=lambda e: e["version"]))

    def summary(self) -> dict[str, Any]:
        records = self.list_versions()
        by_state: dict[str, int] = {}
        for record in records:
            by_state[record.state.value] = by_state.get(record.state.value, 0) + 1
        return {
            "total": len(records), "by_state": by_state,
            "applied": [r.version for r in records if r.state is VersionState.APPLIED],
            "awaiting_approval": [r.version for r in records if r.state is VersionState.GATED],
        }
