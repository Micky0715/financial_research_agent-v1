# 简历证据表

> 规则：A 类的每一项都必须能指向**实现文件 + 测试 + 数据集 + 运行命令 + 原始结果**。
> 指不出来的，一律降到 B 或 C。
> 本文由最终验收轮（`docs/final_verification_audit.md`、
> `docs/tool_eval_leakage_audit.md`）核对后重写，与上一轮的说法有出入之处以本文为准。

**当前全局前提（决定了大量措辞）：**

- 数据集 `human_verified = 0`。所有评测指标只能标注为 draft / synthetic。
- 测试套件 **327 条全部离线**：硬断网下 322 条（新增前）全通过，
  `live_network = 0`、`live_model = 0`。**没有任何真实 provider 证据。**
- 因此：任何涉及"真实成本 / 真实时延 / 真实成功率"的表述都属于 C 类。

---

## A. 已验证，可以写进简历

### A1. 统一 Agent Harness，接管 CLI 与 API 两条生产入口

| 项 | 内容 |
|---|---|
| 能力 | 状态机 + 预算 + 重试 + 熔断 + 检查点 + 结构化 Trace 的统一执行层，默认启用（`USE_AGENT_HARNESS=True`），并保留 `--legacy` 回滚 |
| 实现 | `src/runtime/`（runner/state/budget/policies/checkpoint/errors）、`src/tools/`（registry/executor/schemas）、`src/observability/` |
| 接入点 | `main.py`、`api/task_manager.py`、`tools/tool_gateway.py::_dispatch`（单一挂钩点，5 个 agent 未改） |
| 测试 | `tests/harness/`（206+ 条）；`tests/` 原有 100 条零回归 |
| 运行 | `python scripts/verify_harness_claims.py` |
| 原始结果 | `docs/_evidence/verification_probes.json`（**17 PASS / 0 FAIL / 2 PARTIAL**） |
| 边界 | 生产可用性仅在 fixture 下验证；未做真实并发压测 |

可写表述：
> 设计并落地统一 Agent Harness，通过单一网关挂钩点接管 CLI 与 FastAPI 两条生产路径，
> 5 个既有 Agent 零改动即获得工具级校验、结构化错误分类、预算门禁与全链路 Trace；
> 以 19 个可复跑探针验证生产入口而非源码断言，327 项离线测试零回归。

### A2. 结构化错误分类与差异化重试（实测可分辨）

| 项 | 内容 |
|---|---|
| 能力 | 11 类错误分类；仅 TIMEOUT / RATE_LIMIT / TRANSIENT_NETWORK 可重试；未分类错误默认**不**重试 |
| 实现 | `src/runtime/errors.py`、`src/tools/executor.py` |
| 测试 | `tests/harness/test_runtime_core.py`、`test_executor_and_trace.py` |
| 实测 | TIMEOUT 尝试 **3** 次；AUTH 尝试 **1** 次（`retry_only_transient` 探针） |

### A3. 熔断与预算门禁（实测阻断后续调用）

- 熔断：阈值触发后第 3 次调用**未到达工具层**（工具侧计数停在 2）。
- 预算：上限 2 次 → 实际到达工具层 2 次，2 次被 `BUDGET_EXCEEDED` 拦截，终态 `degraded`。
- 证据：`docs/_evidence/verification_probes.json` 的
  `circuit_breaker_blocks_later_calls` / `budget_blocks_subsequent_calls`。

### A4. 检查点恢复：定位并修复"恢复不跳过"缺陷

这是本轮**最有说服力的一条**，因为它是被审计发现并修复的真实缺陷。

| 项 | 内容 |
|---|---|
| 缺陷 | `plan_resume` 的跳过计划只写进 Trace，从未被执行；恢复运行反而比首次运行多调用工具（4 vs 3） |
| 根因 | `ToolExecutor._dedup` 每次运行从空开始；非外置产物的工具无法重放 |
| 修复 | `ToolExecutor.seed_completed_steps()`；产物一律入 ArtifactStore；恢复跳过发 `STEP_SKIPPED_IDEMPOTENT` 而非 `TOOL_CALL_DEDUPED` |
| 实测 | 首次 3 次工具调用 → **恢复 0 次**，12 个跳过事件 |
| 测试 | `test_resume_actually_skips_completed_work_not_just_reports_a_plan`、`test_resume_skips_are_not_counted_as_redundant_calls` |
| 关键佐证 | **已验证这两条测试能发现该缺陷**：注释掉修复后立即 2 failed，恢复后通过 |

可写表述：
> 在验收审计中发现"断点恢复"实为伪恢复（仅恢复状态、不复用已完成工作，
> 恢复运行工具调用不降反增），定位到去重备忘录未从检查点重建；
> 修复后恢复运行工具调用由 3 降至 0，并补充了两条经反向验证（注释修复即失败）的回归测试。

### A5. Trace 脱敏与"不存隐藏推理"

