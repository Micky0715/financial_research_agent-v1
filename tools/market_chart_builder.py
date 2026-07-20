"""Market charts: price/volume, index comparison, PE/PB percentile (v4 stage F).

数据源全部实测可用：新浪日线（个股）、新浪指数日线、百度估值序列（来自
FinancialDataSnapshot.multiples_history）。任何数据取不到就跳过该图并在
meta.missing_data_note 说明——不补零。
"""
from datetime import datetime, timedelta
from typing import Any, Optional

from tools.akshare_tool import _akshare, _safe, _sina_symbol, resolve_symbol
from tools.chart_common import chart_block, render_lines_base64
from utils.logger import logger

_INDEX = ("sh000001", "上证指数")


def _daily_series(code: str, days: int = 365) -> list[tuple[str, float, float]]:
    """[(date, close, volume)]；失败返回 []。"""
    ak = _akshare()
    sina_sym = _sina_symbol(code)
    if ak is None or sina_sym is None:
        return []
    errors: list[dict] = []
    start = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    df = _safe("stock_zh_a_daily", lambda: ak.stock_zh_a_daily(
        symbol=sina_sym, start_date=start, end_date=datetime.now().strftime("%Y%m%d")), errors)
    if df is None:
        return []
    try:
        return [(str(r["date"]), float(r["close"]), float(r.get("volume", 0)))
                for r in df.to_dict("records") if r.get("close") == r.get("close")]
    except Exception:  # noqa: BLE001
        return []


def _index_series(symbol: str, days: int = 365) -> list[tuple[str, float]]:
    ak = _akshare()
    if ak is None:
        return []
    errors: list[dict] = []
    df = _safe("stock_zh_index_daily", lambda: ak.stock_zh_index_daily(symbol=symbol), errors)
    if df is None:
        return []
    try:
        cutoff = (datetime.now() - timedelta(days=days)).date().isoformat()
        return [(str(r["date"]), float(r["close"])) for r in df.to_dict("records")
                if str(r["date"]) >= cutoff]
    except Exception:  # noqa: BLE001
        return []


def build_market_charts_html(name_or_code: str,
                             snapshot: Optional[dict] = None) -> tuple[str, list[dict]]:
    """个股行情图组：价格+成交量、相对上证指数、PE/PB 历史序列。"""
    resolved = resolve_symbol(name_or_code)
    if resolved is None:
        return "", []
    code, company = resolved
    blocks, metas = [], []

    daily = _daily_series(code)
    if len(daily) >= 20:
        period = f"{daily[0][0]} ~ {daily[-1][0]}"
        price_series = {f"{company}收盘价": [(d, c) for d, c, _ in daily]}
        vol_series = {"成交量": [(d, v) for d, _, v in daily]}
        b64 = render_lines_base64(price_series, f"{company}（{code}）股价与成交量（近一年）",
                                  "元", secondary=vol_series)
        html, meta = chart_block(b64, chart_title=f"{company}股价与成交量", data_period=period,
                                 source="AkShare·新浪财经日线", unit="元/股（成交量：股）",
                                 subject=company,
                                 values_sample={"latest_close": daily[-1][1]})
        if html:
            blocks.append(html)
            metas.append(meta)

        idx = _index_series(_INDEX[0])
        if len(idx) >= 20:
            base_p, base_i = daily[0][1], idx[0][1]
            rebased = {
                f"{company}（重定基=100）": [(d, round(c / base_p * 100, 2)) for d, c, _ in daily],
                f"{_INDEX[1]}（重定基=100）": [(d, round(c / base_i * 100, 2)) for d, c in idx],
            }
            b64 = render_lines_base64(rebased, f"{company} vs {_INDEX[1]}（近一年，重定基）", "指数(基期=100)")
            html, meta = chart_block(b64, chart_title=f"{company}相对{_INDEX[1]}走势",
                                     data_period=period, source="AkShare·新浪财经（个股+指数日线）",
                                     unit="重定基指数", subject=company,
                                     missing_data_note="重定基为工程换算（首日=100）",
                                     values_sample={"latest_close": daily[-1][1]})
            if html:
                blocks.append(html)
                metas.append(meta)

    if snapshot and snapshot.get("multiples_history"):
        mh = snapshot["multiples_history"]
        series = {}
        for key, label in [("pe", "PE(TTM)"), ("pb", "PB")]:
            vals = mh.get(key) or []
            if len(vals) >= 20:
                series[label] = [(i, v) for i, v in enumerate(vals)]
        if series:
            b64 = render_lines_base64(series, f"{company} PE/PB 近一年序列（百度估值）", "倍",
                                      max_xticks=6)
            html, meta = chart_block(
                b64, chart_title=f"{company} PE/PB 历史序列", data_period="近一年（交易日序列）",
                source="AkShare·百度股市通估值", unit="倍", subject=company,
                missing_data_note="x 轴为序列序号（源数据未附日期戳）",
                values_sample={"latest_pe": snapshot.get("pe"), "latest_pb": snapshot.get("pb")})
            if html:
                blocks.append(html)
                metas.append(meta)

    logger.info(f"market charts {code}: {len(blocks)} charts")
    return "\n".join(blocks), metas
