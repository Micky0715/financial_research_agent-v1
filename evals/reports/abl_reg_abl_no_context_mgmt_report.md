# Agent 评测报告

- 生成时间：2026-09-12T18:08:51+00:00
- 原始结果文件：`evals/results/abl_reg_abl_no_context_mgmt.jsonl`（13 行，13 个任务，每任务 1.0 次 trial）
- 原始结果 SHA-256：`eefa2153b1941210e48571a3f5c969457e1b91fa8458c29c6fa705d3ce56ad6e`
- 实验臂：abl_no_context_mgmt
- 配置哈希：1368c3bafc2cd5a6
- 运行模式：离线 fixture 回放（确定性，无网络、无费用）

> **数据性质声明**：本报告的任务集 gold_status 分布为 `{"needs_human_review": 13}`，其中 human_verified = **0**。**没有任何任务经过人工复核**，因此以下全部指标只能作为 draft/synthetic 结果，不得作为已验证的效果结论对外引用。

## 总体指标

| 指标 | 数值 |
|---|---|
| Task Success Rate（全部 trial 均通过） | 0.8462 |
| Task Success Rate（至少一次通过） | 0.8462 |
| 综合评分均值 | 0.8849 |
| 综合评分最差值 | 0.749 |
| 同任务多 trial 评分标准差均值 | 0 |
| Tool Selection Precision | 0.6795 |
| Necessary Tool Recall | 1 |
| Unnecessary Tool Call Rate | 0 |
| Tool Outcome Success | 0.7696 |
| Alternative Valid Path Rate | n/a |
| Tool Selection Recall | 1 |
| Tool Selection F1 | 0.7949 |
| Tool Argument Accuracy | 1 |
| Invalid Tool Call Rate | 0 |
| Redundant Tool Call Rate | 0.0739 |
| Recovery Success Rate | 0 |
| Citation Validity | 1 |
| Citation Coverage | 0.4615 |
| Number Grounding Rate | 0.881 |
| Unsupported Number Rate | 0.1191 |
| Budget Exceeded Rate | 0 |
| 单次运行估算费用(USD) | 0 |
| 平均输入 Token | 0 |
| 平均输出 Token | 0 |
| 平均工具调用数 | 15.3077 |
| 时延 avg / p50 / p95 / max (s) | 0.7402 / 0.6751 / 1.5495 / 1.5495 |

### 安全

- Prompt Injection 导致的工具劫持次数：**0**
- 注入内容泄漏进报告的次数：**0**

### 运行结束状态分布

| 状态 | 次数 |
|---|---|
| degraded | 1 |
| insufficient | 7 |
| succeeded | 5 |

停止原因分布（回答“为什么在这里停止”）：

| stop_reason | 次数 |
|---|---|
| completed | 5 |
| enough_evidence | 1 |
| entity_unverified | 4 |
| no_usable_sources | 3 |

## 分类别

| 类别 | 任务数 | 全通过率 | 综合评分 | 数字溯源率 | 无源数字率 | p95 时延(s) |
|---|---|---|---|---|---|---|
| company_research | 11 | 0.9091 | 0.8838 | 0.8572 | 0.1429 | 1.5495 |
| industry_research | 1 | 0 | 0.7893 | n/a | n/a | 0.51 |
| macro_research | 1 | 1 | 0.993 | 1 | 0 | 0.7173 |

## 分 split

| split | 任务数 | 全通过率 | 综合评分 |
|---|---|---|---|
| regression | 13 | 0.8462 | 0.8849 |

## 方法与限制

- 所有数字由 `evals/reports/build_report.py` 从原始 JSONL 行计算，无人工填写路径。
- Citation Validity 的定义是“报告中的 `[sN]` 标记都能解析到真实 source_id”，**不校验被引页面是否真的支持该句结论**——这是工程引用检查，不是事实核查。
- Number Grounding 把数字分为 sourced / akshare / assumption / calculated / unsourced 五类，假设与测算如实归类，不洗成“有来源”。
- Tool Selection 指标只在任务显式声明了期望/禁用工具时计算，其余任务不计入分母。
- 离线模式下模型被确定性 stub 替换，因此 token/费用为 0，**不能**据此宣称真实成本；真实成本必须用 `--live` 跑出来。