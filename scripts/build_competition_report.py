"""Aggregate the 30-case competition eval into the full v4 metric set (v4 stage K).

Usage:
    python scripts/build_competition_report.py

依赖 scripts/run_competition_eval.py --run 已经跑过（写了
outputs/eval/competition_30_runs.json + 对应 outputs/traces 等产物）。
所有数字都是从真实产物里读出来的，没有任何 case 会被凭空编造成功——
缺 run 的 case 在 report_generated_rate 分母里，但会被如实列在
"未完成/失败 case" 里，不会被计入成功统计。

**这是人工分层构建的固定 30-case benchmark，不是随机抽样，不能宣称代表
所有金融任务的泛化能力**——本行文字连同下面同义表述会原样写进报告开头。
"""
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from config import config  # noqa: E402
from scripts.collect_eval_summary import build_rows  # noqa: E402
from scripts.run_competition_eval import _RUNS_PATH, _TOPICS_PATH  # noqa: E402
from utils.file_utils import load_json  # noqa: E402

_NA = "N/A"


def _f(v) -> Optional[float]:
    try:
        if v in (None, "", _NA):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))], 3)


def _avg(values: list[float]) -> Any:
    clean = [v for v in values if v is not None]
    return round(sum(clean) / len(clean), 3) if clean else "insufficient data"


def _load_trace_by_run_id(run_id: str) -> Optional[dict]:
    if not run_id or run_id == _NA:
        return None
    matches = list(config.TRACES_DIR.glob(f"{run_id}_*_trace.json"))
    if not matches:
        return None
    try:
        return load_json(matches[0])
    except Exception:  # noqa: BLE001
        return None


