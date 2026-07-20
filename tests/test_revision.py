"""Bounded self-check revision (v4 stage G): tools/report_reviser.py."""
from evaluators.report_evaluator import evaluate_report
from schemas.source import Source
from tools.report_reviser import apply_revision, identify_revision_issues

_CLEAN_MD = (
    "## 执行摘要\n营收8040亿元 [s1]。\n## 公司/行业概况\n概况 [s1]。\n## 核心数据与趋势\n趋势 [s1]。\n"
    "## 财务表现或经营情况分析\n营收、净利润、毛利率、现金流分析 [s1]。\n"
    "## 估值分析\nPE、PB、DCF 估值 [s1]。\n## 风险提示\n市场风险 [s1]。风险提示内容足够长足够长足够长。\n"
    "## 投资观点\n观点 [s1]。\n## 参考来源\n[s1] a\n\n## 免责声明\n本报告不构成投资建议。\n"
)


def _sources(n=5):
    return [Source(source_id=f"s{i + 1}", url=f"https://www.stcn.com/{i}", content="x" * 300) for i in range(n)]


def test_clean_report_triggers_no_revision():
    sources = _sources()
    evaluation = evaluate_report(_CLEAN_MD, sources, "company_research")
    issues = identify_revision_issues(evaluation, _CLEAN_MD, "company_research")
    assert issues == [], f"clean report should not trigger revision, got {issues}"


def test_invalid_citation_and_missing_disclaimer_trigger_targeted_fixes():
    md = (
        "## 执行摘要\n营收8040亿元 [s1][s99]。\n## 风险提示\n市场风险 [s1]。风险提示内容足够长足够长。\n"
        "## 参考来源\n[s1] a\n"
    )  # s99 不存在（幻觉引用）；无免责声明
    sources = _sources()
    evaluation = evaluate_report(md, sources, "company_research")

    issues = identify_revision_issues(evaluation, md, "company_research")
    kinds = {i["kind"] for i in issues}
    assert "invalid_citations" in kinds
    assert "missing_disclaimer" in kinds

    result = apply_revision(md, issues, sources, analysis=None)
    assert result["changed"]
    assert "[s99]" not in result["markdown"], "hallucinated citation to non-existent source must be removed"
    assert "[s1]" in result["markdown"], "valid citation must survive revision"
    assert "免责声明" in result["markdown"]
    assert "正文引用标记" in result["modified_sections"]
    assert "免责声明" in result["modified_sections"]
    # revision must never invent a new citation to replace the removed one
    assert result["markdown"].count("[s99]") == 0


def test_apply_revision_only_touches_flagged_sections():
    """定向修复：不相关章节内容必须原样保留（不重写无关内容）。"""
    md = (
        "## 执行摘要\n这是一段完全不含数字的独特摘要文本，用于校验修订不改动它。[s1]\n"
        "## 风险提示\n市场风险 [s1]。风险提示内容足够长足够长足够长。\n## 参考来源\n[s1] a\n"
    )
    sources = _sources()
    evaluation = evaluate_report(md, sources, "company_research")
    issues = identify_revision_issues(evaluation, md, "company_research")
    assert issues, "this report is missing a disclaimer and should trigger at least one issue"
    result = apply_revision(md, issues, sources, analysis=None)
    assert "这是一段完全不含数字的独特摘要文本，用于校验修订不改动它。" in result["markdown"]


def test_unsourced_number_matching_real_snapshot_gets_real_marker_not_fabricated():
    md = "## 财务表现或经营情况分析\n公司营收为100.0亿元，表现稳健。\n## 参考来源\n[s1] a\n"
    sources = _sources()
    snapshot = {"revenue": 100.0, "net_profit": None, "gross_margin": None, "roe": None,
                "operating_cash_flow": None, "debt_ratio": None, "pe": None, "pb": None,
                "ps": None, "market_cap": None, "price": None}
    evaluation = evaluate_report(md, sources, "company_research",
                                 analysis={"financial_snapshot_full": snapshot})
    issues = identify_revision_issues(evaluation, md, "company_research")
    assert any(i["kind"] == "unsourced_numbers" for i in issues)

    result = apply_revision(md, issues, sources, analysis={"financial_snapshot_full": snapshot})
    assert "AkShare 结构化数据核对一致" in result["markdown"]


def test_unmatched_unsourced_number_stays_honest_not_fabricated():
    """没有匹配到真实结构化数据的未溯源数字，修订不得编造引用去'洗白'它。"""
    md = "## 财务表现或经营情况分析\n据传公司营收为9999.0亿元。\n## 参考来源\n[s1] a\n"
    sources = _sources()
    snapshot = {"revenue": 100.0}
    evaluation = evaluate_report(md, sources, "company_research",
                                 analysis={"financial_snapshot_full": snapshot})
    issues = identify_revision_issues(evaluation, md, "company_research")
    result = apply_revision(md, issues, sources, analysis={"financial_snapshot_full": snapshot})
    assert "9999.0亿元" in result["markdown"]
    assert "AkShare 结构化数据核对一致" not in result["markdown"], \
        "must not attach a fake grounding marker to a number with no real match"
