"""DCF 数学/守卫/来源标注 + 相对估值工具测试。"""
import pytest

from tools.valuation_dcf import dcf_valuation, dcf_valuation_range, extract_dcf_inputs, run_dcf_for_sources
from tools.valuation_relative import relative_valuation
from schemas.source import Source


def test_dcf_math_hand_checked():
    r = dcf_valuation(100, [0.10], 0.09, 0.025)
    fcf1 = 110.0
    expected = fcf1 / 1.09 + (fcf1 * 1.025 / (0.09 - 0.025)) / 1.09
    assert abs(r["enterprise_value"] - expected) < 0.01


def test_dcf_rejects_divergent_terminal():
    with pytest.raises(ValueError):
        dcf_valuation(100, [0.1], 0.02, 0.03)
    with pytest.raises(ValueError):
        dcf_valuation(-5, [0.1], 0.09, 0.025)


def test_dcf_scenarios_ordered():
    r = dcf_valuation_range(100, [0.1] * 5, 0.09, 0.025)
    vr = r["valuation_range"]
    assert vr["bear"] < vr["base"] < vr["bull"]


def test_dcf_inputs_prefer_akshare_snapshot():
    snapshot = {
        "operating_cash_flow": 500.0,
        "period": "2025年报",
        "yearly_series": {"revenue": [(2024, 1000.0), (2025, 1100.0)]},
    }
    params = extract_dcf_inputs([], "测试公司", snapshot)
    assert params["provenance"]["base_fcf"]["kind"] == "akshare"
    assert params["provenance"]["base_fcf"]["akshare_field"] == "akshare:operating_cash_flow"
    assert params["provenance"]["first_year_growth"]["kind"] == "derived"
    assert abs(params["provenance"]["first_year_growth"]["value"] - 0.10) < 0.001


def test_dcf_inputs_text_extraction_and_default():
    src = Source(source_id="s1", content="公司经营性现金流净额1332亿元。营收同比增长17.04%。")
    params = extract_dcf_inputs([src], "测试")
    assert params["provenance"]["base_fcf"]["kind"] == "extracted"
    assert params["provenance"]["base_fcf"]["source_id"] == "s1"
    # nothing extractable -> config defaults, honestly labeled
    params2 = extract_dcf_inputs([], "测试")
    assert params2["provenance"]["base_fcf"]["kind"] == "default"


def test_run_dcf_output_contract():
    src = Source(source_id="s3", content="净利润326.19亿元。营收同比增长12.75%。")
    r = run_dcf_for_sources([src], "测试")
    assert r["valuation_method"] == "two_stage_dcf"
    assert r["bear_value"] < r["base_value"] < r["bull_value"]
    assert any("[s3]" in s for s in r["input_sources"])
    assert any("config_assumption" in s for s in r["input_sources"])
    assert "warning" in r and "不构成" in r["warning"]


def test_relative_valuation_signals_and_degraded():
    snapshot = {
        "pe": 10.0, "pb": 2.0, "ps": None,
        "multiples_history": {"pe": [20.0 + i * 0.1 for i in range(60)], "pb": []},
    }
    r = relative_valuation(snapshot)
    assert r["multiples"]["pe"]["valuation_signal"] == "undervalued"  # 10 below all history
    assert r["multiples"]["pb"]["valuation_signal"] == "unknown"     # no history
    assert r["degraded"] is True
    assert "ps" in r["missing_fields"]
    # 参照系假设必须明示，不冒充行业可比
    assert any("自身" in a for a in r["assumptions"])
