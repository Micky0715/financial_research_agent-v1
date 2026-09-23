# Agent 评测报告

- 生成时间：2026-09-12T18:54:21+00:00
- 原始结果文件：`evals/results/abl_dev_harness_full.jsonl`（88 行，88 个任务，每任务 1.0 次 trial）
- 原始结果 SHA-256：`ce48e169670e2865e84ee440fc9391251fcf4593b67575c8f0572af7ce8dd3ea`
- 实验臂：harness_full
- 配置哈希：5987b485053e210f, e01930fccf2d52c2, e76ae9cc5c5f1c4f
- 运行模式：离线 fixture 回放（确定性，无网络、无费用）

> **数据性质声明**：本报告的任务集 gold_status 分布为 `{"synthetic_draft": 87, "machine_verified": 1}`，其中 human_verified = **0**。**没有任何任务经过人工复核**，因此以下全部指标只能作为 draft/synthetic 结果，不得作为已验证的效果结论对外引用。

## 总体指标

| 指标 | 数值 |
|---|---|
| Task Success Rate（全部 trial 均通过） | 0.8068 |
| Task Success Rate（至少一次通过） | 0.8068 |
| 综合评分均值 | 0.9301 |
| 综合评分最差值 | 0.7465 |
| 同任务多 trial 评分标准差均值 | 0 |
| Tool Selection Precision | 0.5 |
| Necessary Tool Recall | n/a |
| Unnecessary Tool Call Rate | n/a |
| Tool Outcome Success | 0.9119 |
| Alternative Valid Path Rate | n/a |
| Tool Selection Recall | 1 |
| Tool Selection F1 | 0.65 |
| Tool Argument Accuracy | 1 |
| Invalid Tool Call Rate | 0 |
| Redundant Tool Call Rate | 0.0306 |
| ~~Recovery Success Rate~~（已废弃，见下方六项恢复指标） | 0 |
| 同工具重试恢复率 | 0.6875 |
| 跨工具/降级恢复率 | 0.4762 |
| 降级仍完成率 | 0.3529 |
| Checkpoint 恢复成功率 | 1 |
| False Success Rate | 0.0114 |
| 成功但含无源数字率 | 0.0154 |
| Unresolved Failure Rate | 0 |
| Citation Validity | 0.9977 |
| Citation Coverage | 0.7318 |
| Number Grounding Rate | 0.9956 |
| Unsupported Number Rate | 0.0044 |
| Budget Exceeded Rate | 0.125 |
| 单次运行估算费用(USD) | 0 |
| 平均输入 Token | 0 |
| 平均输出 Token | 0 |
| 平均工具调用数 | 13.5909 |
| 时延 avg / p50 / p95 / max (s) | 0.7554 / 0.6709 / 1.1732 / 3.9834 |

### 安全

- Prompt Injection 导致的工具劫持次数：**0**
- 注入内容泄漏进报告的次数：**0**

### 运行结束状态分布

| 状态 | 次数 |
|---|---|
| degraded | 6 |
| insufficient | 23 |
| succeeded | 59 |

停止原因分布（回答“为什么在这里停止”）：

| stop_reason | 次数 |
|---|---|
| budget_exhausted | 11 |
| completed | 59 |
| enough_evidence | 6 |
| entity_unverified | 1 |
| no_usable_sources | 11 |

## 分类别

| 类别 | 任务数 | 全通过率 | 综合评分 | 数字溯源率 | 无源数字率 | p95 时延(s) |
|---|---|---|---|---|---|---|
| company_research | 9 | 1 | 0.9857 | 1 | 0 | 1.7175 |
| industry_research | 4 | 1 | 0.9868 | 1 | 0 | 3.9834 |
| insufficient_budget | 11 | 0 | 0.7901 | n/a | n/a | 0.3897 |
| long_context | 1 | 1 | 0.7465 | 0.714 | 0.2857 | 0.7784 |
| macro_research | 6 | 1 | 0.9918 | 1 | 0 | 0.6709 |
| multi_source_verification | 1 | 1 | 0.9829 | 1 | 0 | 0.5947 |
| must_refuse | 1 | 1 | 0.9886 | n/a | n/a | 0.4965 |
| pdf_extraction | 1 | 1 | 0.9301 | 1 | 0 | 0.6128 |
| prompt_injection | 10 | 1 | 0.9952 | 1 | 0 | 0.8165 |
| resume_after_interrupt | 11 | 1 | 0.9948 | 1 | 0 | 1.2011 |
| structured_data | 11 | 0.4545 | 0.8836 | 1 | 0 | 1.0409 |
| tool_timeout | 11 | 1 | 0.9904 | 1 | 0 | 1.0972 |
| unreachable_source | 11 | 1 | 0.8392 | n/a | n/a | 0.6217 |

## 分 split

| split | 任务数 | 全通过率 | 综合评分 |
|---|---|---|---|
| dev | 88 | 0.8068 | 0.9301 |

## 方法与限制

- 所有数字由 `evals/reports/build_report.py` 从原始 JSONL 行计算，无人工填写路径。
- Citation Validity 的定义是“报告中的 `[sN]` 标记都能解析到真实 source_id”，**不校验被引页面是否真的支持该句结论**——这是工程引用检查，不是事实核查。
- Number Grounding 把数字分为 sourced / akshare / assumption / calculated / unsourced 五类，假设与测算如实归类，不洗成“有来源”。
- Tool Selection 指标只在任务显式声明了期望/禁用工具时计算，其余任务不计入分母。
- 离线模式下模型被确定性 stub 替换，因此 token/费用为 0，**不能**据此宣称真实成本；真实成本必须用 `--live` 跑出来。