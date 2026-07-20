"""Bounded, targeted report revision (v4 stage G).

Workflow: report -> evaluate -> revise_report (>=1 concrete problem found) ->
final_evaluate. Hard cap: at most one revision round (enforced by the caller
in orchestrator/workflow.py, not by this module looping).

Every fix here is either (a) boilerplate that is true regardless of content
(disclaimer), (b) removal of something wrong (a citation to a source_id that
doesn't exist), or (c) attaching a REAL matching data point that was already
computed but not cited (AkShare/DCF value within 1% tolerance). This module
never invents a citation, never relabels an assumption as sourced data, and
never rewrites unrelated sections - that is the project's number-grounding
red line and it applies to revision too.
"""
import re
from typing import Any, Optional

from schemas.source import Source
from tools.citation_checker import CITATION_RE
from tools.number_grounding import analyze_report_numbers

_DISCLAIMER_BLOCK = (
    "\n\n## 免责声明\n\n本报告由自动化研究系统基于公开信息生成，不构成投资建议；"
    "估值与情景分析依赖假设条件，结果仅供研究参考；使用者需自行核实数据并承担决策风险。\n"
)
_RISK_HEADING = "## 风险提示（自动补充）"
_REFERENCE_HEADING = "## 参考来源"


def identify_revision_issues(evaluation: dict[str, Any], markdown: str,
                             report_type: str) -> list[dict[str, str]]:
    """具体、可定向修复的问题列表；空列表=不触发修订（避免无意义改稿）。"""
    diagnostics = evaluation.get("diagnostics", {}) or {}
    criteria = evaluation.get("criteria_scores", {}) or {}
    issues: list[dict[str, str]] = []

    if diagnostics.get("invalid_citation_count", 0) > 0:
        issues.append({"kind": "invalid_citations",
                       "reason": f"发现 {diagnostics['invalid_citation_count']} 处指向不存在来源的引用"})
    if diagnostics.get("unsourced_number_count", 0) > 0:
        issues.append({"kind": "unsourced_numbers",
                       "reason": f"发现 {diagnostics['unsourced_number_count']} 处未溯源数字"})
    if "免责声明" not in markdown and "不构成投资建议" not in markdown:
        issues.append({"kind": "missing_disclaimer", "reason": "报告缺少免责声明"})
    if report_type == "company_research" and criteria.get("risk_warning", 1.0) < 0.5:
        issues.append({"kind": "missing_risk_section", "reason": "风险提示章节缺失或内容过短"})
    if diagnostics.get("source_count", 0) > 0 and diagnostics.get("citation_count", 0) == 0:
        issues.append({"kind": "missing_reference_list", "reason": "有来源但正文/参考列表中无任何 [sX] 引用"})
    return issues


def _strip_invalid_citations(markdown: str, valid_ids: set) -> tuple[str, int]:
    removed = 0

    def _sub(m: re.Match) -> str:
        nonlocal removed
        raw = m.group(1)
        sid = raw[len("source_"):] if raw.startswith("source_") else raw
        if sid in valid_ids:
            return m.group(0)
        removed += 1
        return ""

    revised = CITATION_RE.sub(_sub, markdown)
    return revised, removed


