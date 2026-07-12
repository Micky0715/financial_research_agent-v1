"""Search the local memory index (v3 stage I).

Usage:
    python scripts/search_memory.py --query "比亚迪估值分析"
    python scripts/search_memory.py --query "相关性误杀" --kind bad_case --top-k 3
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from memory.report_memory import search_memory  # noqa: E402

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Search the local memory index")
    parser.add_argument("--query", required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--kind", choices=["report_summary", "bad_case"], default=None)
    args = parser.parse_args()

    results = search_memory(args.query, top_k=args.top_k, kind=args.kind)
    if not results:
        print("(no results - run scripts/build_memory_index.py first?)")
    for r in results:
        print(f"[{r['score']:.3f}] ({r.get('kind')}) {r.get('topic')} "
              f"| quality={r.get('quality_score')} | {r.get('source_path', '')[-50:]}")
