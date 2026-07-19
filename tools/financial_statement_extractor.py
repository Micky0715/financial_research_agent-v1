"""Three-statement extractor on sina statements via akshare (v4 stage B).

ak.stock_financial_report_sina(stock="sh600519", symbol="利润表/资产负债表/现金流量表")
实测返回全部报告期（季报+年报，报告日 YYYYMMDD，元）。本模块：
- 保留原始字段名 + 标准化字段名 + 单位（元 -> 亿元，EPS 保留元）
- 推导科目显式标 derived（毛利、自由现金流）
- 缺失科目进 missing_fields，绝不填充
- 12h 磁盘缓存；任何失败返回 degraded ThreeStatements，不抛异常
"""
import json
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from config import config
from schemas.company_research import StatementItem, StatementPeriod, ThreeStatements
from tools.akshare_tool import _akshare, _safe, _sina_symbol, resolve_symbol
from utils.logger import logger

_CACHE_DIR = config.CACHE_DIR / "akshare"
_TTL_HOURS = 12

# 标准化字段 -> (报表, 候选原始列名列表)。候选按优先级尝试，全部缺失则 missing。
_FIELD_MAP: dict[str, tuple[str, list[str]]] = {
    # 利润表
    "revenue": ("income", ["营业总收入", "营业收入"]),
    "operating_cost": ("income", ["营业成本"]),
    "operating_profit": ("income", ["营业利润"]),
    "net_profit": ("income", ["净利润"]),
    "parent_net_profit": ("income", ["归属于母公司所有者的净利润"]),
    "eps": ("income", ["基本每股收益", "稀释每股收益"]),
    # 资产负债表
    "total_assets": ("balance", ["资产总计"]),
    "total_liabilities": ("balance", ["负债合计"]),
    "current_assets": ("balance", ["流动资产合计"]),
    "current_liabilities": ("balance", ["流动负债合计"]),
    "cash": ("balance", ["货币资金"]),
    "inventory": ("balance", ["存货"]),
    "accounts_receivable": ("balance", ["应收账款"]),
    "short_term_debt": ("balance", ["短期借款"]),
    "long_term_debt": ("balance", ["长期借款"]),
    "bonds_payable": ("balance", ["应付债券"]),
    "equity": ("balance", ["所有者权益(或股东权益)合计", "所有者权益合计"]),
    "parent_equity": ("balance", ["归属于母公司股东权益合计", "归属于母公司所有者权益合计"]),
    # 现金流量表
    "operating_cash_flow": ("cashflow", ["经营活动产生的现金流量净额"]),
    "investing_cash_flow": ("cashflow", ["投资活动产生的现金流量净额"]),
    "financing_cash_flow": ("cashflow", ["筹资活动产生的现金流量净额"]),
    "capital_expenditure": ("cashflow", ["购建固定资产、无形资产和其他长期资产所支付的现金",
                                         "购建固定资产、无形资产和其他长期资产支付的现金"]),
    "cash_received_from_sales": ("cashflow", ["销售商品、提供劳务收到的现金"]),
}
_EPS_FIELDS = {"eps"}


def _period_label(raw: str) -> str:
    y, md = raw[:4], raw[4:8]
    return f"{y}年报" if md == "1231" else f"{y}Q{ {'0331': 1, '0630': 2, '0930': 3}.get(md, '?') }"


def _f(v) -> Optional[float]:
    try:
        if v is None or v != v:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _yi(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v / 1e8, 4)


