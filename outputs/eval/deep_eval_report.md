# Deep Evaluation Report

数据来源：`outputs/eval/eval_summary.csv`（10 个 eval topic，各取最新 run）。样本量为 10，P90/P95 参考意义有限，如实呈现不外推。

## 总体

- total_cases: 10
- success_rate: 1.0
- avg_source_count: 5.0
- avg_quality_score: 0.798
- avg_number_grounding_rate: insufficient data
- avg tier1_or_tier2_ratio: 0.28
- total_time: avg 160.658s | P50 133.912s | P90 212.785s | P95 296.103s | max 296.103s

## 按 report_type 分组

| report_type | cases | avg_quality | avg_sources | avg_grounding | avg_authority |
|---|---|---|---|---|---|
| company_research | 5 | 0.775 | 5.0 | insufficient data | 0.604 |
| industry_research | 5 | 0.821 | 5.0 | insufficient data | 0.504 |

## score_cap_reason 分布

- ungrounded_number_count>0: 9
- none: 1

## main_issue 分布

- ungrounded_number_count>0: 9
- none: 1

