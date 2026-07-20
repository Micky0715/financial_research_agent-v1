"""AkShare field-level data lineage builder (v4 stage I).

不满足于 provider=akshare：逐字段记录归一化字段名/原始字段名/取值/单位/
报告期/provider/原始发布平台/来源URL或接口说明/抓取时间/换算说明/推导来源/
是否缺失/置信度（schemas/financial_data.py::LineageEntry）。

AkShare 大多数接口不会回传原始数据的精确 URL（它只是转载/聚合免费公开
数据），本模块如实记录：接口名称 + 原始发布平台 + no_direct_source_url=True，
不编造一个看起来精确的 URL。
"""
from typing import Any, Optional

from schemas.financial_data import LineageEntry
from tools.financial_data_normalizer import INDICATOR_RAW_FIELD_MAP, MONEY_FIELDS

# 归一化字段 -> (provider接口名, 原始发布/转载平台, 接口说明)
_ABSTRACT_ENDPOINT = ("stock_financial_abstract", "新浪财经",
                     "新浪财经个股财务摘要接口（历年年报核心指标矩阵）")
_VALUATION_ENDPOINT = ("stock_zh_valuation_baidu", "百度股市通",
                       "百度股市通个股估值指标接口（PE/PB/总市值 近一年序列）")
_DAILY_ENDPOINT = ("stock_zh_a_daily", "新浪财经",
                   "新浪财经个股日线行情接口（取最新收盘价）")

_FIELD_SOURCE: dict[str, tuple[str, str, str]] = {
    "revenue": _ABSTRACT_ENDPOINT, "net_profit": _ABSTRACT_ENDPOINT,
    "operating_cash_flow": _ABSTRACT_ENDPOINT, "total_assets": _ABSTRACT_ENDPOINT,
    "total_liabilities": _ABSTRACT_ENDPOINT, "roe": _ABSTRACT_ENDPOINT,
    "gross_margin": _ABSTRACT_ENDPOINT, "debt_ratio": _ABSTRACT_ENDPOINT,
    "pe": _VALUATION_ENDPOINT, "pb": _VALUATION_ENDPOINT, "market_cap": _VALUATION_ENDPOINT,
    "price": _DAILY_ENDPOINT,
}
_UNIT_BY_FIELD = {
    "revenue": "亿元", "net_profit": "亿元", "operating_cash_flow": "亿元",
    "total_assets": "亿元", "total_liabilities": "亿元", "gross_margin": "%",
    "roe": "%", "debt_ratio": "%", "pe": "倍", "pb": "倍", "ps": "倍",
    "market_cap": "亿元", "price": "元",
}


def build_snapshot_lineage(fields: dict[str, Optional[float]], period: str, fetched_at: str,
                           missing_fields: list[str],
                           derived_fields: Optional[dict[str, list[str]]] = None
                           ) -> dict[str, dict[str, Any]]:
    """归一化字段 dict -> {field: LineageEntry.model_dump()}。

    derived_fields: {field: [来源字段列表]}，如 {"debt_ratio": ["total_liabilities","total_assets"]}
    ——debt_ratio 缺失时由资产/负债推导，需和 normalizer 里的推导逻辑保持一致。
    """
    derived_fields = derived_fields or {}
    lineage: dict[str, dict[str, Any]] = {}
    missing_set = set(missing_fields)

    for field, value in fields.items():
        is_derived = field in derived_fields
        endpoint, platform, desc = _FIELD_SOURCE.get(
            field, ("unknown_endpoint", "unknown", "字段来源未登记"))
        entry = LineageEntry(
            normalized_field=field,
            raw_field="" if is_derived else INDICATOR_RAW_FIELD_MAP.get(field, ""),
            value=value, unit=_UNIT_BY_FIELD.get(field, ""), period=period,
            provider="akshare", original_platform=platform,
            source_url="", endpoint_description=f"{endpoint}（{desc}）",
            no_direct_source_url=True, fetched_at=fetched_at,
            transformation=("元 -> 亿元（数量级启发式换算）" if field in MONEY_FIELDS and not is_derived
                            else ("由 " + "/".join(derived_fields.get(field, [])) + " 计算得出"
                                  if is_derived else "")),
            derived_from=derived_fields.get(field, []),
            missing=(field in missing_set) or value is None,
            confidence="medium" if is_derived else ("high" if value is not None else "low"),
        )
        lineage[field] = entry.model_dump()
    return lineage


def build_ps_lineage(ps_value: Optional[float], market_cap: Optional[float],
                     revenue: Optional[float], period: str, fetched_at: str) -> dict[str, Any]:
    """PS 是市值/营收派生，没有独立 provider 接口，单独记录 derived lineage。"""
    entry = LineageEntry(
        normalized_field="ps", raw_field="", value=ps_value, unit="倍", period=period,
        provider="akshare", original_platform="百度股市通/新浪财经（派生）",
        source_url="", endpoint_description="市销率 = 总市值 / 营业总收入（本项目内推导，非 provider 直接字段）",
        no_direct_source_url=True, fetched_at=fetched_at,
        transformation="market_cap / revenue（派生计算）",
        derived_from=["market_cap", "revenue"], missing=ps_value is None,
        confidence="medium" if ps_value is not None else "low",
    )
    return entry.model_dump()


def lineage_summary(field_lineage: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """字段总数/有原始来源字段数/无直接URL字段数/derived字段数/missing字段数。"""
    total = len(field_lineage)
    has_source = sum(1 for e in field_lineage.values()
                     if e.get("endpoint_description") and "unknown" not in e.get("endpoint_description", ""))
    no_url = sum(1 for e in field_lineage.values() if e.get("no_direct_source_url"))
    derived = sum(1 for e in field_lineage.values() if e.get("derived_from"))
    missing = sum(1 for e in field_lineage.values() if e.get("missing"))
    return {"total_fields": total, "fields_with_source": has_source,
           "fields_no_direct_url": no_url, "derived_fields": derived, "missing_fields": missing}
