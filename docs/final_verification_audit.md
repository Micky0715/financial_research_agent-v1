# 最终验收审计（Phase 1）

> 本文不复述上一轮实施报告，而是**逐条探测**其声明。
> 每个结论都由 `scripts/verify_harness_claims.py` 中一个可重跑的探针产生，
> 原始结果在 `docs/_evidence/verification_probes.json`。
> 探针**驱动生产入口**（`main.py` / FastAPI / 真实 orchestrator），不是读源码下结论。

复现：

```bash
python scripts/verify_harness_claims.py                 # 全部探针
python scripts/classify_tests.py                        # 测试分层统计
python -m pytest tests -q -p audit_netblock_plugin      # 硬断网跑全套测试
```

---

## 0. 起点：工作区来源界定

审计开始时的 `git status` 已保存为 `docs/_evidence/git_status_before.txt`：
**14 个已跟踪文件被修改、18 个未跟踪条目**，HEAD = `8daf13b`。

其中以下修改**不是本轮验收产生的**，属于接手前的上一轮开发：

| 文件 | 增量 | 内容 |
|---|---|---|
| `main.py` | +55 | CLI 接入 Harness，新增 `--legacy` / `--resume-run-id` / `--enable-memory` |
| `api/task_manager.py` | +28 | API 接入 Harness，终态映射 |
| `api/routes/reports.py`、`api/schemas.py` | +8 | 响应体暴露 harness 字段 |
| `agents/research_agent.py` | +54 | `contextvars.copy_context()` 传播 + forced-fallback 复用首轮候选 |
| `config.py` | +6 | `USE_AGENT_HARNESS` / `HARNESS_MAX_CONCURRENCY` / `HARNESS_CONTEXT_BUDGET_TOKENS` |
| `src/runtime/run_context.py` | 重写 | 进程级绑定 → 纯 ContextVar |
| `README.md` | 318 行改动 | 未在本轮验证其全部声明 |

本轮验收自身的改动见第 5 节。

---

## 1. 探针结果总表

| # | 声明 | 判定 | 关键证据 |
|---|---|---|---|
| 1 | CLI 默认路径经过 Harness | **PASS** | `python main.py`（无额外参数）exit=0，输出含 `Harness run:` 与 trace 路径；`USE_AGENT_HARNESS=True` |
| 2 | legacy 模式仍可运行 | **PASS** | `main.py --legacy` exit=0，输出**不含**任何 harness 字段 |
| 3 | API 默认路径经过 Harness | **PASS** | `POST /reports` → `GET /reports/{id}` 返回 `harness_run_id`、`harness_status=succeeded`，且 trace 文件真实存在 |
| 4 | API 状态与 Harness 终态一致 | **PASS** | `failed/cancelled/insufficient → failed`、`degraded → degraded` 映射存在于 `api/task_manager.py` |
| 5 | 并发 Harness 运行互不干扰 | **PASS** | 2 个线程并发运行，均 `succeeded`，无绑定冲突 |
| 6 | 线程池中的检索仍经过 executor | **PASS** | 生产路径 ResearchAgent：fixture 实际被调 **10** 次，trace 记录 **10** 次 |
| 7 | asyncio 任务内绑定正确、退出后解绑 | **PASS** | `await` 后仍可见，块退出后为 None |
| 8 | 预算耗尽后阻止后续工具调用 | **PASS** | 上限 2 → 实际到达工具层 **2** 次，2 次被 `BUDGET_EXCEEDED` 拦截，终态 `degraded` |
| 9 | 只重试临时错误 | **PASS** | TIMEOUT 尝试 **3** 次；AUTH 尝试 **1** 次 |
| 10 | 熔断后后续请求不再到达工具 | **PASS** | 熔断前工具被调 2 次，熔断后第 3 次调用后仍为 **2** 次 |
| 11 | **Checkpoint 真正跳过已完成步骤** | **修复前 FAIL → 修复后 PASS** | 见第 2 节 |
| 12 | 工具调用真实经过 ToolExecutor | **PASS** | fixture 被调 4 次 / trace `tool_call_started` 4 次 |
| 13 | 没有绕过网关的直接网络调用 | **PASS（有已知例外）** | `agents/` 中无直接 import；**2 个 `tools/*.py` 直接 `import akshare`**，不经 Registry/Executor |
| 14 | Trace 记录真实模型调用与 token | **PARTIAL** | 离线 stub 直接替换 `BaseAgent.call_llm`，不产生模型事件。**token/费用口径只能由 live canary 验证，目前未验证** |
| 15 | Trace 脱敏且不存隐藏推理 | **PASS** | `sk-*` / `Bearer *` / `reasoning_content` 均未落盘；`tokens_in`、`authority_tier` 等正常字段保留 |
| 16 | 记忆确实进入上下文 | **PASS** | 植入的标记文本出现在 `build_context()` 拼装结果中 |
| 17 | 记忆接入了实际运行链路 | **PASS** | `HarnessRunner` 中同时存在 `retrieve_for_run` 与 `learn_from_run` |
| 18 | 离线优化只生成文件、不自动部署 | **PASS** | 5 个版本提案全部 `proposed`；未审批版本调用 `apply_version` 抛 `ValueError`；无 overlay 生成 |
| 19 | **缓存命中的检索绕过 executor** | **PARTIAL（新发现）** | 见第 3 节 |

