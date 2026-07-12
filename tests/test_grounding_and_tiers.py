"""source tier / number grounding / citation checker 测试。"""
from tools.citation_checker import check_citations
from tools.number_grounding import analyze_report_numbers
from tools.source_tier import classify_source, tier_counts


def test_source_tier_classification():
    assert classify_source("https://static.cninfo.com.cn/f.pdf")["authority_tier"] == "tier1"
    assert classify_source("https://www.stcn.com/article/1.html")["authority_tier"] == "tier2"
    t3 = classify_source("https://xueqiu.com/123/456")
    assert t3["authority_tier"] == "tier3" and t3["is_user_generated_content"]
    assert classify_source("")["authority_tier"] == "unknown"
    assert classify_source("", source_type="local_file")["authority_tier"] == "local_user_file"
    # 公告 PDF 标题信号升 tier1
    assert classify_source("https://random-site.com/a.pdf", title="2025年度报告全文", source_type="pdf")[
        "authority_tier"] == "tier1"


def test_tier_counts_ratio():
    classified = [classify_source(u) for u in (
        "https://static.cninfo.com.cn/a.pdf", "https://www.stcn.com/1", "https://blog.example.com/x")]
    counts = tier_counts(classified)
    assert counts["total"] == 3 and abs(counts["tier1_or_tier2_ratio"] - 2 / 3) < 0.01


def test_citation_checker_flags_hallucinated_ids():
    r = check_citations("营收增长 [s1]，净利大增 [s9]。", ["s1", "s2"])
    assert r["invalid_citations"] == ["s9"]
    assert r["invalid_citation_count"] == 1


def test_number_grounding_categories():
    md = (
        "## 财务\n营收8040亿元，同比增长3.46% [s1]。\n"
        "净利润823.2亿元（AkShare）。\n"
        "折现率取9%（配置假设）。\n"
        "基准情形模型测算估值5872亿元。\n"
        "毛利率高达26.27%。\n"  # 无任何标注 -> unsourced
    )
    r = analyze_report_numbers(md, ["s1"], snapshot={"net_profit": 823.2}, dcf={"valuation_range": {"base": 5872.0}})
    assert r["sourced_number_count"] == 2       # 8040亿元 + 3.46%
    assert r["akshare_number_count"] == 1
    assert r["assumption_number_count"] == 1
    assert r["calculated_number_count"] == 1
    assert r["unsourced_number_count"] == 1
    assert r["value_verified"]["akshare"] == 1  # 823.2 与 snapshot 对上
    assert r["value_verified"]["calculated"] == 1
    assert 0 < r["number_grounding_rate"] < 1


def test_number_grounding_years_not_counted():
    r = analyze_report_numbers("2025年公司经营稳健。", ["s1"])
    assert r["total_number_count"] == 0  # 年份不算实质性数字
