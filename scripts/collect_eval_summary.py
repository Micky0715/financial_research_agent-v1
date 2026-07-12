"""Scan outputs/traces + outputs/sources + outputs/evaluations and build a
per-topic summary table for the fixed eval/topics.json evaluation suite.

Usage:
    python scripts/collect_eval_summary.py

Only summarizes the topics listed in eval/topics.json - one row per topic,
using that topic's most recent run (by trace finished_at/started_at). Older
runs of the same topic, debug runs, and any run whose topic isn't in
eval/topics.json are excluded, so re-running individual topics while
iterating doesn't pollute the eval suite with stale rows. A topic with no
matching run yet still gets a row (main_issue=missing_run) instead of being
silently dropped, and a single malformed/missing file must never abort the
whole scan.
"""
import csv
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import config  # noqa: E402
from utils.file_utils import load_json  # noqa: E402

FIELDNAMES = [
    "run_id",
    "topic",
    "report_type",
    "success",
    "source_count",
    "quality_score",
    "citation_coverage",
    "source_grounding",
    "source_count_score",
    "authority_score",
    "financial_depth_score",
    "valuation_depth_score",
    "risk_grounding_score",
    "total_time",
    "research_time",
    "browser_time",
    "analyze_time",
    "report_time",
    "cache_hit_count",
    "cache_miss_count",
    "planner_raw_task_count",
    "planner_normalized_task_count",
    "relaxed_relevance_used",
    "score_cap_applied",
    "score_cap_reason",
    "report_path",
    "trace_path",
    "sources_path",
    "evaluation_path",
    "main_issue",
    # v3 additions
    "eval_report_type",
    "number_grounding_rate",
    "tier1_or_tier2_ratio",
]

_NA = "N/A"
_DEFAULT_TOPICS_PATH = config.BASE_DIR / "eval" / "topics.json"


