# Baseline Comparison: Direct LLM vs. Agent System

Computed from `outputs/eval/direct_llm_baseline.csv` (10 topic(s)) and `outputs/eval/eval_summary.csv` (10 run(s)).

## Comparison Table

| metric | direct_llm | agent_system | conclusion |
|---|---|---|---|
| avg_source_count | 0.0 | 5.0 | Agent system grounds reports in real fetched sources; direct LLM cites none. |
| avg_citation_coverage | 0.0 | 1.0 | Agent system's citations are checkable against `outputs/sources/`; direct LLM has nothing to check. |
| avg_source_grounding | 0.0 | 1.0 | Same pattern - direct LLM's answer is ungrounded by construction, not by a scoring gap. |
| avg_quality_score | N/A (not scored) | 0.831 | evaluate_report_quality() depends on citations/sources the baseline doesn't produce, so it isn't a fair scale for direct-LLM output. |
| avg_total_time | 58.474s | 124.336s | Direct LLM is a single call and is faster; the agent system spends time searching, fetching, and scoring sources. |
| trace_observability | none | `outputs/traces/*.json` (research/browser/planner/performance/compression/evaluation metrics) | Agent system records how a report was produced; direct LLM output has no audit trail. |
| bad_case_replayability | not applicable | trace + sources + evaluation persisted per run_id | Agent system bad cases (e.g. 0 sources, relaxed relevance used) can be diagnosed after the fact from `outputs/`; direct LLM failures leave no diagnostic trace. |
| external_source_support | none (model knowledge only) | web search + page/PDF fetch + domain allow/deny list | Agent system can fetch and incorporate external web/PDF sources at runtime, including information not contained in the model parameters; direct LLM mainly relies on model knowledge and prompt input. |

## Conclusions

- Direct LLM generation is faster because it is a single LLM call with no search, fetch, source scoring, or trace persistence overhead.
- Direct LLM output has no real citations or verifiable sources: avg_citation_coverage and avg_source_grounding are 0 by construction, because the baseline does not fetch or attach external sources.
- The agent system is slower, but produces reports with fetched sources, source-linked citations, structured evaluation results, and a full execution trace covering research, browser, planner, performance, compression, and evaluation metrics.
- The agent system can incorporate external web/PDF sources at runtime, while the direct LLM baseline mainly relies on model knowledge and prompt input.
- The goal of this project is not to generate the fastest possible report, but to make financial report generation more traceable, evaluable, and stable. The comparison makes this trade-off explicit with real metrics.
