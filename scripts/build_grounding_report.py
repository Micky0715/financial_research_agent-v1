"""Number-level grounding report over the eval topics (v3 stage D).

Usage:
    python scripts/build_grounding_report.py

Reads each eval topic's latest report markdown + sources + trace (for the
structured-data snapshot / DCF outputs when present) and runs
tools/number_grounding.py over the report text. Writes
outputs/eval/grounding_report.md. Offline - no network, no LLM.
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from config import config  # noqa: E402
from tools.number_grounding import analyze_report_numbers  # noqa: E402
from utils.file_utils import load_json  # noqa: E402


def _report_markdown(report_path: str) -> str:
    """Report file -> markdown-ish text (strip tags crudely for .html)."""
    path = Path(report_path)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix == ".html":
        import re

        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", text)
        text = re.sub(r"<[^>]+>", "", text)
    return text


def main() -> None:
    csv_path = config.EVAL_DIR / "eval_summary.csv"
    if not csv_path.exists():
        print(f"[error] {csv_path} not found. Run scripts/collect_eval_summary.py first.")
        sys.exit(1)
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8", newline="")))

    lines = [
        "# 数字级 grounding 报告",
        "",
        "对每个 eval topic 最新报告全文的实质性数字（金额/百分比/倍数）做溯源分类：",
        "sourced（[sX] 引用）/ akshare（结构化数据标注）/ assumption（配置假设标注）/ "
        "calculated（模型测算标注）/ unsourced（无任何溯源）。",
        "分类基于句级标注启发式，**不等于逐数字的事实审计**；假设类数字标注为假设即视为已溯源，",
        "不会也不应该为其伪造来源引用。",
        "",
        "| topic | 数字总数 | grounding率 | sourced | akshare | 假设 | 测算 | unsourced | 无效引用 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    per_topic: list[tuple[str, dict]] = []
    for row in rows:
        topic = row.get("topic", "?")
        report_path = row.get("report_path", "")
        sources_path = row.get("sources_path", "")
        trace_path = row.get("trace_path", "")
        markdown = _report_markdown(report_path)
        if not markdown:
            lines.append(f"| {topic} | (报告缺失) | - | - | - | - | - | - | - |")
            continue
        try:
            sources = load_json(Path(sources_path)) if sources_path and Path(sources_path).exists() else []
        except Exception:  # noqa: BLE001
            sources = []
        snapshot = dcf = None
        try:
            if trace_path and Path(trace_path).exists():
                trace = load_json(Path(trace_path))
                # snapshot/dcf 不直接存 trace（体积原因），从 analysis 结果无法离线取；
                # 这里用 trace 里的 structured_data_metrics 判断是否有结构化数据，
                # 数值核对退化为标注核对（如实说明，不假装做了数值核对）。
                _ = trace
        except Exception:  # noqa: BLE001
            pass

        result = analyze_report_numbers(markdown, [s.get("source_id", "") for s in sources], snapshot, dcf)
        per_topic.append((topic, result))
        lines.append(
            f"| {topic} | {result['total_number_count']} | {result['number_grounding_rate']:.2f} "
            f"| {result['sourced_number_count']} | {result['akshare_number_count']} "
            f"| {result['assumption_number_count']} | {result['calculated_number_count']} "
            f"| {result['unsourced_number_count']} | {result['invalid_citation_count']} |"
        )

    if per_topic:
        totals = {
            k: sum(r[k] for _, r in per_topic)
            for k in ("total_number_count", "grounded_number_count", "unsourced_number_count",
                      "assumption_number_count", "calculated_number_count", "akshare_number_count",
                      "sourced_number_count", "invalid_citation_count")
        }
        overall_rate = (
            totals["grounded_number_count"] / totals["total_number_count"]
            if totals["total_number_count"] else 1.0
        )
        lines += [
            "",
            "## 汇总",
            "",
            f"- 数字总数: {totals['total_number_count']}，整体 number_grounding_rate: **{overall_rate:.3f}**",
            f"- sourced: {totals['sourced_number_count']} | akshare: {totals['akshare_number_count']} "
            f"| 假设: {totals['assumption_number_count']} | 测算: {totals['calculated_number_count']} "
            f"| unsourced: {totals['unsourced_number_count']} | 无效引用: {totals['invalid_citation_count']}",
            "",
            "## 未溯源数字（各 topic 最多 3 例）",
            "",
        ]
        for topic, result in per_topic:
            for item in result["unsourced_detail"][:3]:
                lines.append(f"- **{topic}** `{item['number']}`：{item['sentence']}")

        worst = sorted(per_topic, key=lambda tr: tr[1]["number_grounding_rate"])[:5]
        lines += [
            "",
            "## 最典型的 5 个问题 case（grounding 率最低）",
            "",
            *(f"- {t}：grounding 率 {r['number_grounding_rate']:.2f}，unsourced {r['unsourced_number_count']} 个"
              for t, r in worst),
            "",
            "## 说明",
            "",
            "- akshare/calculated 类数字的数值核对需要运行时的 snapshot/DCF 输出，离线报告只做标注",
            "  分类；运行时核对结果见 evaluation diagnostics 的 value_verified 字段。",
            "- HTML 报告以去标签后的文本统计，图表内数字不计入。",
        ]

    out = config.EVAL_DIR / "grounding_report.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Grounding report saved to {out}")


if __name__ == "__main__":
    main()
