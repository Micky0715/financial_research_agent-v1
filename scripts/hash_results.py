"""Record sha256 + line count for every eval result file.

Reports quote these hashes so a reader can tell whether the numbers in a
document were computed from the file currently on disk. Previously the file was
produced by an ad-hoc snippet, which meant it silently went stale whenever a
split was re-run - and a stale hash is worse than no hash, because it looks
like provenance.

    python scripts/hash_results.py            # refresh docs/_evidence/result_hashes.json
    python scripts/hash_results.py --check    # exit 1 if any recorded hash is stale
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS = REPO_ROOT / "evals" / "results"
OUT = REPO_ROOT / "docs" / "_evidence" / "result_hashes.json"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def digest(path: Path) -> dict[str, object]:
    sha = hashlib.sha256()
    lines = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
            lines += chunk.count(b"\n")
    return {"sha256": sha.hexdigest(), "lines": lines}


def collect() -> dict[str, dict[str, object]]:
    return {
        path.relative_to(REPO_ROOT).as_posix(): digest(path)
        for path in sorted(RESULTS.glob("*.jsonl"))
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify recorded hashes without rewriting the file")
    args = parser.parse_args()

    current = collect()
    if args.check:
        if not OUT.exists():
            print(f"missing {OUT.relative_to(REPO_ROOT).as_posix()}")
            return 1
        recorded = json.loads(OUT.read_text(encoding="utf-8"))
        stale = [name for name, info in recorded.items()
                 if name in current and current[name]["sha256"] != info.get("sha256")]
        missing = [name for name in recorded if name not in current]
        for name in stale:
            print(f"STALE   {name}")
        for name in missing:
            print(f"MISSING {name}  (recorded but no longer on disk)")
        untracked = [n for n in current if n not in recorded]
        for name in untracked:
            print(f"NEW     {name}  (on disk but not recorded)")
        print(f"{len(recorded)} recorded, {len(stale)} stale, {len(missing)} missing, "
              f"{len(untracked)} untracked")
        return 1 if (stale or missing) else 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {OUT.relative_to(REPO_ROOT).as_posix()} ({len(current)} files)")
    for name, info in current.items():
        print(f"  {info['sha256'][:16]}  {info['lines']:>5} lines  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
