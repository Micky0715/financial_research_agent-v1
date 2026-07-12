"""Report-wide number-level grounding analysis (v3 stage D).

对报告里的每个"实质性数字"（金额/百分比/倍数/估值指标）做溯源分类：

- sourced_number:     所在句有合法 [sX] 引用（引用的 source_id 真实存在）
- akshare_number:     所在句标注了 AkShare 来源
- config_assumption:  所在句标注了"假设/配置假设"（折现率、永续增长率等）
- calculated_number:  所在句标注了模型测算/DCF/情景估算（工具计算结果）
- unsourced_number:   以上都不满足

原则（与项目红线一致）：**不为提高指标伪造引用**——假设就标假设、测算就标测算，
分类器的职责是如实统计，不是把数字"洗"成有源。年份（19xx/20xx）不算实质性
数字（上下文信息，不需要溯源）。
"""
import re
from typing import Any, Optional

from tools.citation_checker import CITATION_RE, check_citations

# 实质性数字：金额（亿/万/元）、百分比、倍数、估值指标值
_NUMBER_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s*(?:亿元|万元|亿|万(?!一)|元|%|倍)"
)
# 年份本身不需要溯源（"2025年" 的 2025）
_YEAR_RE = re.compile(r"(?:19|20)\d{2}\s*年")

_AKSHARE_HINTS = ("AkShare", "akshare", "结构化金融数据", "结构化数据")
_ASSUMPTION_HINTS = ("配置假设", "假设参数", "假设条件", "为假设", "假设：", "（假设", "(假设")
_CALCULATED_HINTS = ("模型测算", "DCF", "情景", "估算", "测算得出", "工程近似")

_ZH_SENT_RE = re.compile(r"[。！？；\n]")


def _substantive_numbers(sentence: str) -> list[str]:
    """句中的实质性数字（剔除年份片段里的数字）。"""
    without_years = _YEAR_RE.sub("", sentence)
    return [m.group(0) for m in _NUMBER_RE.finditer(without_years)]


def _approx_in(value_text: str, candidates: list[float]) -> bool:
    """value_text 数值是否与任一候选值近似相等（1% 容差）——用于 AkShare/DCF 数值核对。"""
    try:
        v = float(re.sub(r"[^\d.]", "", value_text.replace(",", "")))
    except ValueError:
        return False
    return any(c is not None and abs(v - c) <= max(abs(c) * 0.01, 0.01) for c in candidates)


def _flatten_numeric(obj: Any, out: list[float]) -> None:
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        out.append(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten_numeric(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _flatten_numeric(v, out)


def analyze_report_numbers(
    markdown: str,
    source_ids: list[str],
    snapshot: Optional[dict] = None,
    dcf: Optional[dict] = None,
) -> dict[str, Any]:
    """报告全文数字级 grounding 统计。

    Returns metrics + per-category counts + unsourced 明细（供 grounding report）。
    """
    citation_check = check_citations(markdown, source_ids)
    valid_ids = set(citation_check["valid_citations"])

    snapshot_values: list[float] = []
    if snapshot:
        _flatten_numeric(
            {k: snapshot.get(k) for k in (
                "revenue", "net_profit", "gross_margin", "roe", "operating_cash_flow",
                "debt_ratio", "pe", "pb", "ps", "market_cap", "price")},
            snapshot_values,
        )
    dcf_values: list[float] = []
    if dcf:
        _flatten_numeric(dcf.get("valuation_range"), dcf_values)
        _flatten_numeric(dcf.get("inputs") or (dcf.get("scenarios", {}).get("base", {}) or {}).get("inputs"), dcf_values)

    counts = {"sourced": 0, "akshare": 0, "assumption": 0, "calculated": 0, "unsourced": 0}
    unsourced_detail: list[dict] = []
    verified_value_matches = {"akshare": 0, "calculated": 0}

    for sentence in _ZH_SENT_RE.split(markdown or ""):
        numbers = _substantive_numbers(sentence)
        if not numbers:
            continue
        sent_citations = [c for c in CITATION_RE.findall(sentence)]
        has_valid_citation = any(
            (c[len("source_"):] if c.startswith("source_") else c) in valid_ids for c in sent_citations
        )
        is_akshare = any(h in sentence for h in _AKSHARE_HINTS)
        is_assumption = any(h in sentence for h in _ASSUMPTION_HINTS)
        is_calculated = any(h in sentence for h in _CALCULATED_HINTS)

        for num in numbers:
            if has_valid_citation:
                counts["sourced"] += 1
            elif is_akshare:
                counts["akshare"] += 1
                if _approx_in(num, snapshot_values):
                    verified_value_matches["akshare"] += 1
            elif is_assumption:
                counts["assumption"] += 1
            elif is_calculated:
                counts["calculated"] += 1
                if _approx_in(num, dcf_values):
                    verified_value_matches["calculated"] += 1
            else:
                counts["unsourced"] += 1
                if len(unsourced_detail) < 30:
                    unsourced_detail.append({"number": num, "sentence": sentence.strip()[:100]})

    total = sum(counts.values())
    grounded = total - counts["unsourced"]
    return {
        "total_number_count": total,
        "grounded_number_count": grounded,
        "number_grounding_rate": round(grounded / total, 3) if total else 1.0,
        "sourced_number_count": counts["sourced"],
        "akshare_number_count": counts["akshare"],
        "assumption_number_count": counts["assumption"],
        "calculated_number_count": counts["calculated"],
        "unsourced_number_count": counts["unsourced"],
        "invalid_citation_count": citation_check["invalid_citation_count"],
        "invalid_citations": citation_check["invalid_citations"],
        "value_verified": verified_value_matches,
        "unsourced_detail": unsourced_detail,
    }
