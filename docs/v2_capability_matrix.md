# v2 能力对比表（改动前 vs 改动后）

每一行对应一项能力：v1 状态 → v2 状态 → 具体代码位置 → 验证证据。所有"验证证据"都指向仓库里可复查的产物（commit、eval 输出、trace 字段、对比报告），不是口头声明。

| # | 能力 | v1 状态 | v2 状态 | 代码位置 | 验证证据 |
|---|---|---|---|---|---|
| 1 | 工具调用协议 | 同进程直接函数调用，无协议边界 | `web_search`/`read_webpage`/`read_pdf` 经标准 MCP（官方 `mcp` SDK，FastMCP + stdio）调用：Server 子进程 + 持久 ClientSession，并发多路复用，不可用时优雅降级 | Server: [mcp_server/tools_server.py](../mcp_server/tools_server.py)；Client 网关: [tools/tool_gateway.py](../tools/tool_gateway.py)；Agent 侧仅换 import：[agents/research_agent.py](../agents/research_agent.py) / [agents/browser_agent.py](../agents/browser_agent.py)；调用方式前后对比: [docs/mcp_integration.md](mcp_integration.md) | commit `a81afd1`；eval 10/10、日志零 fallback；bad_cases 11（anyio keeper-task 坑） |
| 2 | 信息源筛选 | 纯关键词/权威度规则打分 | 规则分 + 本地 embedding 语义相似度加权融合（`(1-w)*rule + w*semantic`，w=0.35），三分数字段保留可解释性；模型不可用自动退回纯规则；另加单域名分散守卫 | 语义层: [tools/semantic_scorer.py](../tools/semantic_scorer.py)（bge-small-zh-v1.5，ModelScope 下载，CPU）；融合: [tools/source_filter.py](../tools/source_filter.py)；域名分散: [agents/browser_agent.py](../agents/browser_agent.py) | commit `ff3d547`；召回对比 [outputs/eval/semantic_ranking_compare.md](../outputs/eval/semantic_ranking_compare.md)（命中率 0.905→0.910，含 rule=0.0/semantic=0.809 被正确拉回的实例）；bad_cases 12/13 |
| 3 | 估值建模 | 无估值计算，估值内容全靠 LLM 复述来源 | 确定性两阶段 DCF 工具（显式预测期逐年折现 + Gordon 永续期），bear/base/bull 三情景区间；参数从来源文本抽取（现金流→净利润代理→默认值三级回退），逐参数 extracted/proxy/default 出处标注；AnalyzeAgent 对公司研究显式调用，独立字段进报告 | 计算+抽取: [tools/valuation_dcf.py](../tools/valuation_dcf.py)（`dcf_valuation`/`dcf_valuation_range`/`extract_dcf_inputs`）；集成: [agents/analyze_agent.py](../agents/analyze_agent.py)；呈现规则: [prompts/report_prompt.txt](../prompts/report_prompt.txt) 第 9 条；默认假设: config.py `DCF_*` | 数学单测（手算对照 1692.31 一致）+ 守卫单测（g_term≥r 拒绝）；真实 run：比亚迪 bear/base/bull = 4482/5872/7893 亿元（base_fcf=净利润代理 326.19，provenance 带 source_id）；报告正文含 DCF 区间+工程近似声明；引用修复后复验 eval：10/10、avg_quality 0.724->0.798（基期参数带 [source_id]，假设参数标注"配置假设"） |
| 4 | 图表输出 | 无任何图表 | 从来源正文抽取年度营收/净利润序列，matplotlib 折线图 base64 内嵌 HTML（单文件可分发）；四道守卫（口径/实体/多指标句/数量级离群）；数据点<2 诚实跳过 | 抽取+渲染: [tools/chart_renderer.py](../tools/chart_renderer.py)；嵌入: [tools/report_renderer.py](../tools/report_renderer.py) `extra_html` + [agents/report_agent.py](../agents/report_agent.py)；trace 字段 `chart_embedded` | 真实 run：比亚迪 HTML 报告含 1 张 base64 趋势图；四类误归属实测案例修复过程见 bad_cases 14/15 |
| 5 | Planner 动态性 | normalize_plan 强制固定 5 阶段，无任何阶段重跳（v1 为治 LLM 输出 9/17 个任务的乱象） | 保留固定 5 阶段骨架，新增**有界确定性重执行**：browse 因候选供给不足失败 → 回跳 research 一轮（force_fallback 强制权威站点 query）→ 重试 browse；硬上限 1 轮，只在失败路径触发；trace 记录 `replan_metrics` | 方案评估: [docs/dynamic_planning.md](dynamic_planning.md)（含 A/B/C 三方案对比）；实现: [orchestrator/workflow.py](../orchestrator/workflow.py) replan 块 + [agents/research_agent.py](../agents/research_agent.py) `force_fallback` 参数 + [schemas/trace.py](../schemas/trace.py) | 确定性单测（monkeypatch 空搜索）：`replan_metrics={triggered:True, rounds:1}`，trace 步骤序列 `t1/t2/t1_replan/t2_replan`；成功路径实测 `triggered:False`（10 case eval 全程不触发，零回归） |

## 边界（对表格里每一项的诚实限定）

