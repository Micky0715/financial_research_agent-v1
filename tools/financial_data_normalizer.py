"""Normalization of raw AkShare frames into FinancialDataSnapshot fields.

v3 阶段A/二：把 provider 原始返回（新浪财务摘要的"指标×报告期"矩阵、百度估值
序列等）归一化成统一字段。所有单位统一为：金额=亿元，比率=%（保持 provider
口径），价格=元。取不到的字段进 missing_fields，不编造。

单位换算的诚实边界：新浪财务摘要的金额字段单位是元，用数量级启发式校验后除以
1e8 转亿元；比率字段（ROE/毛利率/资产负债率）provider 已是百分数值，原样保留。
"""
from typing import Any, Optional

# 新浪 stock_financial_abstract 的指标行名 -> 归一化字段名（金额类，元 -> 亿元）
_MONEY_INDICATORS = {
    "营业总收入": "revenue",
    "归母净利润": "net_profit",
    "经营现金流量净额": "operating_cash_flow",
    "资产总计": "total_assets",
    "负债合计": "total_liabilities",
}
# 比率类（provider 给的就是百分数数值）
_RATIO_INDICATORS = {
    "净资产收益率(ROE)": "roe",
    "毛利率": "gross_margin",
    "资产负债率": "debt_ratio",
}


# 归一化字段 -> 原始新浪指标行名（反向映射，供 tools/data_lineage.py 标注
# raw_field 使用；debt_ratio 在两个方向都可能出现，取原始指标名优先）
INDICATOR_RAW_FIELD_MAP: dict[str, str] = {
    field: raw_name for raw_name, field in {**_MONEY_INDICATORS, **_RATIO_INDICATORS}.items()
}
MONEY_FIELDS = set(_MONEY_INDICATORS.values())


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        f = float(value)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _yuan_to_yi(value: Optional[float]) -> Optional[float]:
    """元 -> 亿元。provider 偶尔已经是亿元口径的防御：数量级 < 1e6 的值不再除
    （A股上市公司年营收不可能低于百万元级，除非本来就是亿元口径）。"""
    if value is None:
        return None
    if abs(value) < 1e6:
        return round(value, 4)
    return round(value / 1e8, 4)


def annual_columns(columns: list[str]) -> list[str]:
    """从报告期列里挑出年报列（YYYY1231），按时间升序。"""
    annual = [c for c in columns if isinstance(c, str) and len(c) == 8 and c.endswith("1231")]
    return sorted(annual)


def normalize_financial_abstract(df: Any, max_years: int = 6) -> dict[str, Any]:
    """新浪 stock_financial_abstract 的 DataFrame -> 归一化字段 + 年度序列。

    Returns {"fields": {...最新年报字段...}, "period": "YYYY年报",
             "yearly_series": {field: [(year, value)]}, "missing_fields": [...]}.
    Never raises - a malformed frame yields empty fields with everything in
    missing_fields.
    """
    fields: dict[str, Optional[float]] = {}
    yearly: dict[str, list[tuple[int, float]]] = {}
    missing: list[str] = []
    derived_fields: dict[str, list[str]] = {}
    period = ""

    try:
        indicator_col = "指标"
        rows = {str(r[indicator_col]): r for _, r in df.iterrows()}
        years = annual_columns(list(df.columns))[-max_years:]
        if years:
            period = f"{years[-1][:4]}年报"

        for raw_name, field in {**_MONEY_INDICATORS, **_RATIO_INDICATORS}.items():
            row = rows.get(raw_name)
            if row is None:
                fields[field] = None
                missing.append(field)
                continue
            is_money = raw_name in _MONEY_INDICATORS
            series: list[tuple[int, float]] = []
            for col in years:
                v = _to_float(row.get(col))
                if v is not None:
                    series.append((int(col[:4]), _yuan_to_yi(v) if is_money else round(v, 4)))
            if series:
                yearly[field] = series
                fields[field] = series[-1][1]
            else:
                fields[field] = None
                missing.append(field)

        # 派生：资产负债率缺失但资产/负债齐全时计算（标注 derived，供 tools/data_lineage.py 使用）
        if fields.get("debt_ratio") is None and fields.get("total_assets") and fields.get("total_liabilities"):
            fields["debt_ratio"] = round(fields["total_liabilities"] / fields["total_assets"] * 100, 2)
            if "debt_ratio" in missing:
                missing.remove("debt_ratio")
            derived_fields["debt_ratio"] = ["total_liabilities", "total_assets"]
    except Exception:  # noqa: BLE001 - malformed provider frame -> all missing, never crash
        missing = sorted(set(list(_MONEY_INDICATORS.values()) + list(_RATIO_INDICATORS.values())))
        return {"fields": {}, "period": "", "yearly_series": {}, "missing_fields": missing,
               "derived_fields": {}}

    return {"fields": fields, "period": period, "yearly_series": yearly, "missing_fields": missing,
           "derived_fields": derived_fields}


def latest_value(series_df: Any, value_col: str = "value") -> Optional[float]:
    """百度估值序列 DataFrame 的最新非空值。Never raises."""
    try:
        values = [v for v in (_to_float(x) for x in series_df[value_col]) if v is not None]
        return values[-1] if values else None
    except Exception:  # noqa: BLE001
        return None


def series_values(series_df: Any, value_col: str = "value") -> list[float]:
    """百度估值序列 -> float 列表（用于相对估值的历史分位）。Never raises."""
    try:
        return [v for v in (_to_float(x) for x in series_df[value_col]) if v is not None]
    except Exception:  # noqa: BLE001
        return []
