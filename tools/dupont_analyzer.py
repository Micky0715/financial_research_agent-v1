"""DuPont decomposition from extracted three statements (v4 stage B).

ROE = 净利率 × 总资产周转率 × 权益乘数
（归母口径：归母净利/营业总收入 × 营业总收入/期末总资产 × 期末总资产/归母权益）

任何输入缺失 -> 对应输出为 None 并进 missing_fields，不推算不猜。
reported_roe 来自新浪财务摘要（FinancialDataSnapshot.roe），口径差异在
formula_note 里明示（期末总资产 vs 平均总资产）。
"""
from typing import Optional

from schemas.company_research import DupontResult, ThreeStatements
from tools.financial_statement_extractor import statement_value


def dupont_analysis(statements: ThreeStatements,
                    reported_roe: Optional[float] = None) -> DupontResult:
    result = DupontResult(symbol=statements.symbol, reported_roe=reported_roe)
    if not statements.periods:
        result.missing_fields = ["all: no statement periods"]
        return result
    latest = statements.periods[0]
    result.period = latest.period

    revenue = statement_value(latest, "income", "revenue")
    parent_np = statement_value(latest, "income", "parent_net_profit")
    total_assets = statement_value(latest, "balance", "total_assets")
    parent_equity = statement_value(latest, "balance", "parent_equity")

    for name, v in [("revenue", revenue), ("parent_net_profit", parent_np),
                    ("total_assets", total_assets), ("parent_equity", parent_equity)]:
        if v is None:
            result.missing_fields.append(name)

    if revenue and parent_np is not None:
        result.net_margin = round(parent_np / revenue * 100, 2)
    if revenue and total_assets:
        result.asset_turnover = round(revenue / total_assets, 4)
    if total_assets and parent_equity:
        result.equity_multiplier = round(total_assets / parent_equity, 4)
    if None not in (result.net_margin, result.asset_turnover, result.equity_multiplier):
        result.calculated_roe = round(
            result.net_margin * result.asset_turnover * result.equity_multiplier, 2)
        if reported_roe is not None:
            result.difference = round(result.calculated_roe - reported_roe, 2)
    return result
