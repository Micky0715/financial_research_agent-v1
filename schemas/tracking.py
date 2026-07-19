"""Schemas for periodic tracking reports (v4 stage D)."""
from typing import Any, Optional

from pydantic import BaseModel, Field


class TrackingRecord(BaseModel):
    """One historical observation of an entity (from a pipeline run or a
    tracking-report build)."""

    entity: str
    symbol: str = ""
    period: str = ""                        # 数据报告期，如 "2025年报"/"2026Q1"
    report_type: str = "company_research"
    financial_snapshot: dict[str, Any] = Field(default_factory=dict)
    key_events: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    valuation_assumptions: dict[str, Any] = Field(default_factory=dict)
    source_ids: list[str] = Field(default_factory=list)
    report_path: str = ""
    created_at: str = ""
    run_id: str = ""


class PeriodMetrics(BaseModel):
    """Normalized metrics for one reporting period (from real statements)."""

    period: str
    period_raw: str = ""
    revenue: Optional[float] = None          # 亿元（季度口径=累计值）
    net_profit: Optional[float] = None
    parent_net_profit: Optional[float] = None
    operating_cash_flow: Optional[float] = None
    gross_margin_pct: Optional[float] = None
    debt_ratio_pct: Optional[float] = None
    revenue_single: Optional[float] = None   # 单季（derived），仅 quarterly
    net_profit_single: Optional[float] = None
    yoy_revenue: Optional[float] = None      # %
    yoy_net_profit: Optional[float] = None
    qoq_revenue: Optional[float] = None      # %（单季环比，仅 quarterly）
    qoq_net_profit: Optional[float] = None


class TrackingChange(BaseModel):
    dimension: str                          # 营收增速/盈利背离/毛利率/负债率/估值/风险/管理层表述
    finding: str
    direction: str = ""                     # improving / deteriorating / stable / unknown
    evidence: str = ""


class TrackingReportResult(BaseModel):
    entity: str
    symbol: str
    period_type: str                        # annual / quarterly
    current_period: str = ""
    previous_period: str = ""
    periods: list[PeriodMetrics] = Field(default_factory=list)   # 新->旧
    changes: list[TrackingChange] = Field(default_factory=list)
    prior_record_compared: bool = False     # 是否有历史 run 记录参与对比
    prior_record_notes: list[str] = Field(default_factory=list)
    insufficient_history: bool = False
    insufficient_dimensions: list[str] = Field(default_factory=list)
    markdown: str = ""
    report_path: str = ""
    limitation: str = ""
    fetched_at: str = ""
