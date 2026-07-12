"""Build the local memory index from existing outputs (v3 stage I).

Usage:
    python scripts/build_memory_index.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from memory.report_memory import build_index  # noqa: E402

if __name__ == "__main__":
    result = build_index()
    print(f"Memory index built: {result['records']} record(s) "
          f"({result['report_summaries']} report summaries, {result['bad_cases']} bad cases), "
          f"vector_mode={result['vector_mode']}")
