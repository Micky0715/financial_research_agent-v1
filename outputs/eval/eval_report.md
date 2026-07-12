# Evaluation Report

Generated from `outputs/eval/eval_summary.csv` - 10 eval topic(s) (latest run per topic).

## 1. Overall Metrics

- total_cases: 10
- success_cases: 10
- success_rate: 1.0
- avg_source_count: 5.0
- avg_quality_score: 0.798
- avg_total_time: 160.658s
- avg_research_time: 0.01s
- avg_browser_time: 30.923s
- avg_analyze_time: 60.419s
- avg_report_time: 51.7s

## 2. Stability Metrics

- planner_normalized_task_count all == 5: True
- source_count >= 5: 10 / 10
- source_count < 3: 0 / 10
- quality_score < 0.6: 0 / 10
- total_time > 180s: 3 / 10
- browser_time > 60s: 1 / 10
- analyze_time > 60s: 3 / 10
- report_time > 60s: 2 / 10

## 2.5 By report_type

| report_type | cases | avg_quality | avg_sources | avg_number_grounding | avg_authority | top weakness |
|---|---|---|---|---|---|---|
| company_research | 5 | 0.775 | 5.0 | N/A | 0.604 | ungrounded_number_count>0 |
| industry_research | 5 | 0.821 | 5.0 | N/A | 0.504 | ungrounded_number_count>0 |

## 3. Top Successful Cases

| topic | source_count | quality_score | total_time |
|---|---|---|---|
| 低空经济行业研报 | 5 | 0.907 | 187.9754s |
| 宁德时代投资分析 | 5 | 0.8 | 296.1026s |
| 比亚迪投资价值分析 | 5 | 0.8 | 130.2309s |

## 4. Slow Cases

total_time > 180s, or browser/analyze/report stage individually > 60s.

| topic | total_time | research_time | browser_time | analyze_time | report_time |
|---|---|---|---|---|---|
| 宁德时代投资分析 | 296.1026 | 0.0206 | 95.5534 | 51.2535 | 103.6432 |
| 半导体国产替代行业研究 | 173.2578 | 0.0096 | 17.9663 | 104.1773 | 37.7902 |
| 光伏行业投资风险分析 | 212.7854 | 0.0094 | 28.0146 | 57.7733 | 107.5266 |
| 低空经济行业研报 | 187.9754 | 0.0114 | 18.2481 | 115.5664 | 40.9569 |
| 新能源汽车产业链分析 | 172.5278 | 0.0084 | 39.294 | 83.0591 | 37.1712 |

## 5. Main Issues

| issue | count |
|---|---|
| ungrounded_number_count>0 | 9 |
| none | 1 |

## 6. Observations

- Research cache: 52 hit(s) / 0 miss(es) across all runs - avg_research_time is 0.01s, consistent with cache-hit runs finishing ResearchAgent in well under a second.
- Planner is stable: every run's planner_normalized_task_count is exactly 5 (research/browse/analyze/report/evaluate), regardless of how many raw tasks the LLM planner emitted.
- Among Browser/Analyze/Report, **analyze** has the highest average duration (60.419s) and is the main remaining latency contributor now that Research is cache-accelerated.
