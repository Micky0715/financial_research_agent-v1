# 公司深度研究链路（v4 阶段B + 阶段H）

## 三表抽取

[tools/financial_statement_extractor.py](../tools/financial_statement_extractor.py) 用
`ak.stock_financial_report_sina` 抽取利润表/资产负债表/现金流量表（年报或季报，最新在前）。
每个科目保留：`field`（标准化）、`raw_field`（原始新浪指标名）、`value`、`unit`、`derived`、
`derived_from`、`missing`。推导科目（毛利=营收-营业成本、自由现金流=经营现金流-资本开支）
显式标 `derived=True`，绝不冒充原始科目。

## 比率与杜邦

- [tools/financial_ratio_analyzer.py](../tools/financial_ratio_analyzer.py)：毛利率/净利率/
  资产负债率/流动比率/速动比率/资本开支强度，公式内联可审查。
- [tools/dupont_analyzer.py](../tools/dupont_analyzer.py)：`ROE = 净利率 × 总资产周转率 × 权益乘数`
  （归母口径），同时给出 `reported_roe`（新浪财务摘要口径）与两者差异，差异来自
  "期末总资产 vs 平均总资产"口径不同，在 `formula_note` 里明示，不掩盖。

## 现金流质量

[tools/cashflow_quality_analyzer.py](../tools/cashflow_quality_analyzer.py)：经营现金流/净利润、
自由现金流/净利润、现金收入比、资本开支强度、应收/存货增速对照营收增速。`quality_flags`
是规则提示（阈值写死可审查），不是审计结论。

## 股权与治理

- [tools/shareholder_structure.py](../tools/shareholder_structure.py)：`ak.stock_main_stock_holder`
  真实抓取前十大股东、持股比例、股东总数及环比变化。免费接口没有的字段（实际控制人、
  股权质押、机构持股明细）如实进 `limitations`，不猜不编。
- [tools/corporate_governance_analyzer.py](../tools/corporate_governance_analyzer.py)：
  股权集中度的规则解读（结构化数据）+ 董监高/股权激励/关联交易/治理风险的来源正文
  句级证据抽取（带 `source_id`，没有证据就说没有证据）。

## 同业比较（真实数据，拒绝编造行业均值）

[tools/peer_comparison.py](../tools/peer_comparison.py)：同业集合来自新浪行业/概念板块的
真实成分股（`stock_sector_spot` + `stock_sector_detail`），按市值接近度选取最多 4 家 peer；
PE/PB/市值取自板块行情快照，营收/净利增速/ROE/现金流质量取自各 peer 的新浪财务摘要。
**拿不到板块数据或有效样本 <2 家时返回 `degraded=True` + 具体原因，绝不用行业平均数填充。**

## 上市公司硬校验（v4 阶段H）

[tools/listed_company_registry.py](../tools/listed_company_registry.py) + 
[tools/entity_validator.py](../tools/entity_validator.py) 的公司主题验证是四态：

| 状态 | 含义 | 后续动作 |
|---|---|---|
| `verified` | A股代码表命中（含项目别名表） | 生成完整公司研报 |
| `unsupported_unlisted_company` | 来源正文证实真实存在，但不在 A 股注册表 | 生成"公开资料摘要"，标题与正文顶部明确标注非上市公司研报 |
| `weak` | 证据不足（1-2 个来源） | 继续执行，evaluation 中留痕 |
| `failed` | 无代码表命中且无来源证据 | 终止，`insufficient_entity_evidence` |

实测：10 个真实上市公司（贵州茅台/比亚迪/宁德时代/中芯国际/隆基绿能/五粮液/招商银行/
海康威视/恒瑞医药/中国平安）零误杀；华为技术有限公司（真实但未上市）正确识别为
`unsupported_unlisted_company`；虚构公司"星河量子能源股份有限公司"正确拦截为 `failed`。

## 限制

- 注册表仅覆盖沪/深/北交所 A 股，不含港股/美股/新三板；
- 同业集合是新浪板块口径，非申万/证监会行业分类，也非全行业口径；
- 治理风险抽取依赖检索到的来源文本，检索不到相关表述不代表不存在风险。
