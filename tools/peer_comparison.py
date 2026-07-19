"""Real peer comparison on free sina sector data (v4 stage B).

同业集合来自新浪行业板块真实成分（ak.stock_sector_spot + stock_sector_detail，
全市场 code->sector 映射缓存 7 天）；同业估值（PE/PB/市值）来自板块行情快照，
成长/盈利指标来自各 peer 的新浪财务摘要（12h 缓存）。拿不到同业数据时返回
degraded + limitation——绝不编造行业均值（项目红线）。
"""
import json
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from config import config
from schemas.company_research import PeerComparison, PeerMetric
from tools.akshare_tool import _akshare, _safe, resolve_symbol
from tools.financial_data_normalizer import normalize_financial_abstract
from utils.logger import logger

_SECTOR_MAP_CACHE = config.CACHE_DIR / "akshare_sector_map.json"
_SECTOR_MAP_TTL_DAYS = 7
_ABSTRACT_CACHE_DIR = config.CACHE_DIR / "akshare" / "abstracts"
_ABSTRACT_TTL_HOURS = 12
_MAX_PEERS = 4
_BAD_NAME_TOKENS = ("ST", "退市", "退")


def load_sector_map(force_refresh: bool = False) -> dict[str, Any]:
    """{code: {"sector": 板块名, "label": label, "pe":, "pb":, "mktcap_yi":, "name":}}。

    首次构建需遍历全部新浪行业板块（~49 次调用，1-2 分钟），此后走 7 天缓存。
    失败返回 {}（degraded）。
    """
    try:
        if _SECTOR_MAP_CACHE.exists() and not force_refresh:
            payload = json.loads(_SECTOR_MAP_CACHE.read_text(encoding="utf-8"))
            if datetime.now() - datetime.fromisoformat(payload["fetched_at"]) < timedelta(days=_SECTOR_MAP_TTL_DAYS):
                return payload["map"]
    except Exception:  # noqa: BLE001 - corrupt cache -> rebuild
        pass

    ak = _akshare()
    if ak is None:
        return {}
    errors: list[dict] = []
    spot = _safe("stock_sector_spot", lambda: ak.stock_sector_spot(indicator="新浪行业"), errors)
    if spot is None:
        return {}
    mapping: dict[str, Any] = {}
    t0 = time.perf_counter()
    for _, row in spot.iterrows():
        label, sector_name = str(row.get("label", "")), str(row.get("板块", ""))
        if not label:
            continue
        detail = _safe(f"stock_sector_detail[{sector_name}]",
                       lambda lb=label: ak.stock_sector_detail(sector=lb), errors)
        if detail is None:
            continue
        try:
            for _, s in detail.iterrows():
                code = str(s.get("code", ""))
                if len(code) == 6:
                    mapping[code] = {
                        "sector": sector_name, "label": label, "name": str(s.get("name", "")),
                        "pe": float(s["per"]) if s.get("per") not in (None, 0) else None,
                        "pb": float(s["pb"]) if s.get("pb") not in (None, 0) else None,
                        "mktcap_yi": round(float(s["mktcap"]) / 1e4, 2) if s.get("mktcap") else None,
                    }
        except Exception:  # noqa: BLE001 - one sector's rows are expendable
            continue
    if mapping:
        try:
            _SECTOR_MAP_CACHE.write_text(json.dumps(
                {"fetched_at": datetime.now().isoformat(timespec="seconds"), "map": mapping},
                ensure_ascii=False), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"sector map built: {len(mapping)} stocks in {round(time.perf_counter()-t0,1)}s "
                    f"({len(errors)} sector errors)")
    return mapping


