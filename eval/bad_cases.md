# Bad Case 复盘

记录 v1 迭代过程中发现并修复的真实工程问题。每条按现象 / 原因 / 修改 / 结果 / 指标变化整理，数字尽量取自实际运行的 trace / `outputs/eval/eval_summary.csv`，不是凭空总结。

---

## Bad Case 1：ResearchAgent 串行搜索导致耗时过长

### 现象
`ResearchAgent` 顺序执行 9 个搜索 query（其中很多是把用户 `requirements` 逐一映射成 query，如"财务分析""估值分析""风险提示"各生成一条，与核心 query 语义高度重叠），单次运行 Research 阶段耗时 32-155 秒，波动很大。

### 原因
- Query 生成逻辑把报告章节需求 1:1 映射成搜索 query，数量膨胀且信息增量很低。
- 9 个 query 用 `for` 循环顺序调用 `web_search()`，没有并发。
- 没有搜索结果缓存，重复调试同一个 topic 也要整批重新搜索。

### 修改
- Query 收敛为 5 个核心维度 query + 最多 1 个别名 query，不再逐 requirement 生成。
- 改用 `ThreadPoolExecutor` 并发执行，每个 query 独立 timeout。
- 新增 `utils/cache_utils.py`，按 query 原文缓存搜索结果到 `outputs/cache/search_cache.json`，24 小时 TTL。

### 结果
Research 阶段冷缓存降到 10-16 秒，热缓存（同一 topic 重跑）降到 0.03 秒左右。

### 指标变化
- query 数量：9 -> 5-6
- Research 耗时（冷缓存）：32-155s -> 10-16s
- Research 耗时（热缓存）：- -> ~0.03s
- `outputs/eval/eval_summary.csv` 中 10 个 topic 的 `research_time` 目前全部在 0.02-0.03s（热缓存）

---

## Bad Case 2：Planner 输出重复任务导致执行链路不稳定

### 现象
部分 topic 的 LLM Planner 输出异常多任务，包含重复的 research/browse 阶段。`WorkflowOrchestrator` 按计划顺序执行，导致同一 case 实际跑了多次 research/browse/analyze，耗时和资源消耗成倍增加。

真实记录（`outputs/eval/eval_summary.csv` 中的 `planner_raw_task_count`）：
- 隆基绿能行业地位分析：`raw_task_count=9` -> `normalized_task_count=5`
- 半导体国产替代行业研究：`raw_task_count=17` -> `normalized_task_count=5`

### 原因
`PlanningAgent._validate_plan()` 只校验"必需的 4 个 stage 是否都出现过"，不限制每个 stage 只能出现一次，也不限制任务总数，LLM 输出的任务数量完全不受约束。

### 修改
新增 `normalize_plan()`（[agents/planning_agent.py](../agents/planning_agent.py)），在执行前把任意数量的 LLM 输出强制收敛为固定 5 个任务（research/browse/analyze/report/evaluate，每种最多保留第一个，依赖/顺序/优先级/重试次数全部固定）。这一步不只放在 `PlanningAgent` 内部，`orchestrator/workflow.py` 在执行边界处强制调用，即使 Planner 未来又输出异常计划，执行层也不会重复跑某个阶段。

### 结果
`outputs/eval/eval_summary.csv` 中全部 10 个 topic 的 `planner_normalized_task_count` 都恒为 5，不再出现重复执行。

### 指标变化
- raw tasks：9 / 17 -> normalized 5（两个真实 case）
- `planner_normalized_task_count all == 5`：True（10/10）

---

## Bad Case 3：QualityScorer 完整 topic 匹配导致相关性误杀

### 现象
多个 case 出现"内容读到了，但判定为不相关"：
```
browsed 20 candidates -> 17 usable -> 0 relevant -> top 0 kept
```
失败 case 包括贵州茅台、中芯国际、隆基绿能、AI机器人、光伏等 topic，当时 `source_count=0`，报告无法生成。

### 原因
`QualityScorer._topic_relevance()` 只做"完整 subject 字符串是否出现"的判断。真实文章经常只提到公司别名/股票代码（如"SMIC"而非"中芯国际"）或行业同义词（如"硅片"而非"光伏"），从不逐字复述标准化后的 subject，导致大量真实相关内容被判定为 near-zero relevance。

