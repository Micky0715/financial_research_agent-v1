# V1 Final Status

## 1. 当前版本定位

当前版本是一个**基于公开网页/PDF 来源的金融研报多 Agent 生成系统**，重点验证可追溯、可评估、可复盘的工程链路，而不是追求生成速度或金融分析的专业深度。

## 2. 已完成能力

- Multi-Agent workflow（PlanningAgent -> ResearchAgent -> BrowserAgent -> AnalyzeAgent -> ReportAgent -> Evaluation）
- Web/PDF research（并发搜索 + 缓存，网页/PDF 正文抓取）
- Source scoring（域名白/黑名单 + subject/别名/行业词综合相关性打分）
- Source-linked report generation（`[source_id]` 引用的 Markdown/HTML 报告）
- Evaluation（规则化质量评分，含 source_count 硬性封顶）
- Trace（research/browser/planner/performance/compression/evaluation 六类指标全链路记录）
- 10 case evaluation（`eval/topics.json` 固定评测集 + `eval/eval_runner.py`）
- Direct LLM baseline（对比"有来源引用"和"无来源直接生成"）
- Bad case replay（[eval/bad_cases.md](../eval/bad_cases.md) 记录 10 个真实工程问题的现象/根因/修复/指标变化）

## 3. 最终评测指标

以下是 v1 收口时的评测快照（来自 `eval/topics.json` 10 个 topic 的最新一次 run，详见 `outputs/eval/eval_report.md`）：

- total_cases: 10
- success_cases: 10
- success_rate: 1.0
- avg_source_count: 5.0
- avg_quality_score: 0.831
- avg_total_time: 124.336s
- avg_research_time: 0.029s（warm-cache，见下方说明）
- avg_browser_time: 23.406s
- avg_analyze_time: 34.721s
- avg_report_time: 32.961s
- planner_normalized_task_count all == 5: True

**warm-cache 说明**：`avg_research_time=0.029s` 是 10 个固定 topic 反复评测、命中 `outputs/cache/search_cache.json` 后的结果，不代表首次搜索一个新 topic 的真实耗时。冷缓存下 `ResearchAgent` 历史实测约 10-16 秒。详见 [eval/bad_cases.md](../eval/bad_cases.md) Bad Case 10。

## 4. 当前限制

- 没有长期记忆——每次运行都是无状态的一次性流水线。
- 没有向量数据库——来源筛选依赖关键词和规则，不是语义检索。
- 没有真实金融 API（Wind/AkShare 等）——财务数字全部来自公开网页/PDF 文本抽取。
- 没有严格 DCF 等财务估值建模工具。
- 没有标准 MCP Server/Client 协议封装。
- Evaluation 仍是启发式的工程规则评分，不等价于专业金融分析师判断。
- industry_research 的评分维度还有改进空间——目前复用公司导向的财务关键词表判断"财务深度"，导致部分行业研报（如 AI机器人、低空经济）被 `financial_depth_score=0` 拖累总分，详见 [eval/bad_cases.md](../eval/bad_cases.md) Bad Case 9。
- `source_grounding` 目前是工程引用层面的检查（有没有标注 `[source_id]`），不等于逐条数字的完全事实验证。
- 报告内容不可作为投资建议。

## 5. 后续 v2 方向

- report_type-specific Evaluation（行业研报单独设计评分维度）
- source tier 分级（更细粒度区分官方公告/权威媒体/一般来源）
- 数字级 grounding（从"有没有引用"细化到"每个数字有没有对应来源"）
- AkShare 结构化金融数据工具
- Valuation / DCF 工具
- Docx/PDF 正式报告导出
- MCP-compatible tool registry
- 本地文件上传解析
- 向量数据库与长期知识库

## 6. V1 是否收口

**V1 已完成工程闭环，可以作为稳定版本上传 GitHub。** 后续新功能建议在 v2 分支开发，避免破坏当前评测链路（`eval/topics.json` 固定 10 case + `outputs/eval/` 下的评测汇总/baseline 对比）。
