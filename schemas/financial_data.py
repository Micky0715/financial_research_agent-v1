"""Schema for structured financial data fetched from AkShare (v3 stage A).

FinancialDataSnapshot is the normalized, provider-agnostic shape the rest of
the pipeline consumes. Fields that the provider could not return stay None
and are listed in missing_fields - never fabricated.
"""
from typing import Any, Optional

from pydantic import BaseModel, Field


class LineageEntry(BaseModel):
    """字段级数据血缘（v4 阶段I）：不满足于 provider=akshare，逐字段记录
    归一化字段名/原始字段名/取值/单位/报告期/provider/原始发布平台/
    来源URL或接口说明/抓取时间/换算说明/推导来源/是否缺失/置信度。

    AkShare 无法回传原始数据源 URL 时，no_direct_source_url=True，
    endpoint_description 说明接口名称与原始平台，不冒充有精确 URL。
    """

    normalized_field: str
    raw_field: str = ""
    value: Optional[float] = None
    unit: str = ""
    period: str = ""
    provider: str = "akshare"
    original_platform: str = ""             # 数据原始转载/发布平台，如"新浪财经"
    source_url: str = ""                    # 精确来源 URL（可得时）
    endpoint_description: str = ""          # source_url 为空时的接口名称+说明
    no_direct_source_url: bool = True
    fetched_at: str = ""
    transformation: str = ""                # 单位换算/口径调整说明，无则空
    derived_from: list[str] = Field(default_factory=list)  # 由哪些字段推导得来
    missing: bool = False
    confidence: str = "high"                # high/medium/low（推导值通常 medium）


class FinancialDataSnapshot(BaseModel):
    """Normalized per-company structured financial snapshot.

    Monetary fields are in 亿元 (converted from the provider's raw unit);
    ratio fields are fractions of 1 unless noted. `yearly_series` carries
    multi-year normalized series for charting: {"revenue": [(year, value)],
    "net_profit": [...], "roe": [...], "gross_margin": [...]}.
    """

    symbol: str
    company_name: str = ""
    period: str = ""  # reporting period of the fundamental figures, e.g. "2024年报"

    revenue: Optional[float] = None            # 亿元
    net_profit: Optional[float] = None         # 亿元
    gross_margin: Optional[float] = None       # %
    roe: Optional[float] = None                # %
    operating_cash_flow: Optional[float] = None  # 亿元
    total_assets: Optional[float] = None       # 亿元
    total_liabilities: Optional[float] = None  # 亿元
    debt_ratio: Optional[float] = None         # %

    pe: Optional[float] = None                 # TTM where available
    pb: Optional[float] = None
    ps: Optional[float] = None
    market_cap: Optional[float] = None         # 亿元
    price: Optional[float] = None              # 元

    yearly_series: dict[str, list[tuple[int, float]]] = Field(default_factory=dict)
    multiples_history: dict[str, list[float]] = Field(default_factory=dict)  # e.g. {"pe": [...], "pb": [...]}

    data_sources: list[str] = Field(default_factory=list)   # which provider endpoints supplied data
    missing_fields: list[str] = Field(default_factory=list)  # normalized fields the provider couldn't fill
    fetched_at: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    # v4 阶段I：逐字段血缘（normalized_field -> LineageEntry.model_dump()）。
    # 新增字段，向后兼容：旧代码继续用上面的扁平字段，不受影响。
    field_lineage: dict[str, dict[str, Any]] = Field(default_factory=dict)
