# 正式研报披露模板与合规检查（v4 阶段E）

## 模板

- [templates/formal_research_report.html](../templates/formal_research_report.html)
- [templates/formal_research_report.docx](../templates/formal_research_report.docx)

20 项披露要素：报告标题、研究对象与证券代码、报告日期、报告类型、核心观点、主要财务
指标、研究逻辑、行业或宏观背景、财务分析、估值方法与假设、情景和敏感性分析、风险提示、
图表和表格、数据来源、方法限制、投资评级字段（本系统**不提供投资评级**）、分析师和
执业编号占位说明（**明确无持牌分析师参与**）、利益冲突声明、免责声明、报告传播与使用
限制。**不冒充持牌证券研究报告**——每份正式报告都显式声明"由自动化研究系统生成"。

## 构建

```bash
python scripts/build_formal_report.py --latest
python scripts/build_formal_report.py --run-id <id>
```

[tools/disclosure_builder.py](../tools/disclosure_builder.py) 把一次 pipeline run 的产物
（正文、来源、评估）包装成带完整披露章节的 Markdown；固定披露章节（投资评级/分析师声明/
利益冲突/免责声明/传播限制）是模板文本，正文部分复用真实报告内容。

## 合规检查器

[tools/report_compliance_checker.py](../tools/report_compliance_checker.py) 检查 8 项：
风险提示章节、数据来源、估值假设标注（有估值内容时）、免责声明、研究对象与证券代码
（行业/宏观报告不适用代码项）、无来源关键数字、假设/预测被写成确定性事实的句式
（"目标价将达/必然/保证"类正则）、报告日期。

**未通过合规检查时仍会生成文件，但文件名加 `DRAFT_` 前缀，正文顶部插入
DRAFT/INCOMPLETE 横幅**（列出具体未通过项）——不产出看似正式却带缺陷的最终报告。
这是工程规则检查，不构成法律/监管合规认证。

## 验收证据

对一次真实 v3 历史 run（比亚迪财务分析）跑合规检查：`unsourced_number_count=11` 触发
`DRAFT_formal_<run_id>.md`，其余 7 项检查通过，横幅正确列出未通过项——见
`outputs/formal_reports/`（gitignore，不提交产物本身，只提交代码与本文档）。

## 限制

- 合规检查覆盖比赛要求的 8 个检查点，不是律师/合规官审查的替代品；
- "假设伪装成事实"检测是正则匹配已知高危句式，无法覆盖所有可能的隐蔽表述方式。
