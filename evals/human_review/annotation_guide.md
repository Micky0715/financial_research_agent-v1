# 人工 Gold 审核指南

面向真人审核者。目标不是"审完 200 条"，而是**审完 37 条并且每条都站得住**。
预计耗时 2–3 小时（每条 3–5 分钟）。

---

## 0. 为什么需要你

当前数据集里 **human_verified = 0**。所有任务的"正确答案"要么是脚本生成的，
要么是根据系统自己的运行结果反推的。这意味着：

- 现在所有评测指标只能标注为 draft / synthetic；
- 用这些标签训练的路由器，学到的是**复现当前系统的行为，包括它的错误**；
- 没有人工基准，就无法判断系统"做对了"还是"一贯这么做"。

系统**无法**自行升级标注状态：`validate_gold.py` 会拒绝空审核人、
拒绝 `claude`/`ai`/`test` 这类占位名、拒绝未来日期、拒绝自相矛盾的答案。
只有你填写的内容才算数。

---

## 1. 操作步骤

```bash
# 1) 生成队列（已生成，可跳过；重跑不会打乱顺序）
python -m evals.human_review.build_gold_queue --write

# 2) 编辑队列文件，逐行填写 review 字段
#    evals/human_review/gold_review_queue.jsonl
#    每行是一个独立 JSON 对象；只改 "review" 里的内容，不要动 "current"

# 3) 随时校验填写质量（不写回数据集，安全）
python -m evals.human_review.validate_gold

# 4) 确认无误后写回
python -m evals.human_review.validate_gold --apply
```

建议用支持 JSON Lines 的编辑器（VS Code + Rainbow CSV / JSON 插件）。
可以分多次进行：未填写的行会被识别为"未填写"，不会报错。

---

## 2. 每个字段怎么填

行结构：`stratum` / `task_id` / `query` / `current`（系统当前的假设，供参考）
/ `review`（**你填这里**）。

| 字段 | 类型 | 填写说明 |
|---|---|---|
| `task_is_reasonable` | true / false | 这个任务本身是否是一个**合理的金融研究请求**？如果任务本身有歧义、无法回答、或表述荒谬，填 `false` —— 该任务会被**移出数据集**，而不是升级。这是最重要的一道闸门。 |
| `required_facts` | 列表 | 一份**正确**的报告必须包含哪些事实。格式：`[{"key":"revenue_2024","any_of":["1200.5亿元","1,200.5亿元"],"description":"2024年营业收入"}]`。`any_of` 写下所有可接受的表述形式。**不确定就留空**，空列表比编造的事实好。 |
| `acceptable_sources` | 列表 | 哪些来源可以作为依据（如 `["cninfo.com.cn","交易所公告","公司年报"]`）。 |
| `correct_tool_is_unique` | true / false | 完成该任务是否**只有一条**正确的工具路径？ |
| `acceptable_tool_paths` | 列表的列表 | 所有可接受的工具组合，如 `[["fetch_financial_snapshot"],["web_search","read_webpage"]]`。若上一项为 true，这里应只有一条。 |
| `required_tools` | 列表 | **缺了就一定做不对**的工具。与 `acceptable_tool_paths` 的区别：后者是"可以这么做"，前者是"必须用到"。 |
| `expects_no_tools` | true / false | 正确做法是否**一次工具都不该调用**（纯概念题、信息不足需先澄清）。若为 true，`required_tools` 必须为空。 |
| `correct_final_state` | 枚举 | 正确的运行终态，五选一：<br>`report_generated` 正常出报告<br>`report_with_caveats` 出报告但需标注局限<br>`partial_with_degradation` 降级完成<br>`insufficient_evidence` 资料不足，如实拒绝<br>`refuse` 明确拒答 |
| `degraded_acceptable` | true / false | 降级完成是否也算合格。 |
| `forbidden_claims` | 列表 | 绝不能出现在报告里的表述，如 `["保证收益","目标价"]`。 |
| `citations_support_conclusion` | true / false / `"not_applicable"` | 如果你看了产出报告：引用的来源是否**真的支持**它所在句子的结论？（这是机器查不了的部分） |
| `numbers_correctly_grounded` | true / false / `"not_applicable"` | 报告中的数字是否被正确溯源（有来源 / 标为假设 / 标为测算）？ |
| `score` | 1–5 整数 | 整体质量。1=完全不可用，3=可用但有明显缺陷，5=可直接交付。 |
| `notes` | 字符串 | 任何需要说明的判断依据，尤其是你和系统当前假设不一致的地方。 |
| `annotator` | 字符串 | **必填**。你的真实姓名或稳定标识。不能是 `me`/`test`/`ai`/`claude` 等占位符。 |
| `reviewed_at` | `YYYY-MM-DD` | **必填**。不能是未来日期。 |

