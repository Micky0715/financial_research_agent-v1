# Evaluation Report

Generated from `outputs/eval/eval_summary.csv` - 10 eval topic(s) (latest run per topic).

## 1. Overall Metrics

- total_cases: 10
- success_cases: 10
- success_rate: 1.0
- avg_source_count: 5.0
- avg_quality_score: 0.831
- avg_total_time: 124.336s
- avg_research_time: 0.029s
- avg_browser_time: 23.406s
- avg_analyze_time: 34.721s
- avg_report_time: 32.961s

## 2. Stability Metrics

- planner_normalized_task_count all == 5: True
- source_count >= 5: 10 / 10
- source_count < 3: 0 / 10
- quality_score < 0.6: 0 / 10
- total_time > 180s: 0 / 10
- browser_time > 60s: 0 / 10
- analyze_time > 60s: 0 / 10
- report_time > 60s: 0 / 10

## 3. Top Successful Cases

| topic | source_count | quality_score | total_time |
|---|---|---|---|
| 隆基绿能行业地位分析 | 5 | 0.985 | 133.196s |
| 中芯国际投资风险分析 | 5 | 0.973 | 94.3068s |
| 半导体国产替代行业研究 | 5 | 0.85 | 133.902s |

## 4. Slow Cases

total_time > 180s, or browser/analyze/report stage individually > 60s.

| topic | total_time | research_time | browser_time | analyze_time | report_time |
|---|---|---|---|---|---|
| (none) | | | | | |

## 5. Main Issues

| issue | count |
|---|---|
| ungrounded_number_count>0 | 5 |
| none | 2 |
| financial_depth_score=0 | 2 |
| valuation_depth_score<0.6 | 1 |

## 6. Observations

- Research cache: 51 hit(s) / 0 miss(es) across all runs - avg_research_time is 0.029s, consistent with cache-hit runs finishing ResearchAgent in well under a second.
- Planner is stable: every run's planner_normalized_task_count is exactly 5 (research/browse/analyze/report/evaluate), regardless of how many raw tasks the LLM planner emitted.
- Among Browser/Analyze/Report, **analyze** has the highest average duration (34.721s) and is the main remaining latency contributor now that Research is cache-accelerated.
