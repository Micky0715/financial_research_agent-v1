"""Financial trend chart generation for HTML reports.

v2 模块4：从已抓取来源的正文里抽取 (年份, 指标值) 数据点，用 matplotlib 画
营收/净利润趋势图，以 base64 data-URI 形式嵌入最终 HTML 报告——不产生外部
图片文件依赖，报告仍是单文件可分发的。

诚实边界：
- 数据点来自新闻/研报文本的正则抽取，不是结构化财报数据库，可能有缺失年份、
  口径混杂（全年 vs 半年报）的情况；抽取器只保留"YYYY年 + 指标 + X亿元"能
  对上的高置信度组合，宁缺毋滥；
- 数据点少于 2 个时不画图（单点趋势图没有意义），报告正常生成，只是没有图；
- 图表下方注明数据来自公开资料抽取，仅供参考。
"""
import base64
import io
import re
from typing import Optional

from schemas.source import Source
from utils.logger import logger
from utils.text_utils import COMPANY_ALIASES

# 不用 utils.text_utils.split_sentences：它在英文句点处也切分，"8039.65亿元"
# 会被切成 "8039." / "65 亿元"，带小数的金额抽不到。这里只按中文句读/换行切。
_ZH_SENTENCE_SPLIT_RE = re.compile(r"[。！？\n]")


def _split_zh_sentences(text: str) -> list[str]:
    return [s.strip() for s in _ZH_SENTENCE_SPLIT_RE.split(text or "") if s.strip()]

# 指标关键词 + 该指标句子里"不允许夹在年份和金额之间"的干扰词（避免把市值/销量
# 等其他数字误归到营收/净利润上）
_METRIC_KEYWORDS = {
    "营收": ["营业收入", "营业总收入", "营收"],
    "净利润": ["归母净利润", "净利润"],
}
_METRIC_CONFLICTS = {
    "营收": ["利润", "市值", "纳税", "研发", "销量", "现金", "负债", "分红", "补贴", "保费"],
    "净利润": ["收入", "营收", "市值", "纳税", "研发", "销量", "现金", "负债", "分红", "保费"],
}

# 严格形态："2025年 ... 营收 8039.65亿" —— 年份、指标词、金额在一小段窗口内
_STRICT_RE = {
    "营收": re.compile(
        r"(20\d{2})\s*年[^。；\n]{0,40}?(?:营业?总?收入|营收)[约为达到至]*\s*([0-9,]+(?:\.\d+)?)\s*亿"
    ),
    "净利润": re.compile(
        r"(20\d{2})\s*年[^。；\n]{0,40}?(?:归母)?净利润[约为达到至]*\s*([0-9,]+(?:\.\d+)?)\s*亿"
    ),
}
# 宽松形态：句内所有 "YYYY年 ... X亿" 对（如 "2021年，营收2161.42亿元；2022年
# 跃升至4240.61亿元；2023年到6023.15亿元" 里指标词只出现一次的多年份列举）
_YEAR_AMOUNT_RE = re.compile(r"(20\d{2})\s*年([^。\n]{0,25}?)([0-9,]+(?:\.\d+)?)\s*亿")


def _mentions_other_company(sentence: str, subject: str) -> bool:
    """True when the sentence names a *different* known company than `subject`.

    Catches comparison sentences like "作为对比，比亚迪在2023年的年度净利润是
    300亿元" inside a 宁德时代 report - same scale as the subject's own numbers,
    so the magnitude guard can't catch it. Only covers companies present in
    COMPANY_ALIASES; unknown competitor names still slip through (documented
    limitation).
    """
    for company, aliases in COMPANY_ALIASES.items():
        if company == subject or subject in company or company in subject:
            continue
        if any(a and len(a) >= 2 and a in sentence for a in aliases):
            # the subject itself also being named makes attribution ambiguous
            # either way - skip in both cases
            return True
    return False


