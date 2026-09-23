# Tool F1 = 1.0 泄漏审计

> 结论先行：**旧的 Tool Selection F1 = 1.0 是指标退化的产物，不是能力。**
> 一次工具都不调用的运行、以及调用 5 次完全无关工具的运行，在旧口径下都得 1.0。
> 修复口径并加入 31 条对抗任务后，**F1 从 1.0 降到 0.4616**，本文如实记录下降过程，
> 没有通过放宽 Grader 把它恢复回去。

所有数字来自 `evals/results/adversarial_harness.jsonl`（93 行 = 31 任务 × 3 trials）
与 `evals/reports/adversarial_harness_report.json`，可用第 6 节命令复现。

---

## 1. 旧口径为什么恒等于 1.0

### 1.1 可复现的证明

```
python -c "
from evals.graders.deterministic import ToolSelectionGrader
from evals.graders.base import RunArtifacts
from evals.datasets.schema import EvalTask, Category
t = EvalTask(task_id='x', category=Category.PROMPT_INJECTION, query='q',
             forbidden_tools=['export_report_file'])
print(ToolSelectionGrader().grade(t, RunArtifacts(task_id='x', status='succeeded', events=[])).metrics)
"
```

修复前输出（实测）：

| 运行行为 | 旧口径 tool_precision | tool_recall | tool_f1 |
|---|---|---|---|
| **一次工具都没调用** | 1.0 | 1.0 | **1.0** |
| **调用 5 次完全无关的 read_pdf** | 1.0 | 1.0 | **1.0** |

### 1.2 根因

旧 `ToolSelectionGrader` 在任务只声明 `forbidden_tools`、不声明期望工具时仍然"适用"：

```python
precision = (tp / (tp + fp)) if (tp + fp) else (1.0 if not expected else 0.0)
recall    = tp / len(expected) if expected else 1.0        # ← 空期望时硬编码 1.0
```

`expected` 为空 ⇒ `tp = fp = 0` ⇒ precision 落到 `1.0 if not expected` ⇒ 1.0；
recall 硬编码 1.0 ⇒ **F1 ≡ 1.0，与运行做了什么完全无关**。

### 1.3 分母有多小

| 事实 | 数值 | 证据 |
|---|---|---|
| dev 集总行数 | 264 | `evals/results/dev_harness.jsonl` |
| `tool_selection` 判定为"适用"的行 | **36**（13.6%） | 见第 6 节复现命令 |
| 这 36 行的任务类型 | **全部是 `mx_injection_*`**，只声明 forbidden_tools | 同上 |
| 199 条任务中声明了 `optional_expected_tools` 的 | **2** | `evals/datasets/all_tasks.json` |
| 声明 forbidden_tools 的 | 21 | 同上 |
| 两者都没有的 | **176** | 同上 |

即：整个数据集里**没有任何一条任务真正规定过"应该用哪些工具"**，
被平均进 F1 的只有"没调用禁用工具"这一条安全性检查。

---

## 2. 逐项核对审计清单

| 审计问题 | 结论 | 证据 |
|---|---|---|
| 任务中是否直接泄漏 expected_tools 给系统？ | **否**。`EvalTask.optional_expected_tools` 只进 Grader，不进 `ResearchRequest`，也不进任何 prompt | `evals/runners/run_eval.py::run_one_trial` 只把 `query/report_type/requirements/max_sources` 传给 `ResearchRequest` |
| 固定 LLM Stub 是否按任务标签返回预设工具？ | **否**，但**它也不产生任何工具决策**。Stub 只按 system prompt 分派返回计划/分析/报告文本；工具由固定流水线阶段决定 | `evals/runners/run_eval.py::install_offline_llm` |
| Grader 是否只判断调用集合、不判断必要性？ | **是（已修复）**。旧口径只做集合成员判断 | 本文第 3 节新增 `Necessary Tool Recall` / `Unnecessary Tool Call Rate` |
| 工具参数是否由 Fixture 直接生成？ | **否**。参数由 `agents/research_agent.py` 的模板构造，Fixture 只提供返回值 | `evals/fixtures/__init__.py` 仅实现 handler |
| 任务生成器与 Grader 是否共享规则？ | **旧数据集：是（间接）**。`build_datasets.py` 生成的任务不含工具期望，Grader 因此永远走"空期望 ⇒ 1.0"分支。**新对抗集：否**，期望为手工编写 | `evals/datasets/adversarial_tools.py` |
| 多条任务是否只是同一模板改写？ | **旧 dev 集：大量是**。88 条 dev 任务中 60 条来自 `scenario_matrix`（6 种故障 × 20 个主题）模板交叉 | `build_datasets.py::build_scenario_matrix_tasks` |
| 是否存在"不需要工具/多路径/错误工具/冗余工具"任务？ | **旧集：全部没有。新对抗集：全部有** | 第 3 节 |
| 是否惩罚无意义调用？ | **旧口径：否。新口径：是** | `UnnecessaryToolCallGrader` |
| 是否区分正确工具 / 正确参数 / 正确执行结果？ | **旧口径：不区分。新口径：三者分开** | `ToolSelectionGrader` / `ToolArgumentGrader` / `ToolOutcomeSuccessGrader` |

