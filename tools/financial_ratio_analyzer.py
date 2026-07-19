"""Financial ratios from extracted three statements (v4 stage B).

输入缺失 -> 比率为 None + missing_fields，不猜。所有比率公式内联可审查。
"""
from typing import Any, Optional

from schemas.company_research import ThreeStatements
from tools.financial_statement_extractor import statement_value


def _ratio(num: Optional[float], den: Optional[float], scale: float = 1,
           digits: int = 3) -> Optional[float]:
    if num is None or den in (None, 0):
        return None
    return round(num / den * scale, digits)


def compute_financial_ratios(statements: ThreeStatements) -> dict[str, Any]:
    """最新期核心比率 + 多期序列（供图表/趋势）。"""
    if not statements.periods:
        return {"symbol": statements.symbol, "ratios": {}, "series": {},
                "missing_fields": ["all: no statement periods"], "period": ""}
    latest = statements.periods[0]
    v = lambda stmt, f, p=latest: statement_value(p, stmt, f)  # noqa: E731

    ratios = {
        "gross_margin_pct": _ratio(v("income", "gross_profit"), v("income", "revenue"), 100, 2),
        "net_margin_pct": _ratio(v("income", "net_profit"), v("income", "revenue"), 100, 2),
        "debt_ratio_pct": _ratio(v("balance", "total_liabilities"), v("balance", "total_assets"), 100, 2),
        "current_ratio": _ratio(v("balance", "current_assets"), v("balance", "current_liabilities")),
        "quick_ratio": _ratio(
            None if v("balance", "current_assets") is None or v("balance", "inventory") is None
            else v("balance", "current_assets") - v("balance", "inventory"),
            v("balance", "current_liabilities")),
        "capex_to_revenue": _ratio(v("cashflow", "capital_expenditure"), v("income", "revenue")),
    }
    series: dict[str, list[tuple[str, float]]] = {}
    for p in reversed(statements.periods):  # 旧->新
        for key, stmt, field in [("revenue", "income", "revenue"),
                                 ("net_profit", "income", "net_profit"),
                                 ("operating_cash_flow", "cashflow", "operating_cash_flow"),
                                 ("free_cash_flow", "cashflow", "free_cash_flow")]:
            val = statement_value(p, stmt, field)
            if val is not None:
                series.setdefault(key, []).append((p.period, val))
    return {
        "symbol": statements.symbol,
        "period": latest.period,
        "ratios": ratios,
        "series": series,
        "missing_fields": [k for k, val in ratios.items() if val is None],
        "note": "比率由新浪三表科目计算（亿元口径），公式见 tools/financial_ratio_analyzer.py",
    }