def extract_metric_series(sources: list[Source], metric: str, subject: str = "") -> list[tuple[int, float]]:
    """Extract deduplicated (year, value 亿元) points for `metric` across all sources.

    Two extraction passes per sentence:
    1. strict: year + metric keyword + amount within one tight window (high
       confidence - counted twice in the vote);
    2. sentence-level: if the sentence mentions the metric keyword, every
       "YYYY年...X亿" pair whose gap text does not contain a conflicting
       metric keyword is attributed to it (catches multi-year run-ons where
       the keyword appears only once).

    When the same year gets different values (scope/rounding differences
    across sources), the most-voted value wins.
    """
    keywords = _METRIC_KEYWORDS.get(metric)
    if not keywords:
        return []
    strict = _STRICT_RE[metric]
    conflicts = _METRIC_CONFLICTS[metric]

    votes: dict[int, list[float]] = {}

    def _vote(year: int, value: float, weight: int = 1) -> None:
        if 2000 <= year <= 2035 and value > 0:
            votes.setdefault(year, []).extend([value] * weight)

    # 口径守卫：季度/半年数据混进年度趋势图比缺一个点更误导，直接跳过
    period_scope_words = ("季度", "上半年", "半年报", "一季报", "三季报", "中报", "1-9月", "前三季")

    for src in sources:
        for sentence in _split_zh_sentences(src.content):
            if any(w in sentence for w in period_scope_words):
                continue
            if subject and _mentions_other_company(sentence, subject):
                continue
            for m in strict.finditer(sentence):
                _vote(int(m.group(1)), float(m.group(2).replace(",", "")), weight=2)

            # 宽松归属只对"单指标句子"生效：句子同时提到营收和净利润时，
            # 句内的年份-金额对到底属于谁没法靠位置无关的规则判定（例如
            # "预计2024年营收3560亿元，比2023年的4009亿元…净利润490亿元"），
            # 这类句子只信上面的严格邻近匹配。
            if any(kw in sentence for kw in keywords) and not any(c in sentence for c in conflicts):
                for m in _YEAR_AMOUNT_RE.finditer(sentence):
                    _vote(int(m.group(1)), float(m.group(3).replace(",", "")))

    series = []
    for year, values in votes.items():
        counts: dict[float, int] = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1
        best = max(counts.items(), key=lambda kv: kv[1])[0]
        series.append((year, best))
    series.sort()

    # 数量级离群守卫：新闻里经常同时提到子公司/关联公司的同名指标（如
    # "比亚迪半导体2020年营收14.4亿元"混进比亚迪母公司 8000 亿级的序列），
    # 正则无法区分实体归属；一个序列内部相差两个数量级基本可以断定是实体
    # 混淆，丢弃小值点比画出一条误导性的"暴跌-暴涨"曲线更负责任。
    if len(series) >= 2:
        peak = max(v for _, v in series)
        series = [(y, v) for y, v in series if v >= peak * 0.05]

    return series


def render_trend_chart_base64(
    series_by_metric: dict[str, list[tuple[int, float]]], title: str
) -> Optional[str]:
    """Render a line chart of the given metric series and return base64 PNG.

    Returns None when no metric has >= 2 data points (a single point is not a
    trend) or matplotlib fails - callers just skip the chart.
    """
    plottable = {m: s for m, s in series_by_metric.items() if len(s) >= 2}
    if not plottable:
        return None

    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        # Windows 中文字体；找不到时 matplotlib 自行回退（图还在，只是中文变方框）
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "sans-serif"]
        plt.rcParams["axes.unicode_minus"] = False

        fig, ax = plt.subplots(figsize=(7.5, 3.8), dpi=110)
        for metric, series in plottable.items():
            years = [y for y, _ in series]
            values = [v for _, v in series]
            ax.plot(years, values, marker="o", linewidth=2, label=metric)
            for y, v in series:
                ax.annotate(f"{v:,.0f}", (y, v), textcoords="offset points", xytext=(0, 8), fontsize=8)

        ax.set_title(title, fontsize=12)
        ax.set_ylabel("亿元")
        ax.set_xticks(sorted({y for s in plottable.values() for y, _ in s}))
        ax.grid(axis="y", alpha=0.3)
        ax.legend(fontsize=9)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001 - a chart failure must never break report generation
        logger.warning(f"chart rendering failed, report will have no chart: {exc}")
        return None


