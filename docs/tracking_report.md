# 季度/年度跟踪报告（v4 阶段D）

## 用法

```bash
python scripts/build_tracking_report.py --symbol 002594 --period annual
python scripts/build_tracking_report.py --symbol 600519 --period quarterly
```

## 数据与口径

[tools/financial_statement_extractor.py](../tools/financial_statement_extractor.py) 抽取近 5 期
年报（`annual`）或近 12 期季报（`quarterly`，多留基期给同比/单季推导用）。

- **同比**：找 `period_raw` 年份-1、月日相同的期（中国季报是累计口径，同期可比，不用"Q1 累计
  除以4"这类错误估算）。
- **环比（季度模式）**：先推导单季值（`cum(Q)-cum(Q-1)`，显式标 `derived`），再算单季环比——
  不直接对累计值做环比（那样算出来的数字没有意义）。
- **水平值对比（如经营现金流）用"上年同期"而不是"上一期"做基期**——曾在实现中发现一个
  真实 bug：季度模式下如果拿 Q1 累计值和上一条记录（可能是全年报表）比较，会得出"暴跌
  90%"这种虚假结论；已修复为强制用同口径基期，`tests/test_tracking_and_compliance.py` 有
  专门回归测试防止复发。

## 历史对比

[tracking/periodic_data_store.py](../tracking/periodic_data_store.py) 每次成功的公司研报 run
（`orchestrator/workflow.py`）会 best-effort 追加一条 `TrackingRecord`（含快照/风险/估值假设/
report_path）到 `outputs/tracking/records.jsonl`（JSONL，追加写，失败不影响主流程）。
跟踪报告构建时读取该 symbol 的历史记录，对比估值假设变化与上次报告提示的风险是否兑现；
**本系统首次跟踪某个主体时如实说"无历史报告记录"，不伪造同比/环比**。

## 输出结构

跟踪报告固定包含：本期与上期变化（营收增速/盈利背离/毛利率/现金流/负债率五个维度）、
近 5(年)/6(季度) 期趋势表、历史报告对比（估值假设变化+风险跟踪）、数据不足维度列表
（`insufficient_dimensions`，历史期不够时如实列出，不强行凑同比）、免责声明。

## 验收证据

真实运行：比亚迪年度跟踪（`insufficient=1`，5 项真实变化）、贵州茅台季度跟踪（本期2026Q1
vs 上期2025年报，经营现金流对比自动切换为 2026Q1 vs 2025Q1 同期，+205.5% 真实值）；
30-case 评测集里贵州茅台年度跟踪 + 万华化学季度跟踪两个真实触发 case（详见
`outputs/eval/competition_report.md` 的 `tracking_report_success_rate`）。

## 限制

- 工具专注上市公司三表（新浪口径）；行业/宏观没有对应的"跨期结构化跟踪引擎"，
  30-case 评测集里行业/宏观的"跟踪题"是周期性措辞的常规报告主题（如"光伏行业2026年
  二季度跟踪"），经标准 industry_research/macro_research 管线验证，不是独立跟踪工具；
- 管理层表述变化的对比依赖历史报告文本，只有在有历史记录时才可用；
- 季报字段完整性弱于年报（新浪季报口径部分科目缺失更常见）。
