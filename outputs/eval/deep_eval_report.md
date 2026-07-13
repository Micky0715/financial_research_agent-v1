# Deep Evaluation Report

数据来源：`outputs/eval/eval_summary.csv`（10 个 eval topic，各取最新 run）。样本量为 10，P90/P95 参考意义有限，如实呈现不外推。

## 总体

- total_cases: 10
- success_rate: 1.0
- avg_source_count: 5.0
- avg_quality_score: 0.8
- avg_number_grounding_rate: 0.869
- avg tier1_or_tier2_ratio: 0.42
- total_time: avg 348.438s | P50 132.45s | P90 358.034s | P95 2143.738s | max 2143.738s

## 按 report_type 分组

| report_type | cases | avg_quality | avg_sources | avg_grounding | avg_authority |
|---|---|---|---|---|---|
| company_research | 5 | 0.8 | 5.0 | 0.909 | 0.704 |
| industry_research | 5 | 0.8 | 5.0 | 0.829 | 0.636 |

## score_cap_reason 分布

- unsourced_number_count>0: 10

## main_issue 分布

- unsourced_number_count>0: 10

