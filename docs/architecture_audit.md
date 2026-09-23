# 仓库审计报告（Phase 0）

> 审计对象：`financial_research_agent`（v4-competition，git HEAD `8daf13b`）
> 审计方式：逐文件通读代码 + 复跑测试 + 核对历史结果文件，不接受 README 的单方面陈述。
> 审计结论中的每个数字都注明了**验证方式**（复跑 / 读原始产物 / 仅静态阅读）。

---

## 0. 审计动作与验证结果

| 动作 | 命令 | 结果 | 性质 |
|---|---|---|---|
| 复跑离线测试 | `python -m pytest tests -q` | **99 passed, 100.25s** | 本次实际复跑 |
| 核对 30-case 评测产物 | 读 `outputs/eval/competition_30_runs.json` | 30 条记录，30 条有 `run_id` 且 `error=null`（company 10 / industry 10 / macro 10） | 读原始产物 |
| 核对 30-case 指标 | 读 `outputs/eval/competition_report.md` | 与 README 第 16 节数字逐项一致 | 读原始产物 |
| 核对 Bad Case 数量 | `grep -cE "^#+ *Bad Case" eval/bad_cases.md` | **31 条**（Bad Case 1–31） | 本次实际统计 |
| 核对 LLM 连通性 | 最小 `call_llm("回复ok")` | 返回 `ok`，`MODEL_NAME=openai/deepseek-v4-pro`，走阿里云 MaaS 兼容端点 | 本次实际调用 |
| 核对密钥泄漏 | `git ls-files \| grep env` | 仅 `.env.example`（占位符 `sk-xxxx`），`.env` 已在 `.gitignore` | 本次实际检查 |
| Docker 构建 | — | **未验证**（无 Docker 守护进程），README 已如实声明 | 未验证 |

**结论：README 中的 31 / 99 / 30-30 三个数字均可被原始产物或复跑证实，不是编造。**
唯一需要修正的是措辞层面的问题，见第 8 节。

---

## 1. 当前真实架构

一句话：**这不是一个多智能体系统，而是一条被 LLM 规划器"装饰"过的固定五阶段确定性流水线。**

```
main.py / api/routes/reports.py
    └─ orchestrator/workflow.py :: WorkflowOrchestrator.run(request)
         ├─ PlanningAgent.plan()          → LLM 产出计划
         ├─ normalize_plan()              → 【关键】把 LLM 计划无条件压回固定 5 阶段
         ├─ _schedule()                   → 拓扑排序（对固定 5 阶段恒等于原序）
         └─ for task in ordered:          → 顺序执行，无并发、无回溯
              ResearchAgent  → BrowserAgent → AnalyzeAgent → ReportAgent → ReportAgent(evaluate)
                                  ↑
                          （唯一的动态分支：browse 失败且候选不足 → 回跳 research 一次）
```

`agents/planning_agent.py:46 normalize_plan()` 是理解本仓库的关键：无论 LLM 生成 5 个还是
11 个任务，`task_id` / `dependencies` / `priority` / `max_retries` / 顺序**全部被强制覆盖**为
`_STAGE_ORDER = [research, browse, analyze, report, evaluate]`，只有 `description` 和
`parameters` 从 LLM 结果里继承。这是一个正确的工程决策（注释里写明了动机：LLM 计划曾导致重复
执行 research/browse），但它意味着：

- Planner 的 LLM 调用对**执行路径零影响**，只影响两个不参与控制流的字符串字段；
- 系统的"自主性"实际集中在 BrowserAgent 的启发式筛选和 workflow 的两处失败分支上。

## 2. 每个节点的输入 / 输出

全部通过一个共享的可变 `context: dict` 传递（`orchestrator/workflow.py:115`），
每个阶段把自己的返回值写进 `_CONTEXT_KEY_BY_TASK_TYPE` 指定的键。