def load_topics(topics_path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Load the eval topics file (default eval/topics.json). Never raises - a
    missing/malformed file just yields an empty list plus a printed error, so
    callers can decide how to handle "no topics" without a stack trace."""
    path = topics_path or _DEFAULT_TOPICS_PATH
    try:
        entries = load_json(path)
    except Exception as exc:  # noqa: BLE001 - caller decides what to do with an empty list
        print(f"[error] could not load topics file {path}: {exc}")
        return []
    if not isinstance(entries, list):
        print(f"[error] {path} does not contain a JSON list")
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("topic")]


def _find_sibling(directory: Path, run_id: str, suffix: str) -> Optional[Path]:
    """Find the single outputs/{directory}/{run_id}_*_{suffix} file for this run, if any."""
    if not directory.exists():
        return None
    matches = sorted(directory.glob(f"{run_id}_*_{suffix}"))
    if not matches:
        print(f"[warn] no {suffix} file found for run_id={run_id} in {directory}")
        return None
    return matches[0]


def _find_report(run_id: str) -> Optional[Path]:
    if not config.REPORTS_DIR.exists():
        return None
    matches = sorted(config.REPORTS_DIR.glob(f"{run_id}_*_report.*"))
    if not matches:
        print(f"[warn] no report file found for run_id={run_id}")
        return None
    return matches[0]


def _load_all_traces() -> list[tuple[Path, dict[str, Any]]]:
    """Load every outputs/traces/*.json once. One bad file is skipped with a warning, not fatal."""
    traces: list[tuple[Path, dict[str, Any]]] = []
    if not config.TRACES_DIR.exists():
        print(f"[warn] traces directory not found: {config.TRACES_DIR}")
        return traces
    for trace_path in sorted(config.TRACES_DIR.glob("*.json")):
        try:
            trace = load_json(trace_path)
        except Exception as exc:  # noqa: BLE001 - one bad trace file must not stop the scan
            print(f"[warn] failed to read {trace_path}: {exc}")
            continue
        if isinstance(trace, dict):
            traces.append((trace_path, trace))
    return traces


def _trace_sort_key(trace: dict[str, Any]) -> str:
    """ISO timestamps sort correctly as strings; finished_at is set at the very end of every
    run (success or abort), so it's the more reliable "latest" signal than started_at."""
    return trace.get("finished_at") or trace.get("started_at") or ""


def _latest_trace_for_topic(
    traces: list[tuple[Path, dict[str, Any]]], topic: str
) -> Optional[tuple[Path, dict[str, Any]]]:
    matches = [(p, t) for p, t in traces if t.get("topic") == topic]
    if not matches:
        return None
    return max(matches, key=lambda item: _trace_sort_key(item[1]))


def _classify_main_issue(success: Any, source_count: Any, score_cap_reason: Any) -> str:
    """Priority-ordered label for what (if anything) looks off about this run:

    1. success == False -> "failed" (the run itself didn't complete - e.g. aborted with no
       usable sources, or the evaluation file never got written).
    2. source_count < 3 -> "low_source_count" (completed, but too thin to trust).
    3. score_cap_reason set -> that exact reason (evaluate_report_quality() already picked
       the single binding cap constraint - e.g. "citation_hits<5" - reuse it verbatim instead
       of inventing a second, possibly-inconsistent label for the same fact).
    4. otherwise -> "none".
    """
    if success is False:
        return "failed"
    if isinstance(source_count, (int, float)) and source_count < 3:
        return "low_source_count"
    if score_cap_reason not in (None, "", _NA):
        return str(score_cap_reason)
    return "none"


def _row_from_trace(trace_path: Path, trace: dict[str, Any], report_type: Any) -> dict[str, Any]:
    """Build one eval_summary row from a trace file + its sibling sources/evaluation files."""
    run_id = trace.get("run_id") or trace_path.stem.split("_")[0]
    topic = trace.get("topic", _NA)

    sources_path = _find_sibling(config.SOURCES_DIR, run_id, "sources.json")
    evaluation_path = _find_sibling(config.EVALUATIONS_DIR, run_id, "evaluation.json")
    report_path = _find_report(run_id)

    source_count = 0
    try:
        source_count = len(load_json(sources_path)) if sources_path else 0
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] failed to read sources file for run_id={run_id}: {exc}")

    evaluation: dict[str, Any] = {}
    try:
        evaluation = load_json(evaluation_path) if evaluation_path else {}
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] failed to read evaluation file for run_id={run_id}: {exc}")
    if not isinstance(evaluation, dict):
        evaluation = {}

    criteria = evaluation.get("criteria_scores", {})
    diagnostics = evaluation.get("diagnostics", {})
    quality_score = evaluation.get("overall_score", _NA)

    performance = trace.get("performance_metrics", {}) or {}
    research_metrics = trace.get("research_metrics", {}) or {}
    planner_metrics = trace.get("planner_metrics", {}) or {}
    browser_metrics = trace.get("browser_metrics", {}) or {}

    total_time = performance.get("total_duration", trace.get("total_duration", _NA))
    success = bool(source_count) and bool(evaluation)

    row = {
        "run_id": run_id,
        "topic": topic,
        "report_type": report_type if report_type else _NA,
        "success": success,
        "source_count": source_count,
        "quality_score": quality_score,
        "citation_coverage": criteria.get("citation_coverage", _NA),
        "source_grounding": criteria.get("source_grounding", _NA),
        "source_count_score": criteria.get("source_count_score", _NA),
        "authority_score": criteria.get("authority_score", _NA),
        "financial_depth_score": criteria.get("financial_depth_score", _NA),
        "valuation_depth_score": criteria.get("valuation_depth_score", _NA),
        "risk_grounding_score": criteria.get("risk_grounding_score", _NA),
        "total_time": total_time,
        "research_time": performance.get("research_duration", _NA),
        "browser_time": performance.get("browser_duration", _NA),
        "analyze_time": performance.get("analyze_duration", _NA),
        "report_time": performance.get("report_duration", _NA),
        "cache_hit_count": research_metrics.get("cache_hit_count", _NA),
        "cache_miss_count": research_metrics.get("cache_miss_count", _NA),
        "planner_raw_task_count": planner_metrics.get("raw_task_count", _NA),
        "planner_normalized_task_count": planner_metrics.get("normalized_task_count", _NA),
        "relaxed_relevance_used": browser_metrics.get("relaxed_relevance_used", _NA),
        "score_cap_applied": diagnostics.get("source_count_cap_applied", _NA),
        "score_cap_reason": diagnostics.get("score_cap_reason", _NA),
        "report_path": str(report_path) if report_path else trace.get("final_report_path", _NA),
        "trace_path": str(trace_path),
        "sources_path": str(sources_path) if sources_path else _NA,
        "evaluation_path": str(evaluation_path) if evaluation_path else _NA,
        "main_issue": _NA,  # filled in below once we have all the numbers
    }
    row["main_issue"] = _classify_main_issue(success, source_count, diagnostics.get("score_cap_reason", _NA))

    # v3: report-type-aware evaluation fields + source tier ratio
    row["eval_report_type"] = evaluation.get("report_type", _NA)
    row["number_grounding_rate"] = diagnostics.get("number_grounding_rate", _NA)
    try:
        if sources_path:
            from tools.source_tier import classify_source, tier_counts

            source_list = load_json(sources_path)
            classified = [
                classify_source(s.get("url", ""), s.get("title", ""), s.get("source_type", "web"))
                for s in source_list
            ]
            row["tier1_or_tier2_ratio"] = tier_counts(classified)["tier1_or_tier2_ratio"]
        else:
            row["tier1_or_tier2_ratio"] = _NA
    except Exception:  # noqa: BLE001 - tier stats are auxiliary
        row["tier1_or_tier2_ratio"] = _NA
    return row


def _missing_row(topic: str, report_type: Any) -> dict[str, Any]:
    """Placeholder row for a topic that has no matching run at all yet."""
    row: dict[str, Any] = {field: _NA for field in FIELDNAMES}
    row["topic"] = topic
    row["report_type"] = report_type if report_type else _NA
    row["success"] = False
    row["source_count"] = 0
    row["main_issue"] = "missing_run"
    return row


def build_rows(topics_path: Optional[Path] = None) -> list[dict[str, Any]]:
    """One row per topic in the eval topics file, using each topic's latest run (if any)."""
    topics = load_topics(topics_path)
    all_traces = _load_all_traces()

    rows: list[dict[str, Any]] = []
    for entry in topics:
        topic = entry["topic"]
        report_type = entry.get("report_type", _NA)
        match = _latest_trace_for_topic(all_traces, topic)
        if match is None:
            print(f"[warn] no run found for topic: {topic}")
            rows.append(_missing_row(topic, report_type))
            continue
        trace_path, trace = match
        rows.append(_row_from_trace(trace_path, trace, report_type))

    return rows


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_markdown(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "run_id", "topic", "report_type", "success", "source_count", "quality_score",
        "total_time", "research_time", "browser_time", "analyze_time", "report_time",
        "cache_hit_count", "cache_miss_count", "planner_raw_task_count",
        "planner_normalized_task_count", "relaxed_relevance_used", "main_issue",
    ]
    found = sum(1 for r in rows if r.get("main_issue") != "missing_run")
    lines = [
        "# Eval Summary (latest run per topic)",
        "",
        f"{found} of {len(rows)} eval topics have at least one run recorded under `outputs/traces/`. "
        "The full field set (including citation/authority scores and file paths) is in "
        "`outputs/eval/eval_summary.csv`.",
        "",
        "| " + " | ".join(cols) + " |",
        "|" + "|".join(["---"] * len(cols)) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(c, _NA)) for c in cols) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    topics = load_topics()
    if not topics:
        print(f"[error] no topics loaded from {_DEFAULT_TOPICS_PATH} - nothing to summarize.")
        sys.exit(1)

    rows = build_rows()
    found = sum(1 for r in rows if r.get("main_issue") != "missing_run")

    csv_path = config.EVAL_DIR / "eval_summary.csv"
    md_path = config.EVAL_DIR / "eval_summary.md"
    write_csv(rows, csv_path)
    write_markdown(rows, md_path)

    print(f"Collected latest {found} run(s) for {len(topics)} eval topic(s).")
    print(f"CSV saved to {csv_path}")
    print(f"Markdown saved to {md_path}")


if __name__ == "__main__":
    main()