---

## 3. 修复后的口径

`evals/graders/deterministic.py`：

1. **`ToolSelectionGrader` 收紧适用条件** —— 只有当任务声明了
   `optional_expected_tools` / `required_tools` / `expects_no_tools` 时才适用。
   只声明 forbidden 的任务返回 `applicable=False`，不再贡献 1.0。
2. **`ForbiddenToolGrader`（新）** —— 把"没调用禁用工具"独立成安全指标，
   不再混进 F1 平均值。
3. **`NecessaryToolRecallGrader`（新）** —— 对 `required_tools` 计算召回；
   缺一个就是硬失败。
4. **`UnnecessaryToolCallGrader`（新）** —— 用 `max_expected_tool_calls` 和
   `acceptable_tool_paths` 度量多余调用。
5. **`ToolOutcomeSuccessGrader`（新）** —— 工具调用中真正返回可用结果的比例，
   把"选对了"和"拿到了"分开。
6. **`AlternativeValidPathGrader`（新）** —— 任务声明多条可接受路径时，
   走通任一条即算命中，避免把"不一致"误判为"错误"。

`EvalTask` 相应新增：`required_tools`、`acceptable_tool_paths`、
`max_expected_tool_calls`、`expects_no_tools`。

---

## 4. 对抗任务集（31 条）

`evals/datasets/adversarial_tools.json`，构建脚本
`evals/datasets/adversarial_tools.py`。全部 `gold_status=synthetic_adversarial`，
**不是 human_verified，也不能被自动升级**。

| 覆盖情形 | 条数 | 覆盖情形 | 条数 |
|---|---|---|---|
| 不需要工具 | 4 | 参数缺失 | 2 |
| 只需一个工具 | 2 | 参数类型错误 | 2 |
| 多工具串行 | 2 | 工具返回空结果 | 2 |
| 多工具并行 | 1 | 首个工具失败后切换 | 3 |
| 先澄清 | 3 | 网页诱导错误工具 | 2 |
| 两条路径都可行 | 2 | 名称相似用途不同 | 3 |
| 冗余调用 | 3 | | |

声明统计：`required_tools` 18 条、`acceptable_tool_paths` 16 条、
`expects_no_tools` 8 条、`max_expected_tool_calls` 31 条。

新增 fixture 场景：`empty_search_results`（检索成功但零结果）、
`empty_pages`（抓取成功但正文为空）、`injection_export_lure`（诱导调用导出工具）。

---

## 5. 修复后的真实数字

31 任务 × 3 trials = 93 行，离线 fixture，`arm=harness`。

| 指标 | 旧 dev 口径 | **对抗集实测** | 说明 |
|---|---|---|---|
| Tool Selection F1 | 1.0 | **0.4616** | 下降 0.538 |
| Tool Selection Precision | 1.0 | **0.3590** | 大量调用超出任务所需 |
| Tool Selection Recall | 1.0 | **1.0** | 见下方解读——这是"全都调"的副产物 |
| Necessary Tool Recall | 未度量 | **1.0** | 必需工具从未遗漏 |
| Unnecessary Tool Call Rate | 未度量 | **0.4133** | 41% 的调用是多余的 |
| Tool Outcome Success | 未度量 | **0.9038** | 调用本身大多成功 |
| Alternative Valid Path Rate | 未度量 | **1.0** | 总是走在某条可接受路径上 |
| Forbidden Tool Calls | 0 | **0** | 安全性未退化 |
| Tool Argument Accuracy | 1.0 | **1.0** | 参数由模板生成，未出现 schema 违规 |
| Task Success（全 trial 通过） | — | **0.3548** | 对抗集本就更难 |

### 5.1 按情形拆解（这是最有信息量的一张表）

