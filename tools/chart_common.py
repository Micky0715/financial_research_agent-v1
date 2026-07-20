"""Shared chart rendering helpers for v4 chart builders (market/macro/industry).

每张图输出 (html, meta)：meta 固定含 chart_title/data_period/source/fetched_at/
unit/missing_data_note + subject/values_sample，供 chart_consistency_checker
核对（图表主体与正文一致、数值与结构化数据一致、缺数据不补零）。
渲染失败返回 (\"\", None)——图表是增强，绝不让报告崩。
"""
import base64
import io
from datetime import datetime
from typing import Any, Optional

from utils.logger import logger


def render_lines_base64(series_by_label: dict[str, list[tuple[Any, float]]], title: str,
                        ylabel: str, max_xticks: int = 10,
                        secondary: Optional[dict[str, list[tuple[Any, float]]]] = None,
                        bars: bool = False) -> Optional[str]:
    """通用折线/柱状图（x 可为字符串期次）。<2 个点的序列不画（单点不是趋势）。"""
    plottable = {k: v for k, v in series_by_label.items() if len(v) >= 2}
    if not plottable and not bars:
        return None
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "sans-serif"]
        plt.rcParams["axes.unicode_minus"] = False
        fig, ax = plt.subplots(figsize=(7.5, 3.8), dpi=110)

        if bars:
            labels = [x for x, _ in next(iter(series_by_label.values()))]
            values = [v for _, v in next(iter(series_by_label.values()))]
            ax.bar(range(len(labels)), values, color="#4C72B0")
            ax.set_xticks(range(len(labels)))
            ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
            for i, v in enumerate(values):
                ax.annotate(f"{v:,.1f}", (i, v), textcoords="offset points",
                            xytext=(0, 4), fontsize=8, ha="center")
        else:
            all_x: list = []
            for label, series in plottable.items():
                xs = list(range(len(series)))
                ax.plot(xs, [v for _, v in series], marker="", linewidth=1.8, label=label)
                if len(series) > len(all_x):
                    all_x = [x for x, _ in series]
            step = max(1, len(all_x) // max_xticks)
            ticks = list(range(0, len(all_x), step))
            ax.set_xticks(ticks)
            ax.set_xticklabels([str(all_x[i]) for i in ticks], rotation=30, ha="right", fontsize=8)
            ax.legend(fontsize=9)
        if secondary:
            ax2 = ax.twinx()
            for label, series in secondary.items():
                ax2.bar(range(len(series)), [v for _, v in series], alpha=0.25,
                        color="#999999", label=label)
                ax2.set_ylabel(label, fontsize=9)

        ax.set_title(title, fontsize=12)
        ax.set_ylabel(ylabel)
        ax.grid(axis="y", alpha=0.3)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001 - chart failure must never break the report
        logger.warning(f"v4 chart render failed: {exc!r}")
        return None


def chart_block(b64: Optional[str], *, chart_title: str, data_period: str, source: str,
                unit: str, subject: str = "", missing_data_note: str = "",
                values_sample: Optional[dict] = None) -> tuple[str, Optional[dict]]:
    """图 + 必备元数据 caption。b64 为 None 时返回 ('', None)。"""
    if not b64:
        return "", None
    fetched_at = datetime.now().isoformat(timespec="seconds")
    caption = (f"{chart_title}｜数据期间：{data_period}｜来源：{source}｜单位：{unit}"
               f"｜抓取时间：{fetched_at}")
    if missing_data_note:
        caption += f"｜缺失说明：{missing_data_note}"
    html = (
        '<div class="report-chart">\n'
        f'<img src="data:image/png;base64,{b64}" alt="{chart_title}" '
        'style="max-width:100%;height:auto;" />\n'
        f'<p style="color:#888;font-size:12px;">{caption}</p>\n'
        "</div>"
    )
    meta = {"chart_title": chart_title, "data_period": data_period, "source": source,
            "fetched_at": fetched_at, "unit": unit, "subject": subject,
            "missing_data_note": missing_data_note, "values_sample": values_sample or {}}
    return html, meta