汇总：**PASS 16 / PARTIAL 3 / FAIL 0**（第 11 项修复后）。

---

## 2. 发现并修复：Resume 并未真正跳过已完成工作

### 现象（修复前实测）

| 指标 | 数值 |
|---|---|
| 首次运行工具调用 | 3 |
| **恢复运行工具调用** | **4（不降反增）** |
| `checkpoint_restored` 事件 | 1 |
| `step_skipped_idempotent` 事件 | **0** |
| resume plan 声称可跳过步骤 | 3 |

### 根因

`src/runtime/runner.py::prepare()` 调用 `plan_resume(restored)`，但**只把结果写进 trace**，
`plan.skip_step_keys` 从未被使用；`ToolExecutor._dedup` 每次运行都是空的。
因此"可恢复"只做到了恢复**状态**，没有恢复**已完成的工作**。

### 修复

1. `ToolExecutor.seed_completed_steps(state)`（新增）：从 checkpoint 中已 SUCCEEDED、
   幂等、无副作用且产物仍在 ArtifactStore 的工具步骤重建去重备忘录，
   通过工具自身的 projector 重算 compact 值，保证与首次运行返回值一致。
2. `_finish_success` 改为**总是**把原始返回写入 ArtifactStore
   （原先仅当 `full is not compact` 才写，导致 AkShare 这类直通工具无法被恢复）。
3. checkpoint 命中改发 `STEP_SKIPPED_IDEMPOTENT` 而非 `TOOL_CALL_DEDUPED`，
   避免把"恢复跳过"误报成"Agent 冗余调用"。
4. `runner.prepare()` 在恢复分支调用 seeding。

### 修复后实测

| 指标 | 数值 |
|---|---|
| 首次运行工具调用 | 3 |
| **恢复运行工具调用** | **0** |
| `step_skipped_idempotent` 事件 | **12** |

### 为什么原测试没发现

**320 个测试全部通过，却没有一个能发现这个缺陷。**
原有恢复相关断言只检查 `checkpoint_restored` 事件存在、
`plan.skip_step_keys` 非空 —— 即"计划说可以跳过"，
而没有断言"实际少调用了工具"。计划正确而执行未生效时，这类断言全部通过。

已补两条真实回归测试（`tests/harness/test_integration_pipeline.py`）：

- `test_resume_actually_skips_completed_work_not_just_reports_a_plan`
  断言**实际到达工具层的调用次数下降**；
- `test_resume_skips_are_not_counted_as_redundant_calls`
  断言恢复跳过不污染冗余指标。

**并已验证这两条测试确实能发现该缺陷**：临时注释掉 seeding 后，
两条测试立即失败（`2 failed, 1 passed`）；恢复后重新通过。

---

## 3. 新发现：热缓存下检索完全绕过 ToolExecutor

`agents/research_agent.py::_search_one_query` 先查磁盘缓存、再走网关：

```python
cached = get_cached_search_result(query)   # ← 先查缓存
if cached is not None: return ...
hits = web_search(query, ...)              # ← 才经过 gateway/executor
```

探针（同一主题跑两轮，第一轮保证冷缓存）：

| | 到达工具层 | trace 记录 | 预算计数 |
|---|---|---|---|
| 冷缓存 | 10 | 10 | 10 |
| **热缓存** | **0** | **0** | **0** |

即热缓存下 **10 次检索意图完全不可见**：无 trace 事件、不扣预算、不参与去重统计。
生产默认 `ENABLE_SEARCH_CACHE=True`。

**影响**：任何在热缓存下产出的 `tool_calls` / 成本 / 延迟数字都会系统性低估真实检索量。
这也解释了此前一次 dev 评测中 `web_search` 事件为 0 的异常。

**当前处理**：离线评测已在 `run_eval.py` 中强制关闭搜索缓存以保证可复现；
生产链路的缓存可见性**尚未修复**，作为已知边界记录，不写入任何对外指标。

---

## 4. 测试真实性审计

### 4.1 分层统计（322 条，`scripts/classify_tests.py`）

| 层级 | 数量 | 说明 |
|---|---|---|
| schema_data | 10 | pydantic 模型、序列化、枚举 |
| unit | 251 | 单函数、无 I/O |
| mock_integration | 37 | 多组件 + patch 边界 |
| fixture_e2e | 24 | 真实 orchestrator/harness + fixture 工具 |
| **live_network** | **0** | —— |
| **live_model** | **0** | —— |

### 4.2 关键结论：整套测试完全离线

```
python -m pytest tests -q -p audit_netblock_plugin
→ 322 passed
```

在**硬断网**（`requests` 全部适配器抛 ConnectionError）条件下 322 条全部通过。
因此：

