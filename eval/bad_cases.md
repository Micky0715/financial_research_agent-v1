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
