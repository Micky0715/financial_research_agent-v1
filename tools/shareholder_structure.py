"""Shareholder structure via sina main-stock-holder endpoint (v4 stage B).

ak.stock_main_stock_holder 实测返回历史各期前十大股东（持股比例/股本性质/
截至日期/股东总数）。免费公开接口没有的字段（实际控制人、股权质押、机构
持股明细）如实进 limitations，不猜不编。
"""
from datetime import datetime
from typing import Any

from config import config
from schemas.company_research import Shareholder, ShareholderStructure
from tools.akshare_tool import _akshare, _safe, resolve_symbol
from utils.logger import logger


def _exchange(code: str) -> str:
    if code.startswith(("60", "68")):
        return "上海证券交易所" + ("（科创板）" if code.startswith("68") else "")
    if code.startswith(("00", "30")):
        return "深圳证券交易所" + ("（创业板）" if code.startswith("30") else "")
    if code.startswith(("83", "87", "43", "92")):
        return "北京证券交易所"
    return "unknown"


def _f(v):
    try:
        if v is None or v != v:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_shareholder_structure(name_or_code: str) -> ShareholderStructure:
    fetched_at = datetime.now().isoformat(timespec="seconds")
    resolved = resolve_symbol(name_or_code)
    if resolved is None:
        return ShareholderStructure(symbol=str(name_or_code), degraded=True, fetched_at=fetched_at,
                                    limitations=[f"cannot resolve symbol: {name_or_code}"])
    code, _name = resolved
    result = ShareholderStructure(symbol=code, exchange=_exchange(code), fetched_at=fetched_at)

    ak = _akshare()
    if ak is None:
        result.degraded = True
        result.limitations.append("akshare unavailable")
        return result

    errors: list[dict] = []
    df = _safe("stock_main_stock_holder", lambda: ak.stock_main_stock_holder(stock=code), errors)
    if df is None:
        result.degraded = True
        result.limitations.append(f"stock_main_stock_holder failed: {errors}")
        return result

    try:
        rows = df.to_dict("records")
        # 按截至日期分组，取最新一期 + 上一期（股东总数对比）
        by_date: dict[str, list[dict]] = {}
        for r in rows:
            d = str(r.get("截至日期", ""))
            if d and d != "NaT":
                by_date.setdefault(d, []).append(r)
        if not by_date:
            result.degraded = True
            result.limitations.append("no dated holder records")
            return result
        dates = sorted(by_date.keys(), reverse=True)
        latest_rows = by_date[dates[0]]
        result.as_of = dates[0][:10]

        holders = []
        for r in latest_rows:
            try:
                rank = int(float(r.get("编号", 0)))
            except (TypeError, ValueError):
                rank = None
            holders.append(Shareholder(
                rank=rank, name=str(r.get("股东名称", ""))[:60],
                share_count=_f(r.get("持股数量")), share_pct=_f(r.get("持股比例")),
                nature=str(r.get("股本性质", "") or "")[:20],
            ))
        holders.sort(key=lambda h: h.rank if h.rank is not None else 99)
        result.top_holders = holders[:10]
        pcts = [h.share_pct for h in result.top_holders if h.share_pct is not None]
        if pcts:
            result.top1_pct = round(max(pcts), 2)
            result.top10_pct = round(sum(pcts), 2)
        result.holder_count = _f(latest_rows[0].get("股东总数"))
        if result.holder_count is None:
            # 股东总数可能只在部分期次填写，向后找最近的有值期
            for d in dates[1:]:
                hc = _f(by_date[d][0].get("股东总数"))
                if hc is not None:
                    result.holder_count = hc
                    break
        for d in dates[1:]:
            hc = _f(by_date[d][0].get("股东总数"))
            if hc is not None and hc != result.holder_count:
                result.holder_count_prev = hc
                break
    except Exception as exc:  # noqa: BLE001
        result.degraded = True
        result.limitations.append(f"holder rows parse failed: {type(exc).__name__}: {str(exc)[:100]}")
        return result

    result.limitations += [
        "实际控制人：免费接口无结构化字段，需查阅公司年报「实际控制人」章节",
        "股权质押：免费接口无结构化数据，未纳入",
        "机构持股明细：新浪主要股东口径不区分基金/外资明细，仅能从股东名称判断性质",
    ]
    result.institutional_note = "前十大股东中含'基金/资管/保险/社保/QFII'字样的可视为机构持股线索"
    result.degraded = bool(errors)
    logger.info(f"shareholder structure {code}: top1={result.top1_pct}% top10={result.top10_pct}% as_of={result.as_of}")
    return result
