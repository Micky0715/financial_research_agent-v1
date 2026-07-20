# 宏观研究链路（v4 阶段A）

## 数据来源与覆盖范围

22 项宏观指标经 AkShare 免费公开接口实取（[tools/macro_data_collector.py](../tools/macro_data_collector.py)），
原始发布机构包括国家统计局、中国人民银行、海关总署、国家外汇管理局、全国银行间同业拆借中心、
美联储、美国劳工统计局等；AkShare 只是转载/聚合层，`original_platform` 字段如实标注实际转载平台
（东方财富数据中心/金十数据/SAFE官网等），且 `no_direct_source_url=True`——AkShare 不回传精确原始
URL，不冒充有。

已知不可用（实测记录在 `KNOWN_UNAVAILABLE`，不是漏做）：
- 社会融资规模（`macro_china_shrzgm`，数据源 mofcom SSL 失败）
- 城镇调查失业率（`macro_china_urban_unemployment`，接口返回非 JSON）
- 美元指数（无实测通过的稳定免费接口）

## 链路组成

1. **指标归一化**（[tools/macro_indicator_normalizer.py](../tools/macro_indicator_normalizer.py)）：
   月度/季度/金十风格/日期-数值四类表格解析器，统一输出 `{value, period, previous_value, yoy, mom}`。
2. **政策文本解析**（[tools/policy_document_parser.py](../tools/policy_document_parser.py)）：
   LLM 结构化抽取（发布机构/日期/目标/工具/受益与受限行业/影响，`evidence_quotes` 与原文逐句比对）；
   LLM 不可用时降级为规则抽取（机构名/日期正则 + 句级线索词分类，不是关键词计数）。
3. **宏观传导链**（[tools/macro_transmission_model.py](../tools/macro_transmission_model.py)）：
   6 条规则图（利率/汇率/通胀/流动性/外需/地产），每条链**只在对应指标真实变动时激活**，
   `evidence_indicators` 指回具体指标 key，可解释、可审查，不是训练出来的因果模型。
4. **灰犀牛风险监控**（[tools/grey_rhino_monitor.py](../tools/grey_rhino_monitor.py)）：
   8 类风险（高杠杆/地产下行/外需下降/通胀反弹/汇率波动/流动性收紧/地缘冲突/产业链集中），
   等级由真实指标 + 工程阈值判定；地缘冲突与产业链集中风险没有免费结构化数据源，
   **永远输出 unknown**，不允许被规则臆造出等级（`tests/test_macro_pipeline.py` 有专门测试保证这一点）。
5. **报告组装**（[tools/macro_report_builder.py](../tools/macro_report_builder.py)）：
   关键指标表/政策解读/传导路径/灰犀牛表/数据来源为确定性渲染（杜绝数字幻觉）；
   核心结论/国内外对比/资产影响由 LLM 撰写（`prompts/macro_report_prompt.txt`），失败时规则文本兜底。

## 验收证据

- 实测 22 指标真实抓取：GDP同比4.7%、CPI同比1.0%、PMI 50.3、LPR 3.0%、中债10Y 1.74%、
  人民币中间价 6.79 等（`outputs/eval/data_lineage_report.md` 有逐字段留痕）。
- 组装后的完整宏观报告 number_grounding_rate = **1.0**（离线验证，30 个数字全部带
  AkShare+机构标注）。
- 30-case 评测集含 10 个宏观主题（GDP/CPI-PPI/利率/汇率/社融信贷/出口/房地产/制造业景气/
  全球流动性/灰犀牛），详见 [eval/topics_competition_30.json](../eval/topics_competition_30.json)
  与 `outputs/eval/competition_report.md`。

## 限制

- 传导链是规则推演，不是计量经济模型，不保证预测准确；
- 政策解析依赖检索到的政策类来源，检索不到政策文件时该章节退化为来源综述；
- 指标存在发布滞后（如工业增加值/出口数据可能滞后 1-2 个月），报告已在数据来源章节注明。
