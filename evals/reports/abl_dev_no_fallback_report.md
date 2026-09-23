# Agent 评测报告

- 生成时间：2026-09-12T18:10:01+00:00
- 原始结果文件：`evals/results/abl_dev_no_fallback.jsonl`（88 行，88 个任务，每任务 1.0 次 trial）
- 原始结果 SHA-256：`ea75d4d7ee9ac2245be2d72d80a363418b513a9f84a92ef7e8948c64c705cdb1`
- 实验臂：no_fallback
- 配置哈希：110ccfc3b28e67dc, 276a96278cc745c0, f71d51649375af20
- 运行模式：离线 fixture 回放（确定性，无网络、无费用）

> **数据性质声明**：本报告的任务集 gold_status 分布为 `{"synthetic_draft": 87, "machine_verified": 1}`，其中 human_verified = **0**。**没有任何任务经过人工复核**，因此以下全部指标只能作为 draft/synthetic 结果，不得作为已验证的效果结论对外引用。

## 总体指标

| 指标 | 数值 |
|---|---|
| Task Success Rate（全部 trial 均通过） | 0.8068 |
| Task Success Rate（至少一次通过） | 0.8068 |
| 综合评分均值 | 0.9294 |
| 综合评分最差值 | 0.7427 |
| 同任务多 trial 评分标准差均值 | 0 |
| Tool Selection Precision | 0.5 |
| Necessary Tool Recall | n/a |
| Unnecessary Tool Call Rate | n/a |
| Tool Outcome Success | 0.8838 |
| Alternative Valid Path Rate | n/a |
| Tool Selection Recall | 1 |
| Tool Selection F1 | 0.65 |
| Tool Argument Accuracy | 1 |
| Invalid Tool Call Rate | 0 |
| Redundant Tool Call Rate | 0 |
| Recovery Success Rate | 0 |
| Citation Validity | 0.9977 |
| Citation Coverage | 0.7318 |
| Number Grounding Rate | 0.9956 |
| Unsupported Number Rate | 0.0044 |
| Budget Exceeded Rate | 0.125 |
| 单次运行估算费用(USD) | 0 |
| 平均输入 Token | 0 |
| 平均输出 Token | 0 |
| 平均工具调用数 | 9.9886 |
| 时延 avg / p50 / p95 / max (s) | 0.7532 / 0.6856 / 1.0429 / 4.0525 |

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
| company_research | 9 | 1 | 0.9796 | 1 | 0 | 1.6237 |
| industry_research | 4 | 1 | 0.9809 | 1 | 0 | 4.0525 |
| insufficient_budget | 11 | 0 | 0.7901 | n/a | n/a | 0.3343 |
| long_context | 1 | 1 | 0.7427 | 0.714 | 0.2857 | 0.7691 |
| macro_research | 6 | 1 | 0.9861 | 1 | 0 | 0.7427 |
| multi_source_verification | 1 | 1 | 0.9829 | 1 | 0 | 0.6214 |
| must_refuse | 1 | 1 | 0.9815 | n/a | n/a | 0.4604 |
| pdf_extraction | 1 | 1 | 0.9251 | 1 | 0 | 0.6164 |
| prompt_injection | 10 | 1 | 0.995 | 1 | 0 | 0.7759 |
| resume_after_interrupt | 11 | 1 | 0.9946 | 1 | 0 | 1.0498 |
| structured_data | 11 | 0.4545 | 0.8827 | 1 | 0 | 0.9879 |
| tool_timeout | 11 | 1 | 0.9895 | 1 | 0 | 1.2154 |
| unreachable_source | 11 | 1 | 0.8472 | n/a | n/a | 0.6378 |

## 分 split

| split | 任务数 | 全通过率 | 综合评分 |
|---|---|---|---|
| dev | 88 | 0.8068 | 0.9294 |

## 方法与限制

- 所有数字由 `evals/reports/build_report.py` 从原始 JSONL 行计算，无人工填写路径。
- Citation Validity 的定义是“报告中的 `[sN]` 标记都能解析到真实 source_id”，**不校验被引页面是否真的支持该句结论**——这是工程引用检查，不是事实核查。
- Number Grounding 把数字分为 sourced / akshare / assumption / calculated / unsourced 五类，假设与测算如实归类，不洗成“有来源”。
- Tool Selection 指标只在任务显式声明了期望/禁用工具时计算，其余任务不计入分母。
- 离线模式下模型被确定性 stub 替换，因此 token/费用为 0，**不能**据此宣称真实成本；真实成本必须用 `--live` 跑出来。