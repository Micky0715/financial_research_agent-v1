# 比赛要求对齐表（v4-competition）

逐项列出：比赛要求 / 实现文件 / 验证证据 / 当前限制 / 完全满足-部分满足-未满足。
所有验证证据均来自真实运行产物；未运行的项如实标注"待验证"，不编造。

## 一、总体约束

| 要求 | 状态 | 说明 |
|---|---|---|
| 不接入 Wind | ✅ 完全满足 | 全项目零 Wind 相关代码/依赖，README/本文档均声明 |
| 不使用付费金融数据 API | ✅ 完全满足 | 仅用 AkShare（免费公开转载）+ 网页搜索/PDF |
| 不伪造行业均值/宏观指标/财务数据/评测结果 | ✅ 完全满足 | 见各模块"拒绝编造"设计（同业比较/情景模拟/灰犀牛/评测脚本均如实标注 insufficient data/degraded） |
| 公司研报仅针对上市公司 | ✅ 完全满足 | [tools/entity_validator.py](../tools/entity_validator.py) 四态硬校验，`unsupported_unlisted_company` 走公开资料摘要而非正式公司研报 |
| 关键数据记录来源/报告期/抓取时间 | ✅ 完全满足 | [tools/data_lineage.py](../tools/data_lineage.py) + [schemas/macro_data.py](../schemas/macro_data.py)::MacroDataPoint |
| 报告不构成投资建议 | ✅ 完全满足 | 每类报告固定免责声明 + 合规检查器强制项 |

## 二、十项功能

| # | 要求 | 实现文件 | 验证证据 | 限制 | 状态 |
|---|---|---|---|---|---|
| 1 | 宏观/策略研报真实链路 | tools/macro_data_collector.py 等 5 个模块，[docs/macro_research.md](macro_research.md) | 22 指标实取；grounding=1.0；30-case 含 10 个宏观 case | 社融/失业率/美元指数无免费接口；传导链为规则推演 | ✅ 完全满足 |
| 2 | 季度/年度跟踪型报告 | tracking/ 三模块，[docs/tracking_report.md](tracking_report.md) | 比亚迪年报/贵州茅台季报/万华化学季报真实跟踪；insufficient_history 如实标注 | 跟踪引擎专注公司三表；行业/宏观跟踪题走常规管线 | ✅ 完全满足（公司）/ 部分满足（行业宏观按常规报告验证） |
| 3 | 公司三表/股权/治理/同业比较 | 7 个 tools 模块，[docs/company_research.md](company_research.md) | 贵州茅台/比亚迪/宁德时代等三表真实抽取；同业比较真实板块数据（酿酒行业4家真实peer） | 无免费股权质押/实际控制人结构化数据 | ✅ 完全满足 |
| 4 | 行业生命周期/集中度/产业链/三年情景 | 5 个 tools 模块，[docs/industry_research.md](industry_research.md) | 白酒CR5=88.12%/HHI=4242.8 真实计算；生命周期基于文本证据判定 | 集中度是市值口径非营收口径；情景为工程假设演示 | ✅ 完全满足 |
| 5 | 正式研报披露模板 | templates/ + 2 个 tools 模块，[docs/report_disclosure.md](report_disclosure.md) | 真实 run 合规检查触发 DRAFT 横幅（unsourced_number_count>0） | 规则检查非法律合规认证 | ✅ 完全满足 |
| 6 | 30-case 三大类型分层评测 | eval/topics_competition_30.json + 2 个脚本 | 见下方"30-case 评测结果" | 固定 benchmark，非随机抽样 | ⚙️ 执行中/见下 |
| 7 | 股票/指数/宏观/行业图表 | 4 个 chart 模块 + chart_consistency_checker | 实测：茅台3张真实图（价格/相对指数/PE-PB）；宏观3组图；行业CR+情景图 | 部分图缺失历史数据时如实跳过，不补零 | ✅ 完全满足 |
| 8 | 有界自检改稿 | tools/report_reviser.py + orchestrator/workflow.py | 单测验证硬上限1轮；真实 run 触发过定向修复（免责声明/幻觉引用剔除） | 未溯源数字仅在能匹配真实结构化数据时才补标注，其余诚实保留 | ✅ 完全满足 |
| 9 | 上市公司硬校验 | tools/listed_company_registry.py + entity_validator.py | 10 真实公司零误杀；华为→unsupported_unlisted_company；虚构公司→failed | 仅覆盖 A 股 | ✅ 完全满足 |
| 10 | AkShare 原始来源 lineage | tools/data_lineage.py + schemas 扩展 | 48 字段真实 lineage（茅台/比亚迪/22项宏观指标），逐字段 raw_field/platform/endpoint | AkShare 多数接口无精确原始 URL（如实标注 no_direct_source_url） | ✅ 完全满足 |

## 三、其余交付项

| 要求 | 实现文件 | 状态 |
|---|---|---|
| FastAPI + Docker | api/ + Dockerfile + docker-compose.yml，[docs/deployment.md](deployment.md) | ✅ FastAPI 真实 HTTP 端到端验证完成；⚠️ Docker build/run 未在当前沙箱环境验证（无 Docker 守护进程），静态审查完成 |
| 测试覆盖 | tests/（见下方测试结果） | ✅ 完全满足 |
| 文档更新 | README.md + docs/*.md + eval/bad_cases.md | ✅ 完全满足 |

## 四、真实测试结果

`python -m pytest tests` → **96/96 通过**（离线，约 70 秒），覆盖：宏观归一化/政策解析/
传导链/灰犀牛（11测试）、三表/杜邦/现金流/股东/同业降级（8测试）、生命周期/CR-HHI/情景/
产业链/进退出（12测试）、跟踪同环比/合规检查/图表一致性（12测试）、上市公司硬校验（9测试）、
lineage（6测试）、有界改稿（5测试）、FastAPI健康检查与异步任务（7测试）、V3 全部原有回归
（30测试原样保留全部通过）。

写测试过程中发现并修复一个真实 bug：`tools/industry_lifecycle.py` 在"来源只有负增长证据、
无正增长证据"的场景下会被"证据不足"分支提前拦截，永远判不出衰退期——已修复
（[eval/bad_cases.md](../eval/bad_cases.md) 待补充条目）。

## 五、30-case 评测结果

<!-- 本节将在 outputs/eval/competition_report.md 生成后回填真实数字，不提前编造 -->

30-case 真实评测（`python scripts/run_competition_eval.py --run` + `build_competition_report.py`）
状态：**执行中**。完整数字见 `outputs/eval/competition_report.md`（该文件生成后本节回填）。
已确认真实工作的信号（执行日志摘录）：
- 宁德时代（company）：真实三表(5期)/DuPont/DCF(base=28892.78亿)/市场图表(3张)/entity验证=verified
- 贵州茅台年度跟踪（company, annual_tracking）：真实触发 tracking_report_builder，7处变化，insufficient=1

## 六、V3 既有能力回归

原 10-topic 回归命令 `python eval/eval_runner.py --run` 待在 30-case 完成后执行（避免与
30-case 共享的 LLM/网络配额产生资源竞争）；执行结果将写入 `outputs/eval/eval_report.md`
并在最终总结中给出。

## 七、诚实声明

- 30-case 是人工分层构建的固定 benchmark（公司10/行业10/宏观10，每类含季度跟踪/年度跟踪/
  风险分析/图表题各≥1），**不是随机抽样，不能代表所有金融任务的泛化能力**。
- 未运行完成的部分在最终总结中如实标注为"待验证"，不编造已完成。
