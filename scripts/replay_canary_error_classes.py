"""Re-classify the *recorded* errors from live runs against the current rules.

Why this exists: the live canary costs real money and takes minutes per task,
so a change to `src/runtime/errors.py` cannot be validated by re-running it.
But every `tool_call_failed` trace event stores the real `error_message`
verbatim, which means the classifier can be replayed offline over real
provider output - not fixtures - for free.

That is exactly how the `empty_content_after_cleaning` rule was validated: the
canary produced 5 UNKNOWN failures, this replay confirmed all 5 become
PERMANENT_FAILURE under the new rule and that the other 10 recorded failures
keep their original class (no shadowing).

Two honest limits:
  * it replays *messages*, so it cannot catch a rule that should have keyed off
    the exception *type* (type names are not persisted);
  * a run recorded before a rule change still shows its original class in its
    own trace. This script reports the delta; it does not rewrite history.

    python scripts/replay_canary_error_classes.py
    python scripts/replay_canary_error_classes.py --results evals/results/live_canary.jsonl
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from src.runtime.errors import classify_exception  # noqa: E402


class _RecordedError(Exception):
    """Carries a recorded message so `classify_exception` sees a real str()."""


def _repo_relative(path: Path | str) -> str:
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return Path(path).name


def collect_failures(results_path: Path) -> list[dict[str, Any]]:
    """Every recorded tool failure, with its original class and message."""
    if not results_path.exists():
        return []
    failures: list[dict[str, Any]] = []
    for line in results_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        trace = row.get("trace_path")
        if not trace:
            continue
        # Pointers are stored repo-relative, so resolve against the repo root
        # rather than the caller's working directory.
        trace = Path(trace)
        if not trace.is_absolute():
            trace = REPO_ROOT / trace
        if not trace.exists():
            continue
        for event_line in Path(trace).read_text(encoding="utf-8").splitlines():
            if not event_line.strip():
                continue
            try:
                event = json.loads(event_line)
            except json.JSONDecodeError:
                continue
            if event.get("event_type") != "tool_call_failed":
                continue
            failures.append({
                "task_id": row.get("task_id", ""),
                "tool": event.get("name", ""),
                "recorded_class": event.get("error_class"),
                "message": event.get("error_message") or "",
                "trace": _repo_relative(trace),
            })
    return failures


def replay(failures: list[dict[str, Any]]) -> dict[str, Any]:
    unchanged, reclassified, unclassifiable = [], [], []
    for failure in failures:
        message = failure["message"]
        if not message:
            # No message persisted: the classifier has nothing to work from, so
            # this row is *not* evidence either way. Counting it as "unchanged"
            # would inflate the agreement rate.
            unclassifiable.append(failure)
            continue
        current = classify_exception(_RecordedError(message)).value
        entry = dict(failure, current_class=current)
        (reclassified if current != failure["recorded_class"] else unchanged).append(entry)

    return {
        "total_failures": len(failures),
        "replayable": len(unchanged) + len(reclassified),
        "unclassifiable_no_message": len(unclassifiable),
        "recorded_distribution": dict(collections.Counter(
            f["recorded_class"] for f in failures if f["message"])),
        "current_distribution": dict(collections.Counter(
            [f["current_class"] for f in unchanged + reclassified])),
        "reclassified": reclassified,
        "distinct_messages": [
            {"count": c, "message": m}
            for m, c in collections.Counter(
                f["message"] for f in failures if f["message"]).most_common()
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", default="evals/results/live_canary.jsonl")
    parser.add_argument("--out", default="docs/_evidence/canary_error_replay.json")
    args = parser.parse_args()

    results_path = REPO_ROOT / args.results if not Path(args.results).is_absolute() else Path(args.results)
    failures = collect_failures(results_path)
    if not failures:
        print(f"no recorded tool failures found in {_repo_relative(results_path)}")
        return 1

    report = replay(failures)
    report["source_results"] = _repo_relative(results_path)

    out_path = REPO_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"source          : {report['source_results']}")
    print(f"failures        : {report['total_failures']} "
          f"(replayable {report['replayable']}, "
          f"no message {report['unclassifiable_no_message']})")
    print(f"recorded classes: {report['recorded_distribution']}")
    print(f"current  classes: {report['current_distribution']}")
    print(f"reclassified    : {len(report['reclassified'])}")
    for entry in report["reclassified"]:
        print(f"  {entry['task_id']:18s} {entry['tool']:14s} "
              f"{entry['recorded_class']} -> {entry['current_class']:18s} "
              f"{entry['message'][:60]!r}")
    print("distinct real messages:")
    for item in report["distinct_messages"]:
        print(f"  {item['count']:>2}x {item['message'][:96]!r}")
    print(f"wrote {_repo_relative(out_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
