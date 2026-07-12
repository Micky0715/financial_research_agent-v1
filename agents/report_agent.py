"""Report agent: generates the final report (LLM + fallback) and evaluates its quality."""
import re
from typing import Any, Optional

from config import config
from schemas.request import ResearchRequest
from schemas.source import Source
from schemas.task import Task
from tools.domain_rules import authority_tier, is_whitelisted
from tools.report_renderer import render_html_report, render_markdown_report
from utils.logger import logger
from utils.text_utils import extract_relevant_excerpt, normalize_topic, truncate

from .base_agent import BaseAgent

_PROMPT_PATH = config.PROMPTS_DIR / "report_prompt.txt"

_REQUIRED_HEADINGS = [
    "执行摘要",
    "概况",
    "趋势",
    "财务",
    "估值",
    "风险",
    "投资观点",
    "参考来源",
]

_MAX_ITEMS_PER_SECTION = 8


def build_report_context(
    request: ResearchRequest,
    analysis: dict[str, Any],
    sources: list[Source],
    source_excerpts: dict[str, str] | None = None,
    max_source_excerpt_chars: int = 500,
) -> dict[str, str]:
    """Build the (compact) analysis_block + sources_block fed to the report LLM prompt.

    Report generation doesn't need full page content - only the structured
    analysis (already a summary) and a short per-source excerpt to ground
    citations. Reuses AnalyzeAgent's extracted key sentences
    (`source_excerpts`) when available instead of recomputing them; falls
    back to a fresh extraction otherwise (e.g. fallback-path analysis).
    """
    import json

    analysis_block = json.dumps(
        {k: v for k, v in analysis.items() if not k.startswith("_")}, ensure_ascii=False
    )[:6000]

    source_excerpts = source_excerpts or {}
    if not sources:
        sources_block = "（无有效来源）"
    else:
        lines = []
        for src in sources:
            excerpt = source_excerpts.get(src.source_id, "")
            excerpt = truncate(excerpt, max_source_excerpt_chars) if excerpt else extract_relevant_excerpt(
                src.content, max_chars=max_source_excerpt_chars
            )
            lines.append(f"[{src.source_id}] {src.title} - {src.url} (score={src.score})\n摘录：{excerpt}")
        sources_block = "\n".join(lines)

    return {"analysis_block": analysis_block, "sources_block": sources_block}


def _format_analysis_value(value: Any, _depth: int = 0) -> str:
    """Recursively render an analysis value (str/list/dict) as readable Markdown text.

    Used only for the deterministic fallback path (rule-based analysis dicts),
    where values may be strings, lists of sentence/source_id dicts, or nested
    dicts keyed by risk/valuation category - never a raw Python repr.
    """
    if isinstance(value, str):
        return value.strip() or "资料不足"

    if isinstance(value, list):
        if not value:
            return "资料不足"
        lines = []
        for item in value[:_MAX_ITEMS_PER_SECTION]:
            if isinstance(item, dict) and "text" in item:
                source_id = item.get("source_id")
                suffix = f" [{source_id}]" if source_id else ""
                lines.append(f"- {item['text']}{suffix}")
            elif isinstance(item, str):
                lines.append(f"- {item}")
            else:
                lines.append(f"- {item}")
        return "\n".join(lines)

    if isinstance(value, dict):
        lines = []
        for k, v in value.items():
            if k.startswith("_") or k == "note":
                continue
            rendered = _format_analysis_value(v, _depth + 1)
            if rendered == "资料不足":
                continue
            heading = "###" if _depth == 0 else "-"
            if heading == "###":
                lines.append(f"### {k}\n{rendered}")
            else:
                lines.append(f"- **{k}**: {rendered}")
        return "\n\n".join(lines) if lines else "资料不足"

    return str(value) if value else "资料不足"


