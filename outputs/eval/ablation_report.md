# Ablation Report（真实历史前后对比整理）

**诚实声明**：本报告不是一次全新的受控 ablation 实验，而是把迭代过程中真实发生、
有据可查的修复前后对比整理成表；每条的证据来源列在最后一列。未实际测量过的维度
在文末明确列为 insufficient data，不编造。

| 改动 | 之前（实测） | 之后（实测） | 证据来源 |
|---|---|---|---|
| Research 串行搜索 -> 并发+缓存 | 冷缓存 32-155s / 无缓存 | 冷缓存 10-16s，热缓存 ~0.03s | bad_cases 1；eval_summary research_time 列 |
| Planner 原样执行 LLM 计划 -> normalize_plan | raw_task_count 9/17（重复执行阶段） | normalized 恒为 5，10/10 | bad_cases 2；planner_metrics |
| QualityScorer 整串匹配 -> 别名/行业词 | 5 个 topic source_count=0（相关性误杀） | 全部恢复 5 来源（中芯国际 0->5，quality 0.973） | bad_cases 3 |
| source_count 无封顶 -> 硬性 cap | source_count=1 时 quality 0.832（虚高） | 同条件封顶 0.5 | bad_cases 5 |
| 纯规则排序 -> +语义融合（w=0.35） | 相关词命中率 0.905 | 0.910（并召回 rule=0.0 的语义相关候选） | outputs/eval/semantic_ranking_compare.md；bad_cases 13（副作用与域名分散守卫） |
| DCF 数字无引用 -> 引用 provenance 来源 | eval avg_quality 0.724（unsourced 数字封顶） | 0.798（最低分 topic 0.54 -> 0.677） | v2 commit 36996ce；bad_cases 17 |
| Browser 无上限抓取 -> 读取上限+提前停止 | browse 阶段 78s（抓 60+ 候选） | 17-31s（读 10-20 个即停） | v2 优化记录；browser_metrics |
| 统一评估 -> report_type 分支 | 行业研报被 financial_depth=0 封顶（如 0.54-0.665） | 行业维度独立评分，不再受公司财务关键词封顶 | bad_cases 9（修复于 v3 stage E）；本次 eval 分组表 |

## 未测量（insufficient data）

- 语义排序对最终报告质量分的独立贡献（未做关闭语义层的受控重跑）
- AkShare 结构化数据对报告质量分的独立贡献（v3 全量 eval 是首次带结构化数据的运行，无同期对照）
- entity validation 的误杀率（需要一批真实的冷门/未上市公司主题标注集）

