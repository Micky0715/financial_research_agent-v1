"""report-type evaluator / entity validator 测试。"""
import tools.entity_validator as ev
from evaluators.report_evaluator import evaluate_report
from schemas.source import Source

_MD_COMPANY = (
    "## 执行摘要\n营收8040亿元 [s1]。\n## 公司/行业概况\n概况 [s1]。\n## 核心数据与趋势\n趋势 [s1]。\n"
    "## 财务表现或经营情况分析\n营收、净利润、毛利率、现金流分析 [s1]。\n"
    "## 估值分析\nPE、PB、DCF 估值 [s1]。\n## 风险提示\n市场风险 [s1]。风险提示内容足够长足够长足够长。\n"
    "## 投资观点\n观点 [s1]。\n## 参考来源\n[s1] a\n"
)


def _sources(n=5):
    return [Source(source_id=f"s{i+1}", url=f"https://www.stcn.com/{i}", content="x" * 300) for i in range(n)]


def test_company_vs_industry_criteria_differ():
    company = evaluate_report(_MD_COMPANY, _sources(), "company_research")
    industry = evaluate_report(_MD_COMPANY, _sources(), "industry_research")
    assert company["report_type"] == "company_research"
    assert "financial_depth_score" in company["criteria_scores"]
    assert "financial_depth_score" not in industry["criteria_scores"]
    assert "market_size_score" in industry["criteria_scores"]


def test_industry_not_capped_by_financial_depth():
    # 一份完全不含公司财务关键词、但行业维度齐全的行业报告
    md = (
        "## 执行摘要\n市场规模持续扩大 [s1]。\n## 行业趋势\n渗透率提升，增长前景明确 [s1]。\n"
        "## 产业链\n上游材料、中游制造、下游应用 [s1]。\n## 政策\n政策支持，规划明确 [s1]。\n"
        "## 竞争格局\n龙头集中度高，市场份额稳定 [s1]。\n## 风险提示\n政策风险 [s1]。风险内容足够长足够长足够长足够长。\n"
        "## 参考来源\n[s1] a\n"
    )
    r = evaluate_report(md, _sources(), "industry_research")
    assert r["score_cap_reason"] != "financial_depth_score=0"
    assert r["criteria_scores"]["market_size_score"] > 0


def test_source_count_caps_hold_for_all_types():
    for rt in ("company_research", "industry_research", "risk_research", "valuation_research", "macro_research"):
        r = evaluate_report(_MD_COMPANY, _sources(1), rt)
        assert r["overall_score"] <= 0.5, f"{rt} must cap at 0.5 with source_count=1"


def test_unknown_report_type_falls_back():
    r = evaluate_report(_MD_COMPANY, _sources(), "story_research")
    assert r["report_type"] == "story_research"
    assert "evaluator_fallback" in r["diagnostics"]


def test_entity_validator_verified_by_alias_map(monkeypatch):
    monkeypatch.setattr(ev, "_stock_list_hit", lambda s: (False, "代码表不可用（测试）"))
    sources = [Source(source_id="s1", content="比亚迪2025年营收8040亿元")]
    r = ev.validate_entity("比亚迪投资价值分析", "company_research", sources)
    assert r["validation_status"] == "verified"


def test_entity_validator_fails_fictional(monkeypatch):
    monkeypatch.setattr(ev, "_stock_list_hit", lambda s: (False, "A股代码表未命中"))
    # 泛金融内容，正文完全不提该主体
    sources = [Source(source_id=f"s{i}", content="公司营收增长，净利润提升，估值处于低位。") for i in range(5)]
    r = ev.validate_entity("星河量子能源股份有限公司投资价值分析", "company_research", sources)
    assert r["validation_status"] == "failed"
    assert r["evidence_sources"] == []


