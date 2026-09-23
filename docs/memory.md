# 长期记忆

## 模型

`src/memory` 区分三种记录：semantic（用户偏好、指标口径、别名、可靠来源）、episodic（经验证的检索/恢复经验）和 procedural（版本化工具、来源、停止与溯源规则）。每条记录带 namespace、subject/project、来源、置信度、重要度、时间、TTL、状态和版本。

记忆默认关闭。写入发生在任务结束后：先从可见 Trace 和可验证结果提取候选，再做 schema 校验、secret/Prompt Injection 过滤、去重、冲突检测和合并。未经验证的模型自评不写入。Procedural 候选不能直接覆盖 active 规则，必须审批，且保留版本和回滚。

检索综合关键词/本地 embedding 相似度、新近、重要度、使用频率、来源可信度与任务类型。过期、低分、跨 namespace/project 或注入污染记录被过滤；独立 token budget 限制注入。模型收到的记忆明确标为背景，事实仍必须由本次证据和引用支持。

用户可通过 Store/Manager 列出、纠正、停用和删除自己 namespace 内的记录。测试覆盖去重、冲突、新旧可信记录、TTL、namespace、项目隔离、注入阻断和删除。

## 多 Session A/B

本次离线构造实验为 3 个主题 × 2 sessions，比较 none、recent_only、vector_only、full。为隔离变量，来源 semantic ranking 被关闭；本地 embedding 预热 202.6227 秒不计入各臂时延。

| 策略 | 下游成功率 | Precision | Recall | 过期误用 | 冲突准确 | 泄漏 | 平均记忆 tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| none | 1 | n/a | n/a | 0 | n/a | 0 | 0 |
| recent_only | 1 | 0 | 0 | 0 | 1 | 0 | 17.6667 |
| vector_only | 1 | 0.5 | 1 | 1 | 1 | 0 | 102.3333 |
| full | 1 | 0.6667 | 1 | 0 | 1 | 0 | 78.6667 |

完整记忆没有改变下游成功率（两侧均 1.0），但增加 78.7 tokens。因此不能宣传“记忆增强效果”；当前仅证明偏好/别名复用和安全策略链路可运行。vector-only 的过期误用也说明仅靠相似度不够。

原始结果：`evals/results/memory_ab/memory_ab_results.json`；报告：`evals/reports/memory_ab_report.md`。这是植入相关性 ground truth 的构造实验，不代表真实多用户长期流量。
