# 行业深度研究链路（v4 阶段C）

## 生命周期判断（有证据、非 LLM 拍板）

[tools/industry_lifecycle.py](../tools/industry_lifecycle.py) 从已检索来源正文抽取增速/负增长/
渗透率的数字证据（`source_id` 可溯源）+ 政策/产能过剩线索词命中的来源数，规则判定
导入期(增速≥15%且渗透率<20%)/成长期(增速≥15%)/成长期后段(5-15%)/成熟期(<5%)/衰退期
(≥2处负增长且无正向证据支撑)。**没有任何数字证据时输出 `unknown`，不做无依据判断**——
这一点专门写了单测（`tests/test_industry_deep.py`）防回归。

## 集中度：CR3/CR5/CR10/HHI（真实市值口径）

[tools/industry_concentration.py](../tools/industry_concentration.py) 用新浪行业/概念板块
真实成分股的**总市值份额**计算集中度（`sample_note` 明确标注这是上市公司市值口径，
不是全行业营收口径——免费公开渠道没有后者的结构化数据）。匹配不到板块或有效成分不足
5 家时 `degraded=True`，不编造。来源正文中的"CR/市占率"表述作为独立证据摘出，不依赖
结构化路径。

## 产业链（模板 + 来源验证）

[tools/industry_chain_builder.py](../tools/industry_chain_builder.py) 内置 10 个重点行业的
上游/中游/下游/终端环节模板（定性行业常识，不含任何数字）；每个环节和候选公司**只有在
本次检索来源正文中真实出现才保留**，模板给的公司名如果来源没提到会被剔除
（`verified_in_sources` 字段可查）。无模板行业退化为通用四段链。

## 三年情景模拟（输入来源逐项标注）

[tools/industry_scenario_model.py](../tools/industry_scenario_model.py) 的每个输入变量标注
`value_kind`：`historical_source`（来源抽取的当前规模）/`external_forecast`（来源抽取的机构
预测增速）/`config_assumption`（bear/bull 相对 base 的调节系数，内联可审查）/`calculated_value`
（逐年复利计算结果）。抽不到真实市场规模时输出**指数形式**（基期=100，`is_indexed=True`），
不虚构"XX亿元"的规模数字。

## 进入/退出策略参考

[tools/industry_entry_exit_analyzer.py](../tools/industry_entry_exit_analyzer.py) 综合生命周期/
增速/集中度/政策/产能过剩信号做规则打分，输出"积极关注/选择性参与/谨慎观望/规避/unknown"——
是策略研究参考，`limitation` 字段明确写"不构成投资建议"。

## 验收证据

- 30-case 评测集含 10 个行业主题；`outputs/eval/competition_report.md` 的
  `industry_scenario_completion_rate`（三年及以上情景占比）字段给出真实统计。
- 白酒行业离线测试：CR5=88.12%、HHI=4242.8（33 家真实成分股，`docs/v2_capability_matrix.md`
  v3 表已有历史记录，v4 版本改为真实 CR/HHI 而非仅"评估器打分"）。

## 限制

- 板块归类为新浪口径，非申万/证监会标准行业分类；
- 产业链模板是定性常识，环节间的量化价格传导系数不可得（`price_transmission` 是文字描述）；
- 情景模拟的 bear/bull 调节系数是工程假设，不是概率分布采样，不构成预测保证。
