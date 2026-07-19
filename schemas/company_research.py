"""Schemas for company deep research: three statements / DuPont / cashflow
quality / shareholder & governance / peer comparison (v4 stage B).

所有字段保留：原始字段名(raw_field)、标准化字段名、单位、报告期、来源、
缺失标注、是否推导(derived)。缺失永远显式，不填充猜测值。
"""
from typing import Any, Optional

from pydantic import BaseModel, Field


class StatementItem(BaseModel):
    """One normalized statement line item."""

    field: str                              # 标准化字段名，如 revenue
    raw_field: str = ""                     # 原始字段名，如 营业总收入
    value: Optional[float] = None
    unit: str = "亿元"
    derived: bool = False                   # True=由其他科目推导
    derived_from: list[str] = Field(default_factory=list)
    missing: bool = False


class StatementPeriod(BaseModel):
    """One reporting period across the three statements."""

    period: str                             # 20241231 -> "2024年报"; 20260331 -> "2026Q1"
    period_raw: str = ""                    # 原始报告日 YYYYMMDD
    income: dict[str, StatementItem] = Field(default_factory=dict)
    balance: dict[str, StatementItem] = Field(default_factory=dict)
    cashflow: dict[str, StatementItem] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)


class ThreeStatements(BaseModel):
    symbol: str
    company_name: str = ""
    periods: list[StatementPeriod] = Field(default_factory=list)  # 新->旧
    frequency: str = "annual"               # annual / quarterly
    provider: str = "akshare"
    endpoint: str = "stock_financial_report_sina"
    original_platform: str = "新浪财经"
    fetched_at: str = ""
    degraded: bool = False
    errors: list[dict] = Field(default_factory=list)


class DupontResult(BaseModel):
    symbol: str
    period: str = ""
    net_margin: Optional[float] = None          # % 归母净利/营业总收入
    asset_turnover: Optional[float] = None      # 次 营业总收入/期末总资产
    equity_multiplier: Optional[float] = None   # 倍 期末总资产/归母权益
    calculated_roe: Optional[float] = None      # % 三因素乘积
    reported_roe: Optional[float] = None        # % 财务摘要口径
    difference: Optional[float] = None          # pct
    formula_note: str = "ROE=净利率×总资产周转率×权益乘数；周转率用期末总资产（非平均），与报告口径存在差异"
    missing_fields: list[str] = Field(default_factory=list)


class CashflowQualityResult(BaseModel):
    symbol: str
    period: str = ""
    ocf_to_net_profit: Optional[float] = None       # 经营现金流/净利润
    fcf_to_net_profit: Optional[float] = None       # 自由现金流/净利润
    cash_revenue_ratio: Optional[float] = None      # 销售收现/营业收入
    capex_intensity: Optional[float] = None         # 资本开支/营业收入
    receivable_growth: Optional[float] = None       # % 应收账款同比
    inventory_growth: Optional[float] = None        # % 存货同比
    revenue_growth: Optional[float] = None          # % 营收同比（对照）
    quality_flags: list[str] = Field(default_factory=list)  # 规则命中的风险/亮点提示
    missing_fields: list[str] = Field(default_factory=list)
    note: str = "自由现金流=经营现金流净额-资本开支（工程口径）；提示为规则判断，非审计结论"


class Shareholder(BaseModel):
    rank: Optional[int] = None
    name: str = ""
    share_count: Optional[float] = None     # 股
    share_pct: Optional[float] = None       # %
    nature: str = ""                        # 股本性质


class ShareholderStructure(BaseModel):
    symbol: str
    exchange: str = ""
    as_of: str = ""                         # 截至日期
    top_holders: list[Shareholder] = Field(default_factory=list)
    top1_pct: Optional[float] = None
    top10_pct: Optional[float] = None
    holder_count: Optional[float] = None    # 股东总数
    holder_count_prev: Optional[float] = None
    actual_controller: str = ""             # 免费接口无结构化字段时为空+limitation
    institutional_note: str = ""
    pledge_note: str = ""
    limitations: list[str] = Field(default_factory=list)
    provider: str = "akshare"
    endpoint: str = "stock_main_stock_holder"
    fetched_at: str = ""
    degraded: bool = False


class GovernanceAnalysis(BaseModel):
    symbol: str
    concentration_comment: str = ""
    holder_trend_comment: str = ""
    governance_risk_mentions: list[dict] = Field(default_factory=list)  # {text, source_id, cue}
    management_notes: list[dict] = Field(default_factory=list)          # 管理层/激励相关来源摘句
    related_party_mentions: list[dict] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class PeerMetric(BaseModel):
    metric: str
    company_value: Optional[float] = None
    peer_values: dict[str, Optional[float]] = Field(default_factory=dict)  # name -> value
    company_percentile: Optional[float] = None   # 0-100，在同业样本中的分位
    unit: str = ""
    source: str = ""


class PeerComparison(BaseModel):
    symbol: str
    sector: str = ""
    peer_list: list[dict] = Field(default_factory=list)   # {code, name, market_cap亿元}
    peer_selection_reason: str = ""
    metric_comparison: list[PeerMetric] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    limitation: str = ""
    degraded: bool = False
    provider: str = "akshare"
    fetched_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
