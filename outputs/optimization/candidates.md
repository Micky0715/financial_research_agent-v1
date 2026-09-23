# 改进候选（提案，未应用）

> 以下全部是**建议**。没有任何一条被自动应用到生产配置；采纳必须先通过 `regression_gate`，再由人工确认。

## 候选 1：cand_c4c4b76c624b0706

- 目标失败模式：`insufficient_retrieval`（检索不足：候选或可用来源数量低于下限），切片规模 33
- 修改类型：`retrieval_param`
- 修改对象：`config.SEARCH_MIN_UNIQUE_RESULTS`
- 版本：`v004`

**修改原因**：33 次运行因可用来源为 0 终止；提高触发权威站点 fallback 的阈值，让第一轮结果偏少时更早补检索，而不是带着稀薄候选进入 browse。

**预期指标变化**：task_success_rate_all_trials +0.03

**可能引入的回归**：fallback 触发更频繁，平均时延与工具调用数上升

**Diff**：

```diff
--- config.SEARCH_MIN_UNIQUE_RESULTS (current)
+++ config.SEARCH_MIN_UNIQUE_RESULTS (candidate)
@@ -1 +1 @@
-SEARCH_MIN_UNIQUE_RESULTS = 20
+SEARCH_MIN_UNIQUE_RESULTS = 30
```

## 候选 2：cand_85fb4a12a0f38f1a

- 目标失败模式：`ungrounded_numbers`（数字未溯源：报告中存在无来源数字），切片规模 3
- 修改类型：`prompt`
- 修改对象：`prompts/report_prompt.txt`
- 版本：`v005`

**修改原因**：3 次运行报告中存在无来源数字；把数字溯源的四类归属直接写进生成指令，并给出“写不出来源就降级为定性描述”的明确出路。

**预期指标变化**：number_grounding_rate +0.05、unsupported_number_rate -0.05

**可能引入的回归**：报告中数字总量下降，可能影响财务深度评分

**Diff**：

```diff
--- prompts/report_prompt.txt (current)
+++ prompts/report_prompt.txt (candidate)
@@ -1 +1,3 @@
 请输出完整的 Markdown 研报正文。
+
+【硬性要求】正文中出现的每一个金额/百分比/倍数，必须满足以下之一：(1) 所在句给出 [sN] 引用；(2) 明确标注来自 AkShare 结构化数据；(3) 明确标注为配置假设；(4) 明确标注为模型测算。无法满足时，改写为定性描述，不要写出该数字。
```
