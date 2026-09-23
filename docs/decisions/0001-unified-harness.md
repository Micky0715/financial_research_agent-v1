# ADR-0001：在既有工作流外增加统一 Harness

- 状态：Accepted
- 日期：2026-09-09

## 决策

保留 `WorkflowOrchestrator` 和现有业务 Agent，在 CLI/API 与工作流之间增加 `HarnessRunner`。工具通过 gateway 感知当前 run，上下文、预算、Trace 和 checkpoint 由 Harness 统一控制；提供显式 legacy 回滚。

## 原因

直接重写会放大业务回归，并重复已有检索、分析、引用和报告能力。外层控制面能以较小改动统一可靠性和可观测性，同时允许逐步把旧工具迁入 Registry。

## 后果

优点是兼容现有入口、可独立评测和恢复；代价是过渡期存在 Harness 与旧 workflow 两层状态，gateway 必须正确传播 ContextVar。测试必须覆盖并发 run 隔离。