### 修改
- 新增 `COMPANY_ALIASES` / `INDUSTRY_SYNONYMS` / `build_relevance_profile()`（[utils/text_utils.py](../utils/text_utils.py)），把 topic 展开成 subject + 别名 + 行业同义词的集合。
- 重写 `QualityScorer._topic_relevance()`（[tools/quality_scorer.py](../tools/quality_scorer.py)），改为 subject/alias/industry_term/权威域名综合判断。

### 结果
中芯国际投资风险分析实测：`browsed 6 candidates -> 6 usable -> 6 relevant -> top 5 kept`（此前是 `20 usable -> 0 relevant`）。

### 指标变化
- 中芯国际 case：source_count 0 -> 5，quality_score 从失败（N/A）-> 0.973（`eval_summary.csv` 最新记录）
- 单测：内容仅含"SMIC"、不含"中芯国际"字样时，`topic_relevance` 从近零 -> 0.75

---

## Bad Case 4：BrowserAgent 对同一批 URL 无意义重试

### 现象
`BrowserAgent` 抓取失败后（`fetched_ok>0` 但 `relevant=0`），`BaseAgent` 的重试机制会用完全相同的候选列表再跑 2 次，日志里连续 3 次出现一模一样的 `browsed 20 candidates -> 17 usable -> 0 relevant -> top 0 kept`，浪费 80-120 秒却不可能得到不同结果。

### 原因
`Task.max_retries` 默认值为 2（共 3 次尝试），且异常处理没有区分"值得重试的错误"（网络问题）和"重试也没用的错误"（同一批数据评分结果确定性为空）。

### 修改
- `BaseAgent.run()` 新增对 `exc.non_retryable` 的识别，标记为 True 时立即停止重试（[agents/base_agent.py](../agents/base_agent.py)）。
- `browse` 任务的 `max_retries` 固定为 1（而非 2），由 `normalize_plan()` 统一设置。
- `BrowserAgent` 区分：`fetched_ok=0`（可能是网络问题，允许重试 1 次）vs `fetched_ok>0 但 relevant=0`（标记 `non_retryable=True`，不再重试同一批 URL）。
- 新增 relaxed relevance 兜底：`usable_count>=5` 时改选分数最高的来源（`metadata.relaxed_selected=True`）。

### 结果
同一批候选不会再被抓取 3 次；`fetched_ok>0` 但 `relevant=0` 时要么走 relaxed relevance 兜底成功，要么直接判定失败并跳过 analyze/report/evaluate。

### 指标变化
- 单个 case 因重试浪费的时间：80-120s -> 0s
- browse 任务 `max_retries`：2 -> 1

---

## Bad Case 5：source_count 太低但 Evaluation 虚高

### 现象
半导体国产替代行业研究一次运行 `source_count=1`，`quality_score` 却是 **0.832**——只有 1 篇来源支撑的报告不应该拿到接近满分的评估。

### 原因
`evaluate_report_quality()` 早期版本是纯加权平均，`source_count` 只影响其中一个维度（`source_count_score`），权重不够高，其余维度（completeness/structure/clarity 等）只要报告格式齐全就能拿高分，足以把总分拉到 0.8+，掩盖了"只有 1 个来源"这个根本性缺陷。

### 修改
在 [agents/report_agent.py](../agents/report_agent.py) 的 `evaluate_report_quality()` 中新增硬性封顶（取 min，不是加权）：
```
source_count == 0  -> overall_score <= 0.3
source_count == 1  -> overall_score <= 0.5
source_count == 2  -> overall_score <= 0.6
source_count < 5   -> overall_score <= 0.85
```
同时新增诊断字段 `source_count_cap_applied` / `score_cap_reason`，记录到底是哪个条件封的顶。

### 结果
单测验证：构造一份 `source_count=1` 但结构完整、引用齐全的报告，`overall_score` 被硬性封顶在 0.5。

### 指标变化
- source_count=1 时质量分：0.832 -> **最高 0.5**
- `diagnostics.source_count_cap_applied`：（不存在）-> `true`/`false` 明确记录

---

## Bad Case 6：eval_summary 统计历史旧 run，导致最终指标污染

### 现象
`python scripts/collect_eval_summary.py` 打印 `Collected 31 run(s)`——把开发过程中积累的历史 run、失败 run、调试 run 全部混进了最终评测统计，而不是只统计 `eval/topics.json` 里 10 个正式 topic 的最新结果。

### 原因
脚本原本是"扫描 `outputs/traces/` 下所有 trace 文件、每个都生成一行"，没有和 `eval/topics.json` 做关联过滤，也没有对同一 topic 的多次历史 run 做去重。