class ReportAgent(BaseAgent):
    """Generates the Markdown/HTML report from analysis+sources, then scores its quality."""

    name = "report_agent"
    description = "生成研报并对报告质量做规则化评估"
    tools = ["render_markdown_report", "render_html_report"]

    def execute(self, task: Task, context: dict[str, Any]) -> dict[str, Any]:
        """Dispatch to report generation or quality evaluation based on task_type."""
        if task.task_type == "evaluate":
            return self._evaluate(context)
        return self._generate(task, context)

    # ------------------------------------------------------------------ #
    # Report generation
    # ------------------------------------------------------------------ #
    def _fallback_sections(self, analysis: dict[str, Any]) -> dict[str, str]:
        """Turn the analysis dict into plain-text sections for the deterministic renderer."""
        return {
            "executive_summary": _format_analysis_value(analysis.get("overview", "")),
            "overview": _format_analysis_value(analysis.get("overview", "")),
            "key_trends": _format_analysis_value(analysis.get("key_trends", "")),
            "financial_analysis": _format_analysis_value(analysis.get("financial_analysis", "")),
            "valuation_analysis": _format_analysis_value(analysis.get("valuation_analysis", "")),
            "risk_analysis": _format_analysis_value(analysis.get("risk_analysis", "")),
            "investment_view": _format_analysis_value(analysis.get("investment_view", "")),
        }

    def _validate_markdown(self, content: str) -> bool:
        """Cheap structural check: does the LLM output look like a real report?"""
        if not content or len(content) < 300:
            return False
        hits = sum(1 for h in _REQUIRED_HEADINGS if h in content)
        return hits >= 5

    def _generate(self, task: Task, context: dict[str, Any]) -> dict[str, Any]:
        """Generate the report body via LLM, falling back to a deterministic template."""
        request: ResearchRequest = context["request"]
        analysis: dict[str, Any] = context.get("analysis", {}).get("analysis", {})
        source_excerpts: dict[str, str] = context.get("analysis", {}).get("source_excerpts", {})
        source_dicts = context.get("sources", {}).get("sources", [])
        sources = [Source(**s) for s in source_dicts]

        title = f"{request.topic} 研究报告"
        markdown_content = None

        report_context = build_report_context(
            request, analysis, sources, source_excerpts, config.MAX_REPORT_EXCERPT_CHARS_PER_SOURCE
        )
        report_context_chars = len(report_context["analysis_block"]) + len(report_context["sources_block"])

        try:
            prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
            prompt = prompt_template.format(
                topic=request.topic,
                report_type=request.report_type,
                requirements="、".join(request.requirements) or "未指定",
                analysis_block=report_context["analysis_block"],
                sources_block=report_context["sources_block"],
            )
            raw = self.call_llm(prompt, system="你是专业金融研报撰写助手，只输出Markdown正文。", temperature=0.4)
            candidate = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", raw.strip())
            if self._validate_markdown(candidate):
                markdown_content = candidate
            else:
                logger.warning("report_agent: LLM markdown failed validation, using fallback template")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"report_agent LLM call failed, using fallback template: {exc}")

        if markdown_content is None:
            sections = self._fallback_sections(analysis)
            markdown_content = render_markdown_report(request.topic, sections, sources)

        output_format = task.parameters.get("output_format", request.output_format)
        charts_html = ""
        if output_format == "html":
            # v2 模块4 + v3 阶段B：结构化数据（AkShare）年度序列优先出图，退回
            # 文本抽取；另加估值汇总表。任何一环失败 charts_html 为空串，报告
            # 照常生成——图表是增强，不是硬依赖。
            from tools.chart_renderer import build_charts_html, build_valuation_table_html

            snapshot = context.get("analysis", {}).get("financial_snapshot_full")
            dcf = context.get("analysis", {}).get("dcf_valuation_full")
            rel = analysis.get("relative_valuation")
            charts_html = build_charts_html(sources, normalize_topic(request.topic), snapshot)
            table_html = build_valuation_table_html(dcf, rel)
            charts_html = "\n".join(p for p in (charts_html, table_html) if p)
            final_content = render_html_report(markdown_content, title, extra_html=charts_html)
        else:
            final_content = markdown_content

        return {
            "title": title,
            "markdown_content": markdown_content,
            "final_content": final_content,
            "output_format": output_format,
            "sources": [s.model_dump() for s in sources],
            "report_compression": {"report_context_chars": report_context_chars},
            "chart_embedded": bool(charts_html),
        }

    # ------------------------------------------------------------------ #
    # Quality evaluation (rule-based)
    # ------------------------------------------------------------------ #
    def _evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        """Rule-based quality scoring of the generated report, see evaluate_report_quality."""
        report_data = context.get("report", {})
        markdown_content = report_data.get("markdown_content", "")
        source_dicts = context.get("sources", {}).get("sources", [])
        sources = [Source(**s) for s in source_dicts]
        return {"evaluation": evaluate_report_quality(markdown_content, sources)}


_CITATION_RE = re.compile(r"\[(?:source_)?s\d+\]|\[source_\d+\]")
# A "numeric claim" - digits paired with a unit/scale word that makes it a
# concrete data point (as opposed to e.g. a bare section number like "1.").
_NUMERIC_CLAIM_RE = re.compile(r"\d+(\.\d+)?\s*(%|亿元|万元|亿|万|倍)")

