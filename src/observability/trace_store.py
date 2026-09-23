"""JSONL trace writer/reader plus the artifact store for oversized payloads.

Storage choice: append-only JSONL on local disk. Not SQLite for the event
stream, because appends from several threads are the common case and a crash
mid-write costs at most the tail line, which the reader skips. Not an external
observability platform, because that would make the project undemoable without
network credentials - a platform adapter is an optional add-on
(`ExternalTraceAdapter`), never the default.

The artifact store is what keeps the stream small: a 40 KB page body is written
once under a content hash, and every event referring to it carries a 16-char
id instead of the body.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from src.observability.events import (
    EventType,
    TraceEvent,
    bound_payload,
    make_event,
    redact,
)
from src.runtime.state import content_hash


class ArtifactStore:
    """Content-addressed local blob store for anything too big for the trace.

    Writes are idempotent by construction: the id *is* the content hash, so
    storing the same page twice costs one file, and a crashed run that reruns
    a fetch does not duplicate data.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, artifact_id: str, suffix: str = ".json") -> Path:
        # Two-level fan-out keeps directory listings usable after a few
        # thousand artifacts on Windows.
        return self.root / artifact_id[:2] / f"{artifact_id}{suffix}"

    def put(self, payload: Any, *, kind: str = "generic") -> str:
        """Store `payload`, returning its artifact id."""
        artifact_id = content_hash({"kind": kind, "payload": payload})
        path = self._path(artifact_id)
        if path.exists():
            return artifact_id
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"artifact_id": artifact_id, "kind": kind, "payload": payload}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, default=str), encoding="utf-8")
        os.replace(tmp, path)
        return artifact_id

    def get(self, artifact_id: str) -> Optional[Any]:
        path = self._path(artifact_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("payload")
        except (OSError, json.JSONDecodeError):
            return None

    def exists(self, artifact_id: str) -> bool:
        return self._path(artifact_id).exists()

    def count(self) -> int:
        return sum(1 for _ in self.root.rglob("*.json"))


class TraceStore:
    """Append-only JSONL event stream for one run.

    Thread-safe: the executor emits from ResearchAgent's worker threads. Every
    event is redacted before it is serialized, so a caller cannot leak a secret
    by forgetting to sanitize.
    """

    #: Payload values longer than this get externalized into the ArtifactStore.
    EXTERNALIZE_OVER_CHARS = 4000

    def __init__(self, path: Path, artifacts: Optional[ArtifactStore] = None,
                 run_id: str = "", autoflush: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts = artifacts
        self.run_id = run_id
        self.autoflush = autoflush
        self._lock = threading.Lock()
        self._seq = 0
        self._closed = False
        self._buffer: list[str] = []
        self._adapters: list["ExternalTraceAdapter"] = []

    # ------------------------------------------------------------------ #
    def add_adapter(self, adapter: "ExternalTraceAdapter") -> None:
        """Attach an optional external sink. Never replaces the local JSONL."""
        self._adapters.append(adapter)

    def _externalize(self, event: TraceEvent) -> TraceEvent:
        """Move oversized payload values into the artifact store.

        With no artifact store attached there is nowhere to put them, so they
        are truncated instead - the only case where trace data is lost, and
        only for in-memory/test stores.
        """
        if self.artifacts is None:
            event.payload = bound_payload(event.payload)
            return event
        oversized = {
            k: v for k, v in event.payload.items()
            if isinstance(v, str) and len(v) > self.EXTERNALIZE_OVER_CHARS
        }
        if not oversized:
            return event
        artifact_id = self.artifacts.put(oversized, kind=f"{event.event_type.value}:payload")
        for key, value in oversized.items():
            event.payload[key] = f"[externalized {len(value)} chars -> artifact:{artifact_id}]"
        event.artifact_id = event.artifact_id or artifact_id
        return event

    def emit(self, event: TraceEvent) -> TraceEvent:
        """Redact, externalize, number and append one event."""
        with self._lock:
            if self._closed:
                return event
            self._seq += 1
            event.seq = self._seq
            if not event.run_id:
                event.run_id = self.run_id
            event = self._externalize(event.redacted())
            line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, default=str)
            self._buffer.append(line)
            if self.autoflush:
                self._flush_locked()
        for adapter in self._adapters:
            try:
                adapter.send(event)
            except Exception:  # noqa: BLE001 - an external sink must never break a run
                pass
        return event

    def event(self, event_type: EventType, **kwargs: Any) -> TraceEvent:
        """Convenience: build and emit in one call."""
        return self.emit(make_event(event_type, run_id=self.run_id, **kwargs))

    def _flush_locked(self) -> None:
        if not self._buffer:
            return
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(self._buffer) + "\n")
        self._buffer.clear()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        with self._lock:
            self._flush_locked()
            self._closed = True

    def __enter__(self) -> "TraceStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    @staticmethod
    def read(path: Path) -> list[dict[str, Any]]:
        """Read a trace file, skipping malformed lines (e.g. a killed writer's tail)."""
        events: list[dict[str, Any]] = []
        p = Path(path)
        if not p.exists():
            return events
        with p.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    @staticmethod
    def iter_dir(directory: Path, pattern: str = "*.jsonl") -> Iterator[tuple[Path, list[dict[str, Any]]]]:
        for path in sorted(Path(directory).glob(pattern)):
            yield path, TraceStore.read(path)


class ExternalTraceAdapter:
    """Optional sink (Langfuse/OTel/whatever). Base class is a no-op.

    Deliberately not wired to any vendor: the project must stay runnable with
    zero external accounts. Subclass and `add_adapter()` if you want one.
    """

    name = "noop"

    def send(self, event: TraceEvent) -> None:  # pragma: no cover - base no-op
        return None


class InMemoryTraceStore(TraceStore):
    """TraceStore that keeps events in memory. Used by unit tests and by the
    replay harness, so tests never touch the filesystem."""

    def __init__(self, run_id: str = "test") -> None:
        import tempfile

        super().__init__(Path(tempfile.gettempdir()) / "_harness_unused.jsonl",
                         artifacts=None, run_id=run_id, autoflush=False)
        self.events: list[TraceEvent] = []

    def emit(self, event: TraceEvent) -> TraceEvent:
        with self._lock:
            self._seq += 1
            event.seq = self._seq
            if not event.run_id:
                event.run_id = self.run_id
            event = event.redacted()
            event.payload = bound_payload(event.payload)
            self.events.append(event)
        return event

    def _flush_locked(self) -> None:  # never touches disk
        self._buffer.clear()

    def of_type(self, event_type: EventType) -> list[TraceEvent]:
        return [e for e in self.events if e.event_type == event_type]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [e.model_dump(mode="json") for e in self.events]


def write_events(path: Path, events: Iterable[dict[str, Any]]) -> Path:
    """Write pre-built event dicts to a JSONL file (used by fixture tooling)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(json.dumps(redact(event), ensure_ascii=False, default=str) + "\n")
    return p
