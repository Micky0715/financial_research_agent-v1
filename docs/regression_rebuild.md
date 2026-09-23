# 回归集重构（Phase 3）

> 结论先行：原来的 41 条回归里，**31 条把缺陷标题当研究主题**，运行时什么都没测到。
> 重构后得到 **13 条可执行任务**（9 条带故障注入），其余 18 条按四类如实归档并排除。
> 原始 `eval/bad_cases.md` 未做任何修改，来源映射完整保留。

复现：

```bash
python -m evals.datasets.regression_rebuild --write
python -m evals.runners.run_eval --split regression_rebuilt --arm harness --trials 3 \
    --results evals/results/regression_rebuilt_harness.jsonl
python -m evals.reports.build_report --results evals/results/regression_rebuilt_harness.jsonl
```

产物：`evals/datasets/regression_rebuilt.json`、`evals/datasets/regression_manifest.json`、
`evals/reports/regression_verified_report.md`。

---

## 1. 原来的回归集为什么不可用

`build_datasets.build_regression_tasks()` 直接用 bad case 的**标题**做 query：

```
query = "ResearchAgent 串行搜索导致耗时过长"
query = "python str.replace 打补丁静默失败，实体验证的终止文案没生效"
query = "eval_runner.py --help 误触发实际评测"
```

这些是缺陷描述，不是研究请求。运行时发生的事情是：流水线老老实实去搜
"str.replace 打补丁静默失败"，搜不到任何金融资料，实体校验判定主体不存在，
运行被记为"如实拒答"——**然后这条回归就通过了**。

它测到的是"系统能拒绝一个不存在的主体"，而不是"该历史缺陷有没有复现"。
41 条里有 31 条是这个形状，因此整个回归集的 0.3659 成功率既不能说明退化，
也不能说明健康。

---

## 2. 重构方法：先分类，再写任务

逐条读 31 个 bad case，按**能不能被复现为 agent 行为**分四类。这一步的价值在于
**敢于把不可复现的排除掉**，而不是硬凑覆盖率。

| 类别 | 条数 | 含义 |
|---|---|---|
`deterministic` | **13** | 可离线注入故障 + 规则判定，已写成可执行任务 |
`needs_human_review` | 4 | 期望值必须由人设定（会计口径、行业判断） |
`needs_network` | 5 | 只能在真实 provider / TLS / 子进程下复现 |
`not_agent_behaviour` | 9 | 真实缺陷，但属于开发工具或性能，不是 agent 行为回归 |

### 2.1 为什么 9 条被判为"不是 agent 行为"

这是最容易被质疑的一步，所以逐条给理由：

| Bad Case | 原缺陷 | 为什么不能做成任务 |
|---|---|---|
| 1 | 串行搜索耗时过长 | 性能问题。离线 fixture 下时延由 stub 决定，做成任务只会测出 fixture 的速度 |
| 6 | eval_summary 统计历史旧 run | 评测脚本缺陷，已被新体系结构性消除（每次运行独立 JSONL） |
| 7 | `--help` 误触发评测 | CLI 参数解析缺陷 |
| 10 | 冷/热缓存评测混淆 | 评测方法学问题。已结构性处理（离线评测强制关缓存）；相关的可观测性盲区另见 `final_verification_audit.md` |
| 19 | 无可靠免费行业可比数据源 | 外部数据可得性限制，修不掉 |
| 20 | `str.replace` 打补丁静默失败 | 开发过程失误（改动未生效） |
| 24 | embedding 模型冷启动慢 | 性能问题 |
| 29 | 同业板块映射首次构建慢 | 性能与缓存预热 |
| 30 | PowerShell 中文编码 | 测试脚手架问题，`bad_cases.md` 原文已注明"非产品缺陷" |

把这 9 条强行写成研究任务，只会得到 9 条永远通过的假覆盖。

### 2.2 13 条可执行任务

每条包含：真实 ResearchRequest、report_type、研究主体、故障场景、
预期终态、是否允许 degraded、`required_tools`、`forbidden_tools`、
`forbidden_claims`，以及回归断言的文字说明。