- 分段精确匹配的脱敏：`sk-*` / `Bearer *` / `cookie` / `reasoning_content` 全部不落盘；
  同时**保留** `tokens_in`、`authority_tier` 等正常字段。
- 修复过一个真实缺陷：早期子串匹配把 `tokens_in`（含 "token"）和 `authority_tier`
  （含 "auth"）误脱敏，破坏了遥测与证据元数据。
- 实现 `src/observability/events.py::redact`；测试 `test_executor_and_trace.py`；
  探针 `trace_redacts_secrets`。

### A6. 评测指标退化的定位与修复（Tool F1 = 1.0）

| 项 | 内容 |
|---|---|
| 发现 | 旧 Tool Selection F1 = 1.0 是**口径退化**：空期望时 precision/recall 双双硬编码为 1.0；264 行中仅 36 行"适用"，且全部只声明 forbidden_tools |
| 证明 | 一次工具都不调用、或调用 5 次无关工具，旧口径都得 1.0（可一行复现） |
| 修复 | 收紧 `ToolSelectionGrader` 适用条件；拆出 `ForbiddenToolGrader`；新增 Necessary Tool Recall / Unnecessary Tool Call Rate / Tool Outcome Success / Alternative Valid Path Rate |
| 新数据 | 31 条手工对抗任务，覆盖 12 类路由情形（`evals/datasets/adversarial_tools.py`） |
| **诚实结果** | **F1 1.0 → 0.4616**，Precision 0.359，多余调用率 0.413 |
| 原始结果 | `evals/results/adversarial_harness.jsonl`（sha256 见 `docs/_evidence/result_hashes.json`） |
| 结论 | 系统对任意输入稳定调用 14–15.5 次工具，**包括正确答案是 0 次调用的任务** —— 它不做工具选择，而是执行固定链 |

可写表述：
> 审计发现工具选择 F1=1.0 系指标退化（空期望分支硬编码满分，有效分母仅 13.6%），
> 重建口径并新增 31 条对抗路由用例后 F1 降至 0.46、无谓调用率 0.41，
> 据此给出"系统执行固定工具链而非进行工具选择"的结论；全过程如实记录未回调门槛。

### A7. 离线优化闭环的安全性（只生成提案，不自动部署）

- 5 个版本提案全部停留在 `proposed`；未审批版本调用 `apply_version` 抛 `ValueError`；
  审批后也只写 overlay 文件，不触碰 `config.py` / `prompts/`。
- 实现 `src/optimization/version_store.py`；测试 `tests/harness/test_optimization.py`；
  探针 `optimization_produces_files_only`。

### A8. 完全离线、可复现的测试与评测体系

- **391 条测试全部离线**（硬断网验证：`pytest -p audit_netblock_plugin`）；约 85 秒。
- 评测离线回放：`python -m evals.runners.run_eval --smoke` 无需 API Key。
- 测试分层统计脚本 `scripts/classify_tests.py` 输出
  schema 10 / unit 251 / mock 37 / fixture_e2e 24 / live 0。

---

### A9. 真实 Provider Canary：首份非 fixture 证据，并因此修出两个真实缺陷

- 16 条人工分层的真实主体，全部跑完，总花费 **$0.2520**（硬上限 `--max-cost-usd`）
- 首次拿到真实口径：单次 **$0.0158**、平均 11,270/4,342 token、平均时延 179.9s、**P95 340.9s**
- 终态 succeeded 13 / degraded 1 / insufficient 2；False Success **0**、Unresolved Failure **0**
- 两条 insufficient 是设计上应当失败的（虚构主体被实体校验拦下、首轮来源全不可用），
  一条 degraded 是预算降级阶梯在真实环境真的触发
- **canary 暴露的真实缺陷**：32 次失败调用里 8 次被误判为 `UNKNOWN`
  （`empty_content_after_cleaning` 与反爬前端返回的非标准 `468 Client Error`），
  补两条规则后 8 次全部归入 `PERMANENT_FAILURE`，其余 24 次分类不变
- **canary 暴露的评测口径问题**：离线 Number Grounding 0.9956 → 真实 0.8126；
  离线「成功但含无源数字」0.0154 → 真实 0.8571。fixture 系统性高估数字溯源质量
- 规则是在 canary 跑起来之后才改的，因此**没有重跑、也没有改写 trace**：
  `scripts/replay_canary_error_classes.py` 用当前规则离线重放 trace 里逐字保存的真实错误消息，
  报告并列"运行时记录 / 当前规则重放"两列（同源同分母，合计均为 32）
- 证据：`evals/reports/live_canary_report.md`、`evals/results/live_canary.jsonl`、
  `docs/_evidence/canary_error_replay.json`

### A10. 单变量消融：把"Harness 有用"降级成可归因的结论

- 7 个臂，除 legacy 外每个臂与 `harness_full` **只差一个开关**
- 旧 README 的 "+12.5pp 归功于 Harness" 已证明无效（同时动了两个变量 + 换了 Grader 集合）；
  真实 Δ 属于 **checkpoint 一项**（0.8068 → 0.6818，Unresolved Failure 0 → 0.125）
