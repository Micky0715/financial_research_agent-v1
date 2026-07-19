"""Schemas for macro economic data / policy parsing / transmission / risks (v4).

Every macro data point records its provenance (source_name / source_url or
endpoint description / release period / fetched_at). Values the provider could
not return stay None and are listed in missing_fields - never fabricated.
"""
from typing import Any, Optional

from pydantic import BaseModel, Field


class MacroDataPoint(BaseModel):
    """One normalized macro indicator observation."""

    indicator_key: str                      # stable english key, e.g. "cpi_yoy"
    indicator_name: str                     # 中文名，如 "CPI同比"
    value: Optional[float] = None
    unit: str = ""                          # %, 点, 亿元, 元/美元 ...
    period: str = ""                        # 报告期，如 "2026-06" / "2026Q2"
    frequency: str = ""                     # monthly / quarterly / yearly / daily / event
    release_date: str = ""                  # 数据发布日（可得时）
    source_url: str = ""                    # 原始来源 URL；akshare 无法提供时留空
    source_name: str = ""                   # 原始发布机构，如 国家统计局
    provider: str = "akshare"
    endpoint: str = ""                      # akshare 接口名
    original_platform: str = ""             # akshare 抓取的转载平台
    no_direct_source_url: bool = True       # akshare 通常无法回传原始 URL，如实标注
    fetched_at: str = ""
    previous_value: Optional[float] = None
    yoy: Optional[float] = None             # 同比（%），可得时
    mom: Optional[float] = None             # 环比（%），可得时
    transformation: str = ""                # 归一化/派生说明，空=原值
    missing_fields: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MacroSnapshot(BaseModel):
    """Bundle of macro indicators for one collection run."""

    indicators: dict[str, MacroDataPoint] = Field(default_factory=dict)
    series: dict[str, list[tuple[str, float]]] = Field(default_factory=dict)  # key -> [(period, value)]
    requested: list[str] = Field(default_factory=list)
    missing_indicators: list[str] = Field(default_factory=list)
    errors: list[dict] = Field(default_factory=list)
    fetched_at: str = ""
    degraded: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyImpactResult(BaseModel):
    """Structured parse of one policy document/text."""

    title: str = ""
    issuer: str = ""                        # 发布机构
    publish_date: str = ""
    policy_topic: str = ""
    policy_goals: list[str] = Field(default_factory=list)
    policy_tools: list[str] = Field(default_factory=list)      # 降准/贴息/补贴/准入...
    supported_industries: list[str] = Field(default_factory=list)
    constrained_industries: list[str] = Field(default_factory=list)
    potential_impacts: list[str] = Field(default_factory=list)
    evidence_quotes: list[str] = Field(default_factory=list)   # 原文摘句（可溯源）
    parse_method: str = ""                  # llm / rule_fallback
    limitation: str = ""
    source_id: str = ""


class TransmissionStep(BaseModel):
    variable: str
    direction: str = ""                     # up / down / mixed
    rationale: str = ""


class TransmissionPath(BaseModel):
    """One explainable macro transmission chain (rule graph, not a causal model)."""

    trigger: str                            # e.g. "利率下降"
    steps: list[TransmissionStep] = Field(default_factory=list)
    affected_sectors: list[str] = Field(default_factory=list)
    evidence_indicators: list[str] = Field(default_factory=list)  # 支撑该触发条件的指标 key
    confidence_note: str = "规则图推演，非因果模型预测，仅供研究参考"


class GreyRhinoRisk(BaseModel):
    risk_name: str
    risk_level: str = "unknown"             # low / medium / high / unknown
    triggered_indicators: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)           # 具体数值+报告期
    possible_transmission_path: str = ""
    limitation: str = ""
