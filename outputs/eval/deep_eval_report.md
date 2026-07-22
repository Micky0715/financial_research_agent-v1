# Deep Evaluation Report

数据来源：`outputs/eval/eval_summary.csv`（10 个 eval topic，各取最新 run）。样本量为 10，P90/P95 参考意义有限，如实呈现不外推。

## 总体

- total_cases: 10
- success_rate: 1.0
- avg_source_count: 4.7
- avg_quality_score: 0.78
- avg_number_grounding_rate: 0.764
- avg tier1_or_tier2_ratio: 0.36
- total_time: avg 208.785s | P50 192.68s | P90 267.221s | P95 420.177s | max 420.177s

## 按 report_type 分组

| report_type | cases | avg_quality | avg_sources | avg_grounding | avg_authority |
|---|---|---|---|---|---|
| company_research | 5 | 0.8 | 5.0 | 0.826 | 0.716 |
| industry_research | 5 | 0.76 | 4.4 | 0.701 | 0.624 |

## score_cap_reason 分布

- unsourced_number_count>0: 9
- source_count=2: 1

## main_issue 分布

- unsourced_number_count>0: 9
- low_source_count: 1

