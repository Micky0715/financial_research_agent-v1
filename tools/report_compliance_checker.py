"""Formal report compliance checker (v4 stage E).

检查项（比赛要求逐条对应）：风险提示 / 数据来源 / 估值假设 / 免责声明 /
研究对象与证券代码 / 无来源关键数字 / 假设伪装成事实 / 报告日期。
返回逐项结果 + 总 pass；失败的报告仍可导出，但必须 DRAFT/INCOMPLETE 标记
（见 disclosure_builder）。检查是工程规则，不等于法律合规审查。
"""
import re
from typing import Any, Optional

from schemas.source import Source
from tools.number_grounding import analyze_report_numbers

_DATE_RE = re.compile(r"\d{4}[-年/]\d{1,2}[-月/]\d{1,2}|\d{4}[-年/]\d{1,2}月?")
_CODE_RE = re.compile(r"\b\d{6}\b|\b[A-Z]{2,4}\.\w{2}\b")
_ASSUMPTION_CUES = ("假设", "配置假设", "情景", "测算", "预测区间")
_VALUATION_CUES = ("DCF", "现金流折现", "估值", "市盈率", "PE", "PB")
# "确定性表述 + 未来价格/目标价" -> 把假设/预测伪装成事实的高危句式
_FACT_DRESSING_RE = re.compile(
    r"(目标价|股价将达|必然|一定会|确定性地|保证)[^。\n]{0,30}\d")


def check_compliance(markdown: str, sources: list[Source],
                     meta: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """markdown: 报告全文；meta: {report_type, symbol, report_date} 可选补充。"""
    meta = meta or {}
    text = markdown or ""
    source_ids = [s.source_id for s in sources]
    grounding = analyze_report_numbers(text, source_ids, None, None)

    has_valuation_content = any(c in text for c in _VALUATION_CUES)
    checks: dict[str, dict] = {
        "has_risk_section": {
            "passed": "风险" in text and ("## " in text or "风险提示" in text),
            "detail": "报告需包含风险提示章节"},
        "has_data_sources": {
            "passed": ("数据来源" in text or "参考来源" in text or "AkShare" in text),
            "detail": "报告需列明数据来源"},
        "has_valuation_assumptions": {
            "passed": (not has_valuation_content) or any(c in text for c in _ASSUMPTION_CUES),
            "detail": "包含估值内容时必须标明假设（无估值内容则不适用）"},
        "has_disclaimer": {
            "passed": "免责声明" in text or "不构成投资建议" in text,
            "detail": "报告需包含免责声明"},
        "has_subject_and_code": {
            "passed": bool(meta.get("symbol")) or bool(_CODE_RE.search(text))
            or meta.get("report_type") in ("industry_research", "macro_research"),
            "detail": "公司研报需有研究对象与证券代码（行业/宏观报告不适用代码项）"},
        "no_unsourced_key_numbers": {
            "passed": grounding.get("unsourced_number_count", 0) == 0,
            "detail": f"无来源数字 {grounding.get('unsourced_number_count', 0)} 个"
                      f"（grounding率 {grounding.get('number_grounding_rate')}）"},
        "no_assumption_dressed_as_fact": {
            "passed": _FACT_DRESSING_RE.search(text) is None,
            "detail": "不得出现'目标价/必然/保证'等把假设或预测写成确定性事实的句式"},
        "has_report_date": {
            "passed": bool(meta.get("report_date")) or bool(_DATE_RE.search(text[:2000])),
            "detail": "报告需有报告日期"},
    }
    failures = [k for k, v in checks.items() if not v["passed"]]
    return {
        "passed": not failures,
        "checks": checks,
        "failures": failures,
        "grounding": {k: grounding.get(k) for k in
                      ("total_number_count", "number_grounding_rate", "unsourced_number_count",
                       "invalid_citation_count")},
        "note": "工程规则检查，不构成法律/监管合规认证",
    }
