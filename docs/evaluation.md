# 评测设计与复现

## 数据

任务 schema 位于 `evals/datasets/schema.py`，包含约束、required facts、期望结果/工具、禁止声明、预算、标签、来源和审核元数据。构建器按主题分组切分，当前产物规模为 dev 88、test 70、regression 41。自动样本是 `synthetic_draft`；确定性历史样本可标 `machine_verified`；只有人工复核后才能标 `human_verified`。

开发结果 `dev_harness.jsonl` 共 264 行（88 × 3 trials），其中 `synthetic_draft=261`、`machine_verified=3`、`human_verified=0`。所以结果只能用于工程迭代，不能称为 Gold Test。

## 运行方式

```bash
python -m evals.runners.run_eval --smoke
python -m evals.runners.run_eval --split dev --arm harness --trials 3
python -m evals.runners.run_eval --split test --arm harness --trials 3
python -m evals.runners.run_eval --split regression --arm harness --trials 1 --results evals/results/regression_harness.jsonl
python -m evals.reports.build_report --results evals/results/dev_harness.jsonl --compare evals/results/dev_baseline_v4.jsonl
```

Runner 支持按 `(task_id, trial, arm, config_hash)` 断点续跑。离线模式同时替换 HTTP、AkShare 直连和模型输出，避免默认 CI 依赖网络。每行结果包含实验臂、配置哈希、trial、Trace/结果引用和 grader 明细；报告记录原始 JSONL SHA-256。

## Grader 与指标

确定性 grader 覆盖 schema、期望/禁止事实、工具选择与参数、最终环境、引用合法性/覆盖率、数字溯源、冗余调用、安全和预算。LLM Judge 是可选项；目前没有用未校准 Judge 冒充独立 Gold。

主要指标为 Task Success、Tool Selection P/R/F1、Tool Argument Accuracy、Invalid Call、Recovery、Citation Validity/Coverage、Number Grounding、Unsupported Number、Redundant Calls、Budget Exceeded、Token/估算费用、avg/P50/P95 latency，以及多 trial 均值、标准差、最差值和成功次数。

## 已复跑的开发集对照

| 指标 | baseline_v4 | harness |
|---|---:|---:|
| Task Success | 0.6818 | 0.8068 |
| Overall mean / worst | 0.8752 / 0.2857 | 0.9340 / 0.7143 |
| Tool P/R/F1 | 1 / 1 / 1 | 1 / 1 / 1 |
| Citation validity / coverage | 0.9974 / 0.6935 | 0.9977 / 0.7318 |
| Number grounding / unsupported | 0.9947 / 0.0053 | 0.9956 / 0.0044 |
| Redundant call rate | 0.0350 | 0.0306 |
| Mean tool calls | 11.7841 | 15.3182 |
| P95 latency (s) | 4.4789 | 1.8308 |

Harness 的收益带来平均 +3.5341 次工具调用的代价。两臂均为离线 fixture、估算费用为 0。Harness 原始结果 SHA-256 为 `43bc4bda399d40f54cda1f7182913c8ccc12a8f3b7d8b4820074170988efd9c8`。

## 冻结后的独立 test

在实现与开发集对照冻结后运行 test 70 tasks × 3 trials（210 行）：Task Success 0.7714，overall mean/worst 0.9365/0.7778，Citation Validity/Coverage 1.0/0.7429，Number Grounding/Unsupported 1.0/0，冗余调用率 0.0298，平均工具调用 14.9286，P95 2.7934 秒。状态为 succeeded 150、insufficient 54、degraded 6；Prompt Injection 劫持和报告泄漏均为 0。

Gold 状态是 `synthetic_draft=207`、`machine_verified=3`、`human_verified=0`。原始文件 `evals/results/test_harness.jsonl`，SHA-256 `34b84ab84eeaeb82ef28e22d801181174fd7766552a62b77fcbac7751d02c2a3`；自动报告 `evals/reports/test_harness_report.md/json`。

当前 Recovery Success 指标只把“同一失败工具步骤后出现成功重试”算作恢复，不把 run 级 checkpoint resume 或跨工具降级计入，因此报告值 0 不能用于否定已单独通过的 11 个 dev 与 9 个 test 中断恢复任务。后续应拆成 tool retry recovery、fallback recovery、checkpoint recovery 三项。

## 回归与限制

41 条 regression 集的一次运行成功率为 0.3659。其主要原因是 31 条历史 Bad Case 被转换成“以问题描述作为研究主题”的 machine-verified 任务，30 条在实体校验阶段停止。该数值揭示数据集构造限制，不能解释为 Harness 大面积功能回归，也不能称套件已通过。后续需逐条改写为可执行输入、故障注入和明确期望状态，再人工复核。

独立 test 应只在候选冻结后运行，避免把 held-out 集变成开发集。真实 Provider、网页漂移、模型随机性、Judge 人工校准和线上成本不在当前离线结论覆盖范围内。
