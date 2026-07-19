"""Schemas for industry deep research (v4 stage C): lifecycle / concentration /
industry chain / multi-year scenarios / entry-exit advice.

判断必须带证据（来源句 + source_id 或结构化样本口径）；数据拿不到就 unknown/
degraded + limitation，不允许无依据结论。
"""
from typing import Any, Optional

from pydantic import BaseModel, Field


class EvidenceItem(BaseModel):
    text: str
    source_id: str = ""                     # 空=结构化数据证据（kind 里注明）
    kind: str = ""                          # growth / penetration / policy / structure / margin


class LifecycleAssessment(BaseModel):
    industry: str
    stage: str = "unknown"                  # 导入期/成长期/成熟期/衰退期/unknown
    stage_reason: str = ""
    evidence: list[EvidenceItem] = Field(default_factory=list)
    signals: dict[str, Any] = Field(default_factory=dict)  # {growth_median_pct, penetration_pct, policy_mentions, member_count}
    limitation: str = ""


class ConcentrationResult(BaseModel):
    industry: str
    sector_name: str = ""                   # 匹配到的新浪板块
    sector_kind: str = ""                   # 行业 / 概念
    sample_note: str = ""                   # 样本口径（A股上市公司市值口径等）
    member_count: int = 0
    cr3: Optional[float] = None             # %（样本口径）
    cr5: Optional[float] = None
    cr10: Optional[float] = None
    hhi: Optional[float] = None             # 0-10000（样本口径）
    top_members: list[dict] = Field(default_factory=list)  # {name, mktcap_yi, share_pct}
    source_mentions: list[EvidenceItem] = Field(default_factory=list)  # 来源正文中的集中度表述
    degraded: bool = False
    limitation: str = ""
    fetched_at: str = ""


class ChainNode(BaseModel):
    layer: str                              # 上游/中游/下游/终端
    segment: str                            # 环节名
    description: str = ""
    key_companies: list[str] = Field(default_factory=list)
    verified_in_sources: bool = False       # 环节词是否在来源正文出现


class IndustryChain(BaseModel):
    industry: str
    nodes: list[ChainNode] = Field(default_factory=list)
    relations: list[dict] = Field(default_factory=list)   # {from, to, relation}
    price_transmission: str = ""
    tech_policy_nodes: list[str] = Field(default_factory=list)
    data_basis: str = ""                    # curated_template / llm_extracted / hybrid
    limitation: str = ""


class ScenarioVariable(BaseModel):
    name: str
    base_value: Optional[float] = None
    unit: str = ""
    value_kind: str = ""                    # historical_source / external_forecast / config_assumption / calculated_value
    source_id: str = ""
    note: str = ""


class ScenarioYear(BaseModel):
    year: int
    bear: Optional[float] = None
    base: Optional[float] = None
    bull: Optional[float] = None
    unit: str = ""


class ScenarioModel(BaseModel):
    industry: str
    variable: str = "市场规模"
    years: list[ScenarioYear] = Field(default_factory=list)
    inputs: list[ScenarioVariable] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    is_indexed: bool = False                # True=无真实基数，以100为指数模拟
    limitation: str = ""


class EntryExitAdvice(BaseModel):
    industry: str
    advice: str = "unknown"                 # 积极关注/选择性参与/谨慎观望/规避/unknown
    score_breakdown: dict[str, Any] = Field(default_factory=dict)
    rationale: list[str] = Field(default_factory=list)
    limitation: str = "策略参考基于规则打分，非确定性投资建议"