### 修改
重写 `scripts/collect_eval_summary.py`：以 `eval/topics.json` 的 10 个 topic 为主索引，每个 topic 只取 `finished_at`（回退 `started_at`）最新的一条 trace；找不到 run 的 topic 生成 `main_issue=missing_run` 的占位行，而不是被静默丢弃或让脚本崩溃。

### 结果
控制台输出从"把历史 run 全部混算"变成"精确统计 10 个正式 topic"。

### 指标变化
- 修复前：`Collected 31 run(s)`
- 修复后：`Collected latest 10 run(s) for 10 eval topic(s)`

---

## Bad Case 7：eval_runner.py `--help` 误触发实际评测

### 现象
执行 `python eval/eval_runner.py --help` 期望只看到帮助信息，实际却直接开始跑评测（真实调用 LLM、真实搜索），没有任何 `argparse` 保护。

### 原因
脚本没有使用 `argparse`，`run_eval_suite()` 在 `if __name__ == "__main__":` 里被无条件调用，任何命令行参数（包括 `--help`）都不会被解析和拦截。

### 修改
重写 `eval/eval_runner.py`，用 `argparse` 的互斥参数组实现 `--run`（真正执行）/ `--collect-only`（只汇总现有结果，不重新跑），`--help` 完全交给 `argparse` 内置处理，在解析阶段直接 `exit(0)`，不会碰到任何执行逻辑；不带任何 mode 参数直接运行会报错退出，不会误跑。

### 结果
`python eval/eval_runner.py --help` 只打印用法说明退出；`python eval/eval_runner.py`（不带参数）报错要求必须指定 `--run` 或 `--collect-only`。

### 指标变化
- 定性变化：`--help` 从"触发真实评测执行"变为"只显示帮助，exit code 0"；无参数运行从"静默执行"变为"报错退出，exit code 2"

---

## Bad Case 8：网页/PDF 抓取中的 403/404/empty_content 问题

### 现象
`BrowserAgent` 日志里反复出现同一批站点的抓取失败：`403 Client Error: Forbidden`（知乎、Wikipedia）、`empty_content_after_cleaning`（部分资讯站）。`investing.com`、`wikipedia.org`、`wallstreetcn.com`、`gurufocus.cn`、`lanjinger.com`、`zhihu.com`、`bdstatic.com` 等站点反复出现，每次都要等一次完整的网络超时才能确认失败。

### 原因
这些站点对爬虫有反爬机制（403/429）或返回结构不稳定导致正文提取为空，但当时的域名黑名单没有覆盖它们，每次都要真正发起请求、等到失败才知道该跳过。

### 修改
扩充 `DOMAIN_BLACKLIST`（[tools/domain_rules.py](../tools/domain_rules.py)），把这些反复失败的站点加入黑名单，`BrowserAgent` 的 pre-filter 在抓取前直接排除。

### 结果
这些域名不再出现在抓取尝试里，抓取预算留给真正可能成功的候选。

### 指标变化
- 黑名单命中丢弃数（`browser_metrics.blacklisted_domain_drop_count`）：个位数 -> 常见 7-18（视候选池而定）
- 定性变化：这几个域名的 403/404/empty_content 日志不再出现（pre-filter 阶段已排除）

---

## Bad Case 9：行业研报复用公司财务指标，导致 financial_depth_score 不公平

### 现象
`outputs/eval/eval_summary.csv` 中 AI机器人行业研报、低空经济行业研报的 `main_issue` 显示 `financial_depth_score=0`，导致 `overall_score` 被连带封顶，即使报告本身来源充分、结构完整。

### 原因
`evaluate_report_quality()` 目前对所有 `report_type` 都用同一套公司导向的财务关键词表（营收/净利润/毛利率等）来判断"财务分析深度"。但行业研报本来就应该谈市场规模、产业链、政策趋势，而不是单一公司的财务报表，用公司财务关键词去衡量行业报告的"深度"本身就不公平。

### 修改
**这是当前 v1 已知但尚未修复的限制**，记录在案，作为 v2 方向"report_type-specific Evaluation"的直接动因，暂不改动 Evaluation 主逻辑（避免仓促改动破坏已验证的评分体系）。

### 结果
industry_research 类 topic 的 `quality_score` 目前偏保守，需要结合 `main_issue=financial_depth_score=0` 一起解读，不能直接和 company_research 类 topic 的分数横向比较。

