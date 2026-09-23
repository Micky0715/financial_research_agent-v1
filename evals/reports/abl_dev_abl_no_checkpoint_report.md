# Agent 评测报告

- 生成时间：2026-09-12T18:05:41+00:00
- 原始结果文件：`evals/results/abl_dev_abl_no_checkpoint.jsonl`（88 行，88 个任务，每任务 1.0 次 trial）
- 原始结果 SHA-256：`955cc445138704311788ceb0191213482625a7505431336eb5f764202adce777`
- 实验臂：abl_no_checkpoint
- 配置哈希：a27e4b346bd5fe2e, b323d488a26107b1, cea75aa015159893
- 运行模式：离线 fixture 回放（确定性，无网络、无费用）

> **数据性质声明**：本报告的任务集 gold_status 分布为 `{"synthetic_draft": 87, "machine_verified": 1}`，其中 human_verified = **0**。**没有任何任务经过人工复核**，因此以下全部指标只能作为 draft/synthetic 结果，不得作为已验证的效果结论对外引用。

## 总体指标

| 指标 | 数值 |
|---|---|
| Task Success Rate（全部 trial 均通过） | 0.6818 |
| Task Success Rate（至少一次通过） | 0.6818 |
| 综合评分均值 | 0.8737 |
| 综合评分最差值 | 0.4286 |
| 同任务多 trial 评分标准差均值 | 0 |
| Tool Selection Precision | 0.5 |
| Necessary Tool Recall | n/a |
| Unnecessary Tool Call Rate | n/a |
| Tool Outcome Success | 0.9003 |
| Alternative Valid Path Rate | n/a |
| Tool Selection Recall | 1 |
| Tool Selection F1 | 0.65 |
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
| 时延 avg / p50 / p95 / max (s) | 0.5518 / 0.4904 / 0.8532 / 4.049 |

### 安全

- Prompt Injection 导致的工具劫持次数：**0**
- 注入内容泄漏进报告的次数：**0**

### 运行结束状态分布

| 状态 | 次数 |
|---|---|
| degraded | 6 |
| failed | 11 |
| insufficient | 23 |
| succeeded | 48 |

停止原因分布（回答“为什么在这里停止”）：

| stop_reason | 次数 |
|---|---|
| budget_exhausted | 11 |
| completed | 48 |
| enough_evidence | 6 |
| entity_unverified | 1 |
| no_usable_sources | 11 |
| unknown | 11 |

## 分类别

| 类别 | 任务数 | 全通过率 | 综合评分 | 数字溯源率 | 无源数字率 | p95 时延(s) |
|---|---|---|---|---|---|---|
| company_research | 9 | 1 | 0.9857 | 1 | 0 | 1.5044 |
| industry_research | 4 | 1 | 0.9928 | 1 | 0 | 4.049 |
| insufficient_budget | 11 | 0 | 0.7901 | n/a | n/a | 0.3462 |
| long_context | 1 | 1 | 0.7465 | 0.714 | 0.2857 | 0.6369 |
| macro_research | 6 | 1 | 0.9896 | 1 | 0 | 0.508 |
| multi_source_verification | 1 | 1 | 0.9829 | 1 | 0 | 0.4425 |
| must_refuse | 1 | 1 | 0.9829 | n/a | n/a | 0.4316 |
| pdf_extraction | 1 | 1 | 0.9265 | 1 | 0 | 0.4469 |
| prompt_injection | 10 | 1 | 0.9952 | 1 | 0 | 0.6673 |
| resume_after_interrupt | 11 | 0 | 0.5441 | n/a | n/a | 0.4413 |
| structured_data | 11 | 0.4545 | 0.8836 | 1 | 0 | 0.8901 |
| tool_timeout | 11 | 1 | 0.9901 | 1 | 0 | 0.8996 |
| unreachable_source | 11 | 1 | 0.8392 | n/a | n/a | 0.6474 |

## 分 split

| split | 任务数 | 全通过率 | 综合评分 |
|---|---|---|---|
| dev | 88 | 0.6818 | 0.8737 |

## 执行错误（如实列出，不隐藏）

- `scen_resume_after_interrupt`: ValueError: no checkpoint found for run '2bdc6b6eb564'
- `mx_resume_万华化学2026年一季度跟踪报告`: ValueError: no checkpoint found for run 'bb3baf9347cb'
- `mx_resume_CPI与PPI走势分析`: ValueError: no checkpoint found for run '78ce6f0957ee'
- `mx_resume_低空经济行业研报`: ValueError: no checkpoint found for run '9942f9f49469'
- `mx_resume_中芯国际投资风险分析`: ValueError: no checkpoint found for run 'ff6b38945dc9'
- `mx_resume_中国经济灰犀牛风险监测`: ValueError: no checkpoint found for run 'f401098cd459'
- `mx_resume_中芯国际财务与风险分析`: ValueError: no checkpoint found for run '461ce7fb2da2'
- `mx_resume_人民币汇率走势分析`: ValueError: no checkpoint found for run '199abcd11b51'
- `mx_resume_全球流动性与美联储政策分析`: ValueError: no checkpoint found for run '3b1429ce874f'
- `mx_resume_创新药行业投资机会分析`: ValueError: no checkpoint found for run 'a5a60d806811'
- `mx_resume_恒瑞医药研究报告`: ValueError: no checkpoint found for run 'c17b1ec60b7e'

## 方法与限制

- 所有数字由 `evals/reports/build_report.py` 从原始 JSONL 行计算，无人工填写路径。
- Citation Validity 的定义是“报告中的 `[sN]` 标记都能解析到真实 source_id”，**不校验被引页面是否真的支持该句结论**——这是工程引用检查，不是事实核查。
- Number Grounding 把数字分为 sourced / akshare / assumption / calculated / unsourced 五类，假设与测算如实归类，不洗成“有来源”。
- Tool Selection 指标只在任务显式声明了期望/禁用工具时计算，其余任务不计入分母。
- 离线模式下模型被确定性 stub 替换，因此 token/费用为 0，**不能**据此宣称真实成本；真实成本必须用 `--live` 跑出来。