"""Report agent: generates the final report (LLM + fallback) and evaluates its quality."""
import re
from typing import Any, Optional

from config import config
from schemas.request import ResearchRequest
from schemas.source import Source
from schemas.task import Task
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

        # v4 阶段A：宏观研究走专用组装路径（硬数据段落确定性渲染 + LLM 解读段落）
        macro_bundle = context.get("analysis", {}).get("macro_bundle_full")
        if request.report_type == "macro_research" and macro_bundle:
            return self._generate_macro(task, request, macro_bundle, sources)

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
            subject = normalize_topic(request.topic)
            charts_html = build_charts_html(sources, subject, snapshot)
            table_html = build_valuation_table_html(dcf, rel)
            extra_charts = ""
            chart_metas: list = []
            try:
                if request.report_type == "company_research" and snapshot:
                    from tools.market_chart_builder import build_market_charts_html

                    extra_charts, chart_metas = build_market_charts_html(subject, snapshot)
                elif request.report_type == "industry_research":
                    from tools.industry_chart_builder import build_industry_charts_html

                    industry_bundle = context.get("analysis", {}).get("industry_deep_full")
                    if industry_bundle:
                        extra_charts = build_industry_charts_html(industry_bundle)
            except Exception as exc:  # noqa: BLE001 - v4 charts are an enhancement
                logger.warning(f"v4 charts failed (degraded): {exc!r}")
            if chart_metas:
                try:
                    from tools.chart_consistency_checker import check_chart_consistency

                    consistency = check_chart_consistency(
                        chart_metas, expected_subject=subject,
                        reference_values={"latest_pe": snapshot.get("pe"),
                                          "latest_pb": snapshot.get("pb")} if snapshot else None,
                        report_text=markdown_content)
                    if not consistency["all_consistent"]:
                        logger.warning(f"chart consistency issues: {consistency['results']}")
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"chart consistency check failed (degraded): {exc!r}")
            charts_html = "\n".join(p for p in (charts_html, extra_charts, table_html) if p)
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
    # Macro report generation (v4 stage A)
    # ------------------------------------------------------------------ #
    def _generate_macro(self, task: Task, request: ResearchRequest,
                        macro_bundle: dict, sources: list[Source]) -> dict[str, Any]:
        """宏观报告：指标表/政策/传导/灰犀牛由结构化数据确定性渲染（无数字幻觉），
        核心结论/国内外对比/资产影响由 LLM 撰写，失败自动用规则文本兜底。"""
        from tools.macro_report_builder import assemble_macro_report

        llm_sections: dict[str, str] = {}
        try:
            from agents.analyze_agent import _macro_data_block

            prompt_template = (config.PROMPTS_DIR / "macro_report_prompt.txt").read_text(encoding="utf-8")
            sources_lines = "\n".join(
                f"[{s.source_id}] {s.title} - {s.url}\n摘录：{extract_relevant_excerpt(s.content, max_chars=400)}"
                for s in sources
            ) or "（无有效来源）"
            prompt = prompt_template.format(
                topic=request.topic,
                requirements="、".join(request.requirements) or "未指定",
                macro_block=_macro_data_block(macro_bundle),
                sources_block=sources_lines,
            )
            raw = self.call_llm(prompt, system="你是严谨的宏观研究助理，只输出JSON，不编造数据。", temperature=0.3)
            parsed = self.parse_json_response(raw)
            if isinstance(parsed, dict):
                llm_sections = {k: str(v) for k, v in parsed.items() if isinstance(v, str) and v.strip()}
        except Exception as exc:  # noqa: BLE001 - deterministic fallback sections exist
            logger.warning(f"macro report LLM sections failed, using rule fallback: {exc!r}")

        sources_md = "## 参考来源\n\n" + ("\n".join(
            f"- [{s.source_id}] {s.title}（{s.url}，权威等级：{s.authority_tier}）" for s in sources
        ) if sources else "（无网页来源，本报告基于结构化宏观数据）")

        markdown_content = assemble_macro_report(request.topic, llm_sections, macro_bundle, sources_md)

        output_format = task.parameters.get("output_format", request.output_format)
        charts_html = ""
        if output_format == "html":
            try:
                from tools.macro_chart_builder import build_macro_charts_html

                charts_html = build_macro_charts_html(macro_bundle["macro_snapshot"])
            except Exception as exc:  # noqa: BLE001 - charts are an enhancement
                logger.warning(f"macro charts failed (degraded): {exc!r}")
            final_content = render_html_report(markdown_content, f"{request.topic} 宏观研究报告",
                                               extra_html=charts_html)
        else:
            final_content = markdown_content

        return {
            "title": f"{request.topic} 宏观研究报告",
            "markdown_content": markdown_content,
            "final_content": final_content,
            "output_format": output_format,
            "sources": [s.model_dump() for s in sources],
            "report_compression": {"report_context_chars": 0},
            "chart_embedded": bool(charts_html),
            "llm_sections_used": sorted(llm_sections.keys()),
        }

    # ------------------------------------------------------------------ #
    # Quality evaluation (rule-based)
    # ------------------------------------------------------------------ #
    def _evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        """Rule-based quality scoring, dispatched by report_type (v3 stage E).

        See evaluators/report_evaluator.py - industry reports are no longer
        scored (and capped) on company-style financial-depth keywords.
        """
        from evaluators.report_evaluator import evaluate_report

        report_data = context.get("report", {})
        markdown_content = report_data.get("markdown_content", "")
        source_dicts = context.get("sources", {}).get("sources", [])
        sources = [Source(**s) for s in source_dicts]
        request = context.get("request")
        report_type = getattr(request, "report_type", "company_research")
        evaluation = evaluate_report(
            markdown_content, sources, report_type, analysis=context.get("analysis")
        )
        # v3 阶段H：实体验证状态透传到评估诊断（verified/weak；failed 的 run
        # 根本到不了 evaluate 阶段，见 orchestrator 的终止路径）
        entity_validation = context.get("entity_validation")
        if entity_validation:
            evaluation["diagnostics"]["entity_validation_status"] = entity_validation.get("validation_status")
        return {"evaluation": evaluation}


def evaluate_report_quality(markdown_content: str, sources: list[Source]) -> dict[str, Any]:
    """Backward-compat wrapper (v2 signature): delegates to the report-type-aware
    evaluator with company_research semantics. New code should call
    evaluators.report_evaluator.evaluate_report directly."""
    from evaluators.report_evaluator import evaluate_report

    return evaluate_report(markdown_content, sources, "company_research")
