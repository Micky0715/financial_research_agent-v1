"""Tracking report YoY/QoQ (v4 stage D) + report compliance checker (v4 stage E)
+ chart consistency checker (v4 stage F). Pure computation, no network."""
from schemas.source import Source
from schemas.tracking import PeriodMetrics
from tools.chart_consistency_checker import check_chart_consistency
from tools.report_compliance_checker import check_compliance
from tracking.report_diff_analyzer import compare_with_prior_record, detect_changes
from tracking.tracking_report_builder import _fill_quarterly_single_and_qoq, _fill_yoy

_COMPLETE_MD = (
    "# 贵州茅台 研究报告\n\n报告日期：2026-07-20 证券代码：600519\n\n"
    "## 风险提示\n\n市场风险与政策风险 [s1]。\n\n"
    "## 估值方法与假设\n\nDCF 估值，折现率为配置假设 9% [s1]。\n\n"
    "## 数据来源\n\nAkShare 结构化数据，营收1720.5亿元（AkShare）。\n\n"
    "## 免责声明\n\n本报告不构成投资建议。\n"
)


# ---------------- tracking YoY / QoQ ----------------
def test_fill_yoy_matches_same_period_last_year():
    metrics = [
        PeriodMetrics(period="2026年报", period_raw="20261231", revenue=200.0, net_profit=40.0),
        PeriodMetrics(period="2025年报", period_raw="20251231", revenue=170.0, net_profit=35.0),
    ]
    _fill_yoy(metrics)
    cur = metrics[0]
    assert cur.yoy_revenue == round((200.0 / 170.0 - 1) * 100, 2)
    assert cur.yoy_net_profit == round((40.0 / 35.0 - 1) * 100, 2)


def test_fill_quarterly_single_and_qoq_derives_single_quarter_values():
    metrics = [
        PeriodMetrics(period="2026Q2", period_raw="20260630", revenue=300.0, net_profit=60.0),
        PeriodMetrics(period="2026Q1", period_raw="20260331", revenue=150.0, net_profit=30.0),
    ]
    _fill_quarterly_single_and_qoq(metrics)
    q2 = next(m for m in metrics if m.period_raw == "20260630")
    q1 = next(m for m in metrics if m.period_raw == "20260331")
    assert q1.revenue_single == 150.0  # Q1 累计=单季
    assert q2.revenue_single == 150.0  # 300 - 150
    assert q2.qoq_revenue == round((150.0 / 150.0 - 1) * 100, 2)


def test_detect_changes_reports_insufficient_with_single_period():
    changes, insufficient = detect_changes([PeriodMetrics(period="p", period_raw="20261231")])
    assert changes == []
    assert "历史期不足" in insufficient[0]


def test_detect_changes_uses_same_period_base_for_cashflow_not_prior_period():
    """季度模式下现金流应该跟上年同期比，而不是跟上一期(可能是全年)比——
    避免"Q1累计 vs 全年"这种虚假暴跌。"""
    q1_2026 = PeriodMetrics(period="2026Q1", period_raw="20260331", revenue=500.0, net_profit=250.0,
                            operating_cash_flow=270.0)
    fy_2025 = PeriodMetrics(period="2025年报", period_raw="20251231", revenue=1700.0, net_profit=820.0,
                            operating_cash_flow=615.0)
    q1_2025 = PeriodMetrics(period="2025Q1", period_raw="20250331", revenue=470.0, net_profit=240.0,
                            operating_cash_flow=88.0)
    changes, _ = detect_changes([q1_2026, fy_2025, q1_2025], same_period_base=q1_2025)
    ocf_change = next(c for c in changes if c.dimension == "经营现金流")
    assert "2025Q1" in ocf_change.evidence  # 用同期而不是上一期(全年)做基期
    assert ocf_change.direction == "improving"  # 270 vs 88，真实大幅改善


def test_compare_with_prior_record_honest_when_no_history():
    changes, notes, compared = compare_with_prior_record(None, {"pe": 20})
    assert compared is False
    assert changes == []
    assert notes


# ---------------- report compliance checker ----------------
def test_compliant_report_passes_all_checks():
    result = check_compliance(_COMPLETE_MD, [Source(source_id="s1", content="x" * 100)],
                              {"report_type": "company_research", "symbol": "600519"})
    assert result["passed"] is True
    assert result["failures"] == []


def test_missing_disclaimer_fails_compliance():
    md = _COMPLETE_MD.replace("## 免责声明\n\n本报告不构成投资建议。\n", "")
    result = check_compliance(md, [Source(source_id="s1", content="x" * 100)])
    assert result["passed"] is False
    assert "has_disclaimer" in result["failures"]


def test_fact_dressed_as_assumption_fails_compliance():
    md = _COMPLETE_MD + "\n目标价将达到2000元，公司股价必然上涨。"
    result = check_compliance(md, [Source(source_id="s1", content="x" * 100)])
    assert "no_assumption_dressed_as_fact" in result["failures"]


def test_unsourced_key_numbers_fail_compliance():
    md = "# X\n\n报告日期：2026-07-20\n\n公司营收为9999亿元，创历史新高。\n\n## 免责声明\n不构成投资建议。"
    result = check_compliance(md, [])
    assert "no_unsourced_key_numbers" in result["failures"]


# ---------------- chart consistency checker ----------------
def test_chart_consistency_flags_subject_mismatch():
    metas = [{"chart_title": "股价图", "data_period": "2026", "source": "AkShare",
             "unit": "元", "subject": "五粮液", "missing_data_note": "", "values_sample": {}}]
    result = check_chart_consistency(metas, expected_subject="贵州茅台")
    assert result["all_consistent"] is False
    assert "subject_mismatch" in result["results"][0]["issues"][0]


def test_chart_consistency_flags_value_beyond_tolerance():
    metas = [{"chart_title": "PE图", "data_period": "2026", "source": "AkShare", "unit": "倍",
             "subject": "贵州茅台", "missing_data_note": "", "values_sample": {"latest_pe": 25.0}}]
    result = check_chart_consistency(metas, expected_subject="贵州茅台",
                                     reference_values={"latest_pe": 20.0})
    assert result["all_consistent"] is False
    assert "value_mismatch" in result["results"][0]["issues"][0]


def test_chart_consistency_passes_when_matching():
    metas = [{"chart_title": "PE图", "data_period": "2026", "source": "AkShare", "unit": "倍",
             "subject": "贵州茅台", "missing_data_note": "", "values_sample": {"latest_pe": 20.05}}]
    result = check_chart_consistency(metas, expected_subject="贵州茅台",
                                     reference_values={"latest_pe": 20.0})
    assert result["all_consistent"] is True