def _attach_real_markers(markdown: str, sources: list[Source],
                         snapshot: Optional[dict], dcf: Optional[dict]) -> tuple[str, int]:
    """未溯源数字里，凡与 snapshot/DCF 真实值匹配（1%容差）的句子，补一句真实
    来源标注；不匹配的句子原样保留（诚实：宁可继续记为 unsourced，也不编造）。"""
    grounding = analyze_report_numbers(markdown, [s.source_id for s in sources], snapshot, dcf)
    fixed = 0
    revised = markdown
    for detail in grounding.get("unsourced_detail", []):
        sentence = detail["sentence"]
        if not sentence or sentence not in revised:
            continue
        try:
            value = float(re.sub(r"[^\d.]", "", detail["number"]))
        except ValueError:
            continue
        marker = None
        if snapshot and any(
            isinstance(v, (int, float)) and abs(value - v) <= max(abs(v) * 0.01, 0.01)
            for v in (snapshot.get(k) for k in (
                "revenue", "net_profit", "gross_margin", "roe", "operating_cash_flow",
                "debt_ratio", "pe", "pb", "ps", "market_cap", "price")) if v is not None
        ):
            marker = "（AkShare 结构化数据核对一致）"
        elif dcf and dcf.get("valuation_range") and any(
            isinstance(v, (int, float)) and abs(value - v) <= max(abs(v) * 0.01, 0.01)
            for v in dcf["valuation_range"].values() if isinstance(v, (int, float))
        ):
            marker = "（与 DCF 模型测算结果一致）"
        if marker and marker not in sentence:
            revised = revised.replace(sentence, sentence + marker, 1)
            fixed += 1
    return revised, fixed


def apply_revision(markdown: str, issues: list[dict[str, str]], sources: list[Source],
                   analysis: Optional[dict] = None) -> dict[str, Any]:
    """按 issues 逐项定向修复；只改问题相关片段，不重写无关内容。"""
    revised = markdown
    modified_sections: list[str] = []
    reasons: list[str] = []
    analysis = analysis or {}
    snapshot = analysis.get("financial_snapshot_full")
    dcf = analysis.get("dcf_valuation_full")
    valid_ids = {s.source_id for s in sources}

    for issue in issues:
        kind = issue["kind"]
        if kind == "invalid_citations":
            revised, removed = _strip_invalid_citations(revised, valid_ids)
            if removed:
                modified_sections.append("正文引用标记")
                reasons.append(f"移除 {removed} 处指向不存在来源的幻觉引用")

        elif kind == "unsourced_numbers":
            revised, fixed = _attach_real_markers(revised, sources, snapshot, dcf)
            if fixed:
                modified_sections.append("正文数字标注")
                reasons.append(f"为 {fixed} 处数字补充了与结构化数据核对一致的真实来源标注"
                               "（其余未匹配数字如实保留为未溯源，不编造引用）")
            else:
                reasons.append("未溯源数字未在结构化数据中找到匹配值，不做无依据标注（诚实保留）")

        elif kind == "missing_disclaimer":
            revised = revised.rstrip() + _DISCLAIMER_BLOCK
            modified_sections.append("免责声明")
            reasons.append("补充免责声明章节")

        elif kind == "missing_risk_section":
            rule_risks = ((analysis.get("_rule_hints") or {}).get("risks")) or {}
            risk_lines = []
            for category, items in rule_risks.items():
                for item in (items or [])[:2]:
                    if isinstance(item, dict) and item.get("text"):
                        sid = f"[{item['source_id']}]" if item.get("source_id") else ""
                        risk_lines.append(f"- **{category}**：{item['text']}{sid}")
            body = "\n".join(risk_lines) if risk_lines else (
                "本次分析未能从检索来源中抽取到具体风险点，具体风险请以公司官方公告与年报为准。")
            revised = revised.rstrip() + f"\n\n{_RISK_HEADING}\n\n{body}\n"
            modified_sections.append("风险提示")
            reasons.append("补充风险提示章节" + ("（基于规则抽取的真实来源风险点）" if risk_lines else "（无可用风险证据，如实说明）"))

        elif kind == "missing_reference_list":
            if _REFERENCE_HEADING not in revised and sources:
                ref_lines = "\n".join(f"- [{s.source_id}] {s.title}（{s.url}）" for s in sources)
                revised = revised.rstrip() + f"\n\n{_REFERENCE_HEADING}\n\n{ref_lines}\n"
                modified_sections.append("参考来源")
                reasons.append("补充参考来源列表")

    return {"markdown": revised, "modified_sections": modified_sections, "reasons": reasons,
           "changed": revised != markdown}
