"""Tracking report builder on real multi-period statements (v4 stage D).

季度/年度跟踪报告：期次数据全部来自新浪三表真实报告期（annual=近5年年报，
quarterly=近8个报告期）。同比用同期口径（年报 vs 上年年报；季报累计值 vs
上年同期累计值）；季度环比用推导单季值（cum(Q)-cum(Q-1)，显式标 derived）。
历史期不足的维度进 insufficient_dimensions，绝不伪造。
"""
from datetime import datetime
from typing import Optional

from config import config
from schemas.tracking import PeriodMetrics, TrackingReportResult
from tools.akshare_tool import fetch_financial_snapshot, resolve_symbol
from tools.financial_statement_extractor import extract_three_statements, statement_value
from tracking.periodic_data_store import latest_record
from tracking.report_diff_analyzer import compare_with_prior_record, detect_changes
from utils.logger import logger


def _period_metrics(statements) -> list[PeriodMetrics]:
    out = []
    for p in statements.periods:
        revenue = statement_value(p, "income", "revenue")
        cost = statement_value(p, "income", "operating_cost")
        ta = statement_value(p, "balance", "total_assets")
        tl = statement_value(p, "balance", "total_liabilities")
        out.append(PeriodMetrics(
            period=p.period, period_raw=p.period_raw,
            revenue=revenue,
            net_profit=statement_value(p, "income", "net_profit"),
            parent_net_profit=statement_value(p, "income", "parent_net_profit"),
            operating_cash_flow=statement_value(p, "cashflow", "operating_cash_flow"),
            gross_margin_pct=(round((revenue - cost) / revenue * 100, 2)
                              if revenue and cost is not None else None),
            debt_ratio_pct=round(tl / ta * 100, 2) if ta and tl is not None else None,
        ))
    return out


def _fill_yoy(metrics: list[PeriodMetrics]) -> None:
    """同比：找 period_raw 年份-1、月日相同的期。中国季报为累计口径，同期可比。"""
    by_raw = {m.period_raw: m for m in metrics}
    for m in metrics:
        base = by_raw.get(f"{int(m.period_raw[:4]) - 1}{m.period_raw[4:]}")
        if base is None:
            continue
        if m.revenue is not None and base.revenue:
            m.yoy_revenue = round((m.revenue / base.revenue - 1) * 100, 2)
        if m.net_profit is not None and base.net_profit:
            m.yoy_net_profit = round((m.net_profit / base.net_profit - 1) * 100, 2)


def _fill_quarterly_single_and_qoq(metrics: list[PeriodMetrics]) -> None:
    """单季推导值 + 单季环比（仅 quarterly）。metrics 为新->旧。"""
    by_raw = {m.period_raw: m for m in metrics}
    prev_md = {"1231": "0930", "0930": "0630", "0630": "0331"}
    for m in metrics:
        md = m.period_raw[4:]
        if md == "0331":
            m.revenue_single, m.net_profit_single = m.revenue, m.net_profit
        else:
            prior = by_raw.get(m.period_raw[:4] + prev_md.get(md, ""))
            if prior is not None:
                if m.revenue is not None and prior.revenue is not None:
                    m.revenue_single = round(m.revenue - prior.revenue, 4)
                if m.net_profit is not None and prior.net_profit is not None:
                    m.net_profit_single = round(m.net_profit - prior.net_profit, 4)
    ordered = sorted(metrics, key=lambda x: x.period_raw)  # 旧->新
    for i in range(1, len(ordered)):
        cur, prev = ordered[i], ordered[i - 1]
        if cur.revenue_single is not None and prev.revenue_single:
            cur.qoq_revenue = round((cur.revenue_single / prev.revenue_single - 1) * 100, 2)
        if cur.net_profit_single is not None and prev.net_profit_single:
            cur.qoq_net_profit = round((cur.net_profit_single / prev.net_profit_single - 1) * 100, 2)


def _table(metrics: list[PeriodMetrics], quarterly: bool) -> str:
    head = "| 报告期 | 营收(亿元) | 归母净利(亿元) | 营收同比 | 净利同比 |"
    head += " 单季营收环比 |" if quarterly else " 毛利率 | 资产负债率 |"
    sep = "|---" * (6 if quarterly else 7) + "|"
    rows = []
    for m in metrics:
        fmt = lambda v, suf="": ("—" if v is None else f"{v}{suf}")  # noqa: E731
        row = (f"| {m.period} | {fmt(m.revenue)} | {fmt(m.parent_net_profit)} "
               f"| {fmt(m.yoy_revenue, '%')} | {fmt(m.yoy_net_profit, '%')} ")
        row += f"| {fmt(m.qoq_revenue, '%')} |" if quarterly else \
            f"| {fmt(m.gross_margin_pct, '%')} | {fmt(m.debt_ratio_pct, '%')} |"
        rows.append(row)
    note = ("\n注：季报数值为累计口径（AkShare·新浪三表）；单季环比基于推导单季值"
            "（cum(Q)-cum(Q-1)，derived）。" if quarterly
            else "\n注：数据为年报口径（AkShare·新浪三表）；毛利率为推导值（营收-营业成本）/营收。")
    return "\n".join([head, sep] + rows) + note


