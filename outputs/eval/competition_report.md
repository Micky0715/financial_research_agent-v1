# Competition 30-Case Evaluation Report

**这是人工分层构建的固定 benchmark（公司10/行业10/宏观10，每类内置季度跟踪/年度跟踪/风险分析/图表题各至少1个），不是随机抽样，不能宣称代表所有金融任务的泛化能力。**所有数字均来自真实产物（outputs/traces、outputs/sources、outputs/evaluations、report 文件），没有编造未完成的 case。

## 总体指标

- total_cases: 30
- success_rate: 1.0
- avg_source_count: 4.6
- valid_citation_rate: 1.0
- avg_source_grounding(criteria): 1.0
- avg_number_grounding_rate: 0.85
- avg_tier1_or_tier2_ratio: 0.462
- avg_quality_score: 0.761
- total_latency: avg=236.518s p50=192.68s p90=410.821s p95=517.707s
- report_compliance_pass_rate: 0.0
- chart_generation_rate: 0.867
- entity_validation_pass_rate: 1.0（分布：{'verified': 19, 'weak': 3, 'not_applicable': 8}）
- revision_trigger_rate: 0.2
- revision_improvement(avg after-before, adopted only): 0.0
- tracking_report_success_rate（公司三表跟踪工具，本次尝试 2 例）: 1.0
- macro_indicator_coverage（宏观 case，指标覆盖/22项已注册指标）: 1.0
- company_statement_coverage（公司 case，三表抽取成功占比）: 1.0
- industry_scenario_completion_rate（行业 case，三年及以上情景占比）: 1.0

## 按类别分组

| category | cases | success | avg_quality | avg_sources | avg_grounding | avg_tier_ratio |
|---|---|---|---|---|---|---|
| company | 10 | 10/10 | 0.8 | 5.0 | 0.836 | 0.4 |
| industry | 10 | 10/10 | 0.742 | 4.2 | 0.743 | 0.285 |
| macro | 10 | 10/10 | 0.741 | 4.6 | 0.971 | 0.7 |

## 未完成 / 失败 case（如实列出，不隐藏）

（无——全部 30 个 case 均产出了带来源的报告）

## 方法说明与限制

- report_compliance_pass_rate 用 tools/report_compliance_checker.py 对每个 case 的最终报告重新跑一次规则检查（风险提示/数据来源/估值假设/免责声明/研究对象与代码/无来源数字/假设伪装成事实/报告日期），不是导出正式披露报告时的判定结果。
- chart_generation_rate 按报告 HTML 是否包含 `data:image/png;base64` 判定，markdown-only 输出的 case 不计入分母。
- tracking_report_success_rate 仅统计公司类跟踪题触发的真实 tracking_report_builder 调用（阶段D 的工具专注上市公司三表）；行业/宏观的跟踪题以周期性措辞的常规报告验证，计入上面按类别分组的 success 列，不计入本指标。
- macro_indicator_coverage 以 22 项已在 tools/macro_data_collector.py 中注册且实测可用的指标为分母；社融/城镇调查失业率/美元指数等 3 项已知不可用指标不计入分母（如实排除，非隐藏缺陷）。
- revision_improvement 只统计"改稿被采用"（after_score >= before_score）的场景，被丢弃的改稿不参与均值计算——见 orchestrator/workflow.py 的 adopt-if-improved 逻辑。
