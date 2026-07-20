"""Company deep chain (v4 stage B): three-statement extraction / DuPont /
cashflow quality / shareholder structure / peer comparison degrade.

外部网络（akshare 调用）全部 mock；三表字段映射、杜邦公式、现金流比率计算、
同业集中度百分位等核心计算全部真实执行，不 mock。
"""
import pandas as pd
import pytest

import tools.financial_statement_extractor as fse
import tools.peer_comparison as pc
import tools.shareholder_structure as shstruct
from schemas.company_research import DupontResult, StatementItem, StatementPeriod, ThreeStatements
from tools.cashflow_quality_analyzer import cashflow_quality_analysis
from tools.dupont_analyzer import dupont_analysis


class _FakeAk:
    """模拟 akshare 模块：只实现三表/股东测试用到的接口。"""

    def stock_financial_report_sina(self, stock, symbol):
        if symbol == "利润表":
            return pd.DataFrame({
                "报告日": ["20251231", "20241231"],
                "营业总收入": [1720.5417e8, 1741.0e8],
                "营业成本": [148.9e8, 152.0e8],
                "营业利润": [1090.0e8, 1080.0e8],
                "净利润": [862.3e8, 850.0e8],
                "归属于母公司所有者的净利润": [823.2e8, 850.0e8],
                "基本每股收益": [65.5, 67.7],
            })
        if symbol == "资产负债表":
            return pd.DataFrame({
                "报告日": ["20251231", "20241231"],
                "资产总计": [3038.3e8, 2900.0e8],
                "负债合计": [498.6e8, 470.0e8],
                "流动资产合计": [2500.0e8, 2400.0e8],
                "流动负债合计": [491.0e8, 460.0e8],
                "货币资金": [1800.0e8, 1700.0e8],
                "存货": [500.0e8, 400.0e8],
                "应收账款": [10.0e8, 20.0e8],
                "所有者权益(或股东权益)合计": [2539.7e8, 2430.0e8],
                "归属于母公司股东权益合计": [2446.4e8, 2340.0e8],
            })
        if symbol == "现金流量表":
            return pd.DataFrame({
                "报告日": ["20251231", "20241231"],
                "经营活动产生的现金流量净额": [615.2e8, 600.0e8],
                "投资活动产生的现金流量净额": [-200.0e8, -180.0e8],
                "筹资活动产生的现金流量净额": [-300.0e8, -290.0e8],
                "购建固定资产、无形资产和其他长期资产所支付的现金": [31.3e8, 40.0e8],
                "销售商品、提供劳务收到的现金": [1839.0e8, 1850.0e8],
            })
        raise ValueError(f"unexpected statement: {symbol}")

    def stock_main_stock_holder(self, stock):
        return pd.DataFrame([
            {"编号": "1", "股东名称": "控股集团", "持股数量": 1e9, "持股比例": 54.4,
             "股本性质": "国有法人股", "截至日期": pd.Timestamp("2026-03-31"), "股东总数": 243159},
            {"编号": "2", "股东名称": "香港中央结算", "持股数量": 3e8, "持股比例": 9.6,
             "股本性质": "流通A股", "截至日期": pd.Timestamp("2026-03-31"), "股东总数": 243159},
            {"编号": "1", "股东名称": "控股集团", "持股数量": 1e9, "持股比例": 54.4,
             "股本性质": "国有法人股", "截至日期": pd.Timestamp("2025-12-31"), "股东总数": 255892},
        ])


@pytest.fixture
def fake_akshare(monkeypatch):
    fake = _FakeAk()
    monkeypatch.setattr(fse, "_akshare", lambda: fake)
    monkeypatch.setattr(fse, "resolve_symbol", lambda s: ("600519", "贵州茅台"))
    monkeypatch.setattr(fse, "_sina_symbol", lambda code: f"sh{code}")
    monkeypatch.setattr(shstruct, "_akshare", lambda: fake)
    monkeypatch.setattr(shstruct, "resolve_symbol", lambda s: ("600519", "贵州茅台"))
    return fake


# ---------------- three-statement extraction ----------------
def test_extract_three_statements_maps_raw_fields_and_units(fake_akshare, tmp_path, monkeypatch):
    monkeypatch.setattr(fse.config, "CACHE_DIR", tmp_path)
    result = fse.extract_three_statements("贵州茅台", "annual", max_periods=2)
    assert len(result.periods) == 2
    latest = result.periods[0]
    assert latest.period == "2025年报"
    assert abs(latest.income["revenue"].value - 1720.5417) < 0.01  # 元 -> 亿元
    assert latest.income["revenue"].raw_field == "营业总收入"
    assert latest.income["revenue"].unit == "亿元"
    assert latest.income["eps"].unit == "元"  # EPS 不转换单位
    # 推导科目正确标记 derived，不冒充原始字段
    assert latest.income["gross_profit"].derived is True
    assert latest.income["gross_profit"].derived_from == ["revenue", "operating_cost"]
    assert latest.cashflow["free_cash_flow"].derived is True


