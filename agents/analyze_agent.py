"""Analyze agent: rule-based extraction + LLM synthesis over selected sources."""
import json
from typing import Any

from config import config
from schemas.request import ResearchRequest
from schemas.source import Source
from schemas.task import Task
from tools.financial_analyzer import summarize_financial_points
from tools.risk_analyzer import detect_risks
from tools.valuation_analyzer import detect_valuation_signals
from tools.valuation_dcf import run_dcf_for_sources
from utils.logger import logger
from utils.text_utils import extract_relevant_excerpt, normalize_topic

from .base_agent import BaseAgent

_PROMPT_PATH = config.PROMPTS_DIR / "analysis_prompt.txt"

_REQUIRED_FIELDS = [
    "overview",
    "key_trends",
    "financial_analysis",
    "valuation_analysis",
    "risk_analysis",
    "investment_view",
    "data_limitations",
]


def build_analysis_context(
    sources: list[Source], max_chars_per_source: int = 1800
) -> tuple[str, dict[str, str]]:
    """Build the (compact) sources block fed to the analysis LLM prompt.

    Instead of dumping each source's full page text, keep only the sentences
    that actually mention financial-context keywords (see
    utils.text_utils.extract_relevant_excerpt), capped per source. Returns
    both the assembled prompt block and a {source_id: excerpt} map so
    ReportAgent can reuse the same excerpts later instead of recomputing them
    from scratch.
    """
    excerpts: dict[str, str] = {}
    blocks = []
    for src in sources:
        excerpt = extract_relevant_excerpt(src.content, max_chars=max_chars_per_source)
        excerpts[src.source_id] = excerpt
        blocks.append(f"[{src.source_id}] title={src.title} url={src.url}\n关键信息摘录：{excerpt}\n")
    block_text = "\n".join(blocks) if blocks else "（无有效来源）"
    return block_text, excerpts


class AnalyzeAgent(BaseAgent):
    """Combines keyword-rule extraction (financial/risk/valuation) with an LLM summary."""

    name = "analyze_agent"
    description = "对筛选后的来源做财务、趋势、估值、风险等结构化分析"
    tools = ["summarize_financial_points", "detect_risks", "detect_valuation_signals"]

    def _rule_based_analysis(self, sources: list[Source]) -> dict[str, Any]:
        """Run pure keyword-rule extraction across all sources (no LLM)."""
        financial = summarize_financial_points(sources)

        combined_text = "\n".join(s.content for s in sources)
        risks = detect_risks(combined_text)
        valuation = detect_valuation_signals(combined_text)

        return {"financial": financial, "risks": risks, "valuation": valuation}

    def _fallback_analysis(self, rule_result: dict[str, Any], sources: list[Source]) -> dict[str, Any]:
        """Build a conservative analysis dict purely from rule hints when the LLM fails."""
        has_sources = bool(sources)
        note = "" if has_sources else "资料不足"

        risk_cats = {k: v for k, v in rule_result["risks"].items() if v}
        valuation_cats = {k: v for k, v in rule_result["valuation"].items() if v}

        return {
            "overview": note or "基于已采集来源的摘要请参见财务与趋势要点，资料未经LLM总结。",
            "key_trends": [p["text"] for p in rule_result["financial"]["key_financial_points"][:8]] or ["资料不足"],
            "financial_analysis": rule_result["financial"] or {"note": "资料不足"},
            "valuation_analysis": valuation_cats or {"note": "资料不足"},
            "risk_analysis": risk_cats or {"note": "资料不足"},
            "investment_view": "资料不足，暂无法给出投资观点（LLM总结不可用，仅提供规则抽取结果）。",
            "data_limitations": "LLM 分析调用失败或输出不可用，本次分析仅基于关键词规则抽取。",
        }

    def execute(self, task: Task, context: dict[str, Any]) -> dict[str, Any]:
        """Run rule-based extraction, then ask the LLM to synthesize a structured analysis."""
        request: ResearchRequest = context["request"]
        source_dicts = context.get("sources", {}).get("sources", [])
        sources = [Source(**s) for s in source_dicts]

        rule_result = self._rule_based_analysis(sources)

        original_sources_chars = sum(len(s.content) for s in sources)
        max_chars_per_source = config.MAX_ANALYSIS_CHARS_PER_SOURCE
        sources_block, source_excerpts = build_analysis_context(sources, max_chars_per_source)
        analysis_context_chars = len(sources_block)
        compression_ratio = (
            round(analysis_context_chars / original_sources_chars, 4) if original_sources_chars else 0.0
        )

        try:
            prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
            prompt = prompt_template.format(
                topic=request.topic,
                requirements="、".join(request.requirements) or "未指定",
                sources_block=sources_block,
                rule_hints_block=json.dumps(rule_result, ensure_ascii=False)[:4000],
            )
            raw = self.call_llm(prompt, system="你是严谨的金融分析助手，只输出JSON，不编造数据。")
            parsed = self.parse_json_response(raw)

            if isinstance(parsed, dict) and all(f in parsed for f in _REQUIRED_FIELDS):
                analysis = parsed
            else:
                logger.warning("analyze_agent: LLM output missing required fields, using fallback")
                analysis = self._fallback_analysis(rule_result, sources)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"analyze_agent LLM call failed, using fallback: {exc}")
            analysis = self._fallback_analysis(rule_result, sources)

        analysis["_rule_hints"] = rule_result

        # v2 模块3：显式调用 DCF 估值工具（tools/valuation_dcf.py），结果作为
        # 独立字段挂到 analysis 上，而不是混进 LLM 的综合总结——计算过程是
        # 确定性代码，参数出处（extracted/proxy/default）逐项可查。挂在 LLM
        # 综合之后：即使 LLM 走了 fallback 路径，估值模块照常工作。
        # 只对公司研究运行——行业主题没有单一的现金流主体，DCF 无意义。
        dcf_full = None
        if request.report_type == "company_research" and sources:
            dcf_full = run_dcf_for_sources(sources, normalize_topic(request.topic))
            if dcf_full is not None:
                # 报告上下文有 6000 字符预算，挂精简版（估值区间 + 参数 + 出处
                # 标注）；含逐年现值明细的完整版进任务结果 -> trace，可复查。
                base = dcf_full["scenarios"]["base"]["inputs"]
                analysis["dcf_valuation"] = {
                    "valuation_range": dcf_full["valuation_range"],
                    "inputs": base,
                    "inputs_provenance": {
                        k: {sk: sv for sk, sv in v.items() if sk in ("kind", "value", "source_id", "basis")}
                        for k, v in dcf_full["inputs_provenance"].items()
                    },
                    "note": dcf_full["note"],
                }
                logger.info(
                    f"DCF valuation: {dcf_full['valuation_range']} "
                    f"(base_fcf {dcf_full['inputs_provenance']['base_fcf']['kind']})"
                )

        result: dict[str, Any] = {
            "analysis": analysis,
            "source_excerpts": source_excerpts,
            "compression_metrics": {
                "original_sources_chars": original_sources_chars,
                "analysis_context_chars": analysis_context_chars,
                "compression_ratio": compression_ratio,
            },
        }
        if dcf_full is not None:
            result["dcf_valuation_full"] = dcf_full
        return result
