"""Build a human-readable evaluation report from outputs/eval/eval_summary.csv.

Usage:
    python scripts/collect_eval_summary.py   # must run first
    python scripts/build_eval_report.py

outputs/eval/eval_summary.csv holds one row per eval/topics.json topic (that
topic's latest run only - see scripts/collect_eval_summary.py), so every
number below is computed over the current 10-topic eval suite, not raw
historical run counts. Nothing here is a hardcoded result - re-running
collect_eval_summary.py after new runs and then this script always reflects
the current state of outputs/traces.
"""
import csv
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config  # noqa: E402


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "" or value == "N/A":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value in ("True", "true", "1"):
        return True
    if value in ("False", "false", "0"):
        return False
    return None


def _avg(values: list[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 3) if clean else None


def _fmt(value: Optional[float], suffix: str = "") -> str:
    return f"{value}{suffix}" if value is not None else "N/A"


def load_rows(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.exists():
        print(f"[error] {csv_path} not found. Run `python scripts/collect_eval_summary.py` first.")
        sys.exit(1)
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def build_report(rows: list[dict[str, Any]]) -> str:
    total_cases = len(rows)
    success_flags = [_to_bool(r.get("success")) for r in rows]
    success_cases = sum(1 for s in success_flags if s)
    success_rate = round(success_cases / total_cases, 3) if total_cases else None

    source_counts = [_to_float(r.get("source_count")) for r in rows]
    quality_scores = [_to_float(r.get("quality_score")) for r in rows]
    total_times = [_to_float(r.get("total_time")) for r in rows]
    research_times = [_to_float(r.get("research_time")) for r in rows]
    browser_times = [_to_float(r.get("browser_time")) for r in rows]
    analyze_times = [_to_float(r.get("analyze_time")) for r in rows]
    report_times = [_to_float(r.get("report_time")) for r in rows]

    lines: list[str] = ["# Evaluation Report", ""]
    lines.append(
        f"Generated from `outputs/eval/eval_summary.csv` - {total_cases} eval topic(s) "
        "(latest run per topic)."
    )
    lines.append("")

    # 1. Overall Metrics
    lines += [
        "## 1. Overall Metrics",
        "",
        f"- total_cases: {total_cases}",
        f"- success_cases: {success_cases}",
        f"- success_rate: {_fmt(success_rate)}",
        f"- avg_source_count: {_fmt(_avg(source_counts))}",
        f"- avg_quality_score: {_fmt(_avg(quality_scores))}",
        f"- avg_total_time: {_fmt(_avg(total_times), 's')}",
        f"- avg_research_time: {_fmt(_avg(research_times), 's')}",
        f"- avg_browser_time: {_fmt(_avg(browser_times), 's')}",
        f"- avg_analyze_time: {_fmt(_avg(analyze_times), 's')}",
        f"- avg_report_time: {_fmt(_avg(report_times), 's')}",
        "",
    ]

    # 2. Stability Metrics
    normalized_counts = [_to_float(r.get("planner_normalized_task_count")) for r in rows]
    all_normalized_to_5 = all(c == 5.0 for c in normalized_counts if c is not None) and any(
        c is not None for c in normalized_counts
    )
    source_ge_5 = sum(1 for c in source_counts if c is not None and c >= 5)
    source_lt_3 = sum(1 for c in source_counts if c is not None and c < 3)
    quality_lt_06 = sum(1 for q in quality_scores if q is not None and q < 0.6)
    total_gt_180 = sum(1 for t in total_times if t is not None and t > 180)
    browser_gt_60 = sum(1 for t in browser_times if t is not None and t > 60)
    analyze_gt_60 = sum(1 for t in analyze_times if t is not None and t > 60)
    report_gt_60 = sum(1 for t in report_times if t is not None and t > 60)

    lines += [
        "## 2. Stability Metrics",
        "",
        f"- planner_normalized_task_count all == 5: {all_normalized_to_5}",
        f"- source_count >= 5: {source_ge_5} / {total_cases}",
        f"- source_count < 3: {source_lt_3} / {total_cases}",
        f"- quality_score < 0.6: {quality_lt_06} / {total_cases}",
        f"- total_time > 180s: {total_gt_180} / {total_cases}",
        f"- browser_time > 60s: {browser_gt_60} / {total_cases}",
        f"- analyze_time > 60s: {analyze_gt_60} / {total_cases}",
        f"- report_time > 60s: {report_gt_60} / {total_cases}",
        "",
    ]


    # 2.5 Report-type grouping (v3 stage E)
    from collections import defaultdict

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        rt_val = r.get("eval_report_type")
        if not rt_val or rt_val == "N/A":
            rt_val = r.get("report_type") or "unknown"
        groups[rt_val].append(r)
    lines += ["## 2.5 By report_type", "", "| report_type | cases | avg_quality | avg_sources | avg_number_grounding | avg_authority | top weakness |", "|---|---|---|---|---|---|---|"]
    for rt, group in sorted(groups.items()):
        q = _avg([_to_float(g.get("quality_score")) for g in group])
        s = _avg([_to_float(g.get("source_count")) for g in group])
        ng = _avg([_to_float(g.get("number_grounding_rate")) for g in group])
        au = _avg([_to_float(g.get("authority_score")) for g in group])
        issues = Counter(g.get("main_issue", "N/A") for g in group)
        top_issue = issues.most_common(1)[0][0] if issues else "N/A"
        lines.append(f"| {rt} | {len(group)} | {_fmt(q)} | {_fmt(s)} | {_fmt(ng)} | {_fmt(au)} | {top_issue} |")
    lines.append("")

    # 3. Top Successful Cases
    ranked = sorted(
        (r for r in rows if _to_float(r.get("quality_score")) is not None),
        key=lambda r: _to_float(r.get("quality_score")),
        reverse=True,
    )
    lines += ["## 3. Top Successful Cases", "", "| topic | source_count | quality_score | total_time |", "|---|---|---|---|"]
    for r in ranked[:3]:
        lines.append(f"| {r.get('topic')} | {r.get('source_count')} | {r.get('quality_score')} | {r.get('total_time')}s |")
    if not ranked:
        lines.append("| (no case has a quality_score yet) | | | |")
    lines.append("")

    # 4. Slow Cases
    def _is_slow(r: dict[str, Any]) -> bool:
        t = _to_float(r.get("total_time"))
        b = _to_float(r.get("browser_time"))
        a = _to_float(r.get("analyze_time"))
        rp = _to_float(r.get("report_time"))
        return (t is not None and t > 180) or (b is not None and b > 60) or (a is not None and a > 60) or (rp is not None and rp > 60)

    slow_cases = [r for r in rows if _is_slow(r)]
    lines += [
        "## 4. Slow Cases",
        "",
        "total_time > 180s, or browser/analyze/report stage individually > 60s.",
        "",
        "| topic | total_time | research_time | browser_time | analyze_time | report_time |",
        "|---|---|---|---|---|---|",
    ]
    for r in slow_cases:
        lines.append(
            f"| {r.get('topic')} | {r.get('total_time')} | {r.get('research_time')} | "
            f"{r.get('browser_time')} | {r.get('analyze_time')} | {r.get('report_time')} |"
        )
    if not slow_cases:
        lines.append("| (none) | | | | | |")
    lines.append("")

    # 5. Main Issues
    issue_counts = Counter(r.get("main_issue", "N/A") for r in rows)
    lines += ["## 5. Main Issues", "", "| issue | count |", "|---|---|"]
    for issue, count in issue_counts.most_common():
        lines.append(f"| {issue} | {count} |")
    lines.append("")

    # 6. Observations (derived from the numbers above, not hardcoded)
    lines += ["## 6. Observations", ""]
    cache_hits = [_to_float(r.get("cache_hit_count")) for r in rows]
    cache_misses = [_to_float(r.get("cache_miss_count")) for r in rows]
    total_hits = sum(v for v in cache_hits if v is not None)
    total_misses = sum(v for v in cache_misses if v is not None)
    if total_hits or total_misses:
        lines.append(
            f"- Research cache: {int(total_hits)} hit(s) / {int(total_misses)} miss(es) across all runs "
            f"- avg_research_time is {_fmt(_avg(research_times), 's')}, consistent with cache-hit runs "
            "finishing ResearchAgent in well under a second."
        )
    if all_normalized_to_5:
        lines.append(
            "- Planner is stable: every run's planner_normalized_task_count is exactly 5 "
            "(research/browse/analyze/report/evaluate), regardless of how many raw tasks the LLM planner emitted."
        )
    else:
        lines.append(
            "- Planner: not every run has a recorded planner_normalized_task_count == 5 - check raw vs "
            "normalized task counts in eval_summary.csv for the affected run(s)."
        )
    slow_stage_avgs = {"browser": _avg(browser_times), "analyze": _avg(analyze_times), "report": _avg(report_times)}
    slowest_stage = max((k for k, v in slow_stage_avgs.items() if v is not None), key=lambda k: slow_stage_avgs[k], default=None)
    if slowest_stage:
        lines.append(
            f"- Among Browser/Analyze/Report, **{slowest_stage}** has the highest average duration "
            f"({_fmt(slow_stage_avgs[slowest_stage], 's')}) and is the main remaining latency contributor "
            "now that Research is cache-accelerated."
        )
    relaxed_used = sum(1 for r in rows if _to_bool(r.get("relaxed_relevance_used")))
    if relaxed_used:
        lines.append(
            f"- Relaxed relevance selection was used in {relaxed_used} / {total_cases} run(s) - QualityScorer "
            "found usable content but nothing crossed the normal relevance threshold, so BrowserAgent fell "
            "back to the highest-scoring usable sources instead of failing outright."
        )
    cap_applied = sum(1 for r in rows if _to_bool(r.get("score_cap_applied")))
    if cap_applied:
        lines.append(
            f"- source_count-based evaluation cap was the binding constraint in {cap_applied} / {total_cases} "
            "run(s) - see the score_cap_reason column in eval_summary.csv for which run(s)."
        )

    return "\n".join(lines) + "\n"


def main() -> None:
    csv_path = config.EVAL_DIR / "eval_summary.csv"
    rows = load_rows(csv_path)
    report = build_report(rows)

    out_path = config.EVAL_DIR / "eval_report.md"
    out_path.write_text(report, encoding="utf-8")
    print(f"Eval report saved to {out_path}")


if __name__ == "__main__":
    main()