### 指标变化
- AI机器人行业研报、低空经济行业研报：`main_issue = financial_depth_score=0`（`outputs/eval/eval_summary.csv` 实测记录，2/10 个 topic）
- 定性变化：从"看不出这两个分数为什么偏低"变成"评估诊断里明确写出原因，可追溯"

---

## Bad Case 10：热缓存评测和冷缓存评测容易混淆

### 现象
`outputs/eval/eval_summary.csv` 里全部 10 个 topic 的 `research_time` 都在 0.02-0.03 秒左右，如果不加说明，容易被误读成"这个系统的搜索永远只要 0.03 秒"。

### 原因
`eval/topics.json` 是固定的 10 个 topic，反复评测会不断命中 `outputs/cache/search_cache.json` 里已经缓存过的搜索结果，所以 `avg_research_time` 实际上是热缓存下的数字，不是首次搜索一个新 topic 的真实耗时。

### 修改
不涉及代码修改，是文档/说明层面的修复：在 [README.md](../README.md) 的"评测结果"一节明确注明 `avg_research_time=0.029s` 是热缓存结果，并给出冷缓存下的真实历史耗时区间（10-16 秒）供参考。

### 结果
读者不会把"热缓存评测数字"误当成"系统首次响应速度"的承诺。

### 指标变化
- 冷缓存 Research 耗时：10-16s
- 热缓存 Research 耗时：~0.03s（`outputs/eval/eval_summary.csv` 中 10/10 topic 的实测值）

---

## Bad Case 11：MCP stdio session 首次调用即 "Connection closed"

### 现象
v2 模块1 把工具调用改走 MCP 后，网关日志显示 session 建立成功，但第一次 `call_tool` 就失败：`MCP call_tool(web_search) failed: Connection closed`，同时 asyncio 抛出 `RuntimeError: Attempted to exit cancel scope in a different task than it was entered in`，网关直接降级为直连调用——协议层形同虚设。

### 原因
MCP Python SDK 的 `stdio_client` / `ClientSession` 内部使用 anyio task group，其 cancel scope 绑定在**进入上下文管理器的那个 asyncio task** 上。最初的实现在一个短命协程里 `__aenter__` 拿到 session 就返回了——协程一结束，task group 被拆除，stdio 传输随之关闭，后续任何调用都是死连接。

### 修改
改为标准的 keeper-task 模式（[tools/tool_gateway.py](../tools/tool_gateway.py)）：一个常驻协程用 `async with` 持有 transport + session 两层上下文，把 session 引用交出去之后 `await asyncio.Event().wait()` 永久驻留；所有 `call_tool` 通过 `run_coroutine_threadsafe` 调度到同一个事件循环上。

### 结果
单次调用、read_webpage/read_pdf、以及 4 路并发共享 session 全部正常；完整 eval（10 topic）全程走 MCP，日志零降级。

### 指标变化
- MCP 调用成功率：首调即挂（0%）-> 全程无降级
- eval 期间 "falling back to direct calls" 日志条数：必现 -> 0

---

## Bad Case 12：embedding 模型 HuggingFace 与 hf-mirror 均下载失败

### 现象
v2 模块2 需要本地 embedding 模型（bge-small-zh-v1.5），`SentenceTransformer('BAAI/bge-small-zh-v1.5')` 直连 huggingface.co 失败；设置 `HF_ENDPOINT=https://hf-mirror.com` 后依然报 `FileMetadataError: Distant resource does not seem to be on huggingface.co`——新版 huggingface_hub 会校验响应来源，镜像站直接被拒。

### 原因
国内网络环境无法直连 HuggingFace；hf-mirror 又与当前版本 huggingface_hub 的元数据校验逻辑不兼容。

### 修改
改用 ModelScope（国内直连）下载同一模型：`modelscope.snapshot_download('BAAI/bge-small-zh-v1.5')`，[tools/semantic_scorer.py](../tools/semantic_scorer.py) 加载时先探测 ModelScope 本地缓存路径（含真实路径 `~/.cache/modelscope/models/BAAI--bge-small-zh-v1.5/snapshots/master`，与文档写的 hub 路径不同——也是实测踩出来的），缓存不存在再走 snapshot_download，全部失败则语义层自动禁用、排序退回纯规则。

### 结果
模型 30 秒内从 ModelScope 下载完成，加载正常（512 维），语义打分对相关/无关文本区分度符合预期（财报解读 0.855 vs 无关内容 0.592）。

