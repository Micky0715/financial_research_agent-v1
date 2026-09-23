"""Rewrite absolute machine paths in eval result files to repo-relative ones.

Why this exists: `trace_path` / `report_path` were stored as absolute paths
(a full drive-letter path under the producing user's home directory). Committing that leaks a username and, worse,
points every reader's tooling at a directory that does not exist on their
machine - `scripts/replay_canary_error_classes.py` silently finds no traces and
reports nothing to replay.

What it touches: **only path-pointer fields**. Scores, metrics, grades, error
classes, stop reasons and every other measurement stay byte-identical. The
script verifies that itself (`--check`) by diffing every record with the path
fields removed, and refuses to write if anything else moved.

    python scripts/normalize_result_paths.py --dry-run   # report only
    python scripts/normalize_result_paths.py             # rewrite in place
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS = REPO_ROOT / "evals" / "results"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

#: Explicit pointer fields, plus a suffix rule so a field added later is
#: covered without editing this list. Enumerating names by hand is how
#: `sources_path` and `evaluation_path` were missed on the first pass.
PATH_FIELDS = ("trace_path", "report_path", "results_path", "base_dir",
               "harness_trace_path", "checkpoint_path", "artifact_dir")
PATH_SUFFIXES = ("_path", "_dir", "_file")


def is_path_field(key: Any) -> bool:
    return isinstance(key, str) and (
        key in PATH_FIELDS or key.endswith(PATH_SUFFIXES))


def to_repo_relative(value: Any) -> Any:
    """Return `value` as a POSIX repo-relative path, or unchanged if it is not
    an absolute path inside this repository."""
    if not isinstance(value, str) or not value:
        return value
    candidate = Path(value.replace("\\", "/"))
    if not candidate.is_absolute():
        return value
    try:
        return candidate.resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        # Absolute but outside the repo: keep the basename only, never the
        # full path - it would still leak the directory layout.
        return candidate.name


def normalize_record(record: Any) -> tuple[Any, int]:
    """Walk the whole structure: pointers also appear nested (for example
    `failure_report.json` stores `trace_path` inside each mined failure)."""
    changed = 0
    if isinstance(record, dict):
        out: Any = {}
        for key, value in record.items():
            if is_path_field(key):
                new_value = to_repo_relative(value)
                if new_value != value:
                    changed += 1
                out[key] = new_value
            else:
                out[key], n = normalize_record(value)
                changed += n
        return out, changed
    if isinstance(record, list):
        out_list = []
        for item in record:
            new_item, n = normalize_record(item)
            out_list.append(new_item)
            changed += n
        return out_list, changed
    return record, changed


def _measurement_only(record: Any) -> Any:
    """The record with every path pointer stripped - what must not change."""
    if isinstance(record, dict):
        return {k: _measurement_only(v) for k, v in record.items() if not is_path_field(k)}
    if isinstance(record, list):
        return [_measurement_only(v) for v in record]
    return record


def process(path: Path, write: bool) -> tuple[int, int, list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    changed_fields = 0
    changed_rows = 0
    problems: list[str] = []

    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            out_lines.append(line)
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name}:{lineno} unparseable ({exc}); left untouched")
            out_lines.append(line)
            continue

        new_record, n = normalize_record(record)
        if n:
            changed_rows += 1
            changed_fields += n
            # Guard: nothing outside PATH_FIELDS may differ.
            if _measurement_only(new_record) != _measurement_only(record):
                problems.append(f"{path.name}:{lineno} measurement content would change - ABORT")
        out_lines.append(json.dumps(new_record, ensure_ascii=False, default=str))

    if write and changed_fields and not problems:
        path.write_text("\n".join(out_lines) + "\n", encoding="utf-8", newline="\n")
    return changed_rows, changed_fields, problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    files = sorted(RESULTS.glob("*.jsonl"))
    # Side-car JSON carries the same pointers: `*.meta.json` stores
    # `results_path`, and the mined `failure_report.json` nests `trace_path`
    # inside every failure record.
    json_files = (sorted(RESULTS.glob("*.meta.json"))
                  + sorted((REPO_ROOT / "outputs" / "optimization").glob("*.json"))
                  # v4 eval outputs are already tracked in git and carry the
                  # same absolute `report_path` pointers.
                  + sorted((REPO_ROOT / "outputs" / "eval").glob("*.json")))
    if not files:
        print(f"no result files under {RESULTS.relative_to(REPO_ROOT).as_posix()}")
        return 1

    total_rows = total_fields = 0
    all_problems: list[str] = []
    for path in files:
        rows, fields, problems = process(path, write=not args.dry_run)
        all_problems.extend(problems)
        total_rows += rows
        total_fields += fields
        if fields:
            print(f"  {fields:6d} fields in {rows:5d} rows   "
                  f"{path.relative_to(REPO_ROOT).as_posix()}")

    for path in json_files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        new_payload, n = normalize_record(payload)
        if not n:
            continue
        if _measurement_only(new_payload) != _measurement_only(payload):
            all_problems.append(f"{path.name} measurement content would change - ABORT")
            continue
        total_fields += n
        print(f"  {n:6d} fields              {path.relative_to(REPO_ROOT).as_posix()}")
        if not args.dry_run:
            path.write_text(json.dumps(new_payload, ensure_ascii=False, indent=2),
                            encoding="utf-8", newline="")
    print(f"{total_fields} path fields across {total_rows} rows in {len(files) + len(json_files)} files "
          f"({'dry run - nothing written' if args.dry_run else 'rewritten'})")
    if all_problems:
        print("\nPROBLEMS (nothing written for the affected files):")
        for p in all_problems:
            print(f"  {p}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
