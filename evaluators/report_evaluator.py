"""Report-type-specific evaluation (v3 stage E).

v1/v2 用同一套公司导向指标打所有报告，导致行业研报因缺少"营收/净利润/毛利率"
被 financial_depth_score=0 封顶（bad_cases 9）。本模块按 report_type 分支：

- company_research:   沿用 v2 全部指标（含 source_count 硬封顶）+ number_grounding_rate
- industry_research:  市场规模/产业链/政策/竞争格局/趋势 五维替代公司财务/估值深度
- risk_research:      风险覆盖/风险溯源/情景分析
- valuation_research: 估值方法/假设完备性/敏感性分析/估值溯源
- macro_research:     宏观指标/政策语境/趋势分析（预留：当前评测集无此类 topic，
                      逻辑已实现但未经真实 case 验证）

所有类型共享的底线不变：source_count 硬性封顶（防刷分红线）、无效引用惩罚、
数字级 grounding。评估仍是启发式工程评分，不等于专业金融分析师判断。
"""
import re
from typing import Any, Optional

from schemas.source import Source
from tools.domain_rules import authority_tier, is_whitelisted
from tools.number_grounding import analyze_report_numbers

_CITATION_RE = re.compile(r"\[(?:source_)?s\d+\]|\[source_\d+\]")

_FINANCIAL_DEPTH_KEYWORDS = ["营收", "收入", "净利润", "毛利率", "现金流", "同比", "亿元", "%"]
_VALUATION_DEPTH_KEYWORDS = ["估值", "PE", "PB", "PS", "市盈率", "市净率", "可比公司", "DCF", "倍"]

_INDUSTRY_KEYWORDS = {
    "market_size_score": (["市场规模", "市场空间", "渗透率", "出货量", "销量", "复合增长", "CAGR"], 3),
    "industry_chain_score": (["产业链", "上游", "下游", "中游", "供应链", "环节", "零部件"], 3),
    "policy_score": (["政策", "规划", "监管", "补贴", "标准", "十四五", "十五五", "指导意见"], 2),
    "competition_score": (["竞争格局", "市占率", "市场份额", "集中度", "龙头", "梯队", "CR"], 2),
    "trend_score": (["趋势", "增长", "预测", "前景", "拐点", "驱动", "展望"], 3),
}
_RISK_CATEGORIES = ["市场", "财务", "运营", "政策", "技术"]
_SCENARIO_KEYWORDS = ["情景", "敏感性", "悲观", "乐观", "基准", "压力测试"]
_VALUATION_METHODS = ["DCF", "现金流折现", "市盈率", "市净率", "市销率", "可比公司", "分部估值", "PE", "PB", "PS"]
_ASSUMPTION_KEYWORDS = ["折现率", "永续增长", "增长率假设", "假设", "配置假设"]
_MACRO_INDICATORS = ["GDP", "CPI", "PPI", "PMI", "利率", "汇率", "货币政策", "财政政策", "社融", "M2"]


