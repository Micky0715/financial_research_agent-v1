"""Entry/exit strategy reference from rule scoring (v4 stage C).

依据：生命周期阶段、集中度、增长、政策与竞争线索。输出是策略参考
（积极关注/选择性参与/谨慎观望/规避），不是确定性投资建议——每个输出
都带 score_breakdown 与 limitation。证据不足时 unknown。
"""
from typing import Optional

from schemas.industry_research import (ConcentrationResult, EntryExitAdvice,
                                       LifecycleAssessment)

_STAGE_SCORE = {"导入期": 1, "成长期": 2, "成长期后段/成熟期": 1, "成熟期": 0, "衰退期": -2}


def analyze_entry_exit(topic: str, lifecycle: LifecycleAssessment,
                       concentration: Optional[ConcentrationResult]) -> EntryExitAdvice:
    advice = EntryExitAdvice(industry=topic)
    breakdown: dict = {}
    rationale: list[str] = []

    if lifecycle.stage == "unknown":
        advice.advice = "unknown"
        advice.rationale = ["生命周期证据不足，不给出无依据的进入/退出参考"]
        advice.limitation = "证据不足；" + advice.limitation
        return advice

    stage_score = _STAGE_SCORE.get(lifecycle.stage, 0)
    breakdown["lifecycle"] = {"stage": lifecycle.stage, "score": stage_score}
    rationale.append(f"生命周期：{lifecycle.stage}（{lifecycle.stage_reason}）")

    growth = lifecycle.signals.get("growth_median_pct")
    growth_score = 0
    if growth is not None:
        growth_score = 2 if growth >= 20 else 1 if growth >= 10 else 0 if growth >= 3 else -1
        rationale.append(f"增速中位数 {growth}%（来源文本证据）")
    breakdown["growth"] = {"median_pct": growth, "score": growth_score}

    conc_score = 0
    if concentration and concentration.cr5 is not None:
        if concentration.cr5 >= 70:
            conc_score = -1
            rationale.append(f"CR5={concentration.cr5}%（{concentration.sample_note[:24]}…）——头部固化，新进入壁垒高")
        elif concentration.cr5 <= 40:
            conc_score = 1
            rationale.append(f"CR5={concentration.cr5}%——格局分散，存在整合与份额提升机会，但竞争激烈")
        else:
            rationale.append(f"CR5={concentration.cr5}%——集中度中等")
    else:
        rationale.append("集中度数据不可得（该维度不计分）")
    breakdown["concentration"] = {"cr5": concentration.cr5 if concentration else None,
                                  "score": conc_score}

    policy_score = 1 if lifecycle.signals.get("policy_source_count", 0) >= 2 else 0
    if policy_score:
        rationale.append(f"{lifecycle.signals['policy_source_count']} 个来源包含政策支持线索")
    breakdown["policy"] = {"score": policy_score}

    decline_pen = -1 if lifecycle.signals.get("decline_cue_source_count", 0) >= 2 else 0
    if decline_pen:
        rationale.append("多个来源提及产能过剩/出清——竞争与供给风险扣分")
    breakdown["oversupply_penalty"] = {"score": decline_pen}

    total = stage_score + growth_score + conc_score + policy_score + decline_pen
    breakdown["total"] = total
    advice.score_breakdown = breakdown
    advice.rationale = rationale
    advice.advice = ("积极关注" if total >= 4 else "选择性参与" if total >= 2
                     else "谨慎观望" if total >= 0 else "规避")
    advice.limitation = ("规则打分（权重与阈值见 tools/industry_entry_exit_analyzer.py），"
                         "未覆盖资本开支强度/技术壁垒的量化数据（免费渠道不可得），"
                         "输出为策略研究参考，不构成投资建议")
    return advice
