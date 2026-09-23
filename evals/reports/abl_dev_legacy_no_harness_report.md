# Agent 评测报告

- 生成时间：2026-09-12T18:13:17+00:00
- 原始结果文件：`evals/results/abl_dev_legacy_no_harness.jsonl`（88 行，88 个任务，每任务 1.0 次 trial）
- 原始结果 SHA-256：`6ad3952e73b6af4d2d92bc9a10e411b551fe1caf097c0a862d77c1f58a5b2ac7`
- 实验臂：legacy_no_harness
- 配置哈希：35937f77e564d334, 713cbf16b7e9dbc0, a4b469b5c86ef591
- 运行模式：离线 fixture 回放（确定性，无网络、无费用）

> **数据性质声明**：本报告的任务集 gold_status 分布为 `{"synthetic_draft": 87, "machine_verified": 1}`，其中 human_verified = **0**。**没有任何任务经过人工复核**，因此以下全部指标只能作为 draft/synthetic 结果，不得作为已验证的效果结论对外引用。

## 总体指标

| 指标 | 数值 |
|---|---|
| Task Success Rate（全部 trial 均通过） | 0.75 |
| Task Success Rate（至少一次通过） | 0.75 |
| 综合评分均值 | 0.9252 |
| 综合评分最差值 | 0.6935 |
| 同任务多 trial 评分标准差均值 | 0 |
| Tool Selection Precision | 0 |
| Necessary Tool Recall | n/a |
| Unnecessary Tool Call Rate | n/a |
| Tool Outcome Success | n/a |
| Alternative Valid Path Rate | n/a |
| Tool Selection Recall | 0 |
| Tool Selection F1 | 0 |
| Tool Argument Accuracy | n/a |
| Invalid Tool Call Rate | n/a |
| Redundant Tool Call Rate | n/a |
| Recovery Success Rate | n/a |
| Citation Validity | 0.9977 |
| Citation Coverage | 0.8568 |
| Number Grounding Rate | 0.9962 |
| Unsupported Number Rate | 0.0038 |
| Budget Exceeded Rate | 0 |
| 单次运行估算费用(USD) | 0 |
| 平均输入 Token | 0 |
| 平均输出 Token | 0 |
| 平均工具调用数 | 0 |
| 时延 avg / p50 / p95 / max (s) | 0.1265 / 0.0774 / 0.0986 / 3.3822 |

### 安全

- Prompt Injection 导致的工具劫持次数：**0**
- 注入内容泄漏进报告的次数：**0**

### 运行结束状态分布

| 状态 | 次数 |
|---|---|
| insufficient | 12 |
| succeeded | 76 |

停止原因分布（回答“为什么在这里停止”）：

| stop_reason | 次数 |
|---|---|
| completed | 76 |
| no_usable_sources | 12 |

## 分类别

| 类别 | 任务数 | 全通过率 | 综合评分 | 数字溯源率 | 无源数字率 | p95 时延(s) |
|---|---|---|---|---|---|---|
| company_research | 9 | 1 | 1 | 1 | 0 | 0.9452 |
| industry_research | 4 | 1 | 1 | 1 | 0 | 3.3822 |
| insufficient_budget | 11 | 0 | 0.7654 | 1 | 0 | 0.0986 |
| long_context | 1 | 1 | 0.6935 | 0.714 | 0.2857 | 0.0994 |
| macro_research | 6 | 1 | 1 | 1 | 0 | 0.0914 |
| multi_source_verification | 1 | 1 | 0.8966 | 1 | 0 | 0.0937 |
| must_refuse | 1 | 1 | 0.8571 | n/a | n/a | 0.0772 |
| pdf_extraction | 1 | 1 | 1 | 1 | 0 | 0.0871 |
| prompt_injection | 10 | 1 | 1 | 1 | 0 | 0.093 |
| resume_after_interrupt | 11 | 1 | 1 | 1 | 0 | 0.0972 |
| structured_data | 11 | 0 | 0.8437 | 1 | 0 | 0.0944 |
| tool_timeout | 11 | 1 | 0.9891 | 1 | 0 | 0.095 |
| unreachable_source | 11 | 1 | 0.854 | n/a | n/a | 0.0774 |

## 分 split

| split | 任务数 | 全通过率 | 综合评分 |
|---|---|---|---|
| dev | 88 | 0.75 | 0.9252 |

## 方法与限制

- 所有数字由 `evals/reports/build_report.py` 从原始 JSONL 行计算，无人工填写路径。
- Citation Validity 的定义是“报告中的 `[sN]` 标记都能解析到真实 source_id”，**不校验被引页面是否真的支持该句结论**——这是工程引用检查，不是事实核查。
- Number Grounding 把数字分为 sourced / akshare / assumption / calculated / unsourced 五类，假设与测算如实归类，不洗成“有来源”。
- Tool Selection 指标只在任务显式声明了期望/禁用工具时计算，其余任务不计入分母。
- 离线模式下模型被确定性 stub 替换，因此 token/费用为 0，**不能**据此宣称真实成本；真实成本必须用 `--live` 跑出来。