| 节点 | 代码 | 输入（从 context 读） | 输出（写回 context 的键） |
|---|---|---|---|
| PlanningAgent | `agents/planning_agent.py` | `request` | 不写 context（返回值经 normalize 后成为 `tasks`） |
| ResearchAgent | `agents/research_agent.py` | `request`、`task.parameters.force_fallback` | `search_results` = `{queries, candidates, research_metrics}` |
| BrowserAgent | `agents/browser_agent.py` | `search_results.candidates`、`request.max_sources` | `sources` = `{sources[], all_scored_count, browser_metrics}` |
| entity 校验 | `tools/entity_validator.py`（在 workflow 内联调用） | `sources`、`request.topic/report_type` | `entity_validation`（四态） |
| AnalyzeAgent | `agents/analyze_agent.py` | `sources`、`request` | `analysis` = `{analysis, akshare_metrics, macro_metrics, company_deep_metrics, industry_deep_metrics, compression_metrics}` |
| ReportAgent(report) | `agents/report_agent.py` | `analysis`、`sources`、`request` | `report` = `{markdown_content, final_content, title, charts_html, report_compression}` |
| ReportAgent(evaluate) | 同上 + `evaluators/report_evaluator.py` | `report`、`sources` | `evaluation` = `{evaluation:{...}}` |
| 有界改稿 | `tools/report_reviser.py`（在 workflow 内联） | `report`、`evaluation` | 覆盖 `report.markdown_content` + `evaluation`（仅当分数不降） |

**问题**：`context` 是无 schema 的裸 dict，没有类型校验，没有版本，不能序列化恢复（里面存着
`ResearchRequest` 对象和完整网页正文）。这是 Phase 1 要解决的第一件事。

## 3. 状态如何传递

- **进程内**：上述 `context` dict，生命周期 = 一次 `run()` 调用。
- **落盘**：只在 `run()` 结束时一次性写四个文件（`outputs/reports|sources|evaluations|traces`）。
- **中途没有任何持久化**。进程被杀 = 全部丢失。`scripts/run_competition_eval.py` 的
  `--resume` 是**套件级**断点续跑（跳过已完成的 topic），**不是 run 级**——一个跑了 400 秒
  的 case 在 report 阶段被杀，重跑时从 research 重新开始。
- API 层 `api/task_manager.py` 是单进程内存字典 + `ThreadPoolExecutor`，重启丢状态（已在
  README 第 4 节声明）。

## 4. 工具调用在哪里完成

| 工具 | 调用点 | 是否经 MCP | 参数校验 | 返回校验 |
|---|---|---|---|---|
| `web_search` | `research_agent.py:106`（线程池内） | 是（`tools/tool_gateway.py`） | 无 | 无 |
| `read_webpage` | `browser_agent.py:113` | 是 | 无 | 只查 `parsed.get("success")` |
| `read_pdf` | `browser_agent.py:105` | 是 | 无 | 只查 `success` |
| `fetch_financial_snapshot` | `analyze_agent.py:27` | 是 | 无 | 只查 `success` |
| 其余 ~45 个 `tools/*.py` | AnalyzeAgent / ReportAgent 内**函数直调** | 否 | 无 | 无 |

`mcp_server/tools_server.py` 只暴露 4 个工具（`web_search` / `read_webpage` / `read_pdf` /
`fetch_financial_snapshot`），schema 由 FastMCP 从 Python 类型注解自动生成，**没有显式
input/output schema，没有"何时使用/何时不要使用"的描述**。其余 45 个 `tools/*.py` 模块是
普通 Python 函数，agent 直接 import 调用——它们不是"Agent 可选择的工具"，而是硬编码的
分析步骤。

**这是本次升级最重要的发现之一：系统里"工具"有两类，只有 4 个是真工具，45 个是被叫做
tool 的库函数。** 新的 Registry 必须如实区分，不能把 49 个都注册成"Agent 可用工具"来虚增
工具数量。

## 5. 重试 / 超时 / 缓存 / Fallback 的实现位置

