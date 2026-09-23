# Agent 评测报告

- 生成时间：2026-09-09T07:39:51+00:00
- 原始结果文件：`evals\results\dev_baseline_v4.jsonl`（264 行，88 个任务，每任务 3.0 次 trial）
- 原始结果 SHA-256：`10943b089fea749f08464b8f2b009e103f614197334b63d53f1ed185abf9ee7e`
- 实验臂：baseline_v4
- 配置哈希：2384a67fd7e63e9d, 72aff97c2bc5e1d8, 954b9cfab73920b0
- 运行模式：离线 fixture 回放（确定性，无网络、无费用）

> **数据性质声明**：本报告的任务集 gold_status 分布为 `{"synthetic_draft": 261, "machine_verified": 3}`，其中 human_verified = **0**。**没有任何任务经过人工复核**，因此以下全部指标只能作为 draft/synthetic 结果，不得作为已验证的效果结论对外引用。

## 总体指标

| 指标 | 数值 |
|---|---|
| Task Success Rate（全部 trial 均通过） | 0.6818 |
| Task Success Rate（至少一次通过） | 0.6818 |
| 综合评分均值 | 0.8752 |
| 综合评分最差值 | 0.2857 |
| 同任务多 trial 评分标准差均值 | 0 |
| Tool Selection Precision | 1 |
| Tool Selection Recall | 1 |
| Tool Selection F1 | 1 |
| Tool Argument Accuracy | 1 |
| Invalid Tool Call Rate | 0 |
| Redundant Tool Call Rate | 0.035 |
| Recovery Success Rate | 0 |
| Citation Validity | 0.9974 |
| Citation Coverage | 0.6935 |
| Number Grounding Rate | 0.9947 |
| Unsupported Number Rate | 0.0053 |
| Budget Exceeded Rate | 0.125 |
| 单次运行估算费用(USD) | 0 |
| 平均输入 Token | 0 |
| 平均输出 Token | 0 |
| 平均工具调用数 | 11.7841 |
| 时延 avg / p50 / p95 / max (s) | 1.4666 / 0.8837 / 4.4789 / 7.0781 |

### 安全

- Prompt Injection 导致的工具劫持次数：**0**
- 注入内容泄漏进报告的次数：**0**

### 运行结束状态分布

| 状态 | 次数 |
|---|---|
| degraded | 18 |
| failed | 33 |
| insufficient | 69 |
| succeeded | 144 |

停止原因分布（回答“为什么在这里停止”）：

| stop_reason | 次数 |
|---|---|
| budget_exhausted | 33 |
| completed | 144 |
| enough_evidence | 18 |
| entity_unverified | 3 |
| no_usable_sources | 33 |
| unknown | 33 |

## 分类别

| 类别 | 任务数 | 全通过率 | 综合评分 | 数字溯源率 | 无源数字率 | p95 时延(s) |
|---|---|---|---|---|---|---|
| company_research | 9 | 1 | 0.9907 | 1 | 0 | 1.0884 |
| industry_research | 4 | 1 | 1 | 1 | 0 | 5.7894 |
| insufficient_budget | 11 | 0 | 0.772 | n/a | n/a | 4.0737 |
| long_context | 1 | 1 | 0.9031 | 0.714 | 0.2857 | 1.0046 |
| macro_research | 6 | 1 | 1 | 1 | 0 | 1.2879 |
| multi_source_verification | 1 | 1 | 1 | 1 | 0 | 0.891 |
| must_refuse | 1 | 1 | 1 | n/a | n/a | 0.6177 |
| pdf_extraction | 1 | 1 | 0.9 | 1 | 0 | 0.9634 |
| prompt_injection | 10 | 1 | 0.9936 | 1 | 0 | 4.6833 |
| resume_after_interrupt | 11 | 0 | 0.5219 | n/a | n/a | 6.8909 |
| structured_data | 11 | 0.4545 | 0.8615 | 1 | 0 | 3.4877 |
| tool_timeout | 11 | 1 | 0.9924 | 1 | 0 | 1.3193 |
| unreachable_source | 11 | 1 | 0.8852 | n/a | n/a | 1.0174 |

## 分 split

| split | 任务数 | 全通过率 | 综合评分 |
|---|---|---|---|
| dev | 88 | 0.6818 | 0.8752 |

## 执行错误（如实列出，不隐藏）

- `scen_resume_after_interrupt`: ValueError: no checkpoint found for run 'b54e62cc9aa3'
- `scen_resume_after_interrupt`: ValueError: no checkpoint found for run '994bc836d657'
- `scen_resume_after_interrupt`: ValueError: no checkpoint found for run 'b0bbe43e4e7c'
- `mx_resume_万华化学2026年一季度跟踪报告`: ValueError: no checkpoint found for run 'f60e17a2185a'
- `mx_resume_万华化学2026年一季度跟踪报告`: ValueError: no checkpoint found for run '76c67f5cb7b3'
- `mx_resume_万华化学2026年一季度跟踪报告`: ValueError: no checkpoint found for run '2723aebc0c41'
- `mx_resume_CPI与PPI走势分析`: ValueError: no checkpoint found for run 'c84ea17c4acb'
- `mx_resume_CPI与PPI走势分析`: ValueError: no checkpoint found for run '0aaa48dcfe4c'
- `mx_resume_CPI与PPI走势分析`: ValueError: no checkpoint found for run '0b3c58c071f1'
- `mx_resume_低空经济行业研报`: ValueError: no checkpoint found for run 'bb92849afca0'
- `mx_resume_低空经济行业研报`: ValueError: no checkpoint found for run '6a5ab24efc79'
- `mx_resume_低空经济行业研报`: ValueError: no checkpoint found for run '2f4ba605d756'
- `mx_resume_中芯国际投资风险分析`: ValueError: no checkpoint found for run 'c2d17bc30e66'
- `mx_resume_中芯国际投资风险分析`: ValueError: no checkpoint found for run '5d17f0ecbcb8'
- `mx_resume_中芯国际投资风险分析`: ValueError: no checkpoint found for run 'd2ce07604fa6'
- `mx_resume_中国经济灰犀牛风险监测`: ValueError: no checkpoint found for run '9123df5c68a1'
- `mx_resume_中国经济灰犀牛风险监测`: ValueError: no checkpoint found for run '5542da943c2f'
- `mx_resume_中国经济灰犀牛风险监测`: ValueError: no checkpoint found for run '2aa9473c04ab'
- `mx_resume_中芯国际财务与风险分析`: ValueError: no checkpoint found for run '77554a8e24aa'
- `mx_resume_中芯国际财务与风险分析`: ValueError: no checkpoint found for run 'c5c8902dd38a'

## 方法与限制

- 所有数字由 `evals/reports/build_report.py` 从原始 JSONL 行计算，无人工填写路径。
- Citation Validity 的定义是“报告中的 `[sN]` 标记都能解析到真实 source_id”，**不校验被引页面是否真的支持该句结论**——这是工程引用检查，不是事实核查。
- Number Grounding 把数字分为 sourced / akshare / assumption / calculated / unsourced 五类，假设与测算如实归类，不洗成“有来源”。
- Tool Selection 指标只在任务显式声明了期望/禁用工具时计算，其余任务不计入分母。
- 离线模式下模型被确定性 stub 替换，因此 token/费用为 0，**不能**据此宣称真实成本；真实成本必须用 `--live` 跑出来。