### 指标变化
- 模型可用性：HF 直连/镜像 0% -> ModelScope 100%
- 失败模式：硬报错中断 -> 语义层可选降级（`semantic_score=None`，排序退回纯规则，主链路不受影响）

---

## Bad Case 13：语义排序把同站点内容批量推进抓取窗口，放大单站反爬风险

### 现象
模块2 接入语义排序后的 eval 中，贵州茅台财务与估值分析一轮 `browsed=20 -> usable=3`（17 个候选抓取失败），最终只选出 3 个来源（quality 0.8，触发 source_count<5 封顶）。对比模块1 同 topic 的运行：`browsed=10 -> usable=10`、5 来源。

### 原因
雪球的个股深度帖标题（"贵州茅台深度投研报告""估值及预期收益评估"）与查询语义高度匹配，语义层把 4 个雪球帖同时推进 top6（此前规则排序只有 2 个）；恰逢该站点对连续抓取批量返回 403，窗口里的高排名候选成片失效。语义分本身没错——错在排序层不知道"同一站点集中意味着共享同一个失效风险"。

### 修改
[agents/browser_agent.py](../agents/browser_agent.py) 在排序后加域名分散守卫：单域名最多占抓取窗口 4 席，超出的候选不丢弃、只后移。同时把 `rule_score`/`semantic_score` 写进 trace 的 `top_ranked_sources`，此类问题以后可以直接从 trace 定位是谁把候选推进来的。

### 结果
抓取窗口对单站点当天反爬保持韧性；该 case 仍成功生成报告（3 来源、0.8 分，评估封顶机制如实反映了来源不足）。

### 指标变化
- 单域名在抓取窗口的最大占比：无限制 -> 4/20
- trace 可观测性：top_ranked_sources 仅 rank_score -> rank/rule/semantic 三分数齐全
- 遗留方向：把"站点历史可抓取成功率"作为排序信号（v2 后续）

---

## Bad Case 14：通用句子切分器在英文句点处断句，带小数的金额全部抽不到

### 现象
v2 模块3/4 的财务参数抽取（DCF 输入、图表数据点）对"营收8039.65亿元"这类带小数的金额永远匹配失败，只有整数金额（"8040亿元"）能抽到；同一批来源里 DCF 的 base_fcf 落到默认值分支，图表数据点大量缺失。

### 原因
`utils/text_utils.py::split_sentences` 的切分正则 `(?<=[。！？.!?\n])` 包含英文句点——"8039.65" 会被切成 "8039." 和 "65 亿元" 两个"句子"，逐句匹配的正则再也拼不回完整金额。v1 的关键词计数场景对此不敏感（关键词不含数字），所以两年没暴露；一到"抽取数值"的场景就必然踩中。

### 修改
[tools/valuation_dcf.py](../tools/valuation_dcf.py) 和 [tools/chart_renderer.py](../tools/chart_renderer.py) 各自使用只按中文句读/换行切分的 `_split_zh_sentences`（`[。！？；\n]`），不动 `split_sentences` 本身——它的现有调用方（关键词计数、摘录抽取）行为保持不变。

### 结果
"净利润326.19亿元""经营性现金流净额1332亿元"等带小数金额正常抽取；比亚迪 DCF 的 base_fcf 从默认值分支变为真实抽取（净利润代理 326.19 亿），宁德时代抽到真实经营现金流 1332 亿。

### 指标变化
- 带小数金额抽取成功率：0% -> 正常
- DCF base_fcf 出处：default（凑不出参数）-> extracted/proxy（真实来源句子 + source_id）

---

## Bad Case 15：图表数据抽取的三类实体/口径误归属

### 现象
图表序列抽取在真实来源上陆续出现三类错误数据点：
1. `净利润(2022, 91)`——"2022年**前三季度**归母净利润91亿"（季度累计当年度值）；
2. `营收(2020, 14.4)`——"**比亚迪半导体**2020年营收14.4亿元"（子公司数字归到母公司，与 8000 亿级序列差 500 倍）；
3. `净利润(2023, 300)`——宁德时代报告里"作为对比，**比亚迪**在2023年的年度净利润是300亿元"（对比句里竞争对手的数字归到主体）；
4. `净利润(2023, 4009)`——"预计2024年营收3560亿元，比2023年的4009亿元…净利润490亿元"（多指标长句里营收值归到净利润）。

### 原因
正则抽取没有实体归属和口径概念：句子里出现"年份+指标词+金额"就认，分不清季度/年度、母公司/子公司/竞争对手、同句多指标的归属。