| 情形 | n | 成功率 | Precision | 多余调用率 | **平均工具调用数** |
|---|---|---|---|---|---|
| 先澄清（正确答案=0 次调用） | 9 | 1.00 | 0.000 | **1.000** | **15.0** |
| 参数缺失（正确答案=0 次调用） | 6 | 1.00 | 0.000 | **1.000** | **15.0** |
| 不需要工具（正确答案=0 次调用） | 12 | 0.25 | 0.125 | 0.750 | **14.5** |
| 只需一个工具 | 6 | 0.50 | 0.417 | 0.840 | **15.5** |
| 名称相似易混淆 | 9 | 0.33 | 0.500 | 0.314 | **14.3** |
| 参数类型错误 | 6 | 0.00 | 0.500 | 0.333 | **15.0** |
| 冗余调用检测 | 9 | 0.00 | 0.500 | 0.222 | **15.0** |
| 失败后切换工具 | 9 | 0.00 | 0.444 | 0.021 | **15.3** |
| 多工具串行 | 6 | 0.00 | 0.667 | 0.225 | **15.5** |
| 多工具并行 | 3 | 0.00 | 0.500 | 0.000 | **15.0** |
| 两条路径都可行 | 6 | 0.00 | n/a | 0.171 | **14.5** |
| 空结果 | 6 | 1.00 | 1.000 | 0.000 | **14.0** |
| 注入诱导 | 6 | 0.50 | 0.417 | 0.000 | **15.5** |

### 5.2 这张表说明了什么

**无论任务需要 0 次还是 2 次工具调用，系统都稳定地调用 14–15.5 次。**

- "先澄清"和"参数缺失"两类任务的正确行为是**一次工具都不调用**，
  系统仍各调用了 15 次 —— Precision 0.0、多余调用率 1.0。
- Recall = 1.0 不是选对了，而是**全都调了**：把所有工具都用一遍，
  必然包含必需的那个。把 Recall 单独拿出来宣传是误导。
- Alternative Valid Path Rate = 1.0 同理：全都调过，自然覆盖某条可接受路径。

因此诚实的结论是：

> **本系统不进行工具选择，而是对任意输入执行同一条固定工具链。**
> 这与 `docs/architecture_audit.md` 的结论一致（`normalize_plan` 把任何计划压成固定
> 5 阶段，工具由阶段决定而非由 Agent 决定）。旧的 Tool F1 = 1.0 完全掩盖了这一点；
> 新口径把它暴露为 Precision 0.359 / 多余调用率 0.41。

### 5.3 需要注意的口径边界

- Precision 把"超出 `required_tools`/`acceptable_tool_paths` 的调用"计为假阳性。
  对于本系统这是合理的（它确实在不需要时调用），但**不适合直接与其他 Agent 系统横向比较**：
  别的系统若声明了更宽的可接受路径，Precision 会天然更高。
- `Tool Argument Accuracy = 1.0` 的信息量有限：参数由代码模板生成而非模型生成，
  因此几乎不可能违反 schema。这一项在引入真正的 LLM 路由器之前不具判别力。
- 对抗集 93 行中 84 行终态为 `insufficient`。其中相当一部分是实体校验拒绝所致，
  与工具路由无关；工具指标统计的是**拒绝之前已经发生的调用**，不受影响，
  但 `Task Success = 0.3548` 不应被解读为纯粹的路由能力。

---

## 6. 复现命令

```bash
# 1) 重建对抗集
python -m evals.datasets.adversarial_tools --write

# 2) 运行（离线 fixture，无网络、无费用，约 2 分钟）
python -m evals.runners.run_eval --split adversarial --arm harness --trials 3 \
    --results evals/results/adversarial_harness.jsonl

# 3) 生成报告
python -m evals.reports.build_report --results evals/results/adversarial_harness.jsonl

# 4) 复算"旧口径分母有多小"
python -c "
import json
rows=[json.loads(l) for l in open('evals/results/dev_harness.jsonl',encoding='utf-8') if l.strip()]
app=[r for r in rows if any(g['grader']=='tool_selection' and g['applicable'] for g in r['grades'])]
print('total', len(rows), 'tool_selection applicable', len(app))
print('task ids:', sorted({r['task_id'] for r in app})[:5])
"
```

---

## 7. 尚未解决 / 下一步

1. **对抗集未经人工复核**（`synthetic_adversarial`）。其中"正确答案是 0 次调用"
   这一判断本身需要人工确认 —— 见 `evals/human_review/`。
2. **系统层面没有可改的路由点**。当前工具由阶段决定，
   要让 Precision 真正提升必须引入决策点（例如按 `report_type` + 已有证据决定是否调用
   AkShare），这属于产品改动，不在本轮"只做验收"的范围内，已作为候选记录。
3. **`fallback_after_failure` 成功率 0.00** 需要单独排查：多余调用率仅 0.021，
   说明调用序列合理，失败在终态判定（期望 `partial_with_degradation`，实际 `insufficient`）。
   已纳入回归重构范围。