def test_entity_validator_unsupported_unlisted_company(monkeypatch):
    """v4 阶段H：真实但不在 A 股注册表覆盖范围内的实体（如未上市/港美股/私营企业）
    -> unsupported_unlisted_company，不是 verified 也不是 failed。"""
    monkeypatch.setattr(ev, "_stock_list_hit", lambda s: (False, "A股代码表未命中"))
    monkeypatch.setattr(ev, "check_listed_status", lambda s: {
        "listed": False, "symbol": None, "exchange": None, "name": None,
        "reason": "未在 AkShare 全市场 A 股代码-名称表中找到对应证券代码",
        "coverage_note": "本注册表仅覆盖沪/深/北交所 A 股",
    })
    sources = [Source(source_id=f"s{i}", title="华为相关报道",
                      content="华为在多个领域持续投入研发，华为的产品线覆盖通信与终端。") for i in range(4)]
    r = ev.validate_entity("华为投资价值分析", "company_research", sources)
    assert r["validation_status"] == "unsupported_unlisted_company"
    assert r["listed_status"] == "unlisted"
    assert r["symbol"] is None
    assert len(r["evidence_sources"]) >= 3


def test_entity_validator_verified_carries_symbol_and_exchange(monkeypatch):
    monkeypatch.setattr(ev, "_stock_list_hit", lambda s: (True, "A股代码表命中: 贵州茅台(600519)"))
    monkeypatch.setattr(ev, "check_listed_status", lambda s: {
        "listed": True, "symbol": "600519", "exchange": "上海证券交易所",
        "name": "贵州茅台", "reason": "A股代码表命中：贵州茅台（600519，上海证券交易所）",
        "coverage_note": "本注册表仅覆盖沪/深/北交所 A 股",
    })
    sources = [Source(source_id="s1", content="贵州茅台2025年营收")]
    r = ev.validate_entity("贵州茅台投资价值分析", "company_research", sources)
    assert r["validation_status"] == "verified"
    assert r["listed_status"] == "listed"
    assert r["symbol"] == "600519"
    assert r["exchange"] == "上海证券交易所"


def test_entity_validator_industry_verified():
    sources = [Source(source_id=f"s{i}", content="光伏组件与硅片产能扩张，太阳能装机增长。") for i in range(3)]
    r = ev.validate_entity("光伏行业投资风险分析", "industry_research", sources)
    assert r["validation_status"] == "verified"


def test_entity_validator_macro_research_bypasses_validation():
    """bad_cases 31：宏观研究没有可验证实体概念，不应套用行业式逐字匹配
    （曾导致 9/10 真实 30-case 宏观主题被误判 insufficient_entity_evidence）。"""
    r = ev.validate_entity("中国GDP与经济增长展望", "macro_research", [])
    assert r["validation_status"] == "not_applicable"
    r2 = ev.validate_entity("利率环境与货币政策分析", "risk_research", [])
    assert r2["validation_status"] == "not_applicable"


def test_entity_validator_industry_uses_core_term_not_full_descriptive_phrase():
    """bad_cases 31：不在内置 INDUSTRY_SYNONYMS 里的行业主题，之前只会用整段
    描述性短语（如"创新药行业投资机会"）逐字匹配，真实文章几乎不会出现这个
    完整短语，导致真实合法主题被误判 failed。修复后应从"行业/产业"前提取
    核心词（"创新药"）作为候选，能匹配到只提及核心词的真实来源。"""
    sources = [
        Source(source_id=f"s{i}", title="创新药相关报道",
              content="创新药领域近年持续放量，多家企业创新药管线加速推进。")
        for i in range(3)
    ]
    r = ev.validate_entity("创新药行业投资机会分析", "industry_research", sources)
    assert r["validation_status"] == "verified"
    assert "创新药" in r["matched_aliases"]


def test_entity_validator_industry_still_fails_for_truly_unrelated_sources():
    """核心词提取修复不能让"什么都能过"——完全不相关的来源仍应判 failed。"""
    sources = [Source(source_id=f"s{i}", content="公司营收增长，净利润提升，估值处于低位。")
              for i in range(5)]
    r = ev.validate_entity("创新药行业投资机会分析", "industry_research", sources)
    assert r["validation_status"] == "failed"
