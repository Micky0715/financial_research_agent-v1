"""Cashflow quality analysis from extracted three statements (v4 stage B).

全部指标由真实报表科目计算；输入缺失 -> 指标为 None 并进 missing_fields。
quality_flags 是规则提示（阈值写死可审查），不是审计结论。
"""
from typing import Optional

from schemas.company_research import CashflowQualityResult, ThreeStatements
from tools.financial_statement_extractor import statement_value


def _growth(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None or prev == 0:
        return None
    return round((cur / prev - 1) * 100, 2)


def cashflow_quality_analysis(statements: ThreeStatements) -> CashflowQualityResult:
    result = CashflowQualityResult(symbol=statements.symbol)
    if not statements.periods:
        result.missing_fields = ["all: no statement periods"]
        return result
    latest = statements.periods[0]
    prev = statements.periods[1] if len(statements.periods) > 1 else None
    result.period = latest.period

    revenue = statement_value(latest, "income", "revenue")
    net_profit = statement_value(latest, "income", "net_profit")
    ocf = statement_value(latest, "cashflow", "operating_cash_flow")
    fcf = statement_value(latest, "cashflow", "free_cash_flow")
    capex = statement_value(latest, "cashflow", "capital_expenditure")
    cash_sales = statement_value(latest, "cashflow", "cash_received_from_sales")
    ar = statement_value(latest, "balance", "accounts_receivable")
    inv = statement_value(latest, "balance", "inventory")

    if net_profit:
        if ocf is not None:
            result.ocf_to_net_profit = round(ocf / net_profit, 3)
        if fcf is not None:
            result.fcf_to_net_profit = round(fcf / net_profit, 3)
    if revenue:
        if cash_sales is not None:
            result.cash_revenue_ratio = round(cash_sales / revenue, 3)
        if capex is not None:
            result.capex_intensity = round(capex / revenue, 3)
    if prev is not None:
        result.receivable_growth = _growth(ar, statement_value(prev, "balance", "accounts_receivable"))
        result.inventory_growth = _growth(inv, statement_value(prev, "balance", "inventory"))
        result.revenue_growth = _growth(revenue, statement_value(prev, "income", "revenue"))

    for name, v in [("ocf_to_net_profit", result.ocf_to_net_profit),
                    ("fcf_to_net_profit", result.fcf_to_net_profit),
                    ("cash_revenue_ratio", result.cash_revenue_ratio),
                    ("capex_intensity", result.capex_intensity),
                    ("receivable_growth", result.receivable_growth),
                    ("inventory_growth", result.inventory_growth)]:
        if v is None:
            result.missing_fields.append(name)

    # 规则提示（阈值为工程启发式，写死可审查）
    if result.ocf_to_net_profit is not None:
        if result.ocf_to_net_profit >= 1.0:
            result.quality_flags.append("经营现金流对净利润覆盖充分（比值≥1）")
        elif result.ocf_to_net_profit < 0.6:
            result.quality_flags.append("经营现金流明显弱于净利润（比值<0.6），盈利含金量需关注")
    if result.cash_revenue_ratio is not None and result.cash_revenue_ratio < 0.8:
        result.quality_flags.append("现金收入比<0.8，收入回款质量需关注")
    if (result.receivable_growth is not None and result.revenue_growth is not None
            and result.receivable_growth > result.revenue_growth + 15):
        result.quality_flags.append("应收账款增速显著快于营收（差>15pct），存在垫资/回款风险")
    if (result.inventory_growth is not None and result.revenue_growth is not None
            and result.inventory_growth > result.revenue_growth + 20):
        result.quality_flags.append("存货增速显著快于营收（差>20pct），存在积压风险")
    if result.capex_intensity is not None and result.capex_intensity > 0.25:
        result.quality_flags.append("资本开支强度>25%营收，自由现金流承压")
    return result
