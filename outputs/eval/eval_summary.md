# Eval Summary (latest run per topic)

10 of 10 eval topics have at least one run recorded under `outputs/traces/`. The full field set (including citation/authority scores and file paths) is in `outputs/eval/eval_summary.csv`.

| run_id | topic | report_type | success | source_count | quality_score | total_time | research_time | browser_time | analyze_time | report_time | cache_hit_count | cache_miss_count | planner_raw_task_count | planner_normalized_task_count | relaxed_relevance_used | main_issue |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 778d481557 | 宁德时代投资分析 | company_research | True | 5 | 0.8 | 109.2937 | 0.0103 | 11.9427 | 33.7048 | 31.2946 | 6 | 0 | 5 | 5 | False | ungrounded_number_count>0 |
| 4267de51db | 比亚迪投资价值分析 | company_research | True | 5 | 0.8 | 128.2955 | 0.0144 | 24.6622 | 38.2603 | 37.0336 | 5 | 0 | 5 | 5 | False | ungrounded_number_count>0 |
| 14eb2d88ae | 贵州茅台财务与估值分析 | company_research | True | 5 | 0.8 | 102.0081 | 0.0636 | 20.9534 | 22.2833 | 28.0041 | 5 | 0 | 5 | 5 | False | ungrounded_number_count>0 |
| 9c2e279254 | 中芯国际投资风险分析 | company_research | True | 5 | 0.973 | 94.3068 | 0.0262 | 14.0397 | 29.5247 | 23.7216 | 5 | 0 | 5 | 5 | False | none |
| bc5fc17ab7 | 隆基绿能行业地位分析 | company_research | True | 5 | 0.985 | 133.196 | 0.0334 | 15.9122 | 40.5502 | 26.8427 | 5 | 0 | 9 | 5 | False | none |
| 54a3193090 | AI机器人行业研报 | industry_research | True | 5 | 0.75 | 124.347 | 0.0197 | 28.8506 | 40.9999 | 41.1026 | 5 | 0 | 5 | 5 | False | financial_depth_score=0 |
| 2a570169c3 | 半导体国产替代行业研究 | industry_research | True | 5 | 0.85 | 133.902 | 0.0178 | 39.9484 | 22.454 | 31.2913 | 5 | 0 | 17 | 5 | False | valuation_depth_score<0.6 |
| 823f6c24a3 | 光伏行业投资风险分析 | industry_research | True | 5 | 0.8 | 115.5263 | 0.0197 | 16.0604 | 26.5302 | 39.0217 | 5 | 0 | 5 | 5 | False | ungrounded_number_count>0 |
| f5128d3b68 | 低空经济行业研报 | industry_research | True | 5 | 0.75 | 141.2122 | 0.0705 | 36.6227 | 42.6808 | 26.452 | 5 | 0 | 5 | 5 | False | financial_depth_score=0 |
| 3bcee12627 | 新能源汽车产业链分析 | industry_research | True | 5 | 0.8 | 161.2723 | 0.0177 | 25.0687 | 50.2194 | 44.8456 | 5 | 0 | 5 | 5 | False | ungrounded_number_count>0 |
