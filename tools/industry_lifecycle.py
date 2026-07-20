"""Industry lifecycle assessment with explicit evidence (v4 stage C).

判断依据全部可溯源：来源正文抽取的增速/渗透率/政策句（带 source_id）+
板块结构化数据（成员数）。规则阈值内联可审查；证据不足 -> unknown，
不让 LLM 拍结论。
"""
import re
import statistics
from typing import Optional

from schemas.industry_research import EvidenceItem, LifecycleAssessment
from schemas.source import Source

_SENT_RE = re.compile(r"[。！？；\n]")
_GROWTH_RE = re.compile(r"(?:同比增长|增速|复合增长率|CAGR|年均增长)[^\d%（(]{0,12}?([\d.]+)\s*%")
_NEG_GROWTH_RE = re.compile(r"(?:同比下降|下滑|负增长|同比减少)[^\d%（(]{0,12}?([\d.]+)\s*%")
_PENETRATION_RE = re.compile(r"渗透率[^\d%（(]{0,12}?([\d.]+)\s*%")
_POLICY_CUES = ("政策", "规划", "补贴", "支持", "指导意见", "行动方案", "十四五", "十五五")
_DECLINE_CUES = ("产能过剩", "出清", "萎缩", "衰退", "淘汰落后")


def _extract(sources: list[Source], regex: re.Pattern, kind: str,
             limit: int = 6) -> tuple[list[float], list[EvidenceItem]]:
    values, evidence = [], []
    for src in sources:
        for sent in _SENT_RE.split(src.content or ""):
            sent = sent.strip()
            if not (10 <= len(sent) <= 250):
                continue
            m = regex.search(sent)
            if m:
                try:
                    v = float(m.group(1))
                except ValueError:
                    continue
                if 0 < v <= 500:
                    values.append(v)
                    evidence.append(EvidenceItem(text=sent[:150], source_id=src.source_id, kind=kind))
                    if len(values) >= limit:
                        return values, evidence
    return values, evidence


def assess_lifecycle(topic: str, sources: list[Source],
                     member_count: Optional[int] = None) -> LifecycleAssessment:
    result = LifecycleAssessment(industry=topic)

    growth_vals, growth_ev = _extract(sources, _GROWTH_RE, "growth")
    neg_vals, neg_ev = _extract(sources, _NEG_GROWTH_RE, "growth", limit=4)
    pen_vals, pen_ev = _extract(sources, _PENETRATION_RE, "penetration", limit=4)
    result.evidence = growth_ev + neg_ev + pen_ev

    policy_hits, decline_hits = [], []
    for src in sources:
        text = (src.title or "") + (src.content or "")[:3000]
        if any(c in text for c in _POLICY_CUES):
            policy_hits.append(src.source_id)
        if any(c in text for c in _DECLINE_CUES):
            decline_hits.append(src.source_id)

    growth_median = round(statistics.median(growth_vals), 1) if growth_vals else None
    penetration = round(statistics.median(pen_vals), 1) if pen_vals else None
    result.signals = {
        "growth_median_pct": growth_median,
        "negative_growth_mentions": len(neg_vals),
        "penetration_pct": penetration,
        "policy_source_count": len(set(policy_hits)),
        "decline_cue_source_count": len(set(decline_hits)),
        "listed_member_count": member_count,
    }

    # 规则判定（阈值为工程启发式，写死可审查）。注意：growth_median 只汇总
    # _GROWTH_RE（正向）命中，纯负增长证据(neg_vals)不参与其计算——判"证据不足"
    # 前必须把 neg_vals 也算作有效证据，否则"来源只提到下降"的行业会被误判
    # unknown（实际应判衰退期，见下方分支）。
    if growth_median is None and penetration is None and not neg_vals:
        result.stage = "unknown"
        result.stage_reason = "来源中未抽取到可靠的行业增速/渗透率数字，不做无证据判断"
        result.limitation = "证据不足；建议补充行业协会/统计局来源"
        return result

    if len(neg_vals) >= 2 and (growth_median is None or growth_median < 5):
        result.stage = "衰退期"
        result.stage_reason = f"多处负增长表述（{len(neg_vals)}处）且增速中位数低"
    elif growth_median is not None and growth_median >= 15:
        if penetration is not None and penetration < 20:
            result.stage = "导入期"
            result.stage_reason = f"增速中位数{growth_median}%且渗透率仅{penetration}%（<20%）"
        else:
            result.stage = "成长期"
            result.stage_reason = f"增速中位数{growth_median}%（≥15%）" + (
                f"，渗透率{penetration}%仍有空间" if penetration is not None else "")
    elif growth_median is not None and growth_median >= 5:
        result.stage = "成长期后段/成熟期"
        result.stage_reason = f"增速中位数{growth_median}%（5-15%），增长降速"
    else:
        result.stage = "成熟期"
        result.stage_reason = f"增速中位数{growth_median}%（<5%）"
    if result.signals["decline_cue_source_count"] >= 2 and result.stage != "衰退期":
        result.stage_reason += f"；但{result.signals['decline_cue_source_count']}个来源提及产能过剩/出清，存在结构性衰退信号"

    result.limitation = ("阶段判定基于来源文本抽取的数字证据与规则阈值（导入<20%渗透、"
                         "成长≥15%增速、成熟<5%），样本受检索来源限制，非行业协会权威划分")
    return result