| 机制 | 位置 | 实现 | 缺口 |
|---|---|---|---|
| 任务级重试 | `agents/base_agent.py:37` | `while attempts <= task.max_retries`，**立即重试无退避** | 无指数退避、无抖动、不区分错误类型 |
| 不可重试标记 | `base_agent.py:63` | `exc.non_retryable` 属性 | 只有 BrowserAgent 用；靠鸭子类型而非错误分类 |
| 搜索查询超时 | `research_agent.py:182` | `future.result(timeout=SEARCH_QUERY_TIMEOUT)` | 线程无法真正取消，仅放弃等待 |
| HTTP 超时 | `config.REQUEST_TIMEOUT=10` | requests 层 | — |
| MCP 调用超时 | `tool_gateway.py:124` | `future.result(timeout=MCP_TOOL_TIMEOUT)` | — |
| 搜索缓存 | `utils/cache_utils.py` | 按 query 精确匹配，JSON 落盘，TTL 24h | 只缓存搜索，网页/PDF 抓取不缓存 |
| MCP → 直调降级 | `tool_gateway.py:141` | 单向不可逆（`self._failed = True` 后进程内永久直调） | 无熔断恢复、无半开状态 |
| 搜索 fallback | `research_agent.py:248` | 候选 < 20 或被 replan 强制 → 跑 5 条 `site:` query | — |
| 相关性放宽 fallback | `browser_agent.py:248` | 0 个相关但 ≥5 个可用 → 按总分取 top_k 并打标 | — |
| 动态 replan | `workflow.py:188` | browse 失败且 `browser_fallback_triggered` → 回跳 research 一次 | 硬上限 1，且**只对 browse 一种失败生效** |
| 有界改稿 | `workflow.py:286` | ≤1 轮，分数不降才采用 | — |
| LLM 降级 | 各 agent 的 `_fallback_analysis` 等 | LLM 失败 → 规则式产出 | — |

**没有的**：熔断器、指数退避、并发上限（除搜索线程池）、预算控制、重复调用检测、任务取消、
幂等键、审批钩子。

## 6. Trace 实际记录了什么

`schemas/trace.py :: TraceLog`，一次 run 一个 JSON 文件（不是 JSONL 事件流）：

**有的**：`run_id`、`topic`、起止时间、`total_duration`、`steps[]`（agent/task_id/task_type/
success/detail/duration）、`selected_sources[]`（**含完整正文**）、`errors[]`、
`final_report_path`，以及 9 个 `*_metrics` 字典（planner / research / replan /
structured_data / browser / performance / compression / evaluation_diagnostics / revision /
macro_chain / company_deep / industry_deep）和 `entity_validation`。

**没有的**（Phase 1 必须补）：
- 单次**工具调用**记录（工具名 / 参数 / 返回状态 / 时延）——只有阶段级聚合；
- 单次**模型调用**记录（模型名 / input tokens / output tokens / 费用 / 失败原因）——
  **全仓库没有任何 token 或费用统计**；
- 重试事件、熔断事件、预算事件、checkpoint 事件、记忆读写事件；
- 事件时间戳（只有 run 级 started/finished）；
- schema 版本号；
- 脱敏（目前 trace 里不含 header/cookie，但也没有任何脱敏机制，新增字段时无护栏）。

`selected_sources` 里存完整网页正文，导致单个 trace 文件可达数百 KB——这也是为什么
`outputs/traces/` 被 gitignore。

## 7. 现有测试与数据集规模

| 资产 | 规模 | 性质 |
|---|---|---|
| `tests/` | 13 个文件，**99 个测试**，100 秒，全离线 | 真实通过（本次复跑） |
| `eval/topics.json` | 10 个 topic | 只有 `topic/report_type/requirements` 三个字段 |
| `eval/topics_competition_30.json` | 30 个 case | 8 个字段，含 `category/sector/tags/tracking_symbol` |
| `eval/test_cases.json` | 10 条 | 与 `topics.json` 高度重复，疑似遗留 |
| `eval/bad_cases.md` | **31 条**，45 KB | 高质量、有代码定位、有修复记录——本仓库最有价值的资产之一 |
| `outputs/eval/*.json|csv|md` | 15 个聚合报告 | 真实运行产物 |
| `outputs/evaluations/` | 约 200 个历史 run 评估 | 真实产物（gitignore） |

**评测数据的根本缺口**：现有 topic 文件里**没有任何一条期望值**。没有 `required_facts`、
没有 `expected_outcome`、没有 `forbidden_claims`、没有 `optional_expected_tools`。因此现有
"评测"实质是**无参考自评**——用 `evaluators/report_evaluator.py` 的启发式规则给自己打分，
再用 `number_grounding` 统计自己引用了多少。它能测"报告长得像不像研报"，**不能测"报告说得
对不对"**，也完全无法测工具选择、参数正确性、恢复能力。

