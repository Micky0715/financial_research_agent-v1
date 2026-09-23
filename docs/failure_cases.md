# 失败案例与回归证据

## 本次发现并修复

| 问题 | 根因 | 修复 | 验证 |
|---|---|---|---|
| Fallback 重复核心查询 | 强制 fallback 重新执行同一批 research queries | 复用核心结果，只执行新增且去重的 fallback queries | research/harness 集成测试与 smoke Trace |
| 并发 API Trace 串线 | Harness run 绑定使用进程全局 executor；线程池子任务未传播 ContextVar | 仅使用 ContextVar，并对每个并发查询用独立 `copy_context()` 传播 | `test_concurrent_harness_runs_keep_tool_traces_isolated` |
| 离线 macro 测试仍可能触网 | fixture 只拦截通用 HTTP，未覆盖直接 macro collector | 增加 macro corpus/snapshot，并 patch 直连 collector | eval fixture 测试、5/5 smoke |
| Productive query 记忆始终为空 | writer 从 completed 事件取 arguments，而参数只记录在 started 事件 | 按 step_id 关联 started/completed 后抽取成功参数 | memory 集成/单元测试 |
| 伪分析 JSON 导致字段缺失 | 离线 LLM stub 不满足 AnalyzeAgent 的完整 schema | 使用包含七个必需段落的稳定 JSON fixture | smoke 无缺字段降级告警 |

## 正确失败而非伪成功

- 不可验证实体以 `entity_unverified` / `insufficient` 停止。
- 无可用来源以 `no_usable_sources` 停止。
- 预算耗尽以 `budget_exhausted` 停止并写 budget event。
- PDF 或结构化金融数据失败允许基于其他证据降级，结果显式为 `degraded`；没有任何证据时不生成成功状态。
- 非临时性认证、参数、schema 与 policy 错误不盲目重试。

这些场景由 `tests/harness/test_integration_pipeline.py`、`test_failure_matrix.py`、`test_recovery_semantics.py`、`test_security.py` 及 eval 数据集覆盖。

## Regression 数据限制

当前 41 条回归集满足数量要求，但其中 31 条来自历史 Bad Case 文档。自动转换把部分“问题描述”直接当作研报主题，导致一次 Harness 运行 30 条以 `entity_unverified` 结束，总体 Task Success 0.3659。该结果不能证明功能严重退化，也不能作为通过门禁的证据。

修复方向是把每条历史问题改造成：真实 ResearchRequest、明确故障注入、期望工具/终态、禁止声明及人工审核状态。完成前，optimization gate 必须把该套件视为未准备完成，候选不得自动批准。

## 未消除的现实风险

- 真实网页的反爬、内容漂移、付费墙、动态脚本和 Provider 限流只在 fixture 中近似。
- 本地线程超时无法强制终止已进入第三方阻塞调用的 Python 线程。
- API 队列状态在服务重启后丢失，尽管 Harness checkpoint 仍可用于显式恢复。
- 多租户 API 尚未接认证主体，默认 `api_local` namespace 不能直接用于共享生产服务。
- 引用 URL 合法不等于内容权威；投资结论仍需人工审核。
