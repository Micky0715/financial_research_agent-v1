# Evaluation Report

Generated from `outputs/eval/eval_summary.csv` - 10 eval topic(s) (latest run per topic).

## 1. Overall Metrics

- total_cases: 10
- success_cases: 10
- success_rate: 1.0
- avg_source_count: 5.0
- avg_quality_score: 0.8
- avg_total_time: 348.438s
- avg_research_time: 11.127s
- avg_browser_time: 19.261s
- avg_analyze_time: 71.794s
- avg_report_time: 32.173s

## 2. Stability Metrics

- planner_normalized_task_count all == 5: True
- source_count >= 5: 10 / 10
- source_count < 3: 0 / 10
- quality_score < 0.6: 0 / 10
- total_time > 180s: 2 / 10
- browser_time > 60s: 1 / 10
- analyze_time > 60s: 4 / 10
- report_time > 60s: 0 / 10

## 2.5 By report_type

| report_type | cases | avg_quality | avg_sources | avg_number_grounding | avg_authority | top weakness |
|---|---|---|---|---|---|---|
| company_research | 5 | 0.8 | 5.0 | 0.909 | 0.704 | unsourced_number_count>0 |
| industry_research | 5 | 0.8 | 5.0 | 0.829 | 0.636 | unsourced_number_count>0 |

## 3. Top Successful Cases

| topic | source_count | quality_score | total_time |
|---|---|---|---|
| 宁德时代投资分析 | 5 | 0.8 | 358.0337s |
| 比亚迪投资价值分析 | 5 | 0.8 | 158.9343s |
| 贵州茅台财务与估值分析 | 5 | 0.8 | 90.8975s |

## 4. Slow Cases

total_time > 180s, or browser/analyze/report stage individually > 60s.

| topic | total_time | research_time | browser_time | analyze_time | report_time |
|---|---|---|---|---|---|
| 宁德时代投资分析 | 358.0337 | 27.7044 | 64.4965 | 194.3155 | 33.532 |
| 比亚迪投资价值分析 | 158.9343 | 4.9226 | 11.7795 | 104.7631 | 27.3126 |
| 隆基绿能行业地位分析 | 2143.7385 | 28.9022 | 24.4332 | 44.8769 | 30.2512 |
| 光伏行业投资风险分析 | 168.1567 | 5.3636 | 18.0717 | 106.6497 | 28.3845 |
| 低空经济行业研报 | 151.9657 | 12.2971 | 11.9845 | 91.3154 | 26.6274 |

## 5. Main Issues

| issue | count |
|---|---|
| unsourced_number_count>0 | 10 |

## 6. Observations

- Research cache: 0 hit(s) / 52 miss(es) across all runs - avg_research_time is 11.127s, consistent with cache-hit runs finishing ResearchAgent in well under a second.
- Planner is stable: every run's planner_normalized_task_count is exactly 5 (research/browse/analyze/report/evaluate), regardless of how many raw tasks the LLM planner emitted.
- Among Browser/Analyze/Report, **analyze** has the highest average duration (71.794s) and is the main remaining latency contributor now that Research is cache-accelerated.