- retry / budget / context 压缩 / fallback 对成功率的影响是**精确的 0**；
  fallback 多花 3.6 次工具调用而成功率与来源数不变
- 重构回归集上 legacy 与 harness 成功率**完全相同**（0.8462），
  差别是 Unresolved Failure **0.5385 → 0**
- `scripts/build_ablation_matrix.py` 在任务数/trial 数/Grader 集合三项不一致时
  **拒绝给出归因结论**，该拒绝行为有 16 条测试（`tests/harness/test_ablation_validity.py`）
- 证据：`evals/reports/ablation_matrix_dev.md`、`ablation_matrix_reg.md`（含 `validity_problems: []`）

---

## B. 工程已实现，但效果尚未被证明

| 能力 | 实现程度 | 缺什么 |
|---|---|---|
| **长期记忆（三类型 + 命名空间隔离 + 版本化 procedural）** | 代码完整，35 条测试通过；已接入 `HarnessRunner`；探针确认记忆内容确实进入拼装后的上下文 | **没有规模足够的 A/B 证据**。上一轮实验规模过小且下游成功率全为 1.0，不能证明记忆有收益。按项目既定标准，**默认关闭**，且不得宣传"记忆增强" |
| **离线优化闭环** | 失败归因 + 候选生成 + 回归门禁 + 版本库全部可运行，门禁逻辑有 28 条测试 | **尚未完成一次"重构后回归集 + 独立测试集"的完整门禁通过**。目前只能称为"候选生成与门禁框架" |
| **Router 后训练准备** | 轨迹导出（7 类数据集）+ Data Card + LoRA 配置 + 前置条件检查脚本 | **未训练**。且导出标签全部来自确定性 grader，`human_verified=0`，训练出的路由器上限是复现当前系统 |
| **上下文分层压缩** | 实现 + 一致性校验 + 单测齐全 | 未证明压缩对下游报告质量的影响（无对照实验） |
| **回归集** | 已重构：31 条 bad case 全部显式分类，13 条成为可执行任务（真实 query + 故障注入 fixture + 明确期望），9 条判定非 Agent 行为问题并写明理由，5 条需真实网络，4 条转人工队列。见 `docs/regression_rebuild.md` | **期望值本身未经人工确认**：13 条任务的 `gold_status` 全为 `needs_human_review`。可以说"能防回归"，不能说"期望正确" |

---

## C. 不得写入简历

| 不能写 | 原因 |
|---|---|
| 生产级 / 线上高并发 / 高可用 | 只在单机 fixture 下验证；无压测、无 SLO、无线上流量 |
| 人工 Gold 数据集 / 人工标注 N 条 | **human_verified = 0**。审核队列已就绪但无人填写 |
| 真实成功率（带置信区间） | 有 16 条真实 canary，但 **n=16、每条 1 trial**，不足以给出区间估计 |
| 真实时延**降低** / 真实成本**节省** | 只有一次真实测量（$0.0158/次、P95 341s），**没有改造前的真实基线**，因此可以说"真实成本是多少"，不能说"降低了多少" |
| "记忆显著提升效果" | 无规模足够的 A/B 证据；当前默认关闭 |
| 自我进化 / 自主优化 / RSI | 候选由规则生成，且不自动部署；只能称"回归门禁控制下的离线优化**框架**" |
| 已训练 Router / LoRA 已上线 | 未训练，无 GPU 运行记录 |
| 已通过完整回归门禁 | 回归集已重构，但 v004/v005 候选的最终门禁仍未运行；5 个候选状态全为 `proposed` |
| "多智能体协作" | `normalize_plan` 把任何计划压成固定 5 阶段；对抗集实测证明系统执行固定工具链，不做工具选择 |
| Tool Selection F1 = 1.0 | 已证明是指标退化；真实值 0.4616 |
| Docker 部署已验证 | 本轮与上一轮均未执行 build |

---

## D. 复现命令速查

```bash
# 测试（离线，约 85 秒）
python -m pytest tests -q
python -m pytest tests -q -p audit_netblock_plugin      # 硬断网验证

# 生产入口探针（19 项）
python scripts/verify_harness_claims.py

# 测试分层
python scripts/classify_tests.py

# 工具路由对抗集
python -m evals.datasets.adversarial_tools --write
python -m evals.runners.run_eval --split adversarial --arm harness --trials 3 \
    --results evals/results/adversarial_harness.jsonl
python -m evals.reports.build_report --results evals/results/adversarial_harness.jsonl

# 人工审核（需要真人）
python -m evals.human_review.build_gold_queue --write
python -m evals.human_review.validate_gold
```

原始结果与哈希：`docs/_evidence/result_hashes.json`、`docs/_evidence/verification_probes.json`、
`docs/_evidence/test_classification.json`。