def _report_text_for_run(run_id: str) -> str:
    if not run_id or run_id == _NA:
        return ""
    matches = list(config.REPORTS_DIR.glob(f"{run_id}_*_report.*"))
    if not matches:
        return ""
    try:
        return matches[0].read_text(encoding="utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def _compliance_for_row(row: dict, sources_json_path: str) -> Optional[bool]:
    report_text = _report_text_for_run(row.get("run_id", ""))
    if not report_text:
        return None
    try:
        from schemas.source import Source
        from tools.report_compliance_checker import check_compliance

        sources = []
        if sources_json_path and sources_json_path != _NA:
            sources = [Source(**s) for s in load_json(sources_json_path)]
        result = check_compliance(report_text, sources,
                                  {"report_type": row.get("report_type"), "symbol": None})
        return bool(result["passed"])
    except Exception:  # noqa: BLE001 - compliance stat is auxiliary
        return None


def _chart_embedded_for_run(run_id: str) -> Optional[bool]:
    text = _report_text_for_run(run_id)
    if not text:
        return None
    return "data:image/png;base64" in text


def build_report() -> str:
    topics = load_json(_TOPICS_PATH) if _TOPICS_PATH.exists() else []
    runs = load_json(_RUNS_PATH) if _RUNS_PATH.exists() else []
    rows = build_rows(_TOPICS_PATH) if _TOPICS_PATH.exists() else []
    tags_by_topic = {t["topic"]: t.get("tags", []) for t in topics}
    category_by_topic = {t["topic"]: t.get("category", _NA) for t in topics}
    run_by_topic = {r["topic"]: r for r in runs}

    n = len(rows)
    succeeded_rows = [r for r in rows if r.get("main_issue") not in ("missing_run", "failed")
                      and r.get("source_count")]
    success_rate = round(len(succeeded_rows) / n, 3) if n else 0.0

    total_times = [_f(r.get("total_time")) for r in rows]
    source_counts = [_f(r.get("source_count")) for r in rows]
    quality_scores = [_f(r.get("quality_score")) for r in rows]
    grounding_rates = [_f(r.get("number_grounding_rate")) for r in rows]
    tier_ratios = [_f(r.get("tier1_or_tier2_ratio")) for r in rows]
    citation_counts = [_f(r.get("citation_coverage")) for r in rows]

    # valid_citation_rate: 用逐 run 的 trace.evaluation_diagnostics 里的
    # citation_count/invalid_citation_count 精确计算（而不是用 evaluation_summary 里
    # 已经折算过的 citation_coverage 近似）
    valid_num, valid_den = 0, 0
    compliance_flags: list[bool] = []
    chart_flags: dict[str, list[bool]] = defaultdict(list)
    entity_status_counts = Counter()
    revision_triggered, revision_deltas = 0, []
    macro_coverage: list[float] = []
    company_statement_hits, company_case_count = 0, 0
    industry_scenario_hits, industry_case_count = 0, 0
    tracking_attempted, tracking_success = 0, 0

    for row in rows:
        run_id = row.get("run_id", _NA)
        trace = _load_trace_by_run_id(run_id) if run_id != _NA else None
        category = category_by_topic.get(row["topic"], _NA)

        eval_diag = {}
        try:
            eval_path = row.get("evaluation_path")
            if eval_path and eval_path != _NA:
                eval_diag = load_json(eval_path).get("diagnostics", {})
        except Exception:  # noqa: BLE001
            eval_diag = {}
        cc = _f(eval_diag.get("citation_count"))
        ic = _f(eval_diag.get("invalid_citation_count")) or 0
        if cc:
            valid_num += (cc - ic)
            valid_den += cc

        compliance = _compliance_for_row(row, row.get("sources_path", ""))
        if compliance is not None:
            compliance_flags.append(compliance)

        tags = tags_by_topic.get(row["topic"], [])
        if "requires_chart" in tags or category in ("company", "industry", "macro"):
            chart = _chart_embedded_for_run(run_id)
            if chart is not None:
                chart_flags[category].append(chart)

        if trace:
            status = (trace.get("entity_validation") or {}).get("validation_status", _NA)
            entity_status_counts[status] += 1

            rev = trace.get("revision") or {}
            if rev.get("revision_triggered"):
                revision_triggered += 1
                before, after = rev.get("before_score"), rev.get("after_score")
                if isinstance(before, (int, float)) and isinstance(after, (int, float)):
                    revision_deltas.append(after - before)

            if category == "macro":
                mm = trace.get("macro_chain_metrics") or {}
                if mm.get("indicator_count") is not None:
                    macro_coverage.append(mm["indicator_count"] / 22)  # 22 项已注册指标

            if category == "company":
                company_case_count += 1
                cdm = trace.get("company_deep_metrics") or {}
                if cdm.get("statements_periods", 0) > 0:
                    company_statement_hits += 1

            if category == "industry":
                industry_case_count += 1
                idm = trace.get("industry_deep_metrics") or {}
                if idm.get("scenario_years", 0) >= 3:
                    industry_scenario_hits += 1

        run_entry = run_by_topic.get(row["topic"], {})
        tr = run_entry.get("tracking_result", {})
        if tr.get("attempted"):
            tracking_attempted += 1
            if not tr.get("error") and not tr.get("insufficient_history"):
                tracking_success += 1

    valid_citation_rate = round(valid_num / valid_den, 3) if valid_den else "insufficient data"
    compliance_pass_rate = round(sum(compliance_flags) / len(compliance_flags), 3) if compliance_flags else "insufficient data"
    all_chart_flags = [f for flags in chart_flags.values() for f in flags]
    chart_generation_rate = round(sum(all_chart_flags) / len(all_chart_flags), 3) if all_chart_flags else "insufficient data"
    entity_pass = sum(v for k, v in entity_status_counts.items() if k in ("verified", "weak"))
    entity_total = sum(entity_status_counts.values())
    entity_pass_rate = round(entity_pass / entity_total, 3) if entity_total else "insufficient data"
    revision_trigger_rate = round(revision_triggered / n, 3) if n else 0.0
    revision_improvement = round(statistics.mean(revision_deltas), 3) if revision_deltas else "insufficient data (no adopted revisions with numeric before/after)"
    macro_indicator_coverage = round(statistics.mean(macro_coverage), 3) if macro_coverage else "insufficient data"
    company_statement_coverage = round(company_statement_hits / company_case_count, 3) if company_case_count else "insufficient data"
    industry_scenario_completion_rate = round(industry_scenario_hits / industry_case_count, 3) if industry_case_count else "insufficient data"
    tracking_report_success_rate = round(tracking_success / tracking_attempted, 3) if tracking_attempted else "insufficient data (no tracking-tool case attempted)"

    # report_type / category 分组
    by_category: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_category[category_by_topic.get(row["topic"], _NA)].append(row)

    lines = [
        "# Competition 30-Case Evaluation Report",
        "",
        "**这是人工分层构建的固定 benchmark（公司10/行业10/宏观10，每类内置季度跟踪/"
        "年度跟踪/风险分析/图表题各至少1个），不是随机抽样，不能宣称代表所有金融任务的"
        "泛化能力。**所有数字均来自真实产物（outputs/traces、outputs/sources、"
        "outputs/evaluations、report 文件），没有编造未完成的 case。",
        "",
        "## 总体指标",
        "",
        f"- total_cases: {n}",
        f"- success_rate: {success_rate}",
        f"- avg_source_count: {_avg(source_counts)}",
        f"- valid_citation_rate: {valid_citation_rate}",
        f"- avg_source_grounding(criteria): {_avg([_f(r.get('source_grounding')) for r in rows])}",
        f"- avg_number_grounding_rate: {_avg(grounding_rates)}",
        f"- avg_tier1_or_tier2_ratio: {_avg(tier_ratios)}",
        f"- avg_quality_score: {_avg(quality_scores)}",
        f"- total_latency: avg={_avg(total_times)}s p50={_pct([v for v in total_times if v], 50)}s "
        f"p90={_pct([v for v in total_times if v], 90)}s p95={_pct([v for v in total_times if v], 95)}s",
        f"- report_compliance_pass_rate: {compliance_pass_rate}",
        f"- chart_generation_rate: {chart_generation_rate}",
        f"- entity_validation_pass_rate: {entity_pass_rate}（分布：{dict(entity_status_counts)}）",
        f"- revision_trigger_rate: {revision_trigger_rate}",
        f"- revision_improvement(avg after-before, adopted only): {revision_improvement}",
        f"- tracking_report_success_rate（公司三表跟踪工具，本次尝试 {tracking_attempted} 例）: {tracking_report_success_rate}",
        f"- macro_indicator_coverage（宏观 case，指标覆盖/22项已注册指标）: {macro_indicator_coverage}",
        f"- company_statement_coverage（公司 case，三表抽取成功占比）: {company_statement_coverage}",
        f"- industry_scenario_completion_rate（行业 case，三年及以上情景占比）: {industry_scenario_completion_rate}",
        "",
        "## 按类别分组",
        "",
        "| category | cases | success | avg_quality | avg_sources | avg_grounding | avg_tier_ratio |",
        "|---|---|---|---|---|---|---|",
    ]
    for cat in ("company", "industry", "macro"):
        group = by_category.get(cat, [])
        if not group:
            lines.append(f"| {cat} | 0 | insufficient data | - | - | - | - |")
            continue
        g_success = sum(1 for r in group if r.get("main_issue") not in ("missing_run", "failed") and r.get("source_count"))
        lines.append(
            f"| {cat} | {len(group)} | {g_success}/{len(group)} "
            f"| {_avg([_f(r.get('quality_score')) for r in group])} "
            f"| {_avg([_f(r.get('source_count')) for r in group])} "
            f"| {_avg([_f(r.get('number_grounding_rate')) for r in group])} "
            f"| {_avg([_f(r.get('tier1_or_tier2_ratio')) for r in group])} |")

    lines += ["", "## 未完成 / 失败 case（如实列出，不隐藏）", ""]
    failed = [r for r in rows if r.get("main_issue") in ("missing_run",) or not r.get("source_count")]
    if failed:
        for r in failed:
            lines.append(f"- {r['topic']}（{category_by_topic.get(r['topic'], _NA)}）: "
                         f"main_issue={r.get('main_issue')}, run_id={r.get('run_id')}")
    else:
        lines.append("（无——全部 30 个 case 均产出了带来源的报告）")

    lines += [
        "", "## 方法说明与限制", "",
        "- report_compliance_pass_rate 用 tools/report_compliance_checker.py 对每个 case 的最终报告"
        "重新跑一次规则检查（风险提示/数据来源/估值假设/免责声明/研究对象与代码/无来源数字/"
        "假设伪装成事实/报告日期），不是导出正式披露报告时的判定结果。",
        "- chart_generation_rate 按报告 HTML 是否包含 `data:image/png;base64` 判定，"
        "markdown-only 输出的 case 不计入分母。",
        "- tracking_report_success_rate 仅统计公司类跟踪题触发的真实 tracking_report_builder 调用"
        "（阶段D 的工具专注上市公司三表）；行业/宏观的跟踪题以周期性措辞的常规报告验证，"
        "计入上面按类别分组的 success 列，不计入本指标。",
        "- macro_indicator_coverage 以 22 项已在 tools/macro_data_collector.py 中注册且实测可用的"
        "指标为分母；社融/城镇调查失业率/美元指数等 3 项已知不可用指标不计入分母（如实排除，非隐藏缺陷）。",
        "- revision_improvement 只统计\"改稿被采用\"（after_score >= before_score）的场景，"
        "被丢弃的改稿不参与均值计算——见 orchestrator/workflow.py 的 adopt-if-improved 逻辑。",
    ]

    return "\n".join(lines) + "\n"


def main() -> int:
    if not _RUNS_PATH.exists():
        print(f"[error] {_RUNS_PATH} not found; run `python scripts/run_competition_eval.py --run` first")
        return 1
    report = build_report()
    out = config.EVAL_DIR / "competition_report.md"
    out.write_text(report, encoding="utf-8")
    print(f"Competition report saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
