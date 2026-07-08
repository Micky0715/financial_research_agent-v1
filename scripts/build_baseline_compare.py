"""Compare the Agent system (outputs/eval/eval_summary.csv) against the
Direct-LLM baseline (outputs/eval/direct_llm_baseline.csv).

Usage:
    python scripts/collect_eval_summary.py
    python scripts/run_direct_llm_baseline.py
    python scripts/build_baseline_compare.py

Every number in the comparison table is computed from the two CSVs at run
time. trace_observability / bad_case_replayability / external_source_support
are architecture facts (what each pipeline does or doesn't record/use), not
measurements, and are stated as such rather than mixed into the numeric averages.
"""
import csv
import sys
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


def _avg(values: list[Optional[float]]) -> Optional[float]:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 3) if clean else None


def _fmt(value: Optional[float], suffix: str = "") -> str:
    return f"{value}{suffix}" if value is not None else "N/A"


def _load_csv(path: Path) -> Optional[list[dict[str, Any]]]:
    if not path.exists():
        print(f"[error] {path} not found.")
        return None
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    agent_path = config.EVAL_DIR / "eval_summary.csv"
    baseline_path = config.EVAL_DIR / "direct_llm_baseline.csv"

    agent_rows = _load_csv(agent_path)
    baseline_rows = _load_csv(baseline_path)
    if agent_rows is None or baseline_rows is None:
        print(
            "Missing input file(s). Run `python scripts/collect_eval_summary.py` and "
            "`python scripts/run_direct_llm_baseline.py` first, then retry."
        )
        sys.exit(1)

    agent_avg_source_count = _avg([_to_float(r.get("source_count")) for r in agent_rows])
    agent_avg_citation_coverage = _avg([_to_float(r.get("citation_coverage")) for r in agent_rows])
    agent_avg_source_grounding = _avg([_to_float(r.get("source_grounding")) for r in agent_rows])
    agent_avg_quality_score = _avg([_to_float(r.get("quality_score")) for r in agent_rows])
    agent_avg_total_time = _avg([_to_float(r.get("total_time")) for r in agent_rows])

    baseline_avg_source_count = _avg([_to_float(r.get("source_count")) for r in baseline_rows])
    baseline_avg_citation_coverage = _avg([_to_float(r.get("citation_coverage")) for r in baseline_rows])
    baseline_avg_source_grounding = _avg([_to_float(r.get("source_grounding")) for r in baseline_rows])
    baseline_avg_total_time = _avg([_to_float(r.get("total_time")) for r in baseline_rows])

    lines = [
        "# Baseline Comparison: Direct LLM vs. Agent System",
        "",
        f"Computed from `outputs/eval/direct_llm_baseline.csv` ({len(baseline_rows)} topic(s)) and "
        f"`outputs/eval/eval_summary.csv` ({len(agent_rows)} run(s)).",
        "",
        "## Comparison Table",
        "",
        "| metric | direct_llm | agent_system | conclusion |",
        "|---|---|---|---|",
        f"| avg_source_count | {_fmt(baseline_avg_source_count)} | {_fmt(agent_avg_source_count)} | "
        "Agent system grounds reports in real fetched sources; direct LLM cites none. |",
        f"| avg_citation_coverage | {_fmt(baseline_avg_citation_coverage)} | {_fmt(agent_avg_citation_coverage)} | "
        "Agent system's citations are checkable against `outputs/sources/`; direct LLM has nothing to check. |",
        f"| avg_source_grounding | {_fmt(baseline_avg_source_grounding)} | {_fmt(agent_avg_source_grounding)} | "
        "Same pattern - direct LLM's answer is ungrounded by construction, not by a scoring gap. |",
        f"| avg_quality_score | N/A (not scored) | {_fmt(agent_avg_quality_score)} | "
        "evaluate_report_quality() depends on citations/sources the baseline doesn't produce, so it isn't "
        "a fair scale for direct-LLM output. |",
        f"| avg_total_time | {_fmt(baseline_avg_total_time, 's')} | {_fmt(agent_avg_total_time, 's')} | "
        "Direct LLM is a single call and is faster; the agent system spends time searching, fetching, and "
        "scoring sources. |",
        "| trace_observability | none | `outputs/traces/*.json` (research/browser/planner/performance/"
        "compression/evaluation metrics) | Agent system records how a report was produced; direct LLM "
        "output has no audit trail. |",
        "| bad_case_replayability | not applicable | trace + sources + evaluation persisted per run_id | "
        "Agent system bad cases (e.g. 0 sources, relaxed relevance used) can be diagnosed after the fact "
        "from `outputs/`; direct LLM failures leave no diagnostic trace. |",
        "| external_source_support | none (model knowledge only) | web search + page/PDF fetch + domain "
        "allow/deny list | Agent system can fetch and incorporate external web/PDF sources at runtime, "
        "including information not contained in the model parameters; direct LLM mainly relies on model "
        "knowledge and prompt input. |",
        "",
        "## Conclusions",
        "",
        "- Direct LLM generation is faster because it is a single LLM call with no search, fetch, source "
        "scoring, or trace persistence overhead.",
        "- Direct LLM output has no real citations or verifiable sources: avg_citation_coverage and "
        "avg_source_grounding are 0 by construction, because the baseline does not fetch or attach "
        "external sources.",
        "- The agent system is slower, but produces reports with fetched sources, source-linked citations, "
        "structured evaluation results, and a full execution trace covering research, browser, planner, "
        "performance, compression, and evaluation metrics.",
        "- The agent system can incorporate external web/PDF sources at runtime, while the direct LLM "
        "baseline mainly relies on model knowledge and prompt input.",
        "- The goal of this project is not to generate the fastest possible report, but to make financial "
        "report generation more traceable, evaluable, and stable. The comparison makes this trade-off "
        "explicit with real metrics.",
        "",
    ]

    out_path = config.EVAL_DIR / "baseline_compare.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Baseline comparison saved to {out_path}")


if __name__ == "__main__":
    main()