_FINANCIAL_DEPTH_KEYWORDS = ["营收", "收入", "净利润", "毛利率", "现金流", "同比", "亿元", "%"]
_VALUATION_DEPTH_KEYWORDS = ["估值", "PE", "PB", "PS", "市盈率", "市净率", "可比公司", "DCF", "倍"]


def _extract_section(content: str, heading_keyword: str) -> str:
    """Return the body text of the first '## ...heading_keyword...' section."""
    pattern = re.compile(
        rf"^##[^\n]*{re.escape(heading_keyword)}[^\n]*\n(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(content)
    return match.group(1) if match else ""


def _keyword_depth_score(text: str, keywords: list[str], full_credit_hits: int) -> float:
    """Fraction of distinct keywords present, capped at 1.0 once `full_credit_hits` are found."""
    if not text:
        return 0.0
    hits = sum(1 for kw in keywords if kw in text)
    return round(min(1.0, hits / full_credit_hits), 3)


def _bucket_score(count: int, thresholds: list[tuple[int, float]]) -> float:
    """Return the score for the first threshold `count` meets, else 0.0."""
    for min_count, score in thresholds:
        if count >= min_count:
            return score
    return 0.0


def _count_ungrounded_numbers(content: str) -> int:
    """Count sentences with a concrete numeric claim (%, 亿元, 倍, ...) but no [source_x] citation."""
    sentences = re.split(r"[。！？\n]", content)
    return sum(1 for s in sentences if _NUMERIC_CLAIM_RE.search(s) and not _CITATION_RE.search(s))


def _authority_score(sources: list[Source]) -> tuple[float, list[str]]:
    """Average per-source authority weight (official > financial media > whitelisted > other)."""
    if not sources:
        return 0.0, []
    weights = []
    authority_domains = []
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


def evaluate_report_quality(markdown_content: str, sources: list[Source]) -> dict[str, Any]:
    """Score a Markdown report on rule-based criteria and return an aggregate result.

    Deliberately hard to max out: a structurally complete report with thin
    financial/valuation substance, few citations, or ungrounded numeric claims
    should land well below 1.0. Pure heuristics - no LLM call, so this always
    runs even if the model provider is unavailable.
    """
    content = markdown_content or ""
    required_sections = [
        "执行摘要", "概况", "趋势", "财务", "估值", "风险", "投资观点", "参考来源",
    ]
    present = [s for s in required_sections if s in content]
    completeness = len(present) / len(required_sections)

    citation_hits = len(_CITATION_RE.findall(content))
    citation_coverage = min(1.0, citation_hits / max(len(sources), 1))

    heading_count = len(re.findall(r"^##\s", content, flags=re.MULTILINE))
    structure = min(1.0, heading_count / 7)

    sentences = [s for s in re.split(r"[。！？\n]", content) if s.strip()]
    avg_len = sum(len(s) for s in sentences) / max(len(sentences), 1)
    clarity = 1.0 if 8 <= avg_len <= 100 else 0.6

    risk_warning = 1.0 if ("风险" in content and len(content.split("风险提示")[-1]) > 30) else (
        0.5 if "风险" in content else 0.0
    )

    source_grounding = 1.0 if (sources and citation_hits > 0) else (0.3 if sources else 0.0)

    source_count = len(sources)
    source_count_score = (
        1.0 if source_count >= 5 else 0.8 if source_count >= 3 else 0.6 if source_count == 2 else 0.3
    )
    citation_count_score = _bucket_score(citation_hits, [(8, 1.0), (5, 0.8), (3, 0.6), (1, 0.4)])

    financial_section = _extract_section(content, "财务")
    valuation_section = _extract_section(content, "估值")
    risk_section = _extract_section(content, "风险")

    financial_depth_score = _keyword_depth_score(financial_section, _FINANCIAL_DEPTH_KEYWORDS, 4)
    valuation_depth_score = _keyword_depth_score(valuation_section, _VALUATION_DEPTH_KEYWORDS, 3)
    if not risk_section:
        risk_grounding_score = 0.0
    elif _CITATION_RE.search(risk_section):
        risk_grounding_score = 1.0
    else:
        risk_grounding_score = 0.3

    authority_score, authority_domains = _authority_score(sources)

    ungrounded_number_count = _count_ungrounded_numbers(content)
    ungrounded_number_penalty = round(min(0.3, 0.02 * ungrounded_number_count), 3)

    criteria_scores = {
        "completeness": round(completeness, 3),
        "citation_coverage": round(citation_coverage, 3),
        "structure": round(structure, 3),
        "clarity": round(clarity, 3),
        "risk_warning": round(risk_warning, 3),
        "source_grounding": round(source_grounding, 3),
        "source_count_score": round(source_count_score, 3),
        "citation_count_score": round(citation_count_score, 3),
        "financial_depth_score": financial_depth_score,
        "valuation_depth_score": valuation_depth_score,
        "risk_grounding_score": round(risk_grounding_score, 3),
        "authority_score": authority_score,
    }

    overall_score = (
        0.15 * criteria_scores["completeness"]
        + 0.15 * criteria_scores["citation_coverage"]
        + 0.10 * criteria_scores["structure"]
        + 0.10 * criteria_scores["clarity"]
        + 0.10 * criteria_scores["risk_warning"]
        + 0.15 * criteria_scores["source_grounding"]
        + 0.10 * criteria_scores["financial_depth_score"]
        + 0.10 * criteria_scores["valuation_depth_score"]
        + 0.05 * criteria_scores["authority_score"]
        - ungrounded_number_penalty
    )

    # Hard caps: a weighted average can still land near 1.0 even when one
    # dimension is genuinely weak (e.g. thin valuation section dragged up by
    # everything else scoring well, or just one source total). Cap
    # overall_score so no single blind spot is masked by the rest of the
    # report looking polished. Collected as (cap, reason) so the binding
    # constraint can be surfaced in diagnostics instead of silently applied.
    cap_candidates: list[tuple[float, Optional[str]]] = [(1.0, None)]
    if source_count == 0:
        cap_candidates.append((0.3, "source_count=0"))
    elif source_count == 1:
        cap_candidates.append((0.5, "source_count=1"))
    elif source_count == 2:
        cap_candidates.append((0.6, "source_count=2"))
    if source_count < 3:
        cap_candidates.append((0.6, "source_count<3"))
    if source_count < 5:
        cap_candidates.append((0.85, "source_count<5"))
    if citation_hits < 5:
        cap_candidates.append((0.8, "citation_hits<5"))
    if ungrounded_number_count > 0:
        cap_candidates.append((0.8, "ungrounded_number_count>0"))
    if valuation_depth_score < 0.6:
        cap_candidates.append((0.85, "valuation_depth_score<0.6"))
    if financial_depth_score < 0.6:
        cap_candidates.append((0.85, "financial_depth_score<0.6"))
    if financial_depth_score == 0.0:
        cap_candidates.append((0.75, "financial_depth_score=0"))

    cap, cap_reason = min(cap_candidates, key=lambda c: c[0])
    overall_score = round(max(0.0, min(overall_score, cap)), 3)
    source_count_cap_applied = bool(cap_reason and cap_reason.startswith("source_count"))

    strengths, weaknesses, recommendations = [], [], []
    for key, val in criteria_scores.items():
        if val >= 0.8:
            strengths.append(f"{key} 表现良好（{val}）")
        elif val <= 0.4:
            weaknesses.append(f"{key} 有待改进（{val}）")
            recommendations.append(f"建议加强 {key} 相关内容")

    if len(present) < len(required_sections):
        missing = [s for s in required_sections if s not in present]
        weaknesses.append(f"缺少章节: {', '.join(missing)}")

    if ungrounded_number_count > 0:
        weaknesses.append(f"发现 {ungrounded_number_count} 处未标注来源的数字表述")
        recommendations.append("为报告中的具体数字/百分比补充 [source_x] 引用")

    if valuation_depth_score < 0.6:
        recommendations.append("补充 PE/PB/可比公司等量化估值指标，避免估值分析流于空泛表述")

    if financial_depth_score == 0.0:
        weaknesses.append("财务深度不足，缺少营收、净利润、毛利率等关键财务信息")
    if financial_depth_score < 0.6:
        recommendations.append("补充营收、净利润、毛利率等具体财务数字")

    if risk_grounding_score < 1.0 and risk_section:
        recommendations.append("风险提示章节应引用具体来源（[source_x]），而不是泛泛而谈")

    if source_count < 5:
        weaknesses.append("来源数量不足，报告可靠性受限")

    if authority_score < 0.4:
        weaknesses.append("权威来源占比较低，建议补充公告、年报或主流财经数据来源")

    return {
        "overall_score": overall_score,
        "criteria_scores": criteria_scores,
        "diagnostics": {
            "source_count": source_count,
            "citation_count": citation_hits,
            "ungrounded_number_count": ungrounded_number_count,
            "report_length": len(content),
            "authority_domains": authority_domains,
            "source_count_cap_applied": source_count_cap_applied,
            "score_cap_reason": cap_reason,
        },
        "strengths": strengths,
        "weaknesses": weaknesses,
        "recommendations": recommendations,
    }
