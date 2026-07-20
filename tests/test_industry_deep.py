"""Industry deep chain (v4 stage C): lifecycle / CR & HHI / 3y scenario /
chain verification / entry-exit scoring. Sector-member fetch is mocked
(network); all concentration/scenario/scoring math executes for real."""
import tools.industry_concentration as ic
from schemas.industry_research import ConcentrationResult, LifecycleAssessment
from schemas.source import Source
from tools.industry_chain_builder import build_industry_chain
from tools.industry_entry_exit_analyzer import analyze_entry_exit
from tools.industry_lifecycle import assess_lifecycle
from tools.industry_scenario_model import build_scenario_model


def _src(sid, title, content):
    return Source(source_id=sid, title=title, url=f"https://x.com/{sid}", content=content)


# ---------------- lifecycle ----------------
def test_lifecycle_growth_stage_from_real_text_evidence():
    sources = [
        _src("s1", "行业增速报告", "该行业2025年市场同比增长22.5%，渗透率仅18%，成长空间广阔。"),
        _src("s2", "政策报告", "国家十四五规划支持该行业发展，多地出台补贴政策。"),
    ]
    result = assess_lifecycle("某行业", sources)
    assert result.stage == "导入期"  # 增速>=15% 且渗透率<20%
    assert result.signals["growth_median_pct"] == 22.5
    assert result.evidence


def test_lifecycle_unknown_without_any_numeric_evidence():
    sources = [_src("s1", "泛行业介绍", "这是一个重要的行业，前景广阔，值得关注。")]
    result = assess_lifecycle("某行业", sources)
    assert result.stage == "unknown"
    assert "不做无证据判断" in result.stage_reason


def test_lifecycle_decline_stage_from_negative_growth_mentions():
    sources = [
        _src("s1", "行业下滑", "该行业本季度同比下降12%，产能过剩问题突出，行业出清加速。"),
        _src("s2", "行业出清", "多家企业同比下降8%，行业面临产能过剩与出清压力。"),
    ]
    result = assess_lifecycle("某行业", sources)
    assert result.stage == "衰退期"


# ---------------- concentration (CR/HHI real math) ----------------
def test_concentration_cr_and_hhi_are_correctly_computed(monkeypatch):
    members = [
        {"code": "600519", "name": "龙头A", "mktcap_yi": 5000.0},
        {"code": "600809", "name": "老二B", "mktcap_yi": 3000.0},
        {"code": "000858", "name": "老三C", "mktcap_yi": 1000.0},
        {"code": "000596", "name": "小厂D", "mktcap_yi": 600.0},
        {"code": "603369", "name": "小厂E", "mktcap_yi": 400.0},
    ]
    monkeypatch.setattr(ic, "_fetch_members", lambda kind, name: members)
    result = ic.compute_concentration("白酒行业", [])
    total = sum(m["mktcap_yi"] for m in members)
    expected_cr3 = round((5000 + 3000 + 1000) / total * 100, 2)
    expected_hhi = round(sum((m["mktcap_yi"] / total * 100) ** 2 for m in members), 1)
    assert result.cr3 == expected_cr3
    assert result.cr5 == round(100.0, 2)  # 只有5家，CR5=100%
    assert result.hhi == expected_hhi
    assert result.degraded is False
    assert "市值口径" in result.sample_note


def test_concentration_degrades_with_no_sector_match():
    result = ic.compute_concentration("完全不存在的冷门行业XYZ", [])
    assert result.degraded is True
    assert result.cr5 is None
    assert "不编造" in result.limitation


def test_concentration_source_mentions_captured_independently():
    sources = [_src("s1", "行业集中度报告", "该行业CR5约为65%，市场份额持续向头部集中。")]
    result = ic.compute_concentration("完全不存在的冷门行业XYZ", sources)
    assert len(result.source_mentions) >= 1
    assert result.source_mentions[0].source_id == "s1"


# ---------------- 3-year scenario (real compounding + provenance tags) ----------------
def test_scenario_model_uses_real_source_size_and_forecast_when_available():
    sources = [
        _src("s1", "市场规模报告", "该行业市场规模约1200亿元，预计未来复合增长率达18%。"),
    ]
    model = build_scenario_model("某行业", sources, years=3)
    assert model.is_indexed is False
    size_input = next(i for i in model.inputs if i.name == "当前市场规模")
    assert size_input.value_kind == "historical_source"
    assert size_input.base_value == 1200.0
    growth_input = next(i for i in model.inputs if i.name == "base情景年增速")
    assert growth_input.value_kind == "external_forecast"
    assert growth_input.base_value == 18.0
    assert len(model.years) == 3
    # bear <= base <= bull 且逐年递增（正增长情形）
    for y in model.years:
        assert y.bear <= y.base <= y.bull
    for i in range(1, len(model.years)):
        assert model.years[i].base > model.years[i - 1].base


def test_scenario_model_falls_back_to_indexed_when_no_real_size():
    model = build_scenario_model("完全没有数字线索的行业", [], years=3)
    assert model.is_indexed is True
    size_input = next(i for i in model.inputs if i.name == "当前市场规模")
    assert size_input.value_kind == "config_assumption"
    growth_input = next(i for i in model.inputs if i.name == "base情景年增速")
    assert growth_input.value_kind == "config_assumption"


# ---------------- industry chain: verified_in_sources reflects real text ----------------
def test_industry_chain_marks_segments_verified_only_when_mentioned():
    sources = [_src("s1", "电池链报道", "宁德时代在动力电池环节保持领先，锂矿价格波动影响成本。")]
    chain = build_industry_chain("新能源汽车", sources)
    battery_node = next(n for n in chain.nodes if n.segment == "动力电池")
    assert battery_node.verified_in_sources is True
    assert "宁德时代" in battery_node.key_companies
    motor_node = next(n for n in chain.nodes if n.segment == "电机电控/汽车电子")
    assert motor_node.verified_in_sources is False
    assert motor_node.key_companies == []  # 模板给的公司未在来源出现，被剔除


def test_industry_chain_unknown_industry_falls_back_to_generic_template():
    chain = build_industry_chain("完全没有模板的行业ABC", [])
    assert chain.data_basis == "generic_template"
    assert len(chain.nodes) == 4


# ---------------- entry/exit scoring ----------------
def test_entry_exit_unknown_when_lifecycle_unknown():
    lifecycle = LifecycleAssessment(industry="X", stage="unknown")
    advice = analyze_entry_exit("X", lifecycle, None)
    assert advice.advice == "unknown"


def test_entry_exit_favors_growth_low_concentration():
    lifecycle = LifecycleAssessment(industry="X", stage="成长期",
                                    signals={"growth_median_pct": 25, "policy_source_count": 3})
    concentration = ConcentrationResult(industry="X", cr5=30.0)
    advice = analyze_entry_exit("X", lifecycle, concentration)
    assert advice.advice in ("积极关注", "选择性参与")
    assert advice.score_breakdown["total"] > 0