def _fetch_abstract_fields(code: str) -> Optional[dict]:
    """peer 的新浪财务摘要归一化字段 + 年度序列（12h 缓存）。失败 None。"""
    cache = _ABSTRACT_CACHE_DIR / f"{code}.json"
    try:
        if cache.exists():
            payload = json.loads(cache.read_text(encoding="utf-8"))
            if datetime.now() - datetime.fromisoformat(payload["fetched_at"]) < timedelta(hours=_ABSTRACT_TTL_HOURS):
                return payload["data"]
    except Exception:  # noqa: BLE001
        pass
    ak = _akshare()
    if ak is None:
        return None
    errors: list[dict] = []
    df = _safe("stock_financial_abstract", lambda: ak.stock_financial_abstract(symbol=code), errors)
    if df is None:
        return None
    data = normalize_financial_abstract(df)
    try:
        _ABSTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(
            {"fetched_at": datetime.now().isoformat(timespec="seconds"), "data": data},
            ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return data


def _rev_growth(data: Optional[dict]) -> Optional[float]:
    try:
        series = data["yearly_series"]["revenue"]
        if len(series) >= 2 and series[-2][1]:
            return round((series[-1][1] / series[-2][1] - 1) * 100, 2)
    except (KeyError, TypeError, IndexError):
        pass
    return None


def _np_growth(data: Optional[dict]) -> Optional[float]:
    try:
        series = data["yearly_series"]["net_profit"]
        if len(series) >= 2 and series[-2][1]:
            return round((series[-1][1] / series[-2][1] - 1) * 100, 2)
    except (KeyError, TypeError, IndexError):
        pass
    return None


def _field(data: Optional[dict], key: str) -> Optional[float]:
    try:
        return data["fields"].get(key)
    except (KeyError, TypeError):
        return None


def _ocf_ratio(data: Optional[dict]) -> Optional[float]:
    ocf, np_ = _field(data, "operating_cash_flow"), _field(data, "net_profit")
    if ocf is None or not np_:
        return None
    return round(ocf / np_, 2)


def _percentile(company: Optional[float], peers: dict[str, Optional[float]]) -> Optional[float]:
    if company is None:
        return None
    values = [v for v in peers.values() if v is not None] + [company]
    if len(values) < 3:
        return None  # 样本太小，分位无意义，如实缺失
    below = sum(1 for v in values if v < company)
    return round(below / (len(values) - 1) * 100, 1)


def compare_with_peers(name_or_code: str) -> PeerComparison:
    """主入口：真实同业比较。同业数据不可得 -> degraded，绝不编造均值。"""
    fetched_at = datetime.now().isoformat(timespec="seconds")
    resolved = resolve_symbol(name_or_code)
    if resolved is None:
        return PeerComparison(symbol=str(name_or_code), degraded=True, fetched_at=fetched_at,
                              limitation=f"cannot resolve symbol: {name_or_code}")
    code, company_name = resolved
    result = PeerComparison(symbol=code, fetched_at=fetched_at)

    sector_map = load_sector_map()
    if not sector_map or code not in sector_map:
        result.degraded = True
        result.limitation = ("新浪行业板块成分不可用或该股不在板块内——无免费同业数据，"
                             "本次不做同业比较（拒绝编造行业均值）")
        return result

    me = sector_map[code]
    result.sector = me["sector"]
    members = [(c, info) for c, info in sector_map.items()
               if info["sector"] == me["sector"] and c != code
               and info.get("mktcap_yi") and info["mktcap_yi"] > 0
               and not any(t in info.get("name", "") for t in _BAD_NAME_TOKENS)]
    if len(members) < 2:
        result.degraded = True
        result.limitation = f"板块「{me['sector']}」有效同业不足 2 家，样本过小，不做比较"
        return result

    my_cap = me.get("mktcap_yi")
    if my_cap:
        members.sort(key=lambda x: abs(x[1]["mktcap_yi"] - my_cap))
        reason = (f"新浪行业板块「{me['sector']}」全部成分中，剔除 ST/退市/无市值个股后，"
                  f"按总市值与 {company_name}（{my_cap}亿元）最接近选取 {_MAX_PEERS} 家")
    else:
        members.sort(key=lambda x: -x[1]["mktcap_yi"])
        reason = (f"目标市值缺失，改按板块「{me['sector']}」市值最大 {_MAX_PEERS} 家作为参照")
    peers = members[:_MAX_PEERS]
    result.peer_list = [{"code": c, "name": i["name"], "market_cap_yi": i["mktcap_yi"]}
                        for c, i in peers]
    result.peer_selection_reason = reason

    my_abstract = _fetch_abstract_fields(code)
    peer_abstracts = {i["name"]: _fetch_abstract_fields(c) for c, i in peers}

    metric_defs = [
        ("营收同比增速", "%", lambda d: _rev_growth(d), _rev_growth(my_abstract), "新浪财务摘要(年度)"),
        ("归母净利同比增速", "%", lambda d: _np_growth(d), _np_growth(my_abstract), "新浪财务摘要(年度)"),
        ("毛利率", "%", lambda d: _field(d, "gross_margin"), _field(my_abstract, "gross_margin"), "新浪财务摘要"),
        ("ROE", "%", lambda d: _field(d, "roe"), _field(my_abstract, "roe"), "新浪财务摘要"),
        ("经营现金流/净利润", "倍", lambda d: _ocf_ratio(d), _ocf_ratio(my_abstract), "新浪财务摘要"),
        ("PE", "倍", None, me.get("pe"), "新浪板块行情"),
        ("PB", "倍", None, me.get("pb"), "新浪板块行情"),
        ("总市值", "亿元", None, my_cap, "新浪板块行情"),
    ]
    quote_key = {"PE": "pe", "PB": "pb", "总市值": "mktcap_yi"}
    for label, unit, fn, my_value, source in metric_defs:
        if fn is not None:
            peer_values = {name: fn(d) for name, d in peer_abstracts.items()}
        else:
            peer_values = {i["name"]: i.get(quote_key[label]) for _, i in peers}
        metric = PeerMetric(metric=label, company_value=my_value, peer_values=peer_values,
                            company_percentile=_percentile(my_value, peer_values),
                            unit=unit, source=f"AkShare·{source}")
        result.metric_comparison.append(metric)
        if my_value is None:
            result.missing_fields.append(label)

    result.limitation = ("同业集合为新浪行业板块口径（非申万/证监会行业），估值为行情快照，"
                         "成长/盈利为最新年度摘要口径；样本仅含所选 peer，不代表全行业均值")
    result.degraded = bool(result.missing_fields)
    result.metadata = {"company_name": company_name, "sector_members": len(members) + 1}
    logger.info(f"peer comparison {code}: sector={result.sector} peers={[p['name'] for p in result.peer_list]}")
    return result
