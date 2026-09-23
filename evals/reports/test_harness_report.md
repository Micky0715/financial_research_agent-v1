# Agent 评测报告

- 生成时间：2026-09-09T11:44:40+00:00
- 原始结果文件：`evals/results/test_harness.jsonl`（210 行，70 个任务，每任务 3.0 次 trial）
- 原始结果 SHA-256：`167318d75bbf5181e84e58a729baf6973330b2b1fa522fc6a2ca257fe1cd8916`
- 实验臂：harness
- 配置哈希：3dae678290f2e41c, b6ec0c7b9be1664a
- 运行模式：离线 fixture 回放（确定性，无网络、无费用）

> **数据性质声明**：本报告的任务集 gold_status 分布为 `{"synthetic_draft": 207, "machine_verified": 3}`，其中 human_verified = **0**。**没有任何任务经过人工复核**，因此以下全部指标只能作为 draft/synthetic 结果，不得作为已验证的效果结论对外引用。

## 总体指标

| 指标 | 数值 |
|---|---|
| Task Success Rate（全部 trial 均通过） | 0.7714 |
| Task Success Rate（至少一次通过） | 0.7714 |
| 综合评分均值 | 0.9382 |
| 综合评分最差值 | 0.8 |
| 同任务多 trial 评分标准差均值 | 0 |
| Tool Selection Precision | n/a |
| Necessary Tool Recall | n/a |
| Unnecessary Tool Call Rate | n/a |
| Tool Outcome Success | 0.9543 |
| Alternative Valid Path Rate | n/a |
| Tool Selection Recall | n/a |
| Tool Selection F1 | n/a |
| Tool Argument Accuracy | 1 |
| Invalid Tool Call Rate | 0 |
| Redundant Tool Call Rate | 0.0298 |
| Recovery Success Rate | 0 |
| Citation Validity | 1 |
| Citation Coverage | 0.7429 |
| Number Grounding Rate | 1 |
| Unsupported Number Rate | 0 |
| Budget Exceeded Rate | 0.1286 |
| 单次运行估算费用(USD) | 0 |
| 平均输入 Token | 0 |
| 平均输出 Token | 0 |
| 平均工具调用数 | 13.1714 |
| 时延 avg / p50 / p95 / max (s) | 0.9788 / 0.7955 / 1.7408 / 7.4884 |

### 安全

- Prompt Injection 导致的工具劫持次数：**0**
- 注入内容泄漏进报告的次数：**0**

### 运行结束状态分布

| 状态 | 次数 |
|---|---|
| degraded | 6 |
| insufficient | 54 |
| succeeded | 150 |

停止原因分布（回答“为什么在这里停止”）：

| stop_reason | 次数 |
|---|---|
| budget_exhausted | 27 |
| completed | 150 |
| enough_evidence | 6 |
| no_usable_sources | 27 |

## 分类别

| 类别 | 任务数 | 全通过率 | 综合评分 | 数字溯源率 | 无源数字率 | p95 时延(s) |
|---|---|---|---|---|---|---|
| company_research | 4 | 1 | 1 | 1 | 0 | 3.009 |
| incomplete_information | 1 | 1 | 1 | 1 | 0 | 0.8102 |
| industry_research | 7 | 1 | 1 | 1 | 0 | 1.1274 |
| insufficient_budget | 9 | 0 | 0.8 | n/a | n/a | 1.1147 |
| macro_research | 3 | 1 | 1 | 1 | 0 | 1.3159 |
| prompt_injection | 10 | 1 | 1 | 1 | 0 | 1.2571 |
| resume_after_interrupt | 9 | 1 | 1 | 1 | 0 | 5.3724 |
| structured_data | 9 | 0.2222 | 0.8522 | 1 | 0 | 1.2728 |
| tool_timeout | 9 | 1 | 1 | 1 | 0 | 1.3249 |
| unreachable_source | 9 | 1 | 0.8675 | n/a | n/a | 0.8589 |

## 分 split

| split | 任务数 | 全通过率 | 综合评分 |
|---|---|---|---|
| test | 70 | 0.7714 | 0.9382 |

## 方法与限制

- 所有数字由 `evals/reports/build_report.py` 从原始 JSONL 行计算，无人工填写路径。
- Citation Validity 的定义是“报告中的 `[sN]` 标记都能解析到真实 source_id”，**不校验被引页面是否真的支持该句结论**——这是工程引用检查，不是事实核查。
- Number Grounding 把数字分为 sourced / akshare / assumption / calculated / unsourced 五类，假设与测算如实归类，不洗成“有来源”。
- Tool Selection 指标只在任务显式声明了期望/禁用工具时计算，其余任务不计入分母。
- 离线模式下模型被确定性 stub 替换，因此 token/费用为 0，**不能**据此宣称真实成本；真实成本必须用 `--live` 跑出来。