### 修改
[tools/chart_renderer.py](../tools/chart_renderer.py) 叠加四道守卫：口径守卫（含"季度/上半年/中报"等词的句子跳过）、实体守卫（句子提到 COMPANY_ALIASES 里其他公司时跳过）、多指标句守卫（同句同时出现营收和净利润时只信严格邻近匹配）、数量级离群守卫（序列内相差 20 倍以上的小值点丢弃）。宁缺毋滥：守卫会牺牲召回（部分 topic 数据点不足 2 个而不画图），换取画出来的图不误导。

### 结果
四类误报在实测来源集上全部清除；数据点充足时正常出图，不足时报告照常生成（无图）。

### 指标变化
- 已知误归属数据点：4 类实测案例 -> 0
- 代价：部分来源集从"能画图（含错点）"变为"诚实不画"
- 遗留：不在 COMPANY_ALIASES 里的竞争对手名仍无法识别（文档已注明）

---

## Bad Case 16：虚构公司主题也能凑出 5 个"相关"来源（groundedness 缺口）

### 现象
模块5 用虚构主题"希兹维恩量子玄武科技投资分析"做 replan 触发测试，预期 browse 因来源不足失败——实际却 `browsed 20 -> 18 usable -> 15 relevant -> top 5 kept`，为一家不存在的公司正常生成了报告，replan 根本没触发。

### 原因
QualityScorer 的相关性评分里，金融关键词密度加分（最高 +0.25）不要求主体词/别名命中；泛金融内容页（谈营收、估值、风险的任意文章）单靠财务词密度就能越过 `_MIN_TOPIC_RELEVANCE=0.05` 阈值。真实主体的报告不受影响（主体词/别名总会命中），但完全虚构/极冷门主体会被语义相近的无关内容"喂饱"。

### 修改
**记录为已知限制，本轮不改**：修法（金融词加分要求至少一个 relevance_term 命中）会动 QualityScorer 主逻辑，需要单独评估对现有 10 case 的影响，避免重蹈 Bad Case 3 的误杀覆辙。列入 v2 后续（与"数字级 grounding"同批）。replan 机制本身改用确定性单测验证（monkeypatch 空搜索结果）：browse 失败 -> t1_replan -> t2_replan -> 硬上限停止，全链路符合设计。

### 结果
限制已文档化；使用者应知晓：对不存在/极冷门的主体，系统可能基于泛金融内容生成看似有据的报告，来源引用真实存在但与主体无关。

### 指标变化
- 定性：groundedness 边界从"未知"变为"已知并文档化"
- replan 机制验证方式：合成主题实测（失败，因为上述原因）-> 确定性单测（通过：triggered=True, rounds=1, 步骤序列 t1/t2/t1_replan/t2_replan）

---

## Bad Case 17：LLM 配额耗尽导致 eval 无效——但意外完成了一次全链路断供韧性实测

### 现象
模块3 的 prompt 修复（DCF 数字须引用 base_fcf 来源）复验 eval 跑出了可疑数据：`avg_duration=27.5s`（正常 130-180s）、`avg_quality=0.676`。检查日志发现 30 处 `LLM call failed`——阿里云免费配额在会话中途耗尽（"The free quota has been exhausted"），planning/analyze/report 的所有 LLM 调用全部失败。

### 原因
一天内跑了 4 轮完整 eval suite + 多次单 topic 验证（每轮 suite 约 30-40 次 LLM 调用），免费额度耗尽。eval_runner 本身没有 LLM 健康预检，跑完才从耗时/质量分异常反推出问题。

### 修改
两层处理：
1. **判定该轮 eval 无效**，模块3/4/5 的回归结论以配额耗尽前的有效 eval 为准（10/10 成功、来源 5.0、质量 0.724 且降分根因已定位为 DCF 数字无引用触发评估器封顶）；prompt 修复的复验推迟到配额恢复后。（**后续**：配额恢复后已复验——10/10 成功、来源 5.0、avg_quality 0.724 -> **0.798**，修复前最低分 0.54 的 topic 回升到全员 ≥0.677；报告中 DCF 基期参数/增长率带 [source_id] 引用、折现率等标注"配置假设"，与设计一致。）
2. **把这轮"事故"当数据用**：全 LLM 断供下，pipeline 靠确定性 fallback（默认计划/规则抽取分析/模板报告）仍然 10/10 跑完、全部 5 来源、质量分 0.54-0.8——v1 设计的"单点失败不致命"兜底链路第一次被完整压测，全部生效。

