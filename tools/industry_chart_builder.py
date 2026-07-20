"""Industry charts: market-size/CR concentration, 3-year scenario bands (v4 stage F).

输入为 analyze_agent._run_industry_deep_chain 产出的 bundle（concentration/
scenario_model 的 model_dump）。数据不足（如未匹配到板块）时对应图跳过，
meta 里注明；不编造集中度或市场规模。
"""
from typing import Any, Optional

from tools.chart_common import chart_block, render_lines_base64
from utils.logger import logger


def build_industry_charts_html(industry_bundle: dict[str, Any]) -> str:
    blocks = []
    concentration = industry_bundle.get("concentration") or {}
    scenario = industry_bundle.get("scenario_model") or {}

    # 1) 集中度：CR3/CR5/CR10 柱状图（真实板块市值份额口径）
    if concentration.get("cr5") is not None:
        bars = {"CR": [("CR3", concentration["cr3"]), ("CR5", concentration["cr5"]),
                       ("CR10", concentration["cr10"])]}
        b64 = render_lines_base64(bars, f"{concentration.get('industry', '')}集中度（{concentration.get('sector_name', '')}）",
                                  "%", bars=True)
        html, meta = chart_block(
            b64, chart_title="行业集中度 CR3/CR5/CR10", data_period=concentration.get("fetched_at", "")[:10],
            source=f"AkShare·新浪{concentration.get('sector_kind', '')}板块",
            unit="%（总市值份额口径）", subject=concentration.get("industry", ""),
            missing_data_note=concentration.get("sample_note", ""),
            values_sample={"cr5": concentration["cr5"], "hhi": concentration.get("hhi")})
        if html:
            blocks.append(html)
    else:
        logger.info("industry concentration chart skipped: no sector match (degraded)")

    # 2) 三年情景模拟（bear/base/bull 带）
    years = scenario.get("years") or []
    if len(years) >= 2:
        series = {
            "bear": [(y["year"], y["bear"]) for y in years if y.get("bear") is not None],
            "base": [(y["year"], y["base"]) for y in years if y.get("base") is not None],
            "bull": [(y["year"], y["bull"]) for y in years if y.get("bull") is not None],
        }
        series = {k: v for k, v in series.items() if len(v) >= 2}
        unit = years[0].get("unit", "")
        indexed_note = "指数形式（无真实规模基数，基期=100）；" if scenario.get("is_indexed") else ""
        b64 = render_lines_base64(series, f"{scenario.get('industry', '')}三年情景模拟", unit)
        html, meta = chart_block(
            b64, chart_title="三年情景模拟（bear/base/bull）",
            data_period=f"{years[0]['year']}-{years[-1]['year']}",
            source="模型测算（config_assumption+calculated_value，见 scenario inputs）",
            unit=unit, subject=scenario.get("industry", ""),
            missing_data_note=indexed_note + "情景为工具演示，非行业预测保证",
            values_sample={"base_final": years[-1].get("base")})
        if html:
            blocks.append(html)

    return "\n".join(blocks)