1. **MCP**：自用 stdio server（网关自动拉起、只服务本 pipeline），不是可供外部 IDE/Agent 连接的独立部署服务；analyze 阶段的规则抽取函数未 MCP 化（无跨进程价值）。
2. **语义排序**：仅排序层，无向量库持久化、无 RAG；对比报告显示改动是增量式的（命中率 +0.005，窗口重叠 0.87），价值在关键词漏召回的个案，不是全面碾压规则。
3. **DCF**：工程近似（净利润代理现金流未扣资本开支、折现率/永续增长是配置假设），输出明确标注不构成目标价。
4. **图表**：数据来自新闻文本正则抽取，守卫策略"宁缺毋滥"——部分主题会因数据点不足而无图。
5. **动态路由**：只覆盖"browse 候选不足"一种场景，重检索 query 是固定模板不是 LLM 改写；每次成功运行的执行路径仍是固定 5 阶段。
6. 虚构/极冷门主体存在 groundedness 缺口（bad_cases 16），相关性评分可能放行泛金融无关内容。

---

# v3 能力对比表（v2 -> v3-full）

| # | 能力 | v2 状态 | v3 状态 | 代码位置 | 验证证据 |
|---|---|---|---|---|---|
| 6 | 结构化金融数据 | 无（财务数字全靠网页/PDF 文本抽取） | AkShare 四个实测可用接口 -> FinancialDataSnapshot（年度序列+估值指标），逐接口降级、缺失字段如实 missing_fields；经 MCP/gateway 统一调用 | [tools/akshare_tool.py](../tools/akshare_tool.py)、[tools/financial_data_normalizer.py](../tools/financial_data_normalizer.py)、[schemas/financial_data.py](../schemas/financial_data.py) | 茅台真实快照：6年营收序列/净利823.2亿/ROE 32.53%/PE 18.21；东财系 ProxyError 实测规避（bad_cases 18）；降级单测 |
| 7 | 相对估值 | 无 | PE/PB 相对自身近一年历史分位（拒绝编造行业均值），PS 无历史如实 unknown | [tools/valuation_relative.py](../tools/valuation_relative.py) | 茅台 PE 18.21 @ 4.4% 分位 -> undervalued（相对自身历史）；bad_cases 19；单测 |
| 8 | DCF 输入来源 | extracted/proxy/default 三级 | +akshare/derived 两级（AkShare 现金流优先、营收 YoY 派生增速），输出 input_sources/assumptions/warning/valuation_method | [tools/valuation_dcf.py](../tools/valuation_dcf.py) | v3 eval 5/5 公司 topic DCF 触发；input_sources 逐参数标注；单测 |
| 9 | 来源分级 | 排序层内部 tier1/2 | 显式 source tier 体系（tier1/2/3/unknown/local_user_file），每个 Source 带 6 个分级字段，质量报告 | [tools/source_tier.py](../tools/source_tier.py)、[scripts/build_source_quality_report.py](../scripts/build_source_quality_report.py) | v3 eval tier1+2 占比 0.42（真实分布，如实呈现）；单测 |
| 10 | 数字级 grounding | 句级"有无引用"检查 | 全文数字五类溯源（sourced/akshare/assumption/calculated/unsourced）+ 幻觉引用检测 + 运行时数值核对 | [tools/number_grounding.py](../tools/number_grounding.py)、[tools/citation_checker.py](../tools/citation_checker.py) | grounding 率 0.691（v2 报告基线）-> 0.879（v3）；抓到真实幻觉引用；单测 |
| 11 | 分型评估 | 单一公司导向指标打所有报告 | company/industry/risk/valuation/macro 五套（后三类未经真实 topic 验证，已注明），行业不再被财务关键词封顶 | [evaluators/report_evaluator.py](../evaluators/report_evaluator.py) | 同一文本 company/industry 评分逻辑分歧单测；v3 eval 行业组 avg 0.8（bad_cases 9 闭环） |
| 12 | 报告导出 | Markdown/HTML | +DOCX（python-docx）/PDF（Edge headless，可降级） | [tools/report_exporter.py](../tools/report_exporter.py)、[scripts/export_reports.py](../scripts/export_reports.py) | 比亚迪真实 DOCX+PDF 双导出；markdown-only run 的 PDF 诚实跳过（bad_cases 22） |
| 13 | 本地文件输入 | 无 | PDF/TXT/MD/CSV/Excel -> source（s901+，不占网页名额），财报字段尽力抽取 | [tools/local_file_reader.py](../tools/local_file_reader.py) | fixtures 实测（TXT 抽出真实营收序列/CSV 表格元数据/坏文件跳过）；单测 |
| 14 | 虚构主体识别 | 无（bad_cases 16 缺口） | A股代码表+别名表+来源正文三重证据，failed -> insufficient_entity_evidence 终止 | [tools/entity_validator.py](../tools/entity_validator.py) | 虚构公司 E2E 实测拦截（相关性层被骗、实体层拦下，bad_cases 23）；10 真实 topic 零误杀 |
| 15 | 记忆 | 无 | 本地轻量向量索引（numpy+bge，关键词降级），默认不参与主链路 | [memory/](../memory/) | 27 条索引，比亚迪查询 0.823 命中；降级单测；bad_cases 24 |
| 16 | 离线测试 | 无正式测试套件 | 30 个 pytest（全离线）+ deep_eval/latency/ablation 报告 | [tests/](../tests/)、scripts/build_*_report.py | 30/30 通过 ~6s；ablation 只用真实历史数据，未测项标 insufficient data |