def build_tracking_report(name_or_code: str, period_type: str = "annual") -> TrackingReportResult:
    """主入口：季度/年度跟踪报告。历史不足 -> insufficient_history，不伪造。"""
    fetched_at = datetime.now().isoformat(timespec="seconds")
    resolved = resolve_symbol(name_or_code)
    if resolved is None:
        return TrackingReportResult(entity=str(name_or_code), symbol="", period_type=period_type,
                                    insufficient_history=True, fetched_at=fetched_at,
                                    limitation=f"cannot resolve symbol: {name_or_code}")
    code, entity = resolved
    result = TrackingReportResult(entity=entity, symbol=code, period_type=period_type,
                                  fetched_at=fetched_at)

    quarterly = period_type == "quarterly"
    # 季度取 12 期：同比需要上年同期、单季推导需要上一季，都要多留基期
    statements = extract_three_statements(code, "quarterly" if quarterly else "annual",
                                          max_periods=12 if quarterly else 5)
    metrics = _period_metrics(statements)
    _fill_yoy(metrics)
    if quarterly:
        _fill_quarterly_single_and_qoq(metrics)
        metrics = metrics[:6]  # 展示最近6期
    result.periods = metrics

    if len(metrics) < 2:
        result.insufficient_history = True
        result.limitation = "可用报告期不足 2 期，无法生成跨期跟踪（insufficient_history）"
        result.markdown = (f"# {entity}（{code}）跟踪报告 - 未生成\n\n"
                           f"**insufficient_history**：{result.limitation}\n")
        return result

    result.current_period, result.previous_period = metrics[0].period, metrics[1].period
    same_period_base = None
    if quarterly:
        cur_raw = metrics[0].period_raw
        same_period_base = next(
            (m for m in metrics if m.period_raw == f"{int(cur_raw[:4]) - 1}{cur_raw[4:]}"), None)
    changes, insufficient = detect_changes(metrics, same_period_base)

    snapshot_env = fetch_financial_snapshot(code)
    snapshot = snapshot_env.get("snapshot") or {}
    prior = latest_record(code)
    prior_changes, prior_notes, compared = compare_with_prior_record(prior, snapshot)
    result.changes = changes + prior_changes
    result.insufficient_dimensions = insufficient + \
        ([] if compared else ["历史报告对比（本系统首次跟踪该主体）"])
    result.prior_record_compared = compared
    result.prior_record_notes = prior_notes

    trend_n = min(len(metrics), 5)
    dir_zh = {"improving": "改善", "deteriorating": "走弱", "stable": "平稳", "unknown": "待核验"}
    changes_md = "\n".join(
        f"- **{c.dimension}**（{dir_zh.get(c.direction, c.direction)}）：{c.finding}（{c.evidence}）"
        for c in result.changes) or "（无可对比变化维度）"
    insuff_md = ("\n".join(f"- {d}" for d in result.insufficient_dimensions)
                 or "（无——全部维度均有数据）")

    result.markdown = f"""# {entity}（{code}）{'季度' if quarterly else '年度'}跟踪报告

数据抓取时间：{fetched_at}；本期：{result.current_period}，上期：{result.previous_period}。

## 本期与上期对比及变化

{changes_md}

## 近{trend_n}期趋势

{_table(metrics[:trend_n], quarterly)}

## 历史报告对比

{"已对比本系统历史报告记录（见上方估值假设/风险跟踪条目）。" if compared else "本系统无该主体历史报告记录（首次跟踪），估值假设变化与风险兑现对比不可用——如实标注，不伪造。"}
{chr(10).join('- ' + n for n in prior_notes)}

## 数据不足维度（insufficient）

{insuff_md}

## 免责声明

本跟踪报告由自动化系统基于 AkShare·新浪三表与本系统历史记录生成，数据存在披露滞后，
变化判断为规则阈值判定，不构成投资建议。
"""
    out_dir = config.OUTPUT_DIR / "tracking"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"tracking_{code}_{period_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    path.write_text(result.markdown, encoding="utf-8")
    result.report_path = str(path)
    result.limitation = "跨期对比仅覆盖新浪三表可得科目；管理层表述变化需依赖历史报告文本，仅在有历史记录时可用"
    logger.info(f"tracking report {code} {period_type}: {len(result.changes)} changes, "
                f"insufficient={len(result.insufficient_dimensions)}")
    return result
