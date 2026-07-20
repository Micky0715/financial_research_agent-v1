# Data Lineage Report（AkShare 字段级数据血缘）

本报告统计的不是笼统的 `provider=akshare`，而是逐字段的：原始字段名、取值、单位、报告期、原始发布/转载平台、接口说明（多数 AkShare 接口不回传精确原始 URL，如实标注 no_direct_source_url）、是否推导计算、是否缺失。

## 公司财务快照（FinancialDataSnapshot.field_lineage）

- **贵州茅台（600519）**：{'total_fields': 13, 'fields_with_source': 13, 'fields_no_direct_url': 13, 'derived_fields': 1, 'missing_fields': 2}
- **比亚迪（002594）**：{'total_fields': 13, 'fields_with_source': 13, 'fields_no_direct_url': 13, 'derived_fields': 1, 'missing_fields': 2}

## 宏观指标（MacroDataPoint，字段结构本身即完整血缘）

- 22 项宏观指标：{'total_fields': 22, 'fields_with_source': 22, 'fields_no_direct_url': 22, 'derived_fields': 0, 'missing_fields': 0}

## 汇总统计

- 字段总数: 48
- 有原始来源（provider+endpoint 已登记）字段数: 48
- 无直接来源 URL 字段数: 48（AkShare 转载性质决定，如实标注，非缺陷）
- 推导（derived）字段数: 2
- 缺失字段数: 4

## 典型 lineage 示例

- `revenue`: raw_field='营业总收入', value=1720.5417, unit=亿元, platform=新浪财经, endpoint=stock_financial_abstract（新浪财经个股财务摘要接口（历年年报核心指标矩阵））, no_direct_source_url=True, derived_from=[], missing=False
- `debt_ratio`: raw_field='资产负债率', value=16.4154, unit=%, platform=新浪财经, endpoint=stock_financial_abstract（新浪财经个股财务摘要接口（历年年报核心指标矩阵））, no_direct_source_url=True, derived_from=[], missing=False
- `ps`: raw_field='', value=9.65, unit=倍, platform=百度股市通/新浪财经（派生）, endpoint=市销率 = 总市值 / 营业总收入（本项目内推导，非 provider 直接字段）, no_direct_source_url=True, derived_from=['market_cap', 'revenue'], missing=False
- `revenue`: raw_field='营业总收入', value=8039.6496, unit=亿元, platform=新浪财经, endpoint=stock_financial_abstract（新浪财经个股财务摘要接口（历年年报核心指标矩阵））, no_direct_source_url=True, derived_from=[], missing=False
- `debt_ratio`: raw_field='资产负债率', value=70.7445, unit=%, platform=新浪财经, endpoint=stock_financial_abstract（新浪财经个股财务摘要接口（历年年报核心指标矩阵））, no_direct_source_url=True, derived_from=[], missing=False
- `ps`: raw_field='', value=1.07, unit=倍, platform=百度股市通/新浪财经（派生）, endpoint=市销率 = 总市值 / 营业总收入（本项目内推导，非 provider 直接字段）, no_direct_source_url=True, derived_from=['market_cap', 'revenue'], missing=False
- `gdp_yoy`: raw_field='GDP同比增速', value=4.7, unit=%, platform=东方财富数据中心(转载), endpoint=macro_china_gdp（国家统计局 发布，经 AkShare 转载）, no_direct_source_url=True, derived_from=[], missing=False
- `usd_cny`: raw_field='人民币兑美元中间价', value=6.7948, unit=元/美元, platform=SAFE官网, endpoint=currency_boc_safe（国家外汇管理局 发布，经 AkShare 转载）, no_direct_source_url=True, derived_from=[], missing=False
