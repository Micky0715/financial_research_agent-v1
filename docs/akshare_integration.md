# AkShare 结构化金融数据接入说明（v3 阶段A/B）

## 使用了哪些接口（全部经过本环境实测）

| AkShare 接口 | 数据源 | 提供字段 | 实测状态 |
|---|---|---|---|
| `stock_financial_abstract(symbol)` | 新浪 | 营业总收入、归母净利润、经营现金流量净额、ROE、毛利率、资产负债率（年度序列，最多 6 年） | ✅ 可用 |
| `stock_zh_valuation_baidu(symbol, indicator, period)` | 百度股市通 | 市盈率(TTM)、市净率、总市值（近一年日度序列） | ✅ 可用 |
| `stock_zh_a_daily(symbol)` | 新浪 | 日线行情（最新收盘价） | ✅ 可用 |
| `stock_info_a_code_name()` | - | 全 A 股代码-名称表（约 5500 只，7 天缓存） | ✅ 可用 |
| `stock_individual_info_em` / `stock_zh_a_hist` 等东方财富系 | 东方财富 | - | ❌ 本环境 ProxyError，**刻意不依赖** |

## 可用字段与缺失处理

`FinancialDataSnapshot`（[schemas/financial_data.py](../schemas/financial_data.py)）的可用字段：
revenue / net_profit / gross_margin / roe / operating_cash_flow / debt_ratio /
pe / pb / market_cap / price + 年度序列（revenue/net_profit/roe/gross_margin）+
pe/pb 近一年历史序列。

- **ps 是派生值**（总市值/营收），metadata 里标注 `ps_derived=true`；
- total_assets / total_liabilities 在新浪摘要中无对应行，**如实进 missing_fields**；
- 任何接口失败对应字段留空 + 记入 missing_fields + errors，**绝不编造**。

## 失败时如何降级

四层降级，任何一层失败都不影响主流程：

1. **akshare 未安装** → `fetch_financial_snapshot` 返回 `{success: false, error: "akshare not installed"}`，Analyze 阶段继续纯网页/PDF 分析；
2. **单个接口失败**（网络/字段变化）→ 该接口字段缺失并记录到 errors，其余接口正常汇总，`degraded=true`；
3. **代码无法识别**（虚构公司/非 A 股）→ `{success: false, error: "cannot resolve symbol"}`（这也是 entity_validator 的信号源之一）；
4. **快照获取整体失败** → DCF 退回 v2 的文本抽取路径，图表退回文本抽取路径，prompt 注入"未获取到结构化数据"的明确说明。

调用状态（success/degraded/symbol/errors/missing_fields）写入 trace 的
`structured_data_metrics` 字段，可事后复查。快照按 symbol 缓存 12 小时
（`outputs/cache/akshare/`），代码表缓存 7 天。

## 为什么没有接 Wind

Wind 是商业数据终端，需要付费授权和专用客户端环境，不符合本项目"公开免费数据
源 + 可复现"的定位。本项目**没有实现 Wind、没有 Wind 相关代码**。

## 数据使用声明

AkShare 数据来自第三方公开接口（新浪/百度），依赖网络和第三方接口稳定性，可能
存在延迟、口径差异或字段变更。所有数据仅供技术研究参考，**不构成投资建议**。
