"""Analyze agent: rule-based extraction + LLM synthesis over selected sources."""
import json
from typing import Any, Optional

from config import config
from schemas.request import ResearchRequest
from schemas.source import Source
from schemas.task import Task
from tools.financial_analyzer import summarize_financial_points
from tools.risk_analyzer import detect_risks
from tools.valuation_analyzer import detect_valuation_signals
from tools.valuation_dcf import run_dcf_for_sources
from tools.valuation_relative import relative_valuation
from utils.logger import logger
from utils.text_utils import extract_relevant_excerpt, normalize_topic


def _fetch_structured_data(subject: str) -> tuple[Optional[dict], dict]:
    """AkShare 结构化数据（v3）：经 tool_gateway（含 MCP 路由）获取快照。

    永不抛异常；返回 (snapshot|None, akshare_metrics)。快照仅对成功获取的
    公司返回；失败/降级细节进 metrics -> trace，主流程不受影响。
    """
    metrics: dict = {"attempted": True, "success": False, "degraded": True, "symbol": None,
                     "error": None, "missing_fields": [], "error_count": 0}
    try:
        from tools.tool_gateway import fetch_financial_snapshot

        envelope = fetch_financial_snapshot(subject)
        metrics.update({
            "success": bool(envelope.get("success")),
            "degraded": bool(envelope.get("degraded")),
            "symbol": envelope.get("symbol"),
            "error": envelope.get("error"),
            "error_count": len(envelope.get("errors") or []),
        })
        snapshot = envelope.get("snapshot")
        if snapshot:
            metrics["missing_fields"] = snapshot.get("missing_fields", [])
        return snapshot, metrics
    except Exception as exc:  # noqa: BLE001 - structured data is an enhancement, never a dependency
        metrics["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning(f"structured financial data fetch failed (degraded): {exc}")
        return None, metrics


def _structured_data_block(snapshot: Optional[dict]) -> str:
    """结构化数据的 prompt 注入块。没有快照时明确说明，不留空白让模型猜。"""
    if not snapshot:
        return "（本次未获取到结构化金融数据，分析仅基于上方网页/PDF资料）"
    import json as _json

    compact = {k: snapshot.get(k) for k in (
        "symbol", "company_name", "period", "revenue", "net_profit", "gross_margin",
        "roe", "operating_cash_flow", "debt_ratio", "pe", "pb", "ps", "market_cap", "price",
    )}
    compact["缺失字段"] = snapshot.get("missing_fields", [])
    return (
        "以下为 AkShare 结构化金融数据（金额单位：亿元；引用这些数字时注明来源为 AkShare，"
        "缺失字段不要编造）：\n" + _json.dumps(compact, ensure_ascii=False)
    )

def _run_macro_chain(sources: list[Source]) -> tuple[Optional[dict], dict]:
    """v4 阶段A：宏观数据链路（指标快照 + 政策解析 + 传导链 + 灰犀牛）。

    永不抛异常；返回 (bundle|None, metrics)。指标获取失败时 bundle 为 None，
    宏观报告退化为纯网页来源综述（metrics 记录原因）。
    """
    metrics: dict = {"attempted": True, "success": False, "degraded": True,
                     "indicator_count": 0, "missing_indicators": [], "error": None,
                     "policy_docs_parsed": 0, "transmission_paths": 0}
    try:
        from schemas.macro_data import MacroSnapshot
        from tools.grey_rhino_monitor import monitor_grey_rhinos
        from tools.macro_data_collector import fetch_macro_snapshot
        from tools.macro_transmission_model import build_transmission_paths
        from tools.policy_document_parser import parse_policy_document

        envelope = fetch_macro_snapshot()
        metrics.update({
            "success": bool(envelope.get("success")),
            "degraded": bool(envelope.get("degraded")),
            "error": envelope.get("error"),
        })
        if not envelope.get("snapshot"):
            return None, metrics
        snapshot = MacroSnapshot(**envelope["snapshot"])
        metrics["indicator_count"] = len(snapshot.indicators)
        metrics["missing_indicators"] = snapshot.missing_indicators

        # 政策文本解析：仅对标题带政策特征的来源（最多2个，控制 LLM 成本）
        policy_cues = ("政策", "通知", "意见", "规划", "会议", "央行", "人民银行",
                       "国务院", "部署", "工作报告", "发改委", "财政部")
        policies = []
        for src in sources:
            if len(policies) >= 2:
                break
            if any(c in (src.title or "") for c in policy_cues) and len(src.content or "") > 200:
                policies.append(parse_policy_document(
                    src.content[:6000], title=src.title, source_id=src.source_id))
        metrics["policy_docs_parsed"] = len(policies)

        paths = build_transmission_paths(snapshot)
        metrics["transmission_paths"] = len(paths)
        risks = monitor_grey_rhinos(snapshot)

        bundle = {
            "macro_snapshot": snapshot.model_dump(),
            "policy_analysis": [p.model_dump() for p in policies],
            "transmission_paths": [p.model_dump() for p in paths],
            "grey_rhino_risks": [r.model_dump() for r in risks],
        }
        return bundle, metrics
    except Exception as exc:  # noqa: BLE001 - macro chain is an enhancement, never a dependency
        metrics["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        logger.warning(f"macro chain failed (degraded): {exc!r}")
        return None, metrics


def _macro_data_block(bundle: Optional[dict]) -> str:
    """宏观结构化数据的 prompt 注入块（精简：指标最新值 + 高风险项）。"""
    if not bundle:
        return "（本次未获取到结构化宏观指标，分析仅基于上方网页/PDF资料）"
    import json as _json

    snap = bundle["macro_snapshot"]
    compact = {
        p["indicator_name"]: f"{p['value']}{p['unit']}@{p['period']}(前值{p['previous_value']})"
        for p in snap["indicators"].values()
    }
    rhinos = {r["risk_name"]: r["risk_level"] for r in bundle.get("grey_rhino_risks", [])
              if r["risk_level"] in ("medium", "high")}
    return (
        "以下为 AkShare 结构化宏观指标（引用时注明来源为 AkShare 及原始机构，缺失指标不要编造）：\n"
        + _json.dumps(compact, ensure_ascii=False)
        + "\n规则监控中高风险项：" + _json.dumps(rhinos, ensure_ascii=False)
        + "\n缺失指标：" + "、".join(snap.get("missing_indicators", []))
    )


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

        # v3 阶段A：结构化金融数据（AkShare）——增强不依赖，失败自动降级为
        # 纯网页/PDF 分析。只对公司研究获取（行业主题没有单一标的）。
        subject = normalize_topic(request.topic)
        snapshot: Optional[dict] = None
        akshare_metrics: dict = {"attempted": False}
        if request.report_type == "company_research":
            snapshot, akshare_metrics = _fetch_structured_data(subject)

        # v4 阶段A：宏观研究走宏观数据链路（指标/政策/传导/灰犀牛）
        macro_bundle: Optional[dict] = None
        macro_metrics: dict = {"attempted": False}
        if request.report_type == "macro_research":
            macro_bundle, macro_metrics = _run_macro_chain(sources)

        try:
            prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
            prompt = prompt_template.format(
                topic=request.topic,
                requirements="、".join(request.requirements) or "未指定",
                sources_block=sources_block,
                rule_hints_block=json.dumps(rule_result, ensure_ascii=False)[:4000],
                structured_data_block=(
                    _macro_data_block(macro_bundle)
                    if request.report_type == "macro_research"
                    else _structured_data_block(snapshot)
                ),
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

        # v3：结构化数据摘要 + 相对估值挂到 analysis（报告可引用；缺失如实标注）
        if snapshot:
            analysis["structured_financial_data"] = {
                "provider": "akshare",
                "symbol": snapshot.get("symbol"),
                "period": snapshot.get("period"),
                "revenue": snapshot.get("revenue"),
                "net_profit": snapshot.get("net_profit"),
                "gross_margin": snapshot.get("gross_margin"),
                "roe": snapshot.get("roe"),
                "operating_cash_flow": snapshot.get("operating_cash_flow"),
                "debt_ratio": snapshot.get("debt_ratio"),
                "pe": snapshot.get("pe"),
                "pb": snapshot.get("pb"),
                "ps": snapshot.get("ps"),
                "market_cap": snapshot.get("market_cap"),
                "price": snapshot.get("price"),
                "missing_fields": snapshot.get("missing_fields", []),
            }
            rel = relative_valuation(snapshot)
            analysis["relative_valuation"] = rel
            logger.info(
                f"relative valuation: "
                f"{ {k: v.get('valuation_signal') for k, v in rel['multiples'].items()} }"
            )

        # v2 模块3：显式调用 DCF 估值工具（tools/valuation_dcf.py），结果作为
        # 独立字段挂到 analysis 上，而不是混进 LLM 的综合总结——计算过程是
        # 确定性代码，参数出处逐项可查。挂在 LLM 综合之后：即使 LLM 走了
        # fallback 路径，估值模块照常工作。
        # 只对公司研究运行——行业主题没有单一的现金流主体，DCF 无意义。
        dcf_full = None
        if request.report_type == "company_research" and sources:
            dcf_full = run_dcf_for_sources(sources, subject, snapshot)
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

        # v4：宏观 bundle 精简版挂 analysis（LLM 可引用），完整版进结果 -> trace/报告
        if macro_bundle is not None:
            snap = macro_bundle["macro_snapshot"]
            analysis["macro_indicators"] = {
                p["indicator_name"]: {"value": p["value"], "unit": p["unit"],
                                      "period": p["period"], "source": p["source_name"]}
                for p in snap["indicators"].values()
            }
            analysis["grey_rhino_summary"] = {
                r["risk_name"]: r["risk_level"] for r in macro_bundle["grey_rhino_risks"]
            }

        result: dict[str, Any] = {
            "analysis": analysis,
            "source_excerpts": source_excerpts,
            "compression_metrics": {
                "original_sources_chars": original_sources_chars,
                "analysis_context_chars": analysis_context_chars,
                "compression_ratio": compression_ratio,
            },
            "akshare_metrics": akshare_metrics,
            "macro_metrics": macro_metrics,
        }
        if snapshot is not None:
            result["financial_snapshot_full"] = snapshot
        if dcf_full is not None:
            result["dcf_valuation_full"] = dcf_full
        if macro_bundle is not None:
            result["macro_bundle_full"] = macro_bundle
        return result
