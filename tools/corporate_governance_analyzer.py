"""Corporate governance analysis: shareholder concentration rules + source-text
evidence extraction (v4 stage B).

结构化部分来自 ShareholderStructure（真实接口数据）；董监高/股权激励/关联
交易/治理风险没有免费结构化接口，改为从已抓取来源正文抽取句级证据（带
source_id 可溯源），没有证据就如实说没有——不做无来源判断。
"""
import re
from typing import Optional

from schemas.company_research import GovernanceAnalysis, ShareholderStructure
from schemas.source import Source

_RISK_CUES = ("质押", "违规", "处罚", "警示函", "立案", "减持", "占用", "诉讼", "监管函", "问询函")
_MGMT_CUES = ("董事长", "总经理", "高管", "管理层", "股权激励", "限制性股票", "期权激励", "核心团队")
_RELATED_CUES = ("关联交易", "关联方", "同业竞争")
_SENT_RE = re.compile(r"[。！？；\n]")


def _collect(sources: list[Source], cues: tuple, limit: int = 6) -> list[dict]:
    out = []
    for src in sources:
        for sent in _SENT_RE.split(src.content or ""):
            sent = sent.strip()
            if not (15 <= len(sent) <= 200):
                continue
            hit = next((c for c in cues if c in sent), None)
            if hit:
                out.append({"text": sent[:150], "source_id": src.source_id, "cue": hit})
                if len(out) >= limit:
                    return out
    return out


def analyze_governance(structure: Optional[ShareholderStructure],
                       sources: list[Source]) -> GovernanceAnalysis:
    result = GovernanceAnalysis(symbol=structure.symbol if structure else "")

    if structure and structure.top1_pct is not None:
        t1, t10 = structure.top1_pct, structure.top10_pct
        if t1 >= 50:
            result.concentration_comment = (
                f"第一大股东持股{t1}%（绝对控股），前十大合计{t10}%——控制权高度集中，"
                "决策效率高但中小股东制衡有限")
        elif t1 >= 30:
            result.concentration_comment = (
                f"第一大股东持股{t1}%（相对控股），前十大合计{t10}%——股权较为集中")
        else:
            result.concentration_comment = (
                f"第一大股东持股{t1}%，前十大合计{t10}%——股权相对分散，需关注控制权稳定性")
        result.concentration_comment += f"（截至{structure.as_of}，AkShare·新浪财经）"
    else:
        result.limitations.append("股东结构数据缺失，集中度无法判断")

    if structure and structure.holder_count and structure.holder_count_prev:
        chg = (structure.holder_count / structure.holder_count_prev - 1) * 100
        trend = "增加" if chg > 0 else "减少"
        result.holder_trend_comment = (
            f"股东总数由{structure.holder_count_prev:,.0f}户变为{structure.holder_count:,.0f}户"
            f"（{trend}{abs(chg):.1f}%，AkShare·新浪财经）——户数减少通常对应筹码集中，反之分散")

    result.governance_risk_mentions = _collect(sources, _RISK_CUES)
    result.management_notes = _collect(sources, _MGMT_CUES, limit=4)
    result.related_party_mentions = _collect(sources, _RELATED_CUES, limit=4)
    if not result.governance_risk_mentions:
        result.limitations.append("已抓取来源中未发现治理风险类表述（不等于不存在风险）")
    result.limitations += (structure.limitations if structure else [])
    result.limitations.append("董事会/高管结构无免费结构化接口，仅能依赖来源正文与年报原文")
    return result
