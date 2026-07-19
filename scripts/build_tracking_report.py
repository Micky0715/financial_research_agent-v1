"""Build a quarterly/annual tracking report for a listed company (v4 stage D).

Usage:
    python scripts/build_tracking_report.py --symbol 002594 --period annual
    python scripts/build_tracking_report.py --symbol 600519 --period quarterly
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from tracking.tracking_report_builder import build_tracking_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="季度/年度跟踪报告")
    parser.add_argument("--symbol", required=True, help="股票代码或公司名，如 002594 / 比亚迪")
    parser.add_argument("--period", default="annual", choices=["annual", "quarterly"])
    args = parser.parse_args()

    result = build_tracking_report(args.symbol, args.period)
    if result.insufficient_history:
        print(f"[insufficient_history] {result.entity}: {result.limitation}")
        return 1
    print(f"entity: {result.entity}({result.symbol}) {result.period_type}")
    print(f"current={result.current_period} previous={result.previous_period}")
    print(f"changes={len(result.changes)} insufficient_dimensions={len(result.insufficient_dimensions)}")
    print(f"prior_record_compared={result.prior_record_compared}")
    print(f"report: {result.report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
