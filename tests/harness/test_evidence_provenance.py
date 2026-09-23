"""Guards that documentation cannot quote provenance it no longer has.

Two failure modes this catches:

  1. A doc quotes a sha256 for a result file that has since been re-run. The
     hash then looks like provenance while proving nothing - worse than
     quoting no hash at all.
  2. `docs/_evidence/result_hashes.json` drifts from the files on disk. It did:
     four entries were stale before `scripts/hash_results.py` existed, because
     the file had been produced by an ad-hoc snippet that nobody re-ran.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = REPO_ROOT / "docs" / "_evidence" / "result_hashes.json"
SHA256_RE = re.compile(r"\b[0-9a-f]{64}\b")

#: Docs whose quoted hashes must resolve to a real file's current content.
DOCS = ("README.md", "docs/resume_evidence.md", "docs/v1_to_harness_interview_guide.md",
        "docs/regression_rebuild.md", "docs/final_verification_audit.md")


def _sha256(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def test_recorded_result_hashes_match_the_files_on_disk():
    if not EVIDENCE.exists():
        pytest.skip("evidence file not built in this checkout")
    recorded = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    stale, missing = [], []
    for name, info in recorded.items():
        path = REPO_ROOT / name
        if not path.exists():
            missing.append(name)
            continue
        if _sha256(path) != info["sha256"]:
            stale.append(name)
    assert not stale, (
        f"stale hashes for {stale}; run `python scripts/hash_results.py` after re-running a split")
    assert not missing, f"recorded but absent: {missing}"


def test_every_sha256_quoted_in_docs_resolves_to_a_current_file():
    if not EVIDENCE.exists():
        pytest.skip("evidence file not built in this checkout")
    recorded = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    known = {info["sha256"] for info in recorded.values()}
    # Report generators also stamp the hash of the results they read.
    for report in (REPO_ROOT / "evals" / "reports").glob("*.md"):
        known |= set(SHA256_RE.findall(report.read_text(encoding="utf-8")))

    orphans: list[tuple[str, str]] = []
    for doc in DOCS:
        path = REPO_ROOT / doc
        if not path.exists():
            continue
        for quoted in SHA256_RE.findall(path.read_text(encoding="utf-8")):
            if quoted not in known:
                orphans.append((doc, quoted))
    assert not orphans, (
        "docs quote sha256 values that match no current result file: "
        + ", ".join(f"{d}:{h[:16]}..." for d, h in orphans))


def test_the_hash_script_detects_a_modified_file(tmp_path):
    """Reverse check: the checker must actually fail on drift."""
    from scripts.hash_results import digest

    target = tmp_path / "r.jsonl"
    target.write_text('{"a":1}\n', encoding="utf-8")
    before = digest(target)
    target.write_text('{"a":2}\n', encoding="utf-8")
    after = digest(target)
    assert before["sha256"] != after["sha256"]
    assert before["lines"] == after["lines"] == 1


# --------------------------------------------------------------------------- #
# Committed evidence must not carry absolute machine paths
# --------------------------------------------------------------------------- #
_DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:(?:\\|\|/)")


def _files_git_would_commit():
    import subprocess
    for args in (["git", "ls-files"], ["git", "ls-files", "--others", "--exclude-standard"]):
        out = subprocess.run(args, cwd=REPO_ROOT, capture_output=True,
                             text=True, encoding="utf-8")
        for name in out.stdout.splitlines():
            if name.strip():
                yield name.strip()


def test_result_files_store_repo_relative_pointers():
    """`trace_path` / `report_path` must never be absolute.

    An absolute pointer bakes the producing machine's username into a committed
    file and resolves to nothing on anyone else's checkout, so
    `scripts/replay_canary_error_classes.py` silently finds no traces and
    reports that there is nothing to replay - a false clean bill of health.

    Run `python scripts/normalize_result_paths.py` if this fails.
    """
    results = REPO_ROOT / "evals" / "results"
    if not results.exists():
        pytest.skip("no results in this checkout")

    offenders = []
    for path in sorted(results.glob("*.jsonl")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            for field in ("trace_path", "report_path"):
                value = row.get(field, "")
                if value and _DRIVE_PATH.match(str(value)):
                    offenders.append(f"{path.name}:{lineno}:{field}={value[:60]}")
    assert not offenders, (
        f"{len(offenders)} absolute pointer(s), first few: {offenders[:3]}")


def test_no_committed_file_contains_an_absolute_user_path():
    """Guards every file git would commit, not just the result files."""
    import subprocess

    try:
        next(iter(_files_git_would_commit()))
    except (StopIteration, FileNotFoundError, subprocess.SubprocessError):
        pytest.skip("git not available")

    text_suffixes = {".jsonl", ".json", ".md", ".py", ".txt", ".yml", ".yaml", ".cfg", ".toml"}
    offenders = []
    for name in _files_git_would_commit():
        path = REPO_ROOT / name
        if path.suffix.lower() not in text_suffixes or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Look for a drive-letter path followed by a user directory. The test
        # files that assert on redaction legitimately contain fake secrets but
        # never absolute paths.
        for match in _DRIVE_PATH.finditer(text):
            tail = text[match.end():match.end() + 6].lower().lstrip("\/")
            if tail.startswith("users"):
                offenders.append(f"{name} @ {match.start()}")
                break
    assert not offenders, f"absolute user paths in: {offenders[:5]}"
