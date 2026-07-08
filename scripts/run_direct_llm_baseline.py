"""Direct-LLM baseline: one un-grounded LLM call per topic, no search/fetch/citations.

Usage:
    python scripts/run_direct_llm_baseline.py

Exists purely as a comparison point for scripts/build_baseline_compare.py
against the Agent pipeline's source-grounded reports - this script never
searches, fetches web/PDF content, or reads outputs/sources; the LLM
answers from its own training data alone. source_count/citation_coverage/
source_grounding are therefore always 0 by construction, not a measurement.
"""
import csv
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from agents.base_agent import BaseAgent  # noqa: E402
from config import config  # noqa: E402
from utils.file_utils import load_json, sanitize_filename, save_text  # noqa: E402
from utils.logger import logger  # noqa: E402

_BASELINE_PROMPT = """请生成一份关于「{topic}」的中文金融研究报告，包含：
1. 公司/行业概况
2. 财务或趋势分析
3. 估值或投资逻辑
4. 风险提示
5. 投资观点

要求：
- 结构清晰；
- 不要编造具体引用来源；
- 如果没有实时数据，请明确说明局限性；
- 报告内容不可作为投资建议。"""

_CITATION_RE = re.compile(r"\[s\d+\]|\[source_?\d*\]")
_STRUCTURE_KEYWORDS = ["概况", "财务", "趋势", "估值", "投资逻辑", "风险", "投资观点"]
_LIMITATION_KEYWORDS = [
    "局限性", "不构成投资建议", "无法获取实时", "仅供参考", "无法保证",
    "数据可能滞后", "不具备实时", "截至我的知识", "训练数据",
]

FIELDNAMES = [
    "topic", "report_type", "output_path", "total_time", "has_citation",
    "source_count", "citation_coverage", "source_grounding", "structure_complete",
    "obvious_limitation", "notes",
]


def run_one(topic: str, report_type: str) -> dict:
    """Generate one baseline report and score it against the same cheap
    structural heuristics used elsewhere in the project (no external calls)."""
    prompt = _BASELINE_PROMPT.format(topic=topic)
    start = time.perf_counter()
    notes = ""
    content = ""
    try:
        content = BaseAgent.call_llm(prompt, system="你是专业金融研究报告撰写助手。", temperature=0.4)
    except Exception as exc:  # noqa: BLE001 - one topic failing must not stop the batch
        notes = f"LLM call failed: {exc}"
        logger.warning(f"baseline generation failed for topic={topic}: {exc}")
    elapsed = round(time.perf_counter() - start, 4)

    safe_topic = sanitize_filename(topic)
    output_path = config.BASELINE_REPORTS_DIR / f"{safe_topic}_baseline.md"
    save_text(output_path, content or f"# {topic}\n\n(generation failed: {notes})\n")

    has_citation = bool(_CITATION_RE.search(content))
    structure_complete = sum(1 for kw in _STRUCTURE_KEYWORDS if kw in content) >= 4
    obvious_limitation = any(kw in content for kw in _LIMITATION_KEYWORDS)

    return {
        "topic": topic,
        "report_type": report_type,
        "output_path": str(output_path),
        "total_time": elapsed,
        "has_citation": has_citation,
        # Structurally guaranteed, not measured: this script never searches or
        # fetches sources, so there is nothing to cite or ground against.
        "source_count": 0,
        "citation_coverage": 0,
        "source_grounding": 0,
        "structure_complete": structure_complete,
        "obvious_limitation": obvious_limitation,
        "notes": notes,
    }


def main() -> None:
    topics_path = config.BASE_DIR / "eval" / "topics.json"
    topics = load_json(topics_path)

    rows = []
    for entry in topics:
        topic = entry["topic"]
        report_type = entry.get("report_type", "company_research")
        print(f"Generating direct-LLM baseline for: {topic}")
        rows.append(run_one(topic, report_type))

    out_path = config.EVAL_DIR / "direct_llm_baseline.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"Baseline CSV saved to {out_path}")
    print(f"Baseline reports saved to {config.BASELINE_REPORTS_DIR}")


if __name__ == "__main__":
    main()
