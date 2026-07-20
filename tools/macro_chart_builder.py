"""Macro charts: GDP/CPI/PPI/PMI, rates/FX, exports, policy timeline (v4 stage F).

输入 MacroSnapshot.model_dump()（已在 macro_report_builder 里组装过一次，这里
只做可视化）。序列不足 2 点的指标跳过；缺失指标在 meta.missing_data_note
如实列出，不补零。
"""
from typing import Any

from tools.chart_common import chart_block, render_lines_base64
from utils.logger import logger

_GROUPS: list[tuple[str, str, list[tuple[str, str]], str]] = [
    ("GDP/CPI/PPI/PMI", "%/点", [
        ("gdp_yoy", "GDP同比"), ("cpi_yoy", "CPI同比"), ("ppi_yoy", "PPI同比"),
        ("pmi_manufacturing", "制造业PMI")], "国家统计局"),
    ("利率与汇率", "%/元", [
        ("lpr_1y", "1年期LPR"), ("cn_bond_10y", "中国10Y国债"),
        ("us_bond_10y", "美国10Y国债"), ("usd_cny", "人民币兑美元中间价")],
     "人民银行/中债/美国财政部/外汇管理局"),
    ("进出口与外储", "%/亿美元", [
        ("exports_yoy", "出口同比"), ("imports_yoy", "进口同比"), ("fx_reserves", "外汇储备")],
     "海关总署/外汇管理局"),
]


def build_macro_charts_html(snapshot: dict[str, Any]) -> str:
    """MacroSnapshot dict -> 图表 HTML（多组趋势图）。返回 '' 表示无可画数据。"""
    indicators = snapshot.get("indicators", {})
    series_map = snapshot.get("series", {})
    fetched_at = snapshot.get("fetched_at", "")
    blocks = []

    for title, unit, keys, source in _GROUPS:
        series_by_label: dict[str, list] = {}
        missing = []
        periods_seen: list[str] = []
        for key, label in keys:
            ser = series_map.get(key) or []
            if len(ser) >= 2:
                series_by_label[label] = ser
                periods_seen += [p for p, _ in ser]
            elif key in indicators:
                missing.append(f"{label}（仅1期数据）")
            else:
                missing.append(f"{label}（缺失）")
        if not series_by_label:
            continue
        b64 = render_lines_base64(series_by_label, f"宏观指标：{title}", unit, max_xticks=8)
        period = f"{min(periods_seen)} ~ {max(periods_seen)}" if periods_seen else ""
        html, meta = chart_block(
            b64, chart_title=title, data_period=period, source=f"AkShare·{source}",
            unit=unit, missing_data_note="；".join(missing),
            values_sample={label: ser[-1][1] for label, ser in series_by_label.items()})
        if html:
            blocks.append(html)

    if not blocks:
        logger.info("macro charts: no plottable series")
    return "\n".join(blocks)