| Bad Case | 重构后的任务 | 故障注入 | 预期终态 | 回归断言 |
|---|---|---|---|---|
| 2 | 贵州茅台投资价值分析 | `default` | report_generated | 计划归一化后仍能走到 report，工具调用不因阶段重复而翻倍 |
| 3 | 贵州茅台财务与估值分析 | **`alias_only_sources`** | report_with_caveats | 来源只用证券代码指代主体时，营收仍应被抽取（含 `required_facts`） |
| 4 | 宁德时代投资分析 | **`all_403`** | insufficient_evidence | 确定性失败不得耗尽重试预算，必须如实报资料不足 |
| 5 | 隆基绿能投资价值分析 | **`single_usable_source`** | report_with_caveats | 只有 1 个来源时必须带局限说明，禁止确定性投资结论 |
| 8 | 比亚迪投资价值分析 | **`empty_pages`** | insufficient_evidence | `success=True` 但正文为空是软失败，不得当作可用来源 |
| 9 | 光伏行业投资风险分析 | `default` | report_with_caveats | 行业主题无证券代码，**禁用** `fetch_financial_snapshot` |
| 13 | 贵州茅台2025年年度跟踪报告 | **`single_domain_flood`** | insufficient_evidence | 单域名全量 403 时不得无限重试 |
| 14 | 示例公司财务与估值分析 | **`decimal_heavy_numbers`** | report_with_caveats | 正文含英文句点与小数时数字仍应被识别并溯源 |
| 16 | 希兹维恩量子玄武科技投资分析 | **`fictional_entity`** | insufficient_evidence | 实体校验必须拦截，不得出现任何财务数值 |
| 18 | 中芯国际投资风险分析 | **`akshare_down`** | report_with_caveats | 结构化数据源不可用时降级为纯网页分析 |
| 22 | 美的集团投资分析 | `default` | report_generated | markdown-only run 必须正常产出报告文件 |
| 23 | 星河量子能源股份有限公司投资价值分析 | **`fictional_entity`** | insufficient_evidence | 用另一个虚构主体，确保拦截不是靠硬编码名单 |
| 31 | CPI与PPI走势分析 | `default` | report_with_caveats | 宏观主题**不得**被实体校验拦截 |

为此新增 4 个故障场景（`evals/runners/replay.py`）：
`alias_only_sources`、`single_usable_source`、`single_domain_flood`、
`decimal_heavy_numbers`。

### 2.3 标注状态

13 条全部是 `gold_status = needs_human_review`，不是 `machine_verified`。
理由必须说清楚：**缺陷本身是真的、已复现、已修复**，但这里写下的
"正确终态应该是 report_with_caveats" 是我**推断**的，没有人复核过。
把它标成 machine_verified 会把"作者的判断"伪装成"机器验证的事实"。

---

## 3. 六个新恢复指标

原来只有一个 `recovery_success_rate`，定义是"某步骤失败过且后来成功了"。
它太粗，无法指导行动：分不清是同工具重试成功、还是换了工具、还是靠降级收口；
更严重的是，**它完全看不见"系统把失败报成了成功"**。

拆成六个，全部可由 trace 单独算出（`evals/graders/recovery.py`）：

| 指标 | 问的问题 | 不适用的情形 |
|---|---|---|
`same_tool_retry_recovery` | 同工具退避重试救回来了吗 | 本次运行没安排任何重试 |
`cross_tool_fallback_recovery` | 某工具彻底失败后运行还完成了吗，靠的是换工具还是降级 | 没有工具彻底失败 |
`degraded_completion` | 触发降级的运行里有多少仍然完成 | 没触发任何降级 |
`checkpoint_resume_success` | 恢复的运行是否成功**且真的复用了已完成工作** | 本次运行没恢复 checkpoint |
`false_success` | **声明成功但产出结构性无效** | 永远适用（保持分母完整） |
`unresolved_failure` | 失败了却没留下可归因的原因 | 永远适用 |

### 3.1 一个必须交代的定义修正

