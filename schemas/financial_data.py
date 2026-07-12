"""Schema for structured financial data fetched from AkShare (v3 stage A).

FinancialDataSnapshot is the normalized, provider-agnostic shape the rest of
the pipeline consumes. Fields that the provider could not return stay None
and are listed in missing_fields - never fabricated.
"""
from typing import Any, Optional

from pydantic import BaseModel, Field


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