现有测试覆盖的是工具函数（DCF、三表抽取、合规检查、实体校验……），**没有一个测试覆盖
orchestrator 的失败路径、恢复路径或并发行为**。

## 8. README 与代码 / 实验结果的不一致

逐条核对后，**没有发现编造的数字**。发现的是 4 处措辞与实现不符：

| # | README 说法 | 代码事实 | 严重度 |
|---|---|---|---|
| 1 | "多智能体工作流"、架构图画成 6 个 Agent 节点 | `normalize_plan()` 把 LLM 计划压成固定 5 阶段，Planner 对控制流零影响；没有 agent 间协商/委派/并行 | **中**——不是造假，但"多智能体"是过度包装 |
| 2 | "工具调用经标准 MCP" | 只有 4 个工具走 MCP；AnalyzeAgent/ReportAgent 里 45 个 `tools/*` 是直接 import | 中 |
| 3 | 第 17 节列出 `scripts/build_ablation_report.py` 等命令 | 脚本存在且可运行，但 `outputs/eval/ablation_report.md` 的消融是**离线重算指标**，不是重新运行系统的真实对照实验 | 中 |
| 4 | "有效引用率 100%" | `valid_citation_rate=1.0` 的定义是"报告里的 `[sX]` 标记都能在 sources 列表里找到 source_id"，**不校验 URL 可访问、不校验引用内容真的支持该句** | 中——指标名比实际含义强 |

另有 1 处需要在新 README 里明确保留的**正确做法**：`report_compliance_pass_rate: 0.0` 被
如实写出来并解释了原因，没有藏起来。这是本仓库文档诚信度的正面证据。

## 9. 哪些是真自主决策，哪些是固定流水线

| 组件 | 判定 | 依据 |
|---|---|---|
| PlanningAgent | **固定流水线**（伪自主） | 输出被 `normalize_plan` 全量覆盖 |
| ResearchAgent | 固定流水线 + 1 个确定性分支 | query 来自硬编码模板；fallback 由 `len(candidates)<20` 触发 |
| BrowserAgent | **半自主** | 域名分散、早停、相关性放宽三条启发式规则真实影响结果，但全是确定性规则，无 LLM 参与决策 |
| AnalyzeAgent | 固定流水线 | 按 `report_type` 走三条硬编码工具链（company/industry/macro），无选择 |
| ReportAgent | 固定流水线 | LLM 只做文本生成，不做决策 |
| 有界改稿 | **半自主** | `identify_revision_issues` 是规则；采纳与否由评分决定 |
| 动态 replan | **半自主** | 确定性触发条件 + 硬上限 |

**没有任何一处是"LLM 选择下一步动作"**。这是本次升级要诚实面对的起点：不能宣称"Agent
为什么选择这个工具"，因为当前系统里 Agent 不选工具。新的 Harness 必须让这个问题**变得
可回答**——要么引入真实的工具选择决策点，要么明确记录"此处是确定性路由，路由依据是 X"。

## 10. 已存在、不应重复实现的能力

以下能力已经真实可用，Phase 1–4 必须**复用而非重写**：

1. `tools/citation_checker.py` + `tools/number_grounding.py` —— 引用合法性 + 数字五分类
   溯源（sourced / akshare / assumption / calculated / unsourced）。**新 Grader 直接调它。**
2. `evaluators/report_evaluator.py` —— 分报告类型的质量评分（5 种 report_type）。
3. `tools/entity_validator.py` + `tools/listed_company_registry.py` —— 实体四态硬校验。
4. `tools/source_tier.py` —— 来源权威分级。
5. `tools/tool_gateway.py` —— MCP 路由 + 降级。**新 Executor 包在它外面，不替换它。**
6. `utils/cache_utils.py` —— 搜索缓存。
7. `agents/*` 全部五个 agent 的业务逻辑 —— 不动。
8. `eval/bad_cases.md` 31 条 —— 直接转成回归集的种子。
9. `tools/report_compliance_checker.py`、`tools/data_lineage.py`、`tools/chart_consistency_checker.py`。
10. `scripts/run_competition_eval.py` 的套件级 resume 模式 —— 新 runner 沿用同样的
    "每 case 完成即落盘"策略。

## 11. 本次升级的最小改动方案

