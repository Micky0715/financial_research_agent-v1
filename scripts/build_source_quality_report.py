"""Source authority tier quality report (v3 stage C).

Usage:
    python scripts/build_source_quality_report.py

Reads each eval topic's latest sources file (via outputs/eval/eval_summary.csv)
plus its evaluation, classifies every source with tools/source_tier.py, and
writes outputs/eval/source_quality_report.md. Pure offline analysis - no
network, no LLM.
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from config import config  # noqa: E402
from tools.source_tier import classify_source, tier_counts  # noqa: E402
from utils.file_utils import load_json  # noqa: E402


def main() -> None:
    csv_path = config.EVAL_DIR / "eval_summary.csv"
    if not csv_path.exists():
        print(f"[error] {csv_path} not found. Run scripts/collect_eval_summary.py first.")
        sys.exit(1)
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8", newline="")))

    lines = [
        "# 来源权威性分级报告（source authority tiers）",
        "",
        "对每个 eval topic 最新一次 run 的入选来源做 tier 分级（tools/source_tier.py）。",
        "**分级是启发式的**：tier1 不代表内容绝对正确，tier3 不代表内容一定差；分级刻画的是来源性质。",
        "",
        "| topic | 来源数 | tier1 | tier2 | tier3 | unknown | tier1+2 占比 | authority_score(eval) |",
        "|---|---|---|---|---|---|---|---|",
    ]

    all_counts: list[dict] = []
    low_ratio_cases: list[tuple[str, float, list[str]]] = []

    for row in rows:
        topic = row.get("topic", "?")
        sources_path = row.get("sources_path", "")
        if not sources_path or sources_path == "N/A" or not Path(sources_path).exists():
            lines.append(f"| {topic} | (无来源文件) | - | - | - | - | - | - |")
            continue
        try:
            sources = load_json(Path(sources_path))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"| {topic} | (读取失败: {exc}) | - | - | - | - | - | - |")
            continue

        classified = [
            classify_source(s.get("url", ""), s.get("title", ""), s.get("source_type", "web"))
            for s in sources
        ]
        counts = tier_counts(classified)
        all_counts.append(counts)
        lines.append(
            f"| {topic} | {counts['total']} | {counts['tier1']} | {counts['tier2']} | {counts['tier3']} "
            f"| {counts['unknown']} | {counts['tier1_or_tier2_ratio']:.2f} | {row.get('authority_score', 'N/A')} |"
        )
        if counts["tier1_or_tier2_ratio"] < 0.5:
            domains = [c["domain"] for c in classified if c["authority_tier"] == "tier3"]
            low_ratio_cases.append((topic, counts["tier1_or_tier2_ratio"], domains))

    if all_counts:
        total = sum(c["total"] for c in all_counts)
        agg = {
            t: sum(c[t] for c in all_counts) for t in ("tier1", "tier2", "tier3", "unknown")
        }
        overall_ratio = (agg["tier1"] + agg["tier2"]) / total if total else 0.0
        lines += [
            "",
            "## 总体分布",
            "",
            f"- 来源总数: {total}",
            *(f"- {t}: {n}（{n / total:.1%}）" for t, n in agg.items() if total),
            f"- **overall tier1_or_tier2_ratio: {overall_ratio:.3f}**",
            "",
            "## authority_score 与 tier 分布的关系",
            "",
            "evaluation 里的 authority_score 由 tier1=1.0 / tier2=0.7 / 白名单=0.5 / 其他=0.3 加权平均而来，",
            "所以 tier1+2 占比高的 topic authority_score 必然高——两者是同一信号的两种呈现，不是独立验证。",
            "",
            "## 低权威来源占比较高的 case",
            "",
        ]
        if low_ratio_cases:
            for topic, ratio, domains in low_ratio_cases:
                lines.append(f"- **{topic}**（tier1+2 占比 {ratio:.2f}）：tier3 域名 {', '.join(domains[:5])}")
            lines += [
                "",
                "典型模式：行业类主题（市场空间/产业链分析）的优质内容多在行业媒体和自媒体上，",
                "官方公告类 tier1 来源天然少——低 tier 占比不一定是检索质量问题，需结合内容相关性判断。",
            ]
        else:
            lines.append("-（本轮没有 tier1+2 占比低于 0.5 的 case）")

    out = config.EVAL_DIR / "source_quality_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Source quality report saved to {out}")


if __name__ == "__main__":
    main()