### 结果
教训固化：看 eval 数字前先看 `avg_duration` 是否在正常区间、日志有没有成批 fallback——异常快的"成功"比失败更危险。后续方向：eval_runner 开跑前做一次 LLM 探活，失败直接拒跑并明确报错，而不是产出一份看似正常的降级评测。

### 指标变化
- 全 LLM 断供下的 pipeline 完成率：未知 -> 实测 10/10（fallback 链路全覆盖）
- 无效 eval 的识别信号：无 -> `avg_duration` 异常（27.5s vs 正常 130-180s）+ 日志 fallback 计数（30 处）

---

## Bad Case 18：AkShare 东方财富系接口在本环境全部 ProxyError

### 现象
v3 接入 AkShare 时，`stock_individual_info_em`、`stock_zh_a_hist` 等东方财富系接口全部抛 `ProxyError: HTTPSConnectionPool(host='push2.eastmoney.com')`，而新浪系（`stock_financial_abstract`、`stock_zh_a_daily`）、百度估值、全 A 股代码表正常。

### 原因
本机网络环境对 eastmoney push2 域名的连接被代理拦截；AkShare 的不同接口走完全不同的数据源域名，可用性必须逐一实测，不能按文档假设。

### 修改
接口选择实测驱动：先探测再封装（探测记录在 docs/akshare_integration.md），[tools/akshare_tool.py](../tools/akshare_tool.py) 只依赖实测可用的四个接口；每个接口独立 try/except，单接口失败只缺对应字段（进 missing_fields + errors），不影响其余字段汇总。

### 结果
贵州茅台真实快照全链路成功：6 年营收序列、净利 823.2 亿、ROE 32.53%、PE 18.21、市值 15063 亿；东财系接口零依赖。

### 指标变化
- 可用接口：按文档全信 -> 实测 4/6 可用，封装只用可用的
- 单接口失败的爆炸半径：整个快照失败 -> 单字段缺失（missing_fields 如实记录）

---

## Bad Case 19：相对估值没有可靠的免费行业可比数据源

### 现象
实现 PE/PB 相对估值时，找不到本环境可用的免费行业可比公司倍数接口（东财系被拦，其他源无行业聚合）。"行业平均 PE"这类参照系拿不到真实数据。

### 原因
免费公开数据源的行业倍数聚合要么不存在、要么依赖被拦截的接口。项目红线禁止编造行业均值。

### 修改
参照系改为**该股票自身近一年倍数分布**（百度估值日度序列，真实数据）：当前值处于自身历史 <=30% 分位报 undervalued、>=70% 报 overvalued，含义明确标注为"相对自身历史"而非"相对同行"（[tools/valuation_relative.py](../tools/valuation_relative.py)，assumptions 字段写明）。PS 无历史序列，诚实输出 unknown + missing_fields。

### 结果
茅台实测：PE 18.21 处于自身近一年 4.4% 分位 -> undervalued（相对自身历史），PS 标 unknown。没有任何编造的行业均值。

### 指标变化
- 定性：从"要么编行业均值要么不做" -> "换真实可得的参照系并明示语义边界"

---

## Bad Case 20：python str.replace 打补丁静默失败，实体验证的终止文案没生效

### 现象
虚构公司 E2E 实测中实体验证正确拦截（来源数量 0），但终止报告用的是通用"资料不足"文案而不是实体验证专用文案，evaluation 文件是空 `{}`——预期的 `entity_validation_failed` 标记没写入。

### 原因
用 python `str.replace` 给 orchestrator 打补丁时，替换目标字符串里写了 `\n`（两个字符），而文件里是真实换行符——**`str.replace` 找不到目标时静默返回原文，不报错**，补丁等于没打，且脚本还打印了"patched"误导排查。

### 修改
改用编辑器精确匹配替换重新修改 abort 路径；离线复测（monkeypatch 泛金融来源 + 虚构主体）确认：报告含 insufficient_entity_evidence 说明、evaluation 带 entity_validation_failed=True。

### 结果
教训固化：str.replace 式补丁必须验证替换确实发生（替换前后内容比对或计数断言），"脚本跑完没报错"不等于"补丁生效"。

### 指标变化
- entity 终止路径的 evaluation：空 {} -> {entity_validation_failed: true, overall_score: 0.0}

---