> **没有任何一条测试验证真实 provider 或真实网络行为。**
> 这对 CI 可复现性是优点，但意味着"系统在真实网络下可用"这一点
> **完全没有测试覆盖**，只能由 live canary 提供证据（目前尚未运行，见第 6 节）。

附带发现：`tests/test_data_lineage.py` 等 3 条测试因源码中出现 `akshare` 字样
被启发式分类器误判为 live_network，实测在断网且移走 `outputs/cache/akshare/` 后
仍 18 条全通过，确认为离线自足；分类器已修正。

### 4.3 抽查 30+ 条测试的弱断言排查

自动扫描（`scripts/classify_tests.py` 的 weak-flag 检测，覆盖全部 322 条）
+ 人工复核，针对审计清单逐项：

| 排查项 | 结果 |
|---|---|
| Mock 直接返回预期答案 | **发现 1 类**：`evals/runners/run_eval.py::install_offline_llm` 的 stub 返回固定报告文本，其中的数字与 fixture 语料一致 —— 这使 `number_grounding`、`citation_validity` 在离线评测中天然接近满分。**已在评测报告的"方法与限制"中标注，不得作为真实溯源能力证据。** |
| 测试仅检查"不报错" | 扫描后 `no_assert` 命中 8 条，人工复核**全部为 `pytest.raises` 断言**（检查行为而非无异常），分类器已修正为不误报 |
| 测试与实现共享常量 | **发现 1 处**：`test_estimate_tokens_handles_cjk_and_ascii` 直接复现 `estimate_tokens` 的分段公式。保留但降级理解为"公式回归锁定"，不作为分词正确性证据 |
| 生成器与 Grader 共享答案逻辑 | **发现，且是本轮最重要的发现之一** —— 见 `docs/tool_eval_leakage_audit.md`：数据集不声明工具期望，Grader 便走"空期望 ⇒ 1.0"分支 |
| 弱断言无法发现回归 | **发现 1 处（已修复）**：resume 测试只断言"计划可跳过"，不断言"实际少调用"，见第 2 节 |
| 被测函数没有经过生产入口 | **发现 1 处（已修复）**：本审计初版探针用裸 `pool.map` 测 ContextVar 传播，未经过 ResearchAgent 生产路径，得到假阳性 FAIL；改为驱动真实 ResearchAgent 后为 PASS |
| Patch 未在测试结束后恢复 | **未发现**。`tests/harness/conftest.py` 全部使用 `monkeypatch` fixture（自动回滚）；`run_eval.py` 的 stub 用 try/finally + `restore_llm` 显式恢复；arm 的 config override 亦在 finally 中还原 |

仅剩 1 条自动标记待观察：
`tests/harness/test_memory.py::test_extraction_records_a_verified_entity_symbol`
（单条 `assert any(...)`），断言有效但覆盖面窄。

---

## 5. 本轮验收自身的改动

| 文件 | 性质 | 说明 |
|---|---|---|
| `src/tools/executor.py` | **实现修复** | 新增 `seed_completed_steps`；产物总是入库；区分恢复跳过与冗余调用 |
| `src/runtime/runner.py` | **实现修复** | 恢复分支真正应用 skip plan |
| `evals/graders/deterministic.py` | **口径修复** | 收紧 `ToolSelectionGrader`；新增 5 个工具指标 Grader |
| `evals/datasets/schema.py` | 结构新增 | `required_tools` / `acceptable_tool_paths` / `max_expected_tool_calls` / `expects_no_tools`；新增 2 个 gold_status |
| `evals/datasets/adversarial_tools.py` | 新增数据 | 31 条对抗任务 |
| `evals/runners/replay.py` | 新增场景 | `empty_search_results` / `empty_pages` / `injection_export_lure` |
| `evals/runners/run_eval.py` | 新增入口 | `--split adversarial` |
| `evals/reports/build_report.py` | 报告新增 | 5 项新工具指标 |
| `tests/harness/test_integration_pipeline.py` | **新增测试** | 2 条 resume 真实回归（已验证能发现缺陷） |
| `scripts/verify_harness_claims.py` | 新增工具 | 19 个可复跑探针 |
| `scripts/classify_tests.py` | 新增工具 | 测试分层与弱断言扫描 |
| `audit_netblock_plugin.py` | 新增工具 | 断网验证插件 |

回归确认：`python -m pytest tests -q` → **322 passed**（含新增 2 条）。

---

## 6. 尚未验证 / 不能声称

1. **真实 provider 行为完全未验证**：0 条 live 测试，token 与费用口径无实测证据。
   `trace_records_model_calls` 为 PARTIAL。
2. **热缓存下的可观测性盲区未修复**（第 3 节），生产 `tool_calls` 会低估。
3. **`tools/*.py` 中 2 处直接 `import akshare`** 不经 Registry/Executor，
   这部分数据获取不受预算、重试、熔断与 trace 覆盖。
4. **README 的全部声明未逐条复验**（本轮只验证了 Harness/Eval/Memory/Optimization 相关部分）。
5. Docker 构建未在本轮执行。
