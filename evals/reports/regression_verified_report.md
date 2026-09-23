# 重构回归集验证报告（legacy vs harness）

- 生成时间：2026-09-12T18:19:14+00:00
- 运行模式：离线 fixture 回放（确定性；token 与费用恒为 0，不含真实成本结论）
- harness 结果：`evals/results/regression_rebuilt_harness.jsonl`  sha256 `e3a2e979320e03d4…`
- legacy 结果：`evals/results/regression_rebuilt_legacy.jsonl`  sha256 `d87d6e2e88087b82…`
- 任务集：13 条可执行回归 × 3 trials（由 31 条真实 bad case 重构，见 docs/regression_rebuild.md）

> ✅ 对照有效性检查通过：任务集、trial 数、Grader 集合三项一致。

## 1. 总体

| 指标 | legacy（无 Harness） | harness | Δ |
|---|---|---|---|
| Task Success（全 trial 通过） | 0.8462 | 0.8462 | +0.0 |
| 通过任务数 | 11/13 | 11/13 | — |
| 不稳定任务（部分 trial 通过） | 0 | 0 | — |
| 有结构化 Trace | **否** | 是 | — |

## 2. 六个恢复/诚信指标

`n/a` = 该臂没有这项能力或本次无适用运行，不是 0 分。legacy 无 trace，因此所有事件派生指标不存在——这正是 Harness 的主要收益。

| 指标 | legacy | n(legacy) | harness | n(harness) | 方向 |
|---|---|---|---|---|---|
| 同工具重试恢复 | n/a | 0 | 0 | 3 | 越高越好 |
| 跨工具/降级恢复 | n/a | 0 | 0.4444 | 27 | 越高越好 |
| 降级仍完成 | n/a | 0 | 1 | 3 | 越高越好 |
| Checkpoint 恢复成功 | n/a | 0 | n/a | 0 | 越高越好 |
| False Success | 0 | 39 | 0 | 39 | 越低越好 |
| 成功但含无源数字 | 0.1667 | 18 | 0.1667 | 18 | 越低越好 |
| Unresolved Failure | 0.5385 | 39 | 0 | 39 | 越低越好 |

## 3. 按原缺陷类型分组

| Bad Case | 原缺陷 | 故障注入 | legacy | harness |
|---|---|---|---|---|
| 2 | Planner 输出重复任务导致执行链路不稳定 | `default` | 通过 | 通过 |
| 3 | QualityScorer 完整 topic 匹配导致相关性误杀 | `alias_only_sources` | 通过 | 通过 |
| 4 | BrowserAgent 对同一批 URL 无意义重试 | `all_403` | 通过 | 通过 |
| 5 | source_count 太低但 Evaluation 虚高 | `single_usable_source` | 通过 | 通过 |
| 8 | 网页/PDF 抓取中的 403/404/empty_content 问题 | `empty_pages` | 通过 | 通过 |
| 9 | 行业研报复用公司财务指标，导致 financial_depth_score  | `default` | **失败** | **失败** |
| 13 | 语义排序把同站点内容批量推进抓取窗口，放大单站反爬风险 | `single_domain_flood` | 通过 | 通过 |
| 14 | 通用句子切分器在英文句点处断句，带小数的金额全部抽不到 | `decimal_heavy_numbers` | **失败** | **失败** |
| 16 | 虚构公司主题也能凑出 5 个"相关"来源（groundedness 缺口） | `fictional_entity` | 通过 | 通过 |
| 18 | AkShare 东方财富系接口在本环境全部 ProxyError | `akshare_down` | 通过 | 通过 |
| 22 | PDF 导出依赖 HTML 报告，markdown-only run 无 P | `default` | 通过 | 通过 |
| 23 | 虚构公司 E2E——纵深防御实录（相关性层被骗，实体层拦截） | `fictional_entity` | 通过 | 通过 |
| 31 | entity_validator 对宏观主题和非内置行业主题的误杀（30-c | `default` | 通过 | 通过 |

## 4. 未被覆盖的 bad case（如实列出）

31 条 bad case 中只有 13 条可复现为可执行任务，其余按类别归档：

| 类别 | 条数 | 含义 |
|---|---|---|
| `deterministic` | 13 | 已重构为可执行任务 |
| `not_agent_behaviour` | 9 | 真实缺陷，但属于开发工具或性能，不是 agent 行为回归 |
| `needs_network` | 5 | 只能在真实 provider / TLS / 子进程下复现 |
| `needs_human_review` | 4 | 期望值必须由人设定（会计口径、行业判断） |

详细逐条理由见 `docs/regression_rebuild.md` 第 2.1 节与 `evals/datasets/regression_manifest.json`。

## 5. 读法与限制

- **13 条规模不足以做统计显著性判断**，只能当作"这些历史缺陷没有复现"的存在性检查。
- **13 条期望值均为 `needs_human_review`**：缺陷是真的、已复现，但"正确终态应该是什么"是作者推断，未经人工复核。
- **9 条 `needs_network` + `needs_human_review` 的缺陷目前零覆盖**，其中 4 条是金融正确性问题，恰恰最需要人工基准。
- 离线 stub 下 token 与费用恒为 0，本报告不含任何真实成本结论。