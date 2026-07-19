"""Normalize raw akshare macro DataFrames into MacroDataPoint (v4 stage A).

每类接口一个解析函数，输入 DataFrame，输出 (point_fields, series)：
- point_fields: 最新一期的 {value, period, previous_value, yoy, mom, release_date}
- series: [(period, value)] 按期升序，供图表使用（最多保留 max_points）

解析失败/列缺失时返回 (None, [])，由 collector 记入 missing —— 不猜列名含义。
"""
import re
from typing import Any, Optional

_MAX_POINTS = 60


def _f(v) -> Optional[float]:
    try:
        if v is None or v != v:  # NaN
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _month_period(text: str) -> str:
    """'2026年06月份' -> '2026-06'."""
    m = re.search(r"(\d{4})年(\d{1,2})月", str(text))
    return f"{m.group(1)}-{int(m.group(2)):02d}" if m else str(text)


def _quarter_period(text: str) -> str:
    """'2026年第1季度' -> '2026Q1'."""
    m = re.search(r"(\d{4})年第(\d)季度", str(text))
    return f"{m.group(1)}Q{m.group(2)}" if m else str(text)


def _finish(series: list[tuple[str, Optional[float]]]) -> tuple[Optional[dict], list]:
    """Shared tail: sort ascending by period, take latest non-null as the point."""
    clean = sorted([(p, v) for p, v in series if v is not None and p], key=lambda x: x[0])
    if not clean:
        return None, []
    clean = clean[-_MAX_POINTS:]
    period, value = clean[-1]
    prev = clean[-2][1] if len(clean) >= 2 else None
    return {"value": value, "period": period, "previous_value": prev}, clean


def parse_month_table(df, value_col: str, yoy_col: str = "", mom_col: str = "",
                      period_col: str = "月份") -> tuple[Optional[dict], list]:
    """东财数据中心风格月度表（月份/当月/同比增长/环比增长）。value_col 可以就是同比列。"""
    try:
        rows = df.to_dict("records")
    except Exception:  # noqa: BLE001
        return None, []
    series = [(_month_period(r.get(period_col, "")), _f(r.get(value_col))) for r in rows]
    point, clean = _finish(series)
    if point is None:
        return None, []
    latest_raw = next(
        (r for r in rows if _month_period(r.get(period_col, "")) == point["period"]), {})
    if yoy_col:
        point["yoy"] = _f(latest_raw.get(yoy_col))
    if mom_col:
        point["mom"] = _f(latest_raw.get(mom_col))
    return point, clean


def parse_quarter_table(df, value_col: str, yoy_col: str = "",
                        period_col: str = "季度") -> tuple[Optional[dict], list]:
    try:
        rows = df.to_dict("records")
    except Exception:  # noqa: BLE001
        return None, []
    series = [(_quarter_period(r.get(period_col, "")), _f(r.get(value_col))) for r in rows]
    point, clean = _finish(series)
    if point is None:
        return None, []
    latest_raw = next(
        (r for r in rows if _quarter_period(r.get(period_col, "")) == point["period"]), {})
    if yoy_col:
        point["yoy"] = _f(latest_raw.get(yoy_col))
    return point, clean


def parse_jin10_table(df) -> tuple[Optional[dict], list]:
    """金十风格表（商品/日期/今值/预测值/前值）：今值为 NaN 的未发布期跳过。"""
    try:
        rows = df.to_dict("records")
    except Exception:  # noqa: BLE001
        return None, []
    series = []
    for r in rows:
        v = _f(r.get("今值"))
        if v is not None:
            series.append((str(r.get("日期", "")), v))
    point, clean = _finish(series)
    if point is None:
        return None, []
    latest_raw = next((r for r in rows if str(r.get("日期", "")) == point["period"]), {})
    prev = _f(latest_raw.get("前值"))
    if prev is not None:
        point["previous_value"] = prev
    point["release_date"] = point["period"]
    return point, clean


def parse_named_column_table(df, period_col: str, value_col: str) -> tuple[Optional[dict], list]:
    """通用 日期列+数值列 表（LPR、国债收益率、汇率等）。"""
    try:
        rows = df.to_dict("records")
    except Exception:  # noqa: BLE001
        return None, []
    series = [(str(r.get(period_col, "")), _f(r.get(value_col))) for r in rows]
    return _finish(series)


def derive_mom_yoy_from_series(series: list[tuple[str, float]],
                               periods_per_year: int) -> dict[str, Optional[float]]:
    """从水平值序列派生环比/同比（%）。仅在原表无现成列时使用，标注 transformation。"""
    out: dict[str, Optional[float]] = {"mom": None, "yoy": None}
    if len(series) >= 2 and series[-2][1]:
        out["mom"] = round((series[-1][1] / series[-2][1] - 1) * 100, 2)
    if len(series) > periods_per_year and series[-1 - periods_per_year][1]:
        out["yoy"] = round((series[-1][1] / series[-1 - periods_per_year][1] - 1) * 100, 2)
    return out