def test_extract_three_statements_unresolved_symbol_degrades(monkeypatch):
    monkeypatch.setattr(fse, "resolve_symbol", lambda s: None)
    result = fse.extract_three_statements("不存在的公司", "annual")
    assert result.degraded is True
    assert result.periods == []
    assert result.errors


# ---------------- DuPont ----------------
def test_dupont_formula_is_real_multiplication():
    period = StatementPeriod(period="2025年报", income={
        "revenue": StatementItem(field="revenue", value=1720.5),
        "parent_net_profit": StatementItem(field="parent_net_profit", value=823.2),
    }, balance={
        "total_assets": StatementItem(field="total_assets", value=3038.3),
        "parent_equity": StatementItem(field="parent_equity", value=2446.4),
    })
    statements = ThreeStatements(symbol="600519", periods=[period])
    result = dupont_analysis(statements, reported_roe=32.53)
    expected_net_margin = round(823.2 / 1720.5 * 100, 2)
    expected_turnover = round(1720.5 / 3038.3, 4)
    expected_multiplier = round(3038.3 / 2446.4, 4)
    assert result.net_margin == expected_net_margin
    assert result.asset_turnover == expected_turnover
    assert result.equity_multiplier == expected_multiplier
    expected_roe = round(expected_net_margin * expected_turnover * expected_multiplier, 2)
    assert result.calculated_roe == expected_roe
    assert result.difference == round(expected_roe - 32.53, 2)


def test_dupont_missing_inputs_reported_not_guessed():
    statements = ThreeStatements(symbol="X", periods=[StatementPeriod(period="p")])
    result = dupont_analysis(statements)
    assert result.calculated_roe is None
    assert "revenue" in result.missing_fields


# ---------------- cashflow quality ----------------
def test_cashflow_quality_flags_receivable_outrunning_revenue():
    cur = StatementPeriod(period="cur", income={
        "revenue": StatementItem(field="revenue", value=100.0),
        "net_profit": StatementItem(field="net_profit", value=20.0),
    }, cashflow={
        "operating_cash_flow": StatementItem(field="operating_cash_flow", value=10.0),
        "free_cash_flow": StatementItem(field="free_cash_flow", value=5.0),
        "cash_received_from_sales": StatementItem(field="cash_received_from_sales", value=70.0),
        "capital_expenditure": StatementItem(field="capital_expenditure", value=5.0),
    }, balance={
        "accounts_receivable": StatementItem(field="accounts_receivable", value=50.0),
        "inventory": StatementItem(field="inventory", value=20.0),
    })
    prev = StatementPeriod(period="prev", income={
        "revenue": StatementItem(field="revenue", value=95.0),
    }, balance={
        "accounts_receivable": StatementItem(field="accounts_receivable", value=20.0),
        "inventory": StatementItem(field="inventory", value=19.0),
    })
    statements = ThreeStatements(symbol="X", periods=[cur, prev])
    result = cashflow_quality_analysis(statements)
    assert result.ocf_to_net_profit == 0.5
    assert result.cash_revenue_ratio == 0.7
    assert any("应收账款" in f for f in result.quality_flags)
    assert any("弱于净利润" in f for f in result.quality_flags)


# ---------------- shareholder structure ----------------
def test_shareholder_structure_top1_top10_and_holder_trend(fake_akshare):
    result = shstruct.fetch_shareholder_structure("贵州茅台")
    assert result.top1_pct == 54.4
    assert round(result.top10_pct, 1) == 64.0
    assert result.holder_count == 243159
    assert result.holder_count_prev == 255892
    assert "实际控制人" in " ".join(result.limitations)


# ---------------- peer comparison: real degrade, no fabricated averages ----------------
def test_peer_comparison_degrades_when_no_sector_data(monkeypatch):
    monkeypatch.setattr(pc, "load_sector_map", lambda force_refresh=False: {})
    monkeypatch.setattr(pc, "resolve_symbol", lambda s: ("600519", "贵州茅台"))
    result = pc.compare_with_peers("贵州茅台")
    assert result.degraded is True
    assert result.peer_list == []
    assert result.metric_comparison == []
    assert "编造" in result.limitation or "无免费同业数据" in result.limitation


def test_peer_comparison_too_few_members_also_degrades(monkeypatch):
    monkeypatch.setattr(pc, "load_sector_map", lambda force_refresh=False: {
        "600519": {"sector": "酿酒行业", "name": "贵州茅台", "pe": 20, "pb": 6, "mktcap_yi": 15000},
        "600809": {"sector": "酿酒行业", "name": "山西汾酒", "pe": 11, "pb": 3, "mktcap_yi": 1400},
    })
    monkeypatch.setattr(pc, "resolve_symbol", lambda s: ("600519", "贵州茅台"))
    result = pc.compare_with_peers("贵州茅台")
    assert result.degraded is True
    assert "样本过小" in result.limitation
