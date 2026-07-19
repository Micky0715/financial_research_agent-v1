"""Build a formal disclosure report from a pipeline run (v4 stage E).

Usage:
    python scripts/build_formal_report.py --latest
    python scripts/build_formal_report.py --run-id <id>

合规检查失败仍会产出，但文件名带 DRAFT_ 前缀、顶部有 DRAFT/INCOMPLETE 横幅。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from config import config  # noqa: E402
from tools.disclosure_builder import build_formal_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="正式披露研报构建")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args()

    run_id = args.run_id
    if args.latest or not run_id:
        traces = sorted(config.TRACES_DIR.glob("*_trace.json"), key=os.path.getmtime)
        if not traces:
            print("[error] no runs found")
            return 1
        run_id = traces[-1].name.split("_")[0]

    result = build_formal_report(run_id)
    if not result.get("success"):
        print(f"[error] {result.get('error')}")
        return 1
    status = "DRAFT/INCOMPLETE（未通过合规检查）" if result["is_draft"] else "PASS"
    print(f"run {run_id}: compliance={status}")
    if result["compliance"]["failures"]:
        for f in result["compliance"]["failures"]:
            print(f"  - 未通过: {f}: {result['compliance']['checks'][f]['detail']}")
    print(f"output: {result['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
