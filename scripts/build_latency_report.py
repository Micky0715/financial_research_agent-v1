"""Per-stage latency report (v3 stage J).

Usage:
    python scripts/build_latency_report.py

Computed from outputs/eval/eval_summary.csv. Sample size is the eval suite
(10 topics) - P90/P95 on 10 points are order statistics, stated as such.
"""
import csv
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from config import config  # noqa: E402

_STAGES = [
    ("total_time", "总耗时"),
    ("research_time", "Research"),
    ("browser_time", "Browser"),
    ("analyze_time", "Analyze"),
    ("report_time", "Report"),
]


def _f(v) -> Optional[float]:
    try:
        return None if v in (None, "", "N/A") else float(v)
    except (TypeError, ValueError):
        return None


def _pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))], 2)


def main() -> None:
    csv_path = config.EVAL_DIR / "eval_summary.csv"
    if not csv_path.exists():
        print("[error] eval_summary.csv not found")
        sys.exit(1)
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8", newline="")))

    lines = [
        "# Latency Report",
        "",
        f"数据来源：eval suite（{len(rows)} topic 各取最新 run）。注意：Research 阶段普遍是"
        "热缓存（搜索缓存命中），冷缓存约 10-16s，见 README 评测说明。",
        "",
        "| 阶段 | avg | p50 | p90 | p95 | max |",
        "|---|---|---|---|---|---|",
    ]
    stage_avgs: dict[str, float] = {}
    for key, label in _STAGES:
        values = [v for v in (_f(r.get(key)) for r in rows) if v is not None]
        if not values:
            lines.append(f"| {label} | insufficient data | - | - | - | - |")
            continue
        avg = round(sum(values) / len(values), 2)
        stage_avgs[label] = avg
        lines.append(
            f"| {label} | {avg}s | {_pct(values, 50)}s | {_pct(values, 90)}s "
            f"| {_pct(values, 95)}s | {round(max(values), 2)}s |"
        )

    slowest = sorted(
        ((r.get("topic", "?"), _f(r.get("total_time"))) for r in rows),
        key=lambda x: x[1] or 0, reverse=True,
    )[:5]
    lines += ["", "## 最慢 5 个 case", ""]
    lines += [f"- {t}: {v}s" for t, v in slowest if v is not None]

    total_avg = stage_avgs.get("总耗时")
    if total_avg:
        lines += ["", "## 每阶段耗时占比（均值口径）", ""]
        other = total_avg
        for label in ("Research", "Browser", "Analyze", "Report"):
            if label in stage_avgs:
                share = stage_avgs[label] / total_avg
                other -= stage_avgs[label]
                lines.append(f"- {label}: {stage_avgs[label]}s（{share:.0%}）")
        lines.append(f"- 其他（planning/评估/落盘）: ~{round(max(other, 0), 2)}s（{max(other, 0) / total_avg:.0%}）")

        bottleneck = max(
            ((label, v) for label, v in stage_avgs.items() if label != "总耗时"),
            key=lambda kv: kv[1], default=None,
        )
        if bottleneck:
            lines += [
                "",
                "## 瓶颈分析",
                "",
                f"- 当前均值口径的最大耗时阶段是 **{bottleneck[0]}**（{bottleneck[1]}s）。",
                "- Research 已被缓存消解（热缓存 <0.1s）；Analyze/Report 的耗时主体是 LLM 调用延迟，",
                "  进一步优化方向是提示词精简与流式/并行化，而不是本地代码路径。",
                "- Browser 耗时随候选站点响应速度波动，读取上限与提前停止已控制其上界。",
            ]

    out = config.EVAL_DIR / "latency_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Latency report saved to {out}")


if __name__ == "__main__":
    main()