---

## 3. 判断口径（重要）

### 3.1 "拒答"是成功，不是失败

如果任务主体是虚构的、信息不足以作答、或问的是尚未发生的期间，
**正确终态是 `insufficient_evidence`**。系统输出一份漂亮但无依据的报告是**失败**，
无论文字多完整。这一条最容易填反。

### 3.2 不要把"系统当前做法"当成正确答案

`current` 字段给出的是系统现在的假设，**它可能就是错的**。
本轮审计已经查出：系统对任何输入都调用 14–15 次工具，包括正确答案是
0 次调用的任务（见 `docs/tool_eval_leakage_audit.md`）。
请按"一个称职的分析师会怎么做"来填，而不是按系统做了什么来填。

### 3.3 多路径要写全

如果营业收入既可以查结构化数据、也可以抓年报网页，两条都要写进
`acceptable_tool_paths`。只写一条会把另一条正确做法误判为错误。

### 3.4 不确定就留空

空字段会被识别为"该项未提供"，不会污染指标。
编造一个 `required_facts` 比留空危害大得多。

### 3.5 事实类判断需要你实际去核对

`required_facts` 里的数字应该是你**查证过**的，不是从系统报告里抄的。
如果没有条件核实，留空并在 `notes` 里说明。

---

## 4. 填写示例

```json
{
  "stratum": "AkShare结构化数据",
  "task_id": "topic_贵州茅台财务与估值分析",
  "query": "贵州茅台财务与估值分析",
  "current": {"expected_outcome": "report_generated", "gold_status": "synthetic_draft"},
  "review": {
    "task_is_reasonable": true,
    "required_facts": [],
    "acceptable_sources": ["cninfo.com.cn", "公司年报", "交易所公告"],
    "correct_tool_is_unique": false,
    "acceptable_tool_paths": [["fetch_financial_snapshot"],
                              ["web_search", "read_webpage"]],
    "required_tools": [],
    "expects_no_tools": false,
    "correct_final_state": "report_with_caveats",
    "degraded_acceptable": true,
    "forbidden_claims": ["保证收益", "目标价"],
    "citations_support_conclusion": true,
    "numbers_correctly_grounded": false,
    "score": 3,
    "notes": "估值部分的 PE 数字没有标注取数日期，属于溯源不完整；结构化与网页两条路径都可接受。",
    "annotator": "张三",
    "reviewed_at": "2026-09-09"
  }
}
```

---

## 5. 完成后会发生什么

`validate_gold.py --apply` 会：

1. 把通过校验的任务升级为 `gold_status=human_verified`，并写入你的
   `annotator` 与 `reviewed_at`；
2. 把你标记 `task_is_reasonable=false` 的任务**从数据集中移除**；
3. 用你填写的 `correct_final_state` / `required_facts` / 工具期望**覆盖**原有假设；
4. 重新导出 `dev.json` / `test.json` / `regression.json`。

在此之后，评测报告中的"数据性质声明"才会显示非零的 human_verified 数量，
相关指标才可以脱去 draft 标签。

---

## 6. 当前覆盖缺口（不是审核项）

队列中有 3 条 `__SHORTFALL__` 标记，表示数据集在该分层下任务数不足：

| 分层 | 需要 | 实有 |
|---|---|---|
| PDF 抽取 | 3 | 2 |
| 来源冲突 | 3 | 2 |
| 长上下文 | 2 | 1 |

这些是**数据集覆盖缺口**，需要补充任务而不是审核。请跳过这些行。
