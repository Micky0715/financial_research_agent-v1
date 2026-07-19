"""Formal research report assembly with full disclosure sections (v4 stage E).

把一次 pipeline run 的产物（正文报告 + 结构化分析 + 来源 + 评估）包装成
带 20 项披露要素的正式研报。合规检查失败时仍可产出，但文件名加 DRAFT_
前缀、报告顶部加 DRAFT / INCOMPLETE 横幅——不产出看似正式的不完整报告。

红线：不冒充持牌证券研究报告；分析师与执业编号为占位说明；明确本报告由
自动化系统生成、不构成投资建议。
"""
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import config
from schemas.source import Source
from tools.report_compliance_checker import check_compliance
from utils.logger import logger

_TYPE_ZH = {"company_research": "公司研究", "industry_research": "行业研究",
            "macro_research": "宏观研究", "risk_research": "风险研究",
            "valuation_research": "估值研究"}

_DRAFT_BANNER = """> ⚠️ **DRAFT / INCOMPLETE —— 草稿，未通过合规检查**
> 未通过项：{failures}。本文件不得作为正式研究报告使用或传播。

"""

_FIXED_SECTIONS = """## 投资评级

本系统**不提供投资评级**（无买入/增持/中性/减持评级体系）。文中一切估值结果
均为工具计算与假设条件的演示。

## 分析师声明（占位说明）

本报告由自动化研究系统（financial_research_agent）生成，**没有持牌证券分析师
参与撰写或复核**；"分析师姓名/执业编号"栏目为披露模板占位项，不适用于本系统。
本报告不代表任何持牌机构或证券分析师意见。

## 利益冲突声明

本系统及其运行者不持有报告所涉证券头寸，不存在与研究对象的业务往来、雇佣或
其他利益关系；数据全部来自免费公开渠道，未接受任何机构的付费委托。

## 免责声明

本报告由自动化研究系统基于免费公开信息生成，不构成投资建议、要约或招揽；
估值依赖假设条件，结果仅供技术研究演示；系统不能完全避免信息滞后与模型误差，
使用者应自行核实并承担全部决策风险。

## 报告传播与使用限制

本报告仅供学习与技术研究使用，不得用于商业分发、不得冒充持牌机构研究产品、
不得删除本节与免责声明后传播。引用时须注明由自动化系统生成。
"""


def _load_run_payload(run_id: str) -> Optional[dict]:
    """从 outputs/ 读取一次 run 的 report/sources/evaluation/trace。"""
    try:
        import re

        trace_path = next(config.TRACES_DIR.glob(f"{run_id}_*_trace.json"))
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        sources_path = next(config.SOURCES_DIR.glob(f"{run_id}_*_sources.json"))
        sources = json.loads(sources_path.read_text(encoding="utf-8"))
        eval_paths = list(config.EVALUATIONS_DIR.glob(f"{run_id}_*_evaluation.json"))
        evaluation = json.loads(eval_paths[0].read_text(encoding="utf-8")) if eval_paths else {}

        report_md = ""
        md_files = sorted(config.REPORTS_DIR.glob(f"{run_id}_*_report.md"))
        html_files = sorted(config.REPORTS_DIR.glob(f"{run_id}_*_report.html"))
        if md_files:
            report_md = md_files[0].read_text(encoding="utf-8", errors="ignore")
        elif html_files:  # html-only run：剥标签保留标题结构（与 export_reports 同策略）
            text = html_files[0].read_text(encoding="utf-8", errors="ignore")
            text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", text)
            text = re.sub(r"<h2[^>]*>", "\n## ", text)
            text = re.sub(r"<h3[^>]*>", "\n### ", text)
            text = re.sub(r"<li[^>]*>", "\n- ", text)
            report_md = re.sub(r"<[^>]+>", "", text)
        return {"trace": trace, "sources": sources, "evaluation": evaluation,
                "report_md": report_md}
    except (StopIteration, OSError, json.JSONDecodeError) as exc:
        logger.warning(f"formal report: cannot load run {run_id}: {exc!r}")
        return None


def build_formal_report(run_id: str, output_dir: Optional[Path] = None) -> dict[str, Any]:
    """主入口：run_id -> 正式披露报告（markdown 落盘 + 合规结果）。"""
    payload = _load_run_payload(run_id)
    if payload is None:
        return {"success": False, "error": f"run {run_id} artifacts not found"}

    trace, sources_raw = payload["trace"], payload["sources"]
    sources = [Source(**s) for s in sources_raw]
    topic = trace.get("topic", "")
    report_type = (payload["evaluation"] or {}).get("report_type") \
        or trace.get("report_type") or "company_research"
    report_date = datetime.now().strftime("%Y-%m-%d")

    symbol = ""
    entity_validation = trace.get("entity_validation") or {}
    structured = trace.get("structured_data_metrics") or {}
    symbol = structured.get("symbol") or entity_validation.get("symbol") or ""

    core_body = payload["report_md"].strip()
    if core_body.startswith("# "):
        core_body = "\n".join(core_body.splitlines()[1:]).strip()

    sources_md = "\n".join(
        f"- [{s.source_id}] {s.title}（{s.url}，权威等级 {s.authority_tier}）" for s in sources
    ) or "（无网页来源，详见正文数据来源标注）"

    quality = (payload["evaluation"] or {}).get("overall_score")
    meta_lines = [
        f"- **研究对象**：{topic}" + (f"（证券代码：{symbol}）" if symbol else ""),
        f"- **报告日期**：{report_date}",
        f"- **报告类型**：{_TYPE_ZH.get(report_type, report_type)}（自动化系统生成）",
        f"- **运行标识**：run_id={run_id}" + (f"，质量评分 {quality}" if quality is not None else ""),
    ]

    markdown = f"""# {topic} 正式研究报告

{chr(10).join(meta_lines)}

## 研究逻辑与方法说明

本报告由受控多智能体流水线生成：检索与来源筛选（含权威分级）→ 结构化数据
获取（AkShare 免费公开接口）→ 规则+LLM 分析 → 报告生成与数字级溯源检查。
方法限制：数据存在披露滞后；估值与情景为假设驱动的工具演示；文本抽取与
规则判断可能遗漏语义信息。

{core_body}

## 数据来源（网页/结构化）

{sources_md}
- 结构化数据：AkShare（新浪财经/百度股市通/国家统计局等免费公开转载源），
  各数字所在句均带来源标注；抓取时间见正文。

{_FIXED_SECTIONS}"""

    compliance = check_compliance(markdown, sources,
                                  {"report_type": report_type, "symbol": symbol,
                                   "report_date": report_date})
    if not compliance["passed"]:
        markdown = _DRAFT_BANNER.format(failures="、".join(compliance["failures"])) + markdown

    out_dir = output_dir or (config.OUTPUT_DIR / "formal_reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = "" if compliance["passed"] else "DRAFT_"
    path = out_dir / f"{prefix}formal_{run_id}.md"
    path.write_text(markdown, encoding="utf-8")
    logger.info(f"formal report {run_id}: compliance={'PASS' if compliance['passed'] else 'DRAFT'} -> {path}")
    return {"success": True, "path": str(path), "compliance": compliance,
            "is_draft": not compliance["passed"], "markdown": markdown}
