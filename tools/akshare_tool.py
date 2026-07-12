"""AkShare structured financial data tool (v3 stage A).

只封装在当前网络环境下**实测可用**的接口（探测记录见
docs/akshare_integration.md）：

- ak.stock_financial_abstract(symbol)      新浪财务摘要：营收/净利/现金流/ROE/毛利率/资产负债 等
- ak.stock_zh_valuation_baidu(symbol, ...) 百度估值：市盈率(TTM)/市净率/总市值 序列
- ak.stock_zh_a_daily(symbol)              新浪日线行情：最新收盘价
- ak.stock_info_a_code_name()              全 A 股代码-名称表（символ解析 + 实体验证）

东方财富系接口（stock_individual_info_em / stock_zh_a_hist 等）在本环境被代理
拦截（ProxyError），刻意不依赖。

降级承诺：akshare 未安装、网络失败、代码无法识别、接口字段变化——任何一种情况
都返回 degraded 结果（success=False 或 partial + missing_fields），绝不抛异常
到主流程，绝不编造数据。没有接 Wind：需要商业授权与专用终端，本项目只用公开
免费数据源。数据仅供研究参考，不构成投资建议。
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from config import config
from schemas.financial_data import FinancialDataSnapshot
from tools.financial_data_normalizer import (
    latest_value,
    normalize_financial_abstract,
    series_values,
)
from utils.logger import logger
from utils.text_utils import COMPANY_ALIASES

_CACHE_DIR = config.CACHE_DIR / "akshare"
_CODE_NAME_CACHE = config.CACHE_DIR / "akshare_code_name.json"
_CODE_NAME_TTL_DAYS = 7
_SNAPSHOT_TTL_HOURS = 12


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _akshare():
    """Import akshare lazily; None when not installed (degraded mode)."""
    try:
        import warnings

        warnings.filterwarnings("ignore")
        import akshare as ak

        return ak
    except Exception as exc:  # noqa: BLE001 - missing/broken install is a degraded state, not a crash
        logger.warning(f"akshare unavailable, structured financial data degraded: {exc}")
        return None


def _safe(endpoint: str, fn, errors: list[dict]) -> Optional[Any]:
    """Run one provider call; on any failure record the error and return None."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - every provider call is individually expendable
        errors.append({"endpoint": endpoint, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
        logger.warning(f"akshare {endpoint} failed: {type(exc).__name__}: {str(exc)[:120]}")
        return None


# ------------------------------------------------------------------ #
# Symbol resolution (also reused by entity_validator)
# ------------------------------------------------------------------ #
def load_code_name_map(force_refresh: bool = False) -> dict[str, str]:
    """Full A-share code->name map, cached to disk. {} when unavailable (degraded)."""
    try:
        if _CODE_NAME_CACHE.exists() and not force_refresh:
            payload = json.loads(_CODE_NAME_CACHE.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(payload["fetched_at"])
            if datetime.now() - fetched < timedelta(days=_CODE_NAME_TTL_DAYS):
                return payload["map"]
    except Exception:  # noqa: BLE001 - corrupt cache -> refetch
        pass

    ak = _akshare()
    if ak is None:
        return {}
    errors: list[dict] = []
    df = _safe("stock_info_a_code_name", lambda: ak.stock_info_a_code_name(), errors)
    if df is None:
        return {}
    try:
        mapping = {str(r["code"]): str(r["name"]).replace(" ", "") for _, r in df.iterrows()}
        _CODE_NAME_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CODE_NAME_CACHE.write_text(
            json.dumps({"fetched_at": _now_iso(), "map": mapping}, ensure_ascii=False),
            encoding="utf-8",
        )
        return mapping
    except Exception:  # noqa: BLE001
        return {}


def resolve_symbol(name_or_code: str) -> Optional[tuple[str, str]]:
    """公司名/简称/代码 -> (6位代码, 官方简称)。解析失败返回 None（不猜）。"""
    query = (name_or_code or "").strip()
    if not query:
        return None

    # 1) 项目内置别名表（含股票代码）
    for company, aliases in COMPANY_ALIASES.items():
        if company in query or query in company or any(a and a in query for a in aliases):
            code = next((a for a in aliases if a.isdigit() and len(a) == 6), None)
            if code:
                return code, company

    # 2) 全市场代码表：代码直查 / 名称精确 / 名称包含
    mapping = load_code_name_map()
    if not mapping:
        return None
    if query.isdigit() and len(query) == 6 and query in mapping:
        return query, mapping[query]
    for code, name in mapping.items():
        if name == query:
            return code, name
    matches = [(c, n) for c, n in mapping.items() if n and n in query]
    if len(matches) == 1:
        return matches[0]
    return None


def _sina_symbol(code: str) -> Optional[str]:
    if code.startswith(("60", "68")):
        return f"sh{code}"
    if code.startswith(("00", "30")):
        return f"sz{code}"
    return None  # 北交所等不支持，如实缺失


# ------------------------------------------------------------------ #
# Snapshot assembly
# ------------------------------------------------------------------ #
def _load_snapshot_cache(code: str) -> Optional[dict]:
    path = _CACHE_DIR / f"{code}.json"
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            fetched = datetime.fromisoformat(payload["fetched_at"])
            if datetime.now() - fetched < timedelta(hours=_SNAPSHOT_TTL_HOURS):
                return payload
    except Exception:  # noqa: BLE001
        pass
    return None


def _save_snapshot_cache(code: str, payload: dict) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_CACHE_DIR / f"{code}.json").write_text(
            json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 - cache write failure is not a functional failure
        pass


def fetch_financial_snapshot(name_or_code: str) -> dict[str, Any]:
    """主入口：公司名/代码 -> 结构化财务快照（统一 metadata 信封）。

    信封字段：provider/symbol/query_type/success/degraded/error/fetched_at/
    source_type/errors/snapshot。degraded=True 表示部分或全部字段缺失（原因在
    errors/missing_fields 里），snapshot 里的字段永远只来自 provider 真实返回。
    """
    envelope: dict[str, Any] = {
        "provider": "akshare",
        "symbol": None,
        "query_type": "financial_snapshot",
        "success": False,
        "degraded": True,
        "error": None,
        "errors": [],
        "fetched_at": _now_iso(),
        "source_type": "structured_financial_data",
        "snapshot": None,
    }

    resolved = resolve_symbol(name_or_code)
    if resolved is None:
        envelope["error"] = f"cannot resolve symbol for: {name_or_code}"
        return envelope
    code, company_name = resolved
    envelope["symbol"] = code

    cached = _load_snapshot_cache(code)
    if cached is not None:
        return cached

    ak = _akshare()
    if ak is None:
        envelope["error"] = "akshare not installed"
        return envelope

    errors: list[dict] = envelope["errors"]
    t0 = time.perf_counter()

    # 1) 新浪财务摘要 -> 基本面字段 + 年度序列
    abstract_df = _safe(
        "stock_financial_abstract", lambda: ak.stock_financial_abstract(symbol=code), errors
    )
    normalized = (
        normalize_financial_abstract(abstract_df)
        if abstract_df is not None
        else {"fields": {}, "period": "", "yearly_series": {}, "missing_fields": [
            "revenue", "net_profit", "gross_margin", "roe", "operating_cash_flow",
            "total_assets", "total_liabilities", "debt_ratio",
        ]}
    )
    data_sources = []
    if abstract_df is not None and normalized["fields"]:
        data_sources.append("akshare:stock_financial_abstract(sina)")

    # 2) 百度估值 -> PE(TTM)/PB/总市值（最新值 + 近一年序列）
    multiples_history: dict[str, list[float]] = {}
    pe = pb = market_cap = None
    for indicator, key in [("市盈率(TTM)", "pe"), ("市净率", "pb"), ("总市值", "market_cap")]:
        df = _safe(
            f"stock_zh_valuation_baidu[{indicator}]",
            lambda ind=indicator: ak.stock_zh_valuation_baidu(symbol=code, indicator=ind, period="近一年"),
            errors,
        )
        if df is not None:
            value = latest_value(df)
            if value is not None:
                if key == "pe":
                    pe = value
                    multiples_history["pe"] = series_values(df)
                elif key == "pb":
                    pb = value
                    multiples_history["pb"] = series_values(df)
                else:
                    market_cap = value
                if f"akshare:stock_zh_valuation_baidu" not in data_sources:
                    data_sources.append("akshare:stock_zh_valuation_baidu")

    # 3) 新浪日线 -> 最新收盘价
    price = None
    sina_sym = _sina_symbol(code)
    if sina_sym:
        start = (datetime.now() - timedelta(days=20)).strftime("%Y%m%d")
        daily = _safe(
            "stock_zh_a_daily(sina)",
            lambda: ak.stock_zh_a_daily(symbol=sina_sym, start_date=start, end_date=datetime.now().strftime("%Y%m%d")),
            errors,
        )
        if daily is not None:
            try:
                closes = [float(v) for v in daily["close"] if v == v]
                if closes:
                    price = round(closes[-1], 2)
                    data_sources.append("akshare:stock_zh_a_daily(sina)")
            except Exception:  # noqa: BLE001
                errors.append({"endpoint": "stock_zh_a_daily(sina)", "error": "close column parse failed"})

    fields = normalized["fields"]
    missing = list(normalized["missing_fields"])
    for key, value in [("pe", pe), ("pb", pb), ("ps", None), ("market_cap", market_cap), ("price", price)]:
        if value is None:
            missing.append(key)
    # ps：百度估值无市销率指标；有营收+市值时派生，否则如实缺失
    ps = None
    if market_cap is not None and fields.get("revenue"):
        ps = round(market_cap / fields["revenue"], 2)
        missing.remove("ps")

    snapshot = FinancialDataSnapshot(
        symbol=code,
        company_name=company_name,
        period=normalized["period"],
        revenue=fields.get("revenue"),
        net_profit=fields.get("net_profit"),
        gross_margin=fields.get("gross_margin"),
        roe=fields.get("roe"),
        operating_cash_flow=fields.get("operating_cash_flow"),
        total_assets=fields.get("total_assets"),
        total_liabilities=fields.get("total_liabilities"),
        debt_ratio=fields.get("debt_ratio"),
        pe=pe,
        pb=pb,
        ps=ps,
        market_cap=market_cap,
        price=price,
        yearly_series=normalized["yearly_series"],
        multiples_history=multiples_history,
        data_sources=data_sources,
        missing_fields=sorted(set(missing)),
        fetched_at=_now_iso(),
        metadata={"ps_derived": ps is not None, "fetch_seconds": round(time.perf_counter() - t0, 2)},
    )

    has_fundamentals = any(
        fields.get(k) is not None for k in ("revenue", "net_profit", "operating_cash_flow")
    )
    envelope["success"] = has_fundamentals or pe is not None or price is not None
    envelope["degraded"] = bool(snapshot.missing_fields) or bool(errors)
    envelope["snapshot"] = snapshot.model_dump()
    if not envelope["success"]:
        envelope["error"] = "all provider endpoints failed"
        envelope["snapshot"] = None

    if envelope["success"]:
        _save_snapshot_cache(code, envelope)
    return envelope
