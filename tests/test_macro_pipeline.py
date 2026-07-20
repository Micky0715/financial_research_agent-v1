"""Macro data chain (v4 stage A): normalizer / policy parser / transmission /
grey-rhino monitor. All pure computation on synthetic inputs, no network."""
import pandas as pd

from schemas.macro_data import MacroDataPoint, MacroSnapshot
from tools.grey_rhino_monitor import monitor_grey_rhinos
from tools.macro_indicator_normalizer import (
    derive_mom_yoy_from_series,
    parse_jin10_table,
    parse_month_table,
    parse_quarter_table,
)
from tools.macro_transmission_model import build_transmission_paths
from tools.policy_document_parser import _rule_parse

# ---------------- indicator normalizer ----------------
def test_parse_month_table_extracts_latest_and_yoy():
    df = pd.DataFrame({
        "月份": ["2026年04月份", "2026年05月份", "2026年06月份"],
        "全国-同比增长": [1.2, 1.1, 1.0],
        "全国-环比增长": [0.1, -0.2, -0.3],
    })
    point, series = parse_month_table(df, "全国-同比增长", mom_col="全国-环比增长")
    assert point["value"] == 1.0
    assert point["period"] == "2026-06"
    assert point["previous_value"] == 1.1
    assert point["mom"] == -0.3
    assert len(series) == 3


def test_parse_quarter_table_extracts_latest():
    df = pd.DataFrame({
        "季度": ["2025年第4季度", "2026年第1季度"],
        "国内生产总值-同比增长": [5.0, 4.7],
    })
    point, _ = parse_quarter_table(df, "国内生产总值-同比增长")
    assert point["period"] == "2026Q1"
    assert point["value"] == 4.7
    assert point["previous_value"] == 5.0


def test_parse_jin10_table_skips_unreleased_periods():
    df = pd.DataFrame({
        "商品": ["x"] * 3,
        "日期": ["2026-07-01", "2026-08-01", "2026-09-01"],
        "今值": [7.2, None, float("nan")],
        "前值": [5.8, 7.2, None],
    })
    point, series = parse_jin10_table(df)
    assert point["value"] == 7.2
    assert point["previous_value"] == 5.8
    assert len(series) == 1  # 未发布期(NaN)被跳过


def test_derive_mom_yoy_from_series():
    series = [(f"2025-{m:02d}", 100 + m) for m in range(1, 13)] + [("2026-01", 115)]
    out = derive_mom_yoy_from_series(series, periods_per_year=12)
    assert out["mom"] is not None
    assert out["yoy"] is not None


# ---------------- policy parser (rule fallback path) ----------------
def test_policy_rule_parser_extracts_issuer_date_tools_and_industries():
    text = ("中国人民银行决定于2026年5月15日下调金融机构存款准备金率0.5个百分点，"
           "旨在保持流动性合理充裕，支持新能源汽车、人工智能等重点行业发展，"
           "严控高耗能行业新增产能。")
    result = _rule_parse(text, "央行降准公告")
    assert result.issuer == "中国人民银行"
    assert result.publish_date == "2026-05-15"
    assert "降低存款准备金率" in result.policy_tools
    assert "新能源汽车" in result.supported_industries
    assert "人工智能" in result.supported_industries
    assert "高耗能" in result.constrained_industries
    assert "高耗能" not in result.supported_industries
    assert result.parse_method == "rule_fallback"
    for quote in result.evidence_quotes:
        assert quote[:15] in text  # 摘句必须真实来自原文


def test_policy_rule_parser_empty_text_degrades_cleanly():
    result = _rule_parse("", "")
    assert result.policy_goals == []
    assert result.evidence_quotes == []


# ---------------- transmission model ----------------
def _snapshot_with(**overrides) -> MacroSnapshot:
    base = {
        "lpr_1y": (3.0, 3.1), "cn_bond_10y": (1.74, 1.80), "usd_cny": (6.80, 6.79),
        "cpi_yoy": (1.0, 1.2), "ppi_yoy": (4.1, 3.9), "m1_yoy": (4.0, 5.5),
        "m2_yoy": (8.0, 8.6), "exports_yoy": (7.2, 5.8), "house_prosperity_index": (91.4, 91.9),
    }
    base.update(overrides)
    indicators = {k: MacroDataPoint(indicator_key=k, indicator_name=k, value=v[0],
                                    previous_value=v[1], period="2026-06") for k, v in base.items()}
    return MacroSnapshot(indicators=indicators)


def test_transmission_path_activates_only_with_real_indicator_evidence():
    snap = _snapshot_with(cn_bond_10y=(1.70, 1.80))  # 下行 -> 应激活利率下行链
    paths = build_transmission_paths(snap)
    triggers = [p.trigger for p in paths]
    assert "利率下行" in triggers
    rate_path = next(p for p in paths if p.trigger == "利率下行")
    assert "cn_bond_10y" in rate_path.evidence_indicators
    assert len(rate_path.steps) >= 2


def test_transmission_path_not_activated_without_evidence():
    snap = MacroSnapshot(indicators={})  # 空快照，没有任何指标证据
    paths = build_transmission_paths(snap)
    assert paths == []


# ---------------- grey rhino monitor ----------------
def test_grey_rhino_high_risk_when_threshold_crossed():
    snap = _snapshot_with(house_prosperity_index=(90.0, 91.9))  # <92 -> high 阈值
    risks = monitor_grey_rhinos(snap)
    real_estate = next(r for r in risks if r.risk_name == "房地产市场下行")
    assert real_estate.risk_level == "high"
    assert real_estate.evidence
    assert "house_prosperity_index" in real_estate.triggered_indicators


def test_grey_rhino_unknown_when_indicator_missing():
    snap = MacroSnapshot(indicators={})
    risks = monitor_grey_rhinos(snap)
    real_estate = next(r for r in risks if r.risk_name == "房地产市场下行")
    assert real_estate.risk_level == "unknown"
    assert real_estate.limitation


def test_grey_rhino_geopolitics_and_supply_chain_never_fabricate_a_level():
    """无结构化数据源的两项风险永远是 unknown，不允许被规则臆造出等级。"""
    snap = _snapshot_with()
    risks = monitor_grey_rhinos(snap)
    names = {r.risk_name: r for r in risks}
    assert names["地缘冲突"].risk_level == "unknown"
    assert names["产业链集中风险"].risk_level == "unknown"