def _chart_div(chart_b64: str, caption: str) -> str:
    return (
        '<div class="report-chart">\n'
        f'<img src="data:image/png;base64,{chart_b64}" alt="财务趋势图" '
        'style="max-width:100%;height:auto;" />\n'
        f'<p style="color:#888;font-size:12px;">{caption}</p>\n'
        "</div>"
    )


def build_charts_html(sources: list[Source], subject: str, snapshot: dict | None = None) -> str:
    """Render the chart section HTML ('' when nothing plottable).

    v3：结构化数据（AkShare 年度序列）优先——口径统一、无实体误归属风险；
    没有结构化数据时退回文本抽取（v2 行为）。两条路径的图注明确标注数据来源，
    数据点不足时诚实返回空串，不生成伪图。
    """
    parts: list[str] = []
    structured = (snapshot or {}).get("yearly_series") or {}

    if structured.get("revenue") or structured.get("net_profit"):
        money_series = {
            label: [(int(y), float(v)) for y, v in structured.get(field, [])]
            for label, field in [("营收", "revenue"), ("净利润", "net_profit")]
            if structured.get(field)
        }
        chart = render_trend_chart_base64(money_series, f"{subject} 营收/净利润年度趋势（AkShare）")
        if chart:
            parts.append(_chart_div(chart, "图：营收/净利润年度趋势。数据来源：AkShare（新浪财务摘要），单位亿元，仅供参考。"))

        ratio_series = {
            label: [(int(y), float(v)) for y, v in structured.get(field, [])]
            for label, field in [("ROE(%)", "roe"), ("毛利率(%)", "gross_margin")]
            if structured.get(field)
        }
        chart = render_trend_chart_base64(ratio_series, f"{subject} ROE/毛利率年度趋势（AkShare）")
        if chart:
            parts.append(_chart_div(chart, "图：ROE/毛利率年度趋势。数据来源：AkShare（新浪财务摘要），单位 %，仅供参考。"))

    if not parts:
        # 降级：v2 文本抽取路径
        series_by_metric = {
            metric: extract_metric_series(sources, metric, subject) for metric in _METRIC_KEYWORDS
        }
        chart = render_trend_chart_base64(series_by_metric, f"{subject} 核心财务指标趋势（公开资料抽取）")
        if chart:
            parts.append(_chart_div(
                chart,
                "图：核心财务指标年度趋势。数据由公开网页/PDF 资料自动抽取，可能存在口径差异或缺失年份，仅供参考。",
            ))
        else:
            points = {m: len(s) for m, s in series_by_metric.items()}
            logger.info(f"no trend chart generated (insufficient data points: {points})")

    return "\n".join(parts)


def build_valuation_table_html(dcf: dict | None, relative: dict | None) -> str:
    """估值结果汇总表（DCF 三情景 + 相对估值信号）。没有任何估值结果时返回 ''。"""
    rows: list[str] = []
    if dcf and dcf.get("valuation_range"):
        vr = dcf["valuation_range"]
        rows.append(
            f"<tr><td>DCF（工程近似）</td><td>悲观 {vr.get('bear')} / 基准 {vr.get('base')} / "
            f"乐观 {vr.get('bull')} 亿元</td><td>模型测算（参数出处见正文）</td></tr>"
        )
    for key, m in ((relative or {}).get("multiples") or {}).items():
        ref = m.get("peer_or_history_reference")
        ref_text = (
            f"自身近一年分位 {m.get('percentile')}%（中位 {ref['median']}）" if ref and m.get("percentile") is not None
            else "历史序列不足"
        )
        rows.append(
            f"<tr><td>{key.upper()} 相对估值</td><td>当前 {m.get('current_multiple')}，"
            f"信号：{m.get('valuation_signal')}</td><td>{ref_text}（AkShare）</td></tr>"
        )
    if not rows:
        return ""
    return (
        '<div class="report-chart"><table border="1" cellspacing="0" cellpadding="6" '
        'style="border-collapse:collapse;font-size:13px;">'
        "<tr><th>估值方法</th><th>结果</th><th>参照/来源</th></tr>"
        + "".join(rows)
        + "</table>"
        + '<p style="color:#888;font-size:12px;">估值结果为工具计算与配置假设的演示，参照系与假设见正文说明，不构成投资建议。</p></div>'
    )
