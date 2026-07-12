"""Report/bad-case memory entries built from existing outputs (v3 stage I).

从 outputs/eval/eval_summary.csv（每 topic 最新 run）、报告文件和
eval/bad_cases.md 构建记忆条目。**默认不参与主链路**——只有
scripts/build_memory_index.py / search_memory.py 显式调用。
"""
import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from config import config
from memory.vector_store import LightVectorStore


def _report_excerpt(report_path: str, max_chars: int = 600) -> str:
    p = Path(report_path)
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8", errors="ignore")
    if p.suffix == ".html":
        text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", text)
        text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text)[:max_chars]


def collect_memory_entries() -> tuple[list[str], list[dict[str, Any]]]:
    """已有产物 -> (texts, metadatas)。"""
    texts, metas = [], []
    now = datetime.now().isoformat(timespec="seconds")

    # 1) 报告摘要（每个 eval topic 的最新 run）
    csv_path = config.EVAL_DIR / "eval_summary.csv"
    if csv_path.exists():
        for row in csv.DictReader(open(csv_path, encoding="utf-8", newline="")):
            excerpt = _report_excerpt(row.get("report_path", ""))
            if not excerpt:
                continue
            texts.append(f"{row.get('topic', '')}\n{excerpt}")
            metas.append({
                "kind": "report_summary",
                "topic": row.get("topic"),
                "report_type": row.get("report_type"),
                "run_id": row.get("run_id"),
                "created_at": now,
                "source_path": row.get("report_path"),
                "quality_score": row.get("quality_score"),
                "tags": ["eval_topic", row.get("report_type", "")],
            })

    # 2) bad cases（按 "## Bad Case" 切分）
    bad_cases_path = config.BASE_DIR / "eval" / "bad_cases.md"
    if bad_cases_path.exists():
        content = bad_cases_path.read_text(encoding="utf-8", errors="ignore")
        for block in re.split(r"\n## ", content):
            if not block.startswith("Bad Case"):
                continue
            title = block.splitlines()[0].strip()
            texts.append(block[:800])
            metas.append({
                "kind": "bad_case",
                "topic": title,
                "report_type": "",
                "run_id": "",
                "created_at": now,
                "source_path": str(bad_cases_path),
                "quality_score": "",
                "tags": ["bad_case"],
            })

    return texts, metas


def build_index() -> dict[str, Any]:
    texts, metas = collect_memory_entries()
    store = LightVectorStore()
    result = store.rebuild(texts, metas)
    return {**result, "report_summaries": sum(1 for m in metas if m["kind"] == "report_summary"),
            "bad_cases": sum(1 for m in metas if m["kind"] == "bad_case")}


def search_memory(query: str, top_k: int = 5, kind: str | None = None) -> list[dict[str, Any]]:
    return LightVectorStore().search(query, top_k=top_k, kind=kind)