**核心原则：新增旁路，不改主干。** 现有 `WorkflowOrchestrator.run()` 的签名、返回值、
落盘产物全部保持不变，99 个测试必须继续通过。

```
新增（不动现有文件）:
  src/runtime/        状态模型、预算、检查点、策略、错误分类、Runner
  src/tools/          Registry / Executor / Schemas —— 包在 tool_gateway 外层
  src/observability/  事件、JSONL trace store、指标聚合
  src/context/        证据账本 + 分层上下文预算
  src/memory/         三类记忆 + SQLite 存储
  src/optimization/   失败挖掘 / 候选生成 / 回归门禁 / 版本库
  evals/              数据集 schema、graders、runners、reports、fixtures
  tests/runtime/ 等   新增测试

修改（外科手术式，逐处可回滚）:
  tools/tool_gateway.py  +8 行：若当前线程绑定了 RunContext，则经 ToolExecutor 调用，
                          否则维持原直调行为 → 现有 agent 一行不改就获得工具级 trace
  orchestrator/workflow.py  +N 行：可选 harness 参数，默认 None 时行为与现在完全一致
  config.py              追加新配置项（全部有默认值）
```

`tool_gateway` 的挂钩点是整个方案的支点：四个真工具的调用全部收敛在那 4 个函数里，
在那里插入 Executor 就能同时拿到工具选择、参数、返回状态、时延、错误分类、重试和预算，
**而 5 个 agent 一行代码都不用改**。

## 12. 风险、兼容性问题与实施顺序

**风险**

| 风险 | 影响 | 缓解 |
|---|---|---|
| Windows + 中文路径/编码 | 已在 Bad Case 20/30 出现过 | 所有新文件 IO 强制 `encoding="utf-8"`；不用 PowerShell 传中文参数 |
| SQLite 多线程写 | Executor 在搜索线程池里被并发调用 | `check_same_thread=False` + 单写锁；trace 写 JSONL（追加安全）而非 SQLite |
| trace 体积 | 现有 trace 已含全文，事件流会更大 | 长结果外置到 ArtifactStore，事件里只留 hash + 摘要 + 路径 |
| 真实评测成本 | 一次 30-case 跑 ≈ 2 小时 + 真实 LLM/网络费用 | smoke eval 走 replay fixture，零成本；全量评测显式手动触发 |
| 循环导入 | `src/` 反向依赖 `tools/`、`config` | `src/` 单向依赖旧模块，旧模块**只**通过 `tool_gateway` 一个挂钩点感知 `src/` |
| 过度设计 | 用户明确禁止"为展示技术栈引入框架" | 不引入 LangGraph/Temporal/Redis/Kafka；存储用 stdlib `sqlite3` |

**兼容性红线**：`python -m pytest tests` 必须始终 99+ 通过；`python main.py` 的 CLI 行为、
`outputs/` 目录结构、FastAPI 的请求/响应 schema 不变。

**实施顺序**（每步跑一次 `pytest tests` 确认无回归）

1. Phase 1a：`src/runtime/{errors,state,budget,policies}` + `src/observability/*` + 单测（纯新增，零风险）
2. Phase 1b：`src/tools/{schemas,registry,executor}` + `tool_gateway` 挂钩 + 单测
3. Phase 1c：`src/runtime/{checkpoint,runner}` + SQLite 存储 + orchestrator 可选接入 + 恢复测试
4. Phase 1d：`src/context/` 证据账本
5. Phase 2：`evals/` 全套（schema → 数据 → grader → runner → report）
6. Phase 3：`src/memory/` + 记忆 A/B
7. Phase 4：`src/optimization/` + 回归门禁
8. Phase 5：轨迹导出 + Data Card + LoRA 配置（不训练）
9. 文档收口：README / architecture / evaluation / memory / failure_cases / ADR / resume_evidence

---

## 附：一句话结论

现有仓库是一个**工程质量很高、文档诚实、但架构上是确定性流水线而非 Agent 系统**的项目。
它缺的不是功能数量（45 个分析工具已经过剩），而是**运行时的可观测性、可恢复性、可评测性
和可归因性**——也就是本次升级要补的那一层。升级的正确姿势是给它装上"仪表盘和黑匣子"，
而不是再加几个 Agent。