# ------------------------------------------------------------------ #
# Shared helpers
# ------------------------------------------------------------------ #
def _extract_section(content: str, heading_keyword: str) -> str:
    pattern = re.compile(
        rf"^##[^\n]*{re.escape(heading_keyword)}[^\n]*\n(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    return match.group(1) if match else ""


def _keyword_depth_score(text: str, keywords: list[str], full_credit_hits: int) -> float:
    if not text:
        return 0.0
    hits = sum(1 for kw in keywords if kw in text)
    return round(min(1.0, hits / full_credit_hits), 3)


def _bucket_score(count: int, thresholds: list[tuple[int, float]]) -> float:
    for min_count, score in thresholds:
        if count >= min_count:
            return score
    return 0.0


def _authority_score(sources: list[Source]) -> tuple[float, list[str]]:
    if not sources:
        return 0.0, []
    weights, authority_domains = [], []
    for src in sources:
        tier = authority_tier(src.url)
        if tier == 1:
            weights.append(1.0)
            authority_domains.append(src.url)
        elif tier == 2:
            weights.append(0.7)
            authority_domains.append(src.url)
        elif is_whitelisted(src.url):
            weights.append(0.5)
        else:
            weights.append(0.3)
    return round(sum(weights) / len(weights), 3), authority_domains


def _common_metrics(content: str, sources: list[Source], analysis: Optional[dict]) -> dict[str, Any]:
    """所有 report_type 共享的基础指标 + 数字级 grounding。"""
    citation_hits = len(_CITATION_RE.findall(content))
    source_count = len(sources)

    snapshot = (analysis or {}).get("financial_snapshot_full")
    dcf = (analysis or {}).get("dcf_valuation_full")
    grounding = analyze_report_numbers(
        content, [s.source_id for s in sources], snapshot, dcf
    )

    sentences = [s for s in re.split(r"[。！？\n]", content) if s.strip()]
    avg_len = sum(len(s) for s in sentences) / max(len(sentences), 1)

    heading_count = len(re.findall(r"^##\s", content, flags=re.MULTILINE))
    authority, authority_domains = _authority_score(sources)

    return {
        "source_count": source_count,
        "citation_hits": citation_hits,
        "grounding": grounding,
        "criteria": {
            "source_count_score": (
                1.0 if source_count >= 5 else 0.8 if source_count >= 3 else 0.6 if source_count == 2 else 0.3
            ),
            "citation_coverage": min(1.0, citation_hits / max(source_count, 1)),
            "citation_count_score": _bucket_score(citation_hits, [(8, 1.0), (5, 0.8), (3, 0.6), (1, 0.4)]),
            "source_grounding": 1.0 if (sources and citation_hits > 0) else (0.3 if sources else 0.0),
            "structure": min(1.0, heading_count / 7),
            "clarity": 1.0 if 8 <= avg_len <= 100 else 0.6,
            "authority_score": authority,
            "number_grounding_rate": grounding["number_grounding_rate"],
        },
        "authority_domains": authority_domains,
    }


def _universal_caps(common: dict[str, Any]) -> list[tuple[float, str]]:
    """所有类型共享的硬性封顶（v2 防刷分红线原样保留）。"""
    caps: list[tuple[float, str]] = [(1.0, "")]
    sc = common["source_count"]
    if sc == 0:
        caps.append((0.3, "source_count=0"))
    elif sc == 1:
        caps.append((0.5, "source_count=1"))
    elif sc == 2:
        caps.append((0.6, "source_count=2"))
    if sc < 5:
        caps.append((0.85, "source_count<5"))
    if common["citation_hits"] < 5:
        caps.append((0.8, "citation_hits<5"))
    if common["grounding"]["unsourced_number_count"] > 0:
        caps.append((0.8, "unsourced_number_count>0"))
    if common["grounding"]["invalid_citation_count"] > 0:
        caps.append((0.75, "invalid_citations_present"))
    return caps


def _finalize(
    report_type: str,
    criteria: dict[str, float],
    weights: dict[str, float],
    caps: list[tuple[float, str]],
    common: dict[str, Any],
    extra_weaknesses: list[str],
    extra_recommendations: list[str],
) -> dict[str, Any]:
    weighted = sum(criteria.get(k, 0.0) * w for k, w in weights.items())
    cap, cap_reason = min(caps, key=lambda c: c[0])
    overall = round(max(0.0, min(weighted, cap)), 3)

    strengths, weaknesses, recommendations = [], list(extra_weaknesses), list(extra_recommendations)
    for key, val in criteria.items():
        if val >= 0.8:
            strengths.append(f"{key} 表现良好（{round(val, 3)}）")
        elif val <= 0.4:
            weaknesses.append(f"{key} 有待改进（{round(val, 3)}）")
            recommendations.append(f"建议加强 {key} 相关内容")
    g = common["grounding"]
    if g["unsourced_number_count"] > 0:
        weaknesses.append(f"发现 {g['unsourced_number_count']} 处未溯源数字")
        recommendations.append("为具体数字补充 [source_id] 引用 / AkShare 标注 / 假设或测算说明")
    if common["source_count"] < 5:
        weaknesses.append("来源数量不足，报告可靠性受限")

    return {
        "report_type": report_type,
        "overall_score": overall,
        "weighted_score": round(weighted, 3),
        "criteria_scores": {k: round(v, 3) for k, v in criteria.items()},
        "score_cap_reason": cap_reason or None,
        "diagnostics": {
            "source_count": common["source_count"],
            "citation_count": common["citation_hits"],
            "report_length": None,  # filled by caller
            "authority_domains": common["authority_domains"],
            "source_count_cap_applied": bool(cap_reason and cap_reason.startswith("source_count")),
            "score_cap_reason": cap_reason or None,
            **{k: g[k] for k in (
                "total_number_count", "number_grounding_rate", "unsourced_number_count",
                "assumption_number_count", "calculated_number_count", "akshare_number_count",
                "sourced_number_count", "invalid_citation_count",
            )},
            "value_verified": g.get("value_verified"),
        },
        "strengths": strengths,
        "weaknesses": weaknesses,
        "recommendations": recommendations,
    }


# ------------------------------------------------------------------ #
# Per-type evaluators
# ------------------------------------------------------------------ #
def _evaluate_company(content: str, sources: list[Source], common: dict) -> dict[str, Any]:
    financial_section = _extract_section(content, "财务")
    valuation_section = _extract_section(content, "估值")
    risk_section = _extract_section(content, "风险")

    criteria = dict(common["criteria"])
    required_sections = ["执行摘要", "概况", "趋势", "财务", "估值", "风险", "投资观点", "参考来源"]
    criteria["completeness"] = sum(1 for s in required_sections if s in content) / len(required_sections)
    criteria["risk_warning"] = 1.0 if ("风险" in content and len(content.split("风险提示")[-1]) > 30) else (
        0.5 if "风险" in content else 0.0
    )
    criteria["financial_depth_score"] = _keyword_depth_score(financial_section, _FINANCIAL_DEPTH_KEYWORDS, 4)
    criteria["valuation_depth_score"] = _keyword_depth_score(valuation_section, _VALUATION_DEPTH_KEYWORDS, 3)
    criteria["risk_grounding_score"] = (
        0.0 if not risk_section else (1.0 if _CITATION_RE.search(risk_section) else 0.3)
    )

    weights = {
        "completeness": 0.12, "citation_coverage": 0.13, "structure": 0.08, "clarity": 0.08,
        "risk_warning": 0.09, "source_grounding": 0.13, "financial_depth_score": 0.10,
        "valuation_depth_score": 0.10, "authority_score": 0.05, "number_grounding_rate": 0.12,
    }
    caps = _universal_caps(common)
    if criteria["valuation_depth_score"] < 0.6:
        caps.append((0.85, "valuation_depth_score<0.6"))
    if criteria["financial_depth_score"] < 0.6:
        caps.append((0.85, "financial_depth_score<0.6"))
    if criteria["financial_depth_score"] == 0.0:
        caps.append((0.75, "financial_depth_score=0"))

    extra_w, extra_r = [], []
    if criteria["valuation_depth_score"] < 0.6:
        extra_r.append("补充 PE/PB/可比公司等量化估值指标")
    if criteria["financial_depth_score"] == 0.0:
        extra_w.append("财务深度不足，缺少营收、净利润、毛利率等关键财务信息")
    return _finalize("company_research", criteria, weights, caps, common, extra_w, extra_r)


def _evaluate_industry(content: str, sources: list[Source], common: dict) -> dict[str, Any]:
    risk_section = _extract_section(content, "风险")

    criteria = dict(common["criteria"])
    required_sections = ["执行摘要", "概况", "趋势", "风险", "参考来源"]
    criteria["completeness"] = sum(1 for s in required_sections if s in content) / len(required_sections)
    for name, (keywords, full_credit) in _INDUSTRY_KEYWORDS.items():
        criteria[name] = _keyword_depth_score(content, keywords, full_credit)
    criteria["risk_grounding_score"] = (
        0.0 if not risk_section else (1.0 if _CITATION_RE.search(risk_section) else 0.3)
    )

    weights = {
        "completeness": 0.08, "citation_coverage": 0.12, "source_grounding": 0.12,
        "number_grounding_rate": 0.12, "market_size_score": 0.10, "industry_chain_score": 0.10,
        "policy_score": 0.08, "competition_score": 0.08, "trend_score": 0.08,
        "risk_grounding_score": 0.07, "authority_score": 0.05,
    }
    # 行业研报不再有 financial/valuation depth 封顶（bad_cases 9 的修复点）
    caps = _universal_caps(common)
    return _finalize("industry_research", criteria, weights, caps, common, [], [])


def _evaluate_risk(content: str, sources: list[Source], common: dict) -> dict[str, Any]:
    risk_section = _extract_section(content, "风险") or content
    criteria = dict(common["criteria"])
    covered = sum(1 for c in _RISK_CATEGORIES if c in content)
    criteria["risk_coverage_score"] = round(covered / len(_RISK_CATEGORIES), 3)
    criteria["risk_source_grounding"] = 1.0 if _CITATION_RE.search(risk_section) else 0.3
    criteria["scenario_analysis_score"] = _keyword_depth_score(content, _SCENARIO_KEYWORDS, 2)

    weights = {
        "risk_coverage_score": 0.22, "risk_source_grounding": 0.18, "scenario_analysis_score": 0.15,
        "source_count_score": 0.12, "authority_score": 0.10, "number_grounding_rate": 0.13,
        "citation_coverage": 0.10,
    }
    return _finalize("risk_research", criteria, weights, _universal_caps(common), common, [], [])


def _evaluate_valuation(content: str, sources: list[Source], common: dict) -> dict[str, Any]:
    valuation_section = _extract_section(content, "估值") or content
    criteria = dict(common["criteria"])
    criteria["valuation_method_score"] = _keyword_depth_score(content, _VALUATION_METHODS, 3)
    criteria["assumption_completeness"] = _keyword_depth_score(content, _ASSUMPTION_KEYWORDS, 2)
    criteria["sensitivity_analysis_score"] = _keyword_depth_score(content, _SCENARIO_KEYWORDS, 2)
    criteria["valuation_grounding_score"] = 1.0 if _CITATION_RE.search(valuation_section) else 0.3

    weights = {
        "valuation_method_score": 0.20, "assumption_completeness": 0.16,
        "sensitivity_analysis_score": 0.14, "valuation_grounding_score": 0.15,
        "source_count_score": 0.12, "number_grounding_rate": 0.13, "citation_coverage": 0.10,
    }
    return _finalize("valuation_research", criteria, weights, _universal_caps(common), common, [], [])


def _evaluate_macro(content: str, sources: list[Source], common: dict) -> dict[str, Any]:
    risk_section = _extract_section(content, "风险")
    criteria = dict(common["criteria"])
    criteria["macro_indicator_score"] = _keyword_depth_score(content, _MACRO_INDICATORS, 3)
    criteria["policy_context_score"] = _keyword_depth_score(content, _INDUSTRY_KEYWORDS["policy_score"][0], 2)
    criteria["trend_analysis_score"] = _keyword_depth_score(content, _INDUSTRY_KEYWORDS["trend_score"][0], 3)
    criteria["risk_grounding_score"] = (
        0.0 if not risk_section else (1.0 if _CITATION_RE.search(risk_section) else 0.3)
    )

    weights = {
        "macro_indicator_score": 0.20, "policy_context_score": 0.15, "trend_analysis_score": 0.15,
        "risk_grounding_score": 0.12, "source_count_score": 0.13, "authority_score": 0.10,
        "number_grounding_rate": 0.15,
    }
    return _finalize("macro_research", criteria, weights, _universal_caps(common), common, [], [])


_EVALUATORS = {
    "company_research": _evaluate_company,
    "industry_research": _evaluate_industry,
    "risk_research": _evaluate_risk,
    "valuation_research": _evaluate_valuation,
    "macro_research": _evaluate_macro,
}


def evaluate_report(
    markdown_content: str,
    sources: list[Source],
    report_type: str = "company_research",
    analysis: Optional[dict] = None,
) -> dict[str, Any]:
    """按 report_type 分支评估。未知类型回退 company_research 逻辑（并在结果中注明）。"""
    content = markdown_content or ""
    evaluator = _EVALUATORS.get(report_type)
    fallback_used = evaluator is None
    if fallback_used:
        evaluator = _evaluate_company

    common = _common_metrics(content, sources, analysis)
    result = evaluator(content, sources, common)
    result["diagnostics"]["report_length"] = len(content)
    if fallback_used:
        result["report_type"] = report_type
        result["diagnostics"]["evaluator_fallback"] = "unknown report_type, company_research logic used"
    return result