## Bad Case 21：数字 grounding 的离线报告做不了数值核对

### 现象
scripts/build_grounding_report.py 离线扫描历史报告时，AkShare/DCF 数字只能做"标注分类"（句子里有没有 AkShare/模型测算标记），做不了数值核对（数字是否真的等于 snapshot/DCF 输出）——因为 snapshot 和 DCF 完整输出没有存进 trace（体积原因只存了指标）。

### 原因
运行时评估（report_agent._evaluate）拿得到内存里的 snapshot/dcf 可以核对数值；离线脚本只有落盘产物。

### 修改
如实分层：运行时评估做数值核对（tools/number_grounding.py 的 value_verified 字段，1% 容差）；离线报告只做标注分类并**在报告里写明这一区别**，不假装做了数值核对。

### 结果
两个口径都真实：evaluation diagnostics 里有 value_verified 计数，grounding_report.md 里有说明段。

### 指标变化
- 定性：避免了"离线报告看起来像做了数值审计"的误导

---

## Bad Case 22：PDF 导出依赖 HTML 报告，markdown-only run 无 PDF 可导

### 现象
export_reports.py --latest 时最新 run 是 markdown 输出（eval 默认格式），PDF 导出没有 HTML 源可用。

### 原因
PDF 路径设计为"从 HTML 打印"（Edge headless），markdown 报告没有对应 HTML 文件。

### 修改
如实跳过并说明（"PDF skipped (no HTML report for this run)"），DOCX 照常从 markdown 生成；对有 HTML 的 run（比亚迪）验证了完整 DOCX+PDF 双导出。不做 markdown->HTML 的临时转换硬凑 PDF（会丢图表）。

### 结果
两条路径都验证：markdown run -> DOCX + PDF 诚实跳过；HTML run -> DOCX + 真实 PDF。

### 指标变化
- 定性：导出失败模式全部显式化（Edge 缺失/无 HTML/打印失败各有明确状态）

---

## Bad Case 23：虚构公司 E2E——纵深防御实录（相关性层被骗，实体层拦截）

### 现象
虚构主体"星河量子能源股份有限公司投资价值分析"实测：第一轮 research 0 候选 -> v2 的 replan 触发强制 fallback 重搜 -> browse 拿到 20 候选、16 usable、**15 被判 relevant**（bad_cases 16 的老缺口：泛金融内容骗过相关性层）-> **entity validation 拦截**（A股代码表未命中 + 零来源正文真实提到该主体）-> 来源数量 0，输出"实体无法验证"说明而非正常研报。

### 原因
相关性层的金融词密度加分不要求主体命中（16 号已记录未修）；v3 的策略是不动相关性层（避免误杀风险），在其后加实体验证层。

### 修改
[tools/entity_validator.py](../tools/entity_validator.py)：A股代码表（5530 只，AkShare 缓存）+ 内置别名表 + 来源正文真实提及三重证据；company_research 全部不命中 -> failed -> 终止并输出 insufficient_entity_evidence。启发式边界如实文档化：未上市真实公司会被判 weak/failed（提示用 --local-files 提供资料）。

### 结果
bad_cases 16 状态：未修复 -> **已修复（拦截层方案）**。10 个真实 eval topic 的验证全部 verified（公司走代码表/别名表，行业走行业词一致性），零误杀。

### 指标变化
- 虚构公司主题：生成看似正常的 5 来源研报 -> 0 来源 + 显式"实体无法验证"报告
- evaluation：quality 0.8（虚假） -> entity_validation_failed=true, overall 0.0

---

## Bad Case 24：memory 与语义层共享模型，进程冷启动慢

### 现象
memory 检索脚本每次以独立进程运行都要重新加载 bge 模型（本机 60-80 秒），连续调用三个脚本时第三个超时——不是功能失败，是模型加载成本没有跨进程复用。

### 原因
sentence-transformers 模型加载（权重读取+torch 初始化）在每个新 Python 进程里都要来一遍；脚本式使用天然放大这个成本。

### 修改
如实记录为已知限制；检索本身有关键词降级路径（模型不可用/加载失败时 bigram 重叠打分，单测覆盖），功能不受影响。批量场景应在同一进程内复用（LightVectorStore 单例已支持）。

### 结果
功能正确性与降级路径验证通过；冷启动成本作为使用注意事项写入 docs/memory_design.md 的边界。

### 指标变化
- 定性：限制从"未知" -> "已知并文档化"（进程内复用 vs 跨进程冷启动）
