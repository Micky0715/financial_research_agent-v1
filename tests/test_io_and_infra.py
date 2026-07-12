"""local file reader / chart guards / memory / gateway fallback / akshare degrade / normalizer 测试。"""
from pathlib import Path

import config as cfgmod
import memory.vector_store as vs
from memory.vector_store import LightVectorStore
from schemas.source import Source
from tools.chart_renderer import extract_metric_series
from tools.financial_data_normalizer import normalize_financial_abstract
from tools.local_file_reader import build_local_sources, read_local_file

_FIXTURES = Path(__file__).parent / "fixtures"


# ---------------- local file reader ----------------
def test_local_txt_extracts_financials():
    src = read_local_file(str(_FIXTURES / "sample_report.txt"), "s901", "比亚迪")
    assert src is not None and src.source_type == "local_file"
    assert src.authority_tier == "local_user_file"
    fin = src.metadata.get("extracted_financials", {})
    assert (2024, 7771.02) in fin.get("营收", [])


def test_local_csv_and_missing_file():
    sources = build_local_sources(
        [str(_FIXTURES / "sample_financials.csv"), str(_FIXTURES / "no_such_file.pdf")], "比亚迪"
    )
    assert len(sources) == 1  # missing file skipped, no exception
    assert sources[0].metadata["extracted_tables"][0]["rows"] == 3


def test_local_unsupported_type(tmp_path):
    p = tmp_path / "x.exe"
    p.write_bytes(b"xx")
    assert read_local_file(str(p), "s901") is None


# ---------------- chart extraction guards ----------------
def test_chart_guard_quarterly_scope():
    src = Source(source_id="s1", content="2022年前三季度归母净利润91亿元。2023年净利润300亿元。")
    series = extract_metric_series([src], "净利润", "")
    assert (2022, 91.0) not in series  # 季度口径被守卫排除


def test_chart_guard_other_company():
    src = Source(source_id="s1", content="作为对比，比亚迪在2023年的年度净利润是300亿元。")
    series = extract_metric_series([src], "净利润", "宁德时代")
    assert series == []  # 竞争对手数字不归属主体


def test_chart_guard_multi_metric_sentence():
    src = Source(source_id="s1", content="预计2024年营收3560亿元，比2023年的4009亿元下降，净利润490亿元。")
    series = extract_metric_series([src], "净利润", "")
    assert (2023, 4009.0) not in series  # 多指标句只信严格邻近


# ---------------- memory (keyword fallback, no model needed) ----------------
def test_memory_keyword_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(vs, "_embed", lambda texts: None)  # 强制无模型降级
    monkeypatch.setattr(vs, "_INDEX_DIR", tmp_path)
    monkeypatch.setattr(vs, "_RECORDS_PATH", tmp_path / "records.jsonl")
    monkeypatch.setattr(vs, "_EMBEDDINGS_PATH", tmp_path / "embeddings.npy")
    store = LightVectorStore()
    result = store.rebuild(
        ["比亚迪估值分析报告摘要", "光伏行业风险研究"],
        [{"kind": "report_summary", "topic": "比亚迪"}, {"kind": "report_summary", "topic": "光伏"}],
    )
    assert result["vector_mode"] is False  # 降级模式
    hits = LightVectorStore().search("比亚迪估值")
    assert hits and hits[0]["topic"] == "比亚迪"


# ---------------- tool gateway graceful fallback ----------------
def test_gateway_direct_fallback(monkeypatch):
    import tools.tool_gateway as gw

    monkeypatch.setattr(cfgmod.config, "USE_MCP_TOOLS", False)
    called = {}

    def fake_direct(query, max_results=8):
        called["q"] = query
        return [{"title": "t", "url": "https://x.com", "snippet": ""}]

    import tools.web_search as ws

    monkeypatch.setattr(ws, "web_search", fake_direct)
    out = gw.web_search("测试查询", max_results=2)
    assert called["q"] == "测试查询" and len(out) == 1


# ---------------- akshare degraded envelopes ----------------
def test_akshare_unresolvable_symbol():
    import tools.akshare_tool as at

    r = at.fetch_financial_snapshot("这绝对不是一家公司XX9Z")
    assert r["success"] is False and r["snapshot"] is None
    assert "cannot resolve" in (r["error"] or "")


def test_akshare_not_installed(monkeypatch):
    import tools.akshare_tool as at

    monkeypatch.setattr(at, "_akshare", lambda: None)
    monkeypatch.setattr(at, "_load_snapshot_cache", lambda code: None)
    r = at.fetch_financial_snapshot("贵州茅台")  # resolves via alias map, then provider missing
    assert r["success"] is False and r["error"] == "akshare not installed"


# ---------------- normalizer ----------------
def test_normalizer_units_and_malformed():
    import pandas as pd

    df = pd.DataFrame({
        "指标": ["营业总收入", "归母净利润", "净资产收益率(ROE)"],
        "20241231": [1.741e11, 8.5e10, 34.46],
        "20231231": [1.505e11, 7.4e10, 31.2],
    })
    out = normalize_financial_abstract(df)
    assert abs(out["fields"]["revenue"] - 1741.0) < 1     # 元 -> 亿元
    assert out["period"] == "2024年报"
    assert (2023, 1505.0) in [(y, round(v)) for y, v in out["yearly_series"]["revenue"]]
    # malformed frame -> everything missing, no crash
    bad = normalize_financial_abstract(object())
    assert bad["fields"] == {} and bad["missing_fields"]
