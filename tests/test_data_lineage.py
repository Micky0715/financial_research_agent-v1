"""tools/data_lineage.py: AkShare field-level lineage (v4 stage I)."""
from tools.data_lineage import build_ps_lineage, build_snapshot_lineage, lineage_summary


def test_build_snapshot_lineage_basic_fields():
    fields = {"revenue": 1720.5, "net_profit": 823.2, "debt_ratio": 16.4}
    lineage = build_snapshot_lineage(fields, "2025年报", "2026-07-20T00:00:00", [])
    assert set(lineage.keys()) == {"revenue", "net_profit", "debt_ratio"}
    rev = lineage["revenue"]
    assert rev["raw_field"] == "营业总收入"
    assert rev["value"] == 1720.5
    assert rev["unit"] == "亿元"
    assert rev["provider"] == "akshare"
    assert rev["original_platform"] == "新浪财经"
    assert rev["no_direct_source_url"] is True
    assert rev["missing"] is False
    assert rev["derived_from"] == []
    assert "元 -> 亿元" in rev["transformation"]


def test_build_snapshot_lineage_marks_missing():
    fields = {"revenue": None}
    lineage = build_snapshot_lineage(fields, "", "2026-07-20T00:00:00", ["revenue"])
    assert lineage["revenue"]["missing"] is True
    assert lineage["revenue"]["confidence"] == "low"


def test_build_snapshot_lineage_derived_field():
    fields = {"debt_ratio": 55.0}
    lineage = build_snapshot_lineage(
        fields, "2025年报", "2026-07-20T00:00:00", [],
        derived_fields={"debt_ratio": ["total_liabilities", "total_assets"]})
    entry = lineage["debt_ratio"]
    assert entry["derived_from"] == ["total_liabilities", "total_assets"]
    assert entry["confidence"] == "medium"
    assert entry["raw_field"] == ""  # 推导字段不冒充有原始指标名
    assert "total_liabilities" in entry["transformation"]


def test_build_ps_lineage_is_always_derived():
    entry = build_ps_lineage(9.65, 16594.83, 1720.5, "2025年报", "2026-07-20T00:00:00")
    assert entry["normalized_field"] == "ps"
    assert entry["derived_from"] == ["market_cap", "revenue"]
    assert entry["no_direct_source_url"] is True
    assert entry["missing"] is False


def test_build_ps_lineage_missing_when_no_value():
    entry = build_ps_lineage(None, None, None, "", "2026-07-20T00:00:00")
    assert entry["missing"] is True
    assert entry["confidence"] == "low"


def test_lineage_summary_counts():
    lineage = {
        "revenue": {"endpoint_description": "stock_financial_abstract（真实）",
                    "no_direct_source_url": True, "derived_from": [], "missing": False},
        "debt_ratio": {"endpoint_description": "stock_financial_abstract（真实）",
                       "no_direct_source_url": True, "derived_from": ["a", "b"], "missing": False},
        "total_assets": {"endpoint_description": "stock_financial_abstract（真实）",
                         "no_direct_source_url": True, "derived_from": [], "missing": True},
        "unknown_field": {"endpoint_description": "unknown_endpoint（字段来源未登记）",
                          "no_direct_source_url": True, "derived_from": [], "missing": False},
    }
    s = lineage_summary(lineage)
    assert s["total_fields"] == 4
    assert s["fields_with_source"] == 3   # unknown_endpoint 不计入
    assert s["fields_no_direct_url"] == 4
    assert s["derived_fields"] == 1
    assert s["missing_fields"] == 1
