# Evaluation Report

Generated from `outputs/eval/eval_summary.csv` - 10 eval topic(s) (latest run per topic).

## 1. Overall Metrics

- total_cases: 10
- success_cases: 10
- success_rate: 1.0
- avg_source_count: 4.7
- avg_quality_score: 0.78
- avg_total_time: 208.785s
- avg_research_time: 35.924s
- avg_browser_time: 39.398s
- avg_analyze_time: 76.08s
- avg_report_time: 41.442s

## 2. Stability Metrics

- planner_normalized_task_count all == 5: True
- source_count >= 5: 9 / 10
- source_count < 3: 1 / 10
- quality_score < 0.6: 0 / 10
- total_time > 180s: 7 / 10
- browser_time > 60s: 1 / 10
- analyze_time > 60s: 5 / 10
- report_time > 60s: 0 / 10

## 2.5 By report_type

| report_type | cases | avg_quality | avg_sources | avg_number_grounding | avg_authority | top weakness |
|---|---|---|---|---|---|---|
| company_research | 5 | 0.8 | 5.0 | 0.826 | 0.716 | unsourced_number_count>0 |
| industry_research | 5 | 0.76 | 4.4 | 0.701 | 0.624 | unsourced_number_count>0 |

## 3. Top Successful Cases

| topic | source_count | quality_score | total_time |
|---|---|---|---|
| 宁德时代投资分析 | 5 | 0.8 | 420.1774s |
| 比亚迪投资价值分析 | 5 | 0.8 | 195.0352s |
| 贵州茅台财务与估值分析 | 5 | 0.8 | 221.9142s |

## 4. Slow Cases

total_time > 180s, or browser/analyze/report stage individually > 60s.

| topic | total_time | research_time | browser_time | analyze_time | report_time |
|---|---|---|---|---|---|
| 宁德时代投资分析 | 420.1774 | 56.1569 | 125.4354 | 144.4322 | 47.8511 |
| 比亚迪投资价值分析 | 195.0352 | 19.9063 | 28.5562 | 86.3691 | 45.2455 |
| 贵州茅台财务与估值分析 | 221.9142 | 11.2231 | 21.3165 | 132.9042 | 49.7673 |
| 中芯国际投资风险分析 | 267.221 | 101.6443 | 36.7357 | 79.8994 | 40.507 |
| 隆基绿能行业地位分析 | 203.6063 | 29.055 | 38.1432 | 75.8964 | 41.5602 |
| 低空经济行业研报 | 192.6804 | 63.5324 | 25.4659 | 51.8568 | 43.6314 |
| 新能源汽车产业链分析 | 190.0069 | 28.9727 | 49.2584 | 59.4672 | 39.4806 |

## 5. Main Issues

| issue | count |
|---|---|
| unsourced_number_count>0 | 9 |
| low_source_count | 1 |

## 6. Observations

- Research cache: 0 hit(s) / 61 miss(es) across all runs - avg_research_time is 35.924s, consistent with cache-hit runs finishing ResearchAgent in well under a second.
- Planner is stable: every run's planner_normalized_task_count is exactly 5 (research/browse/analyze/report/evaluate), regardless of how many raw tasks the LLM planner emitted.
- Among Browser/Analyze/Report, **analyze** has the highest average duration (76.08s) and is the main remaining latency contributor now that Research is cache-accelerated.
- source_count-based evaluation cap was the binding constraint in 1 / 10 run(s) - see the score_cap_reason column in eval_summary.csv for which run(s).
