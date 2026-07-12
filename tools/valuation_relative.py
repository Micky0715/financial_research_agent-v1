"""Relative valuation vs. the stock's own trailing multiple history (v3 stage B).

诚实边界（重要）：
- 参照系是**该股票自身近一年的估值倍数分布**（来自百度估值序列），不是行业
  可比公司均值——本环境没有可靠的免费行业倍数数据源，**拒绝编造行业均值**；
- 因此 signal 的含义是"相对自身近一年历史偏贵/偏便宜"，不是"相对同行"；
- 序列缺失（新股/接口失败）时返回 degraded + unknown，不猜；
- 结果是统计描述，不构成投资建议。
"""
from typing import Any, Optional


def _percentile_of(value: float, history: list[float]) -> Optional[float]:
    """value 在 history 中的分位（0-100）。历史点太少（<30）不给分位。"""
    clean = [h for h in history if h is not None and h > 0]
    if len(clean) < 30:
        return None
    below = sum(1 for h in clean if h <= value)
    return round(below / len(clean) * 100, 1)


def _signal_from_percentile(pct: Optional[float]) -> str:
    if pct is None:
        return "unknown"
    if pct <= 30:
        return "undervalued"
    if pct >= 70:
        return "overvalued"
    return "neutral"


def relative_valuation(snapshot: dict[str, Any]) -> dict[str, Any]:
    """FinancialDataSnapshot(dict) -> PE/PB（及派生 PS）相对自身历史的估值信号。

    Returns:
        {
          "valuation_method": "historical_percentile",
          "multiples": {"pe": {current_multiple, peer_or_history_reference,
                               percentile, valuation_signal}, ...},
          "assumptions": [...],
          "missing_fields": [...],
          "degraded": bool,
        }
    """
    multiples: dict[str, Any] = {}
    missing: list[str] = []
    history_map = snapshot.get("multiples_history") or {}

    for key in ("pe", "pb"):
        current = snapshot.get(key)
        history = history_map.get(key) or []
        if current is None:
            missing.append(key)
            continue
        clean = [h for h in history if h is not None and h > 0]
        pct = _percentile_of(current, history)
        reference = (
            {
                "kind": "own_trailing_1y_history",
                "n_points": len(clean),
                "min": round(min(clean), 2),
                "median": round(sorted(clean)[len(clean) // 2], 2),
                "max": round(max(clean), 2),
            }
            if clean
            else None
        )
        multiples[key] = {
            "current_multiple": current,
            "peer_or_history_reference": reference,
            "percentile": pct,
            "valuation_signal": _signal_from_percentile(pct),
        }
        if reference is None:
            missing.append(f"{key}_history")

    # PS 只有当前派生值、无历史序列 -> 只报告不给信号
    if snapshot.get("ps") is not None:
        multiples["ps"] = {
            "current_multiple": snapshot["ps"],
            "peer_or_history_reference": None,
            "percentile": None,
            "valuation_signal": "unknown",
        }
    else:
        missing.append("ps")

    has_signal = any(m.get("valuation_signal") not in (None, "unknown") for m in multiples.values())
    return {
        "valuation_method": "historical_percentile",
        "multiples": multiples,
        "assumptions": [
            "参照系为该股票自身近一年估值倍数分布（百度估值序列），非行业可比公司",
            "undervalued/overvalued 含义是相对自身历史分位（<=30% / >=70%），非绝对判断",
        ],
        "missing_fields": sorted(set(missing)),
        "degraded": not has_signal or bool(missing),
    }
