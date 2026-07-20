"""Chart consistency checker (v4 stage F).

对每张图的 meta（chart_common.chart_block 产出）做三项核对：
1) 图表主体（subject）与报告研究对象一致（不把别的公司的图挂到这份报告）；
2) 图表期间与正文声称的期间不冲突（正文若提及具体年份，需与 data_period 重叠）；
3) 图表关键数值与结构化数据（snapshot/bundle）一致（1% 容差）。
返回逐图检查结果；不一致时标记 mismatch，供报告生成方决定是否剔除该图——
本模块只检查，不做自动修复。
"""
import re
from typing import Any, Optional

_YEAR_RE = re.compile(r"(20\d{2})")


def _period_years(period: str) -> set:
    return set(int(y) for y in _YEAR_RE.findall(period or ""))


def check_chart_consistency(chart_metas: list[dict], *, expected_subject: str = "",
                            reference_values: Optional[dict[str, float]] = None,
                            report_text: str = "") -> dict[str, Any]:
    """chart_metas: chart_common.chart_block 返回的 meta 列表（跳过 None）。
    reference_values: {字段名: 权威值}（如 snapshot 的 pe/pb/latest_close），
    与图 meta.values_sample 同 key 时按 1% 容差比对。"""
    results = []
    for meta in chart_metas:
        if meta is None:
            continue
        issues = []
        subject = meta.get("subject", "")
        if expected_subject and subject and subject != expected_subject and \
                expected_subject not in subject and subject not in expected_subject:
            issues.append(f"subject_mismatch: 图表主体'{subject}' 与报告研究对象'{expected_subject}' 不一致")

        chart_years = _period_years(meta.get("data_period", ""))
        text_years = _period_years(report_text[:3000]) if report_text else set()
        if chart_years and text_years and not (chart_years & text_years) and len(text_years) <= 3:
            issues.append(f"period_note: 图表期间{sorted(chart_years)}与正文提及年份{sorted(text_years)}无交集（仅提示，不一定错误）")

        if reference_values:
            for key, sample_val in (meta.get("values_sample") or {}).items():
                ref = reference_values.get(key)
                if ref is not None and sample_val is not None and ref != 0:
                    diff_pct = abs(sample_val - ref) / abs(ref)
                    if diff_pct > 0.01:
                        issues.append(f"value_mismatch: {key} 图表值{sample_val} 与结构化数据{ref} "
                                     f"偏差{diff_pct:.1%}（>1%容差）")

        results.append({"chart_title": meta.get("chart_title", ""), "subject": subject,
                        "issues": issues, "consistent": not issues})

    return {
        "charts_checked": len(results),
        "all_consistent": all(r["consistent"] for r in results) if results else True,
        "results": results,
        "note": "一致性检查为规则比对（主体/期间/1%数值容差），不做自动图表修复",
    }
