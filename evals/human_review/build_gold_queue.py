"""Build the human gold-review queue (Phase 4).

Deliberately small: 30-50 stratified tasks, not 200. A reviewer who is asked to
read 200 cases reads none of them carefully, and a queue nobody finishes
produces zero human-verified tasks - which is exactly the state this project is
trying to leave.

    python -m evals.human_review.build_gold_queue --write
    python -m evals.human_review.validate_gold --queue evals/human_review/gold_review_queue.jsonl

Stratification covers the 12 categories the audit brief lists. Selection is
deterministic (sorted by a stable hash) so re-running produces the same queue
and a half-finished review is never invalidated by a reshuffle.

Nothing here can mark a task `human_verified`. That transition happens only in
`validate_gold.py`, and only when a real person's name, a real date and a
complete answer set are present.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from evals.datasets.schema import Category, EvalDataset, EvalTask, GoldStatus  # noqa: E402

HERE = Path(__file__).resolve().parent
QUEUE_PATH = HERE / "gold_review_queue.jsonl"
DATASET_DIR = Path(__file__).resolve().parents[1] / "datasets"

#: The 12 strata the audit brief requires, and how many of each to queue.
STRATA: dict[str, tuple[list[Category], int]] = {
    "公司研究": ([Category.COMPANY_RESEARCH], 5),
    "行业研究": ([Category.INDUSTRY_RESEARCH], 4),
    "宏观研究": ([Category.MACRO_RESEARCH], 4),
    "多来源事实核验": ([Category.MULTI_SOURCE_VERIFICATION], 3),
    "PDF抽取": ([Category.PDF_EXTRACTION], 3),
    "AkShare结构化数据": ([Category.STRUCTURED_DATA], 3),
    "来源冲突": ([Category.SOURCE_CONFLICT], 3),
    "信息不足": ([Category.INCOMPLETE_INFORMATION, Category.MUST_REFUSE], 4),
    "PromptInjection": ([Category.PROMPT_INJECTION], 3),
    "工具失败恢复": ([Category.TOOL_TIMEOUT, Category.UNREACHABLE_SOURCE], 4),
    "长上下文": ([Category.LONG_CONTEXT], 2),
    "Checkpoint恢复": ([Category.RESUME_AFTER_INTERRUPT], 2),
}


def _stable_rank(task: EvalTask) -> str:
    return hashlib.sha256(task.task_id.encode("utf-8")).hexdigest()


def _blank_answers() -> dict[str, Any]:
    """The fields a reviewer fills in. Empty by design."""
    return {
        "task_is_reasonable": None,        # true / false
        "required_facts": [],              # [{"key":..., "any_of":[...], "description":...}]
        "acceptable_sources": [],          # 允许作为依据的来源域名或类型
        "correct_tool_is_unique": None,    # true / false
        "acceptable_tool_paths": [],       # [["web_search","read_webpage"], ...]
        "required_tools": [],
        "expects_no_tools": None,          # true / false
        "correct_final_state": "",         # report_generated / report_with_caveats /
                                           # insufficient_evidence / refuse /
                                           # partial_with_degradation
        "degraded_acceptable": None,       # true / false
        "forbidden_claims": [],
        "citations_support_conclusion": None,   # true / false / "not_applicable"
        "numbers_correctly_grounded": None,     # true / false / "not_applicable"
        "score": None,                     # 1-5 整体质量
        "notes": "",
        "annotator": "",                   # 必填：真实姓名或稳定标识
        "reviewed_at": "",                 # 必填：YYYY-MM-DD
    }


def build_queue(dataset: EvalDataset) -> list[dict[str, Any]]:
    by_category: dict[Category, list[EvalTask]] = defaultdict(list)
    for task in dataset.tasks:
        by_category[task.category].append(task)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stratum, (categories, quota) in STRATA.items():
        pool: list[EvalTask] = []
        for category in categories:
            pool.extend(by_category.get(category, []))
        pool = [t for t in pool if t.task_id not in seen]
        pool.sort(key=_stable_rank)
        chosen = pool[:quota]
        for task in chosen:
            seen.add(task.task_id)
            rows.append({
                "stratum": stratum,
                "task_id": task.task_id,
                "category": task.category.value,
                "report_type": task.report_type,
                "query": task.query,
                "fixture_scenario": task.fixture_scenario,
                "current": {
                    "expected_outcome": task.expected_outcome.value,
                    "gold_status": task.gold_status.value,
                    "required_tools": task.required_tools,
                    "optional_expected_tools": task.optional_expected_tools,
                    "forbidden_tools": task.forbidden_tools,
                    "forbidden_claims": task.forbidden_claims,
                    "required_facts": [f.model_dump() for f in task.required_facts],
                    "data_source": task.data_source,
                    "source_reference": task.source_reference,
                    "note": task.task_constraints.get("note", ""),
                },
                "review": _blank_answers(),
            })
        if len(chosen) < quota:
            rows.append({
                "stratum": stratum,
                "task_id": f"__SHORTFALL__{stratum}",
                "category": "n/a",
                "query": (f"该分层可用任务不足：需要 {quota} 条，数据集中只有 {len(chosen)} 条。"
                          "这是数据集覆盖缺口，不是审核项，请勿填写。"),
                "current": {"shortfall": quota - len(chosen)},
                "review": {},
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the human gold-review queue")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--out", type=Path, default=QUEUE_PATH)
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR / "all_tasks.json")
    args = parser.parse_args()

    dataset = EvalDataset.load(args.dataset)
    rows = build_queue(dataset)
    real = [r for r in rows if not r["task_id"].startswith("__SHORTFALL__")]
    shortfalls = [r for r in rows if r["task_id"].startswith("__SHORTFALL__")]

    print(f"queue rows: {len(real)} reviewable"
          + (f" (+{len(shortfalls)} shortfall markers)" if shortfalls else ""))
    counts: dict[str, int] = defaultdict(int)
    for row in real:
        counts[row["stratum"]] += 1
    for stratum, (_cats, quota) in STRATA.items():
        got = counts.get(stratum, 0)
        flag = "" if got >= quota else f"  <-- 缺 {quota - got} 条"
        print(f"  {stratum:20s} {got}/{quota}{flag}")

    if args.write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"wrote {args.out}")
        print("\n下一步（需要真人完成）：")
        print(f"  1. 阅读 {HERE / 'annotation_guide.md'}")
        print(f"  2. 编辑 {args.out}，逐行填写 review.* 字段")
        print("  3. 运行 python -m evals.human_review.validate_gold")
    else:
        print("(dry run; pass --write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
