"""Deep evaluation report (v3 stage J).

Usage:
    python scripts/build_deep_eval_report.py

Everything is computed from outputs/eval/eval_summary.csv at run time - no
hardcoded results. Percentiles need >=1 valid value; when a column has no
data the report prints "insufficient data" instead of inventing numbers.
"""
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from config import config  # noqa: E402


def _f(v) -> Optional[float]:
    try:
        if v in (None, "", "N/A"):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return round(s[idx], 3)


def _stats(values: list[Optional[float]]) -> dict[str, Any]:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"avg": "insufficient data", "p50": "-", "p90": "-", "p95": "-", "max": "-"}
    return {
        "avg": round(sum(clean) / len(clean), 3),
        "p50": _pct(clean, 50), "p90": _pct(clean, 90), "p95": _pct(clean, 95),
        "max": round(max(clean), 3),
    }


def main() -> None:
    csv_path = config.EVAL_DIR / "eval_summary.csv"
    if not csv_path.exists():
        print("[error] eval_summary.csv not found; run collect_eval_summary first")
        sys.exit(1)
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8", newline="")))
    n = len(rows)
    success = sum(1 for r in rows if r.get("success") == "True")
    total_times = [_f(r.get("total_time")) for r in rows]
    t_stats = _stats(total_times)

    lines = [
        "# Deep Evaluation Report",
        "",
        f"数据来源：`outputs/eval/eval_summary.csv`（{n} 个 eval topic，各取最新 run）。"
        "样本量为 10，P90/P95 参考意义有限，如实呈现不外推。",
        "",
        "## 总体",
        "",
        f"- total_cases: {n}",
        f"- success_rate: {round(success / n, 3) if n else 'insufficient data'}",
        f"- avg_source_count: {_stats([_f(r.get('source_count')) for r in rows])['avg']}",
        f"- avg_quality_score: {_stats([_f(r.get('quality_score')) for r in rows])['avg']}",
        f"- avg_number_grounding_rate: {_stats([_f(r.get('number_grounding_rate')) for r in rows])['avg']}",
        f"- avg tier1_or_tier2_ratio: {_stats([_f(r.get('tier1_or_tier2_ratio')) for r in rows])['avg']}",
        f"- total_time: avg {t_stats['avg']}s | P50 {t_stats['p50']}s | P90 {t_stats['p90']}s "
        f"| P95 {t_stats['p95']}s | max {t_stats['max']}s",
        "",
        "## 按 report_type 分组",
        "",
        "| report_type | cases | avg_quality | avg_sources | avg_grounding | avg_authority |",
        "|---|---|---|---|---|---|",
    ]

    groups = defaultdict(list)
    for r in rows:
        rt = r.get("eval_report_type")
        if not rt or rt == "N/A":
            rt = r.get("report_type") or "unknown"
        groups[rt].append(r)
    for rt, g in sorted(groups.items()):
        lines.append(
            f"| {rt} | {len(g)} | {_stats([_f(x.get('quality_score')) for x in g])['avg']} "
            f"| {_stats([_f(x.get('source_count')) for x in g])['avg']} "
            f"| {_stats([_f(x.get('number_grounding_rate')) for x in g])['avg']} "
            f"| {_stats([_f(x.get('authority_score')) for x in g])['avg']} |"
        )

    cap_counts = Counter(r.get("score_cap_reason", "N/A") or "none" for r in rows)
    issue_counts = Counter(r.get("main_issue", "N/A") for r in rows)
    lines += [
        "",
        "## score_cap_reason 分布",
        "",
        *(f"- {k}: {v}" for k, v in cap_counts.most_common()),
        "",
        "## main_issue 分布",
        "",
        *(f"- {k}: {v}" for k, v in issue_counts.most_common()),
        "",
    ]

    out = config.EVAL_DIR / "deep_eval_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Deep eval report saved to {out}")


if __name__ == "__main__":
    main()