def extract_three_statements(name_or_code: str, frequency: str = "annual",
                             max_periods: int = 5) -> ThreeStatements:
    """公司名/代码 -> 三表（新->旧 max_periods 期）。失败返回 degraded 空结果。"""
    fetched_at = datetime.now().isoformat(timespec="seconds")
    resolved = resolve_symbol(name_or_code)
    if resolved is None:
        return ThreeStatements(symbol=str(name_or_code), degraded=True, fetched_at=fetched_at,
                               errors=[{"error": f"cannot resolve symbol: {name_or_code}"}])
    code, company_name = resolved
    result = ThreeStatements(symbol=code, company_name=company_name, frequency=frequency,
                             fetched_at=fetched_at)

    cache_path = _CACHE_DIR / f"statements_{code}_{frequency}.json"
    try:
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if datetime.now() - datetime.fromisoformat(payload["fetched_at"]) < timedelta(hours=_TTL_HOURS):
                return ThreeStatements(**payload)
    except Exception:  # noqa: BLE001 - corrupt cache -> refetch
        pass

    ak = _akshare()
    sina_sym = _sina_symbol(code)
    if ak is None or sina_sym is None:
        result.degraded = True
        result.errors.append({"error": "akshare unavailable" if ak is None
                              else f"unsupported exchange for sina statements: {code}"})
        return result

    errors: list[dict] = []
    frames: dict[str, Any] = {}
    for stmt, zh in [("income", "利润表"), ("balance", "资产负债表"), ("cashflow", "现金流量表")]:
        df = _safe(f"stock_financial_report_sina[{zh}]",
                   lambda z=zh: ak.stock_financial_report_sina(stock=sina_sym, symbol=z), errors)
        if df is not None:
            try:
                frames[stmt] = {str(r["报告日"]): r for r in df.to_dict("records")}
            except Exception:  # noqa: BLE001
                errors.append({"endpoint": f"sina[{zh}]", "error": "报告日 column parse failed"})
    result.errors = errors
    if not frames:
        result.degraded = True
        return result

    all_dates = sorted({d for f in frames.values() for d in f}, reverse=True)
    if frequency == "annual":
        dates = [d for d in all_dates if d.endswith("1231")][:max_periods]
    else:
        dates = all_dates[:max_periods]

    for date in dates:
        period = StatementPeriod(period=_period_label(date), period_raw=date)
        rows = {stmt: frames.get(stmt, {}).get(date) for stmt in ("income", "balance", "cashflow")}
        for field, (stmt, candidates) in _FIELD_MAP.items():
            row = rows.get(stmt)
            raw_name, value = "", None
            if row is not None:
                for cand in candidates:
                    v = _f(row.get(cand))
                    if v is not None:
                        raw_name, value = cand, v
                        break
            is_eps = field in _EPS_FIELDS
            item = StatementItem(
                field=field, raw_field=raw_name,
                value=value if is_eps else _yi(value),
                unit="元" if is_eps else "亿元",
                missing=value is None,
            )
            getattr(period, stmt)[field] = item
            if value is None:
                period.missing_fields.append(field)

        # 推导科目：毛利、自由现金流（显式 derived，绝不冒充原始科目）
        inc, cf = period.income, period.cashflow
        if not inc["revenue"].missing and not inc["operating_cost"].missing:
            inc["gross_profit"] = StatementItem(
                field="gross_profit", raw_field="", derived=True,
                derived_from=["revenue", "operating_cost"],
                value=round(inc["revenue"].value - inc["operating_cost"].value, 4))
        else:
            period.missing_fields.append("gross_profit")
        if not cf["operating_cash_flow"].missing and not cf["capital_expenditure"].missing:
            cf["free_cash_flow"] = StatementItem(
                field="free_cash_flow", raw_field="", derived=True,
                derived_from=["operating_cash_flow", "capital_expenditure"],
                value=round(cf["operating_cash_flow"].value - cf["capital_expenditure"].value, 4))
        else:
            period.missing_fields.append("free_cash_flow")
        result.periods.append(period)

    result.degraded = bool(errors) or any(p.missing_fields for p in result.periods)
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(result.model_dump_json(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    logger.info(f"three statements {code}: {len(result.periods)} periods, degraded={result.degraded}")
    return result


def statement_value(period: StatementPeriod, stmt: str, field: str) -> Optional[float]:
    item = getattr(period, stmt, {}).get(field)
    return item.value if item is not None and not item.missing else None
