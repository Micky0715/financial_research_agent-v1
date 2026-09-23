# Agent 评测报告

- 生成时间：2026-09-09T11:44:39+00:00
- 原始结果文件：`evals/results/regression_harness.jsonl`（41 行，41 个任务，每任务 1.0 次 trial）
- 原始结果 SHA-256：`e77441e096013e99f883846757755c825acff8d91aa34659bcec70d64f1ca2cf`
- 实验臂：harness
- 配置哈希：3dae678290f2e41c, b6ec0c7b9be1664a
- 运行模式：离线 fixture 回放（确定性，无网络、无费用）

> **数据性质声明**：本报告的任务集 gold_status 分布为 `{"synthetic_draft": 10, "machine_verified": 31}`，其中 human_verified = **0**。**没有任何任务经过人工复核**，因此以下全部指标只能作为 draft/synthetic 结果，不得作为已验证的效果结论对外引用。

## 总体指标

| 指标 | 数值 |
|---|---|
| Task Success Rate（全部 trial 均通过） | 0.3659 |
| Task Success Rate（至少一次通过） | 0.3659 |
| 综合评分均值 | 0.7653 |
| 综合评分最差值 | 0.6593 |
| 同任务多 trial 评分标准差均值 | 0 |
| Tool Selection Precision | n/a |
| Necessary Tool Recall | n/a |
| Unnecessary Tool Call Rate | n/a |
| Tool Outcome Success | 0.9399 |
| Alternative Valid Path Rate | n/a |
| Tool Selection Recall | n/a |
| Tool Selection F1 | n/a |
| Tool Argument Accuracy | 1 |
| Invalid Tool Call Rate | 0 |
| Redundant Tool Call Rate | 0.0061 |
| Recovery Success Rate | 0 |
| Citation Validity | 1 |
| Citation Coverage | 0.2195 |
| Number Grounding Rate | 1 |
| Unsupported Number Rate | 0 |
| Budget Exceeded Rate | 0.0244 |
| 单次运行估算费用(USD) | 0 |
| 平均输入 Token | 0 |
| 平均输出 Token | 0 |
| 平均工具调用数 | 14.8537 |
| 时延 avg / p50 / p95 / max (s) | 0.7454 / 0.6431 / 1.2063 / 2.3388 |

### 安全

- Prompt Injection 导致的工具劫持次数：**0**
- 注入内容泄漏进报告的次数：**0**

### 运行结束状态分布

| 状态 | 次数 |
|---|---|
| degraded | 1 |
| insufficient | 32 |
| succeeded | 8 |

停止原因分布（回答“为什么在这里停止”）：

| stop_reason | 次数 |
|---|---|
| budget_exhausted | 1 |
| completed | 8 |
| enough_evidence | 1 |
| entity_unverified | 30 |
| no_usable_sources | 1 |

## 分类别

| 类别 | 任务数 | 全通过率 | 综合评分 | 数字溯源率 | 无源数字率 | p95 时延(s) |
|---|---|---|---|---|---|---|
| company_research | 11 | 0.1818 | 0.7212 | 1 | 0 | 2.3388 |
| insufficient_budget | 1 | 0 | 0.8 | n/a | n/a | 0.4033 |
| macro_research | 1 | 1 | 1 | 1 | 0 | 0.6 |
| multi_source_verification | 3 | 0 | 0.6593 | n/a | n/a | 0.6189 |
| must_refuse | 5 | 1 | 0.8815 | n/a | n/a | 0.9377 |
| pdf_extraction | 1 | 0 | 0.6593 | n/a | n/a | 0.6088 |
| prompt_injection | 1 | 1 | 1 | 1 | 0 | 1.0077 |
| resume_after_interrupt | 1 | 1 | 1 | 1 | 0 | 1.1973 |
| source_conflict | 2 | 0.5 | 0.8296 | 1 | 0 | 0.9569 |
| structured_data | 7 | 0.2857 | 0.7311 | 1 | 0 | 1.2882 |
| tool_timeout | 4 | 0.25 | 0.7445 | 1 | 0 | 1.0986 |
| unreachable_source | 4 | 0.25 | 0.7113 | n/a | n/a | 0.7664 |

## 分 split

| split | 任务数 | 全通过率 | 综合评分 |
|---|---|---|---|
| regression | 41 | 0.3659 | 0.7653 |

## 方法与限制

- 所有数字由 `evals/reports/build_report.py` 从原始 JSONL 行计算，无人工填写路径。
- Citation Validity 的定义是“报告中的 `[sN]` 标记都能解析到真实 source_id”，**不校验被引页面是否真的支持该句结论**——这是工程引用检查，不是事实核查。
- Number Grounding 把数字分为 sourced / akshare / assumption / calculated / unsourced 五类，假设与测算如实归类，不洗成“有来源”。
- Tool Selection 指标只在任务显式声明了期望/禁用工具时计算，其余任务不计入分母。
- 离线模式下模型被确定性 stub 替换，因此 token/费用为 0，**不能**据此宣称真实成本；真实成本必须用 `--live` 跑出来。