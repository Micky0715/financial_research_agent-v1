"""Macro economic data collector on network-tested free endpoints (v4 stage A).

只封装本环境实测可用的 akshare 宏观接口（2026-07 探测记录见
docs/macro_research.md）。原始数据发布机构（国家统计局/人民银行/海关总署/
国家外汇管理局/美联储等）记录在每个数据点的 source_name；akshare 无法回传
原始 URL 时 no_direct_source_url=True，如实标注转载平台。

实测不可用（不封装、进 KNOWN_UNAVAILABLE，永远如实报缺失）：
- 社会融资规模 macro_china_shrzgm：mofcom SSL 失败
- 城镇调查失业率 macro_china_urban_unemployment：接口返回非 JSON
- 美元指数：无实测通过的稳定免费接口

不接 Wind，不用付费 API，不伪造任何指标。数据仅供研究参考，不构成投资建议。
"""
import json
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from config import config
from schemas.macro_data import MacroDataPoint, MacroSnapshot
from tools.macro_indicator_normalizer import (
    parse_jin10_table,
    parse_month_table,
    parse_named_column_table,
    parse_quarter_table,
)
from utils.logger import logger

_CACHE_PATH = config.CACHE_DIR / "akshare" / "macro_snapshot.json"
_TTL_HOURS = 12

_STATS = "国家统计局"
_EM = "东方财富数据中心(转载)"
_JIN10 = "金十数据(转载)"


def _spec(name_zh, unit, freq, source, platform, endpoint, fetch, parse, transformation=""):
    return {
        "name_zh": name_zh, "unit": unit, "frequency": freq, "source_name": source,
        "original_platform": platform, "endpoint": endpoint, "fetch": fetch,
        "parse": parse, "transformation": transformation,
    }


# key -> spec；fetch(ak)->df, parse(df)->(point_fields|None, series)
_INDICATOR_SPECS: dict[str, dict] = {
    "gdp_yoy": _spec("GDP同比增速", "%", "quarterly", _STATS, _EM, "macro_china_gdp",
                     lambda ak: ak.macro_china_gdp(),
                     lambda df: parse_quarter_table(df, "国内生产总值-同比增长")),
    "cpi_yoy": _spec("CPI同比", "%", "monthly", _STATS, _EM, "macro_china_cpi",
                     lambda ak: ak.macro_china_cpi(),
                     lambda df: parse_month_table(df, "全国-同比增长", mom_col="全国-环比增长")),
    "ppi_yoy": _spec("PPI同比", "%", "monthly", _STATS, _EM, "macro_china_ppi",
                     lambda ak: ak.macro_china_ppi(),
                     lambda df: parse_month_table(df, "当月同比增长")),
    "pmi_manufacturing": _spec("制造业PMI", "点", "monthly", _STATS, _EM, "macro_china_pmi",
                               lambda ak: ak.macro_china_pmi(),
                               lambda df: parse_month_table(df, "制造业-指数")),
    "pmi_non_manufacturing": _spec("非制造业PMI", "点", "monthly", _STATS, _EM, "macro_china_pmi",
                                   lambda ak: ak.macro_china_pmi(),
                                   lambda df: parse_month_table(df, "非制造业-指数")),
    "m1_yoy": _spec("M1同比", "%", "monthly", "中国人民银行", _EM, "macro_china_money_supply",
                    lambda ak: ak.macro_china_money_supply(),
                    lambda df: parse_month_table(df, "货币(M1)-同比增长")),
    "m2_yoy": _spec("M2同比", "%", "monthly", "中国人民银行", _EM, "macro_china_money_supply",
                    lambda ak: ak.macro_china_money_supply(),
                    lambda df: parse_month_table(df, "货币和准货币(M2)-同比增长")),
    "lpr_1y": _spec("1年期LPR", "%", "monthly", "全国银行间同业拆借中心", "中国货币网(转载)",
                    "macro_china_lpr", lambda ak: ak.macro_china_lpr(),
                    lambda df: parse_named_column_table(df, "TRADE_DATE", "LPR1Y")),
    "lpr_5y": _spec("5年期以上LPR", "%", "monthly", "全国银行间同业拆借中心", "中国货币网(转载)",
                    "macro_china_lpr", lambda ak: ak.macro_china_lpr(),
                    lambda df: parse_named_column_table(df, "TRADE_DATE", "LPR5Y")),
    "cn_bond_10y": _spec("中国10年期国债收益率", "%", "daily", "中债/全国银行间市场", "乐咕乐股(转载)",
                         "bond_zh_us_rate", lambda ak: ak.bond_zh_us_rate(),
                         lambda df: parse_named_column_table(df, "日期", "中国国债收益率10年")),
    "us_bond_10y": _spec("美国10年期国债收益率", "%", "daily", "美国财政部", "乐咕乐股(转载)",
                         "bond_zh_us_rate", lambda ak: ak.bond_zh_us_rate(),
                         lambda df: parse_named_column_table(df, "日期", "美国国债收益率10年")),
    "usd_cny": _spec("人民币兑美元中间价", "元/美元", "daily", "国家外汇管理局", "SAFE官网",
                     "currency_boc_safe", lambda ak: ak.currency_boc_safe(),
                     lambda df: _parse_usd_cny(df), "原始口径为元/100美元，已除以100"),
    "exports_yoy": _spec("出口金额同比(美元计)", "%", "monthly", "海关总署", _JIN10,
                         "macro_china_exports_yoy", lambda ak: ak.macro_china_exports_yoy(),
                         parse_jin10_table),
    "imports_yoy": _spec("进口金额同比(美元计)", "%", "monthly", "海关总署", _JIN10,
                         "macro_china_imports_yoy", lambda ak: ak.macro_china_imports_yoy(),
                         parse_jin10_table),
    "trade_balance": _spec("贸易帐(美元计)", "亿美元", "monthly", "海关总署", _JIN10,
                           "macro_china_trade_balance", lambda ak: ak.macro_china_trade_balance(),
                           parse_jin10_table),
    "industrial_production_yoy": _spec("规模以上工业增加值同比", "%", "monthly", _STATS, _JIN10,
                                       "macro_china_industrial_production_yoy",
                                       lambda ak: ak.macro_china_industrial_production_yoy(),
                                       parse_jin10_table),
    "fixed_asset_investment_yoy": _spec("固定资产投资同比", "%", "monthly", _STATS, _EM,
                                        "macro_china_gdzctz", lambda ak: ak.macro_china_gdzctz(),
                                        lambda df: parse_month_table(df, "同比增长")),
    "retail_sales_yoy": _spec("社会消费品零售总额同比", "%", "monthly", _STATS, _EM,
                              "macro_china_consumer_goods_retail",
                              lambda ak: ak.macro_china_consumer_goods_retail(),
                              lambda df: parse_month_table(df, "同比增长")),
    "house_prosperity_index": _spec("国房景气指数", "点", "monthly", _STATS, _EM,
                                    "macro_china_real_estate", lambda ak: ak.macro_china_real_estate(),
                                    lambda df: parse_named_column_table(df, "日期", "最新值")),
    "fx_reserves": _spec("外汇储备", "亿美元", "monthly", "国家外汇管理局", _JIN10,
                         "macro_china_fx_reserves_yearly",
                         lambda ak: ak.macro_china_fx_reserves_yearly(), parse_jin10_table),
    "us_fed_rate": _spec("美联储联邦基金目标利率", "%", "event", "美联储", _JIN10,
                         "macro_bank_usa_interest_rate",
                         lambda ak: ak.macro_bank_usa_interest_rate(), parse_jin10_table),
    "us_cpi_mom": _spec("美国CPI月率", "%", "monthly", "美国劳工统计局", _JIN10,
                        "macro_usa_cpi_monthly", lambda ak: ak.macro_usa_cpi_monthly(),
                        parse_jin10_table),
}

# 实测不可用的指标：永远进 missing_indicators，理由如实给出
KNOWN_UNAVAILABLE: dict[str, str] = {
    "social_financing": "macro_china_shrzgm 数据源(mofcom) SSL 失败，无替代免费接口实测通过",
    "urban_unemployment": "macro_china_urban_unemployment 接口返回非 JSON，实测不可用",
    "usd_index": "无实测通过的稳定免费美元指数接口（不伪造）",
}


def _parse_usd_cny(df):
    point, series = parse_named_column_table(df, "日期", "美元")
    if point is None:
        return None, []
    point["value"] = round(point["value"] / 100, 4)
    if point.get("previous_value") is not None:
        point["previous_value"] = round(point["previous_value"] / 100, 4)
    return point, [(p, round(v / 100, 4)) for p, v in series]


def _akshare():
    try:
        import warnings

        warnings.filterwarnings("ignore")
        import akshare as ak

        return ak
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"akshare unavailable, macro data degraded: {exc}")
        return None


def _load_cache() -> Optional[dict]:
    try:
        if _CACHE_PATH.exists():
            payload = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
            if datetime.now() - datetime.fromisoformat(payload["fetched_at"]) < timedelta(hours=_TTL_HOURS):
                return payload
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_macro_snapshot(keys: Optional[list[str]] = None,
                         use_cache: bool = True) -> dict[str, Any]:
    """主入口：采集全部（或指定 keys）宏观指标 -> MacroSnapshot 信封。

    单指标失败只记入 errors/missing_indicators，绝不中断其余指标，绝不编造。
    """
    envelope: dict[str, Any] = {
        "provider": "akshare", "query_type": "macro_snapshot", "success": False,
        "degraded": True, "error": None, "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "source_type": "structured_macro_data", "snapshot": None,
    }
    requested = keys or list(_INDICATOR_SPECS.keys())

    if keys is None and use_cache:
        cached = _load_cache()
        if cached is not None:
            return cached

    ak = _akshare()
    if ak is None:
        envelope["error"] = "akshare not installed"
        return envelope

    t0 = time.perf_counter()
    indicators: dict[str, MacroDataPoint] = {}
    series: dict[str, list[tuple[str, float]]] = {}
    errors: list[dict] = []
    missing: list[str] = []
    df_cache: dict[str, Any] = {}  # 同一 endpoint 多指标共用一次抓取

    for key in requested:
        spec = _INDICATOR_SPECS.get(key)
        if spec is None:
            missing.append(key)
            if key in KNOWN_UNAVAILABLE:
                errors.append({"indicator": key, "error": KNOWN_UNAVAILABLE[key]})
            continue
        try:
            if spec["endpoint"] not in df_cache:
                df_cache[spec["endpoint"]] = spec["fetch"](ak)
            point_fields, ser = spec["parse"](df_cache[spec["endpoint"]])
        except Exception as exc:  # noqa: BLE001 - each indicator individually expendable
            errors.append({"indicator": key, "error": f"{type(exc).__name__}: {str(exc)[:140]}"})
            missing.append(key)
            continue
        if point_fields is None or point_fields.get("value") is None:
            missing.append(key)
            errors.append({"indicator": key, "error": "no parsable value"})
            continue
        indicators[key] = MacroDataPoint(
            indicator_key=key, indicator_name=spec["name_zh"], unit=spec["unit"],
            frequency=spec["frequency"], source_name=spec["source_name"],
            original_platform=spec["original_platform"], endpoint=spec["endpoint"],
            transformation=spec["transformation"], no_direct_source_url=True,
            fetched_at=envelope["fetched_at"],
            value=point_fields.get("value"), period=str(point_fields.get("period", "")),
            previous_value=point_fields.get("previous_value"),
            yoy=point_fields.get("yoy"), mom=point_fields.get("mom"),
            release_date=str(point_fields.get("release_date", "")),
        )
        if ser:
            series[key] = ser

    for key, reason in KNOWN_UNAVAILABLE.items():
        if key not in missing:
            missing.append(key)
            errors.append({"indicator": key, "error": reason})

    snapshot = MacroSnapshot(
        indicators=indicators, series=series, requested=requested,
        missing_indicators=sorted(set(missing)), errors=errors,
        fetched_at=envelope["fetched_at"], degraded=bool(missing) or bool(errors),
        metadata={"fetch_seconds": round(time.perf_counter() - t0, 2),
                  "indicator_count": len(indicators)},
    )
    envelope["success"] = len(indicators) > 0
    envelope["degraded"] = snapshot.degraded
    envelope["snapshot"] = snapshot.model_dump()
    if not envelope["success"]:
        envelope["error"] = "all macro endpoints failed"
        envelope["snapshot"] = None

    if envelope["success"] and keys is None:
        try:
            _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _CACHE_PATH.write_text(json.dumps(envelope, ensure_ascii=False, default=str),
                                   encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return envelope