`false_success` 的第一版把"报告含无源数字"也算进去。它随即在
`reg005`（Bad Case 5，只有 1 个可用来源）上触发：3/3 trial 报告
`succeeded` 且含 5 个无源数字，`false_success = 0.0769`。

但这个归类是**错的**，理由：本项目从一开始就把数字溯源当作**比率**公开报告，
v4 甚至把 `report_compliance_pass_rate = 0.0` 写进 README 并说明"符合预期"——
因为几乎每份真实报告都至少有一个未标注来源的数字。系统从未声称"所有数字都有来源"，
它公布的是比率。把这种**质量欠缺**折进**诚信指标**，会让 false_success 永远非零、
因而永远无法作为门禁使用。

所以拆成两个，**两个都报，不藏**：

- `false_success`：只含结构性无效——零来源、空报告、悬空引用、出现禁止表述、注入内容进入报告
- `success_with_ungrounded_numbers`：完成了但留下未溯源数字（软信号，单独报）

重构回归集上的实测：

```
false_success                    n=39  mean=0.0000
success_with_ungrounded_numbers  n=18  mean=0.1667
unresolved_failure               n=39  mean=0.0000
same_tool_retry_recovery         n= 3  mean=0.0000
cross_tool_fallback_recovery     n=27  mean=0.4444
degraded_completion              n= 3  mean=1.0000
checkpoint_resume_success        n= 0  (无适用运行)
```

这不是"放宽 grader 换好看数字"：`success_with_ungrounded_numbers = 0.1667`
被完整保留并公开，只是不再被叫成"虚假成功"。

### 3.2 两条值得追问的读数

**`same_tool_retry_recovery = 0.0`（n=3）**：全部来自 `reg018`（AkShare 全量
ProxyError）。该场景下故障是**永久的**，退避重试本来就不该成功。0 是正确答案，
不是缺陷——这说明指标在正确区分"可重试"和"不可重试"。

**`checkpoint_resume_success` 无适用运行**：31 个 bad case 里没有断点恢复类缺陷，
所以重构回归集里没有 resume 任务。该能力由 dev 集的 11 条
`interrupt_after_browse` 任务覆盖，不是漏测。

---

## 4. 重构前后对比

| | 重构前 | 重构后 |
|---|---|---|
| 回归任务数 | 41 | 13（可执行）+ 18（如实归档排除） |
| 拿缺陷标题当 query | **31 条** | 0 条 |
| 带故障注入 | 7/41 | **9/13** |
| 声明 `required_tools` | **0 条** | **13 条** |
| 声明 `forbidden_tools` | 0 条 | 4 条 |
| 声明 `forbidden_claims` | 0 条 | 3 条 |
| 声明 `required_facts` | **0 条** | 1 条 |
| Task Success | 0.3659（无意义） | **0.8462** |
| 恢复指标 | 1 个粗指标 | 6 个可归因指标 |
| 标注状态 | `machine_verified`（名不副实） | `needs_human_review`（如实） |

成功率从 0.3659 涨到 0.8462 **不是系统变好了**——是原来的任务测错了东西。
两个数字不可比，这里并列只为说明旧数字为什么不能用。

---

## 5. 已知限制

1. **13 条期望值未经人工复核**。`needs_human_review` 不是形式主义：
   "只有 1 个来源时应该 report_with_caveats 还是 insufficient" 是可以争论的，
   需要人定。
2. **5 条 `needs_network` 的缺陷目前没有任何回归覆盖**（MCP 连接、
   embedding 下载、LLM 配额、新浪科目名变体、社融 SSL）。
   它们只能靠 live canary 间接触及，且 canary 不会主动构造这些故障。
3. **4 条 `needs_human_review` 的缺陷同样未覆盖**（图表口径归属、
   数值核对、行业生命周期判断、现金流基期）。这四条都是**金融正确性**问题，
   恰恰是最需要人工基准的部分。
4. 重构回归集只有 13 条，**规模不足以做统计显著性判断**，
   只能当作"这些历史缺陷没有复现"的存在性检查。
