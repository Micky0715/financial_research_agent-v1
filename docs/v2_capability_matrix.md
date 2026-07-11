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
