"""Cross-period change detection over real statement metrics (v4 stage D).

输入：PeriodMetrics 列表（新->旧，来自真实三表）+ 可选历史 run 记录。
输出：TrackingChange 列表。规则阈值内联可审查；数据不足的维度进
insufficient_dimensions，不伪造同比/环比。
"""
from typing import Optional

from schemas.tracking import PeriodMetrics, TrackingChange, TrackingRecord


def _chg(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev is None:
        return None
    return round(cur - prev, 2)


def detect_changes(periods: list[PeriodMetrics],
                   same_period_base: Optional[PeriodMetrics] = None
                   ) -> tuple[list[TrackingChange], list[str]]:
    """same_period_base：季度模式传上年同期（累计口径可比基期）——现金流等
    水平值对比用它，避免拿 Q1 累计值对比全年值产生虚假暴跌。"""
    changes: list[TrackingChange] = []
    insufficient: list[str] = []
    if len(periods) < 2:
        return changes, ["all: 历史期不足 2 期，无法做任何跨期对比"]
    cur, prev = periods[0], periods[1]
    level_base = same_period_base or prev  # 水平值（现金流）对比基期
    level_base_note = "上年同期" if same_period_base is not None else "上期"

    # 1) 营收增速加速/放缓（需要两期各自的 yoy）
    if cur.yoy_revenue is not None and prev.yoy_revenue is not None:
        delta = round(cur.yoy_revenue - prev.yoy_revenue, 2)
        direction = "improving" if delta > 2 else "deteriorating" if delta < -2 else "stable"
        verb = "加速" if delta > 2 else "放缓" if delta < -2 else "基本持平"
        changes.append(TrackingChange(
            dimension="营收增速", direction=direction,
            finding=f"本期营收同比{cur.yoy_revenue}%，上期{prev.yoy_revenue}%，增速{verb}（{delta:+}pct）",
            evidence=f"{cur.period} vs {prev.period}，AkShare·新浪三表"))
    else:
        insufficient.append("营收增速对比（缺同比基期数据）")

    # 2) 净利润与收入背离
    if cur.yoy_revenue is not None and cur.yoy_net_profit is not None:
        gap = round(cur.yoy_net_profit - cur.yoy_revenue, 2)
        if abs(gap) > 10:
            changes.append(TrackingChange(
                dimension="盈利与收入背离",
                direction="deteriorating" if gap < 0 else "improving",
                finding=f"本期净利润同比{cur.yoy_net_profit}% vs 营收同比{cur.yoy_revenue}%，"
                        f"背离{gap:+}pct（{'利润承压' if gap < 0 else '利润弹性释放'}）",
                evidence=f"{cur.period}，AkShare·新浪三表"))
        else:
            changes.append(TrackingChange(
                dimension="盈利与收入背离", direction="stable",
                finding=f"净利与营收同比差{gap:+}pct，未见显著背离",
                evidence=f"{cur.period}，AkShare·新浪三表"))
    else:
        insufficient.append("盈利背离分析（缺净利/营收同比）")

    # 3) 毛利率变化
    gm_chg = _chg(cur.gross_margin_pct, prev.gross_margin_pct)
    if gm_chg is not None:
        direction = "improving" if gm_chg > 1 else "deteriorating" if gm_chg < -1 else "stable"
        changes.append(TrackingChange(
            dimension="毛利率", direction=direction,
            finding=f"毛利率由{prev.gross_margin_pct}%变为{cur.gross_margin_pct}%（{gm_chg:+}pct）",
            evidence=f"{cur.period} vs {prev.period}，AkShare·新浪三表（推导毛利口径）"))
    else:
        insufficient.append("毛利率对比")

    # 4) 经营现金流变化（水平值：用同口径基期，季度模式=上年同期累计）
    if cur.operating_cash_flow is not None and level_base.operating_cash_flow not in (None, 0):
        ocf_pct = round((cur.operating_cash_flow / level_base.operating_cash_flow - 1) * 100, 1)
        direction = ("improving" if ocf_pct > 5 else
                     "deteriorating" if ocf_pct < -5 else "stable")
        changes.append(TrackingChange(
            dimension="经营现金流", direction=direction,
            finding=f"经营现金流净额由{level_base_note}{level_base.operating_cash_flow}亿元变为"
                    f"{cur.operating_cash_flow}亿元（{ocf_pct:+}%）",
            evidence=f"{cur.period} vs {level_base.period}，AkShare·新浪三表"))
    else:
        insufficient.append("经营现金流对比（缺同口径基期）")

    # 5) 负债率变化
    dr_chg = _chg(cur.debt_ratio_pct, prev.debt_ratio_pct)
    if dr_chg is not None:
        direction = "deteriorating" if dr_chg > 3 else "improving" if dr_chg < -3 else "stable"
        changes.append(TrackingChange(
            dimension="资产负债率", direction=direction,
            finding=f"资产负债率由{prev.debt_ratio_pct}%变为{cur.debt_ratio_pct}%（{dr_chg:+}pct）",
            evidence=f"{cur.period} vs {prev.period}，AkShare·新浪三表"))
    else:
        insufficient.append("负债率对比")

    return changes, insufficient


def compare_with_prior_record(record: Optional[TrackingRecord],
                              current_snapshot: dict) -> tuple[list[TrackingChange], list[str], bool]:
    """与本系统历史 run 记录对比：估值假设变化 + 上期风险是否仍在。"""
    notes: list[str] = []
    changes: list[TrackingChange] = []
    if record is None:
        return changes, ["无本系统历史报告记录（首次跟踪），估值假设/风险兑现对比不可用"], False

    prior_vr = (record.valuation_assumptions or {}).get("valuation_range")
    if prior_vr:
        changes.append(TrackingChange(
            dimension="估值假设",
            finding=f"上次报告（{record.created_at[:10]}）DCF 估值区间为 {prior_vr}；"
                    "本期区间见估值章节，差异主要来自基期现金流与假设参数更新",
            direction="unknown",
            evidence=f"历史记录 run_id={record.run_id}"))
    else:
        notes.append("历史记录无估值假设，估值变化对比跳过")

    if record.risks:
        changes.append(TrackingChange(
            dimension="风险跟踪",
            finding="上次报告提示的风险：" + "；".join(record.risks[:3])
                    + "。本期需对照最新数据核验是否兑现/扩大（见风险章节）",
            direction="unknown",
            evidence=f"历史记录 {record.created_at[:10]}"))
    else:
        notes.append("历史记录无风险条目")

    prior_pe = (record.financial_snapshot or {}).get("pe")
    cur_pe = current_snapshot.get("pe") if current_snapshot else None
    if prior_pe and cur_pe:
        changes.append(TrackingChange(
            dimension="估值水位",
            finding=f"PE(TTM) 由上次记录的 {prior_pe} 变为 {cur_pe}",
            direction="unknown",
            evidence="AkShare·百度估值（两次抓取时点）"))
    return changes, notes, True
