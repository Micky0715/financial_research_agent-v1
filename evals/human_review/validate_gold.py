"""Validate a filled review queue and promote tasks to `human_verified`.

This is the **only** path to `human_verified` in the project. It refuses on any
of the following, and reports every reason rather than the first:

  - `annotator` empty, or an obvious placeholder ("me", "test", "ai", "claude")
  - `reviewed_at` missing, malformed, or in the future
  - a required answer left null
  - `correct_final_state` not one of the declared outcomes
  - internal contradiction (e.g. `expects_no_tools` true while `required_tools`
    is non-empty; `correct_tool_is_unique` true with several acceptable paths)
  - a task the reviewer marked unreasonable (`task_is_reasonable=false`) -
    those are dropped from the dataset, not promoted

    python -m evals.human_review.validate_gold                 # report only
    python -m evals.human_review.validate_gold --apply         # write back
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from evals.datasets.schema import (  # noqa: E402
    EvalDataset,
    ExpectedOutcome,
    GoldStatus,
    RequiredFact,
    Split,
)

HERE = Path(__file__).resolve().parent
QUEUE_PATH = HERE / "gold_review_queue.jsonl"
DATASET_DIR = Path(__file__).resolve().parents[1] / "datasets"

_PLACEHOLDER_ANNOTATORS = {"", "me", "test", "tester", "ai", "gpt", "claude", "llm",
                           "assistant", "n/a", "na", "none", "todo", "xxx", "自己", "本人"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Answers that must be present for a row to be promotable.
_REQUIRED_ANSWERS = ("task_is_reasonable", "correct_final_state", "annotator", "reviewed_at")


def _validate_row(row: dict[str, Any]) -> tuple[bool, list[str]]:
    problems: list[str] = []
    review = row.get("review") or {}
    task_id = row.get("task_id", "?")

    if task_id.startswith("__SHORTFALL__"):
        return False, ["shortfall marker, not a review row"]

    if not any(v not in (None, "", [], {}) for v in review.values()):
        return False, ["未填写（整行为空）"]

    for field in _REQUIRED_ANSWERS:
        if review.get(field) in (None, ""):
            problems.append(f"缺少必填项 `{field}`")

    annotator = str(review.get("annotator", "")).strip()
    if annotator.lower() in _PLACEHOLDER_ANNOTATORS:
        problems.append(f"annotator={annotator!r} 不是有效的真人标识")

    reviewed_at = str(review.get("reviewed_at", "")).strip()
    if reviewed_at:
        if not _DATE_RE.match(reviewed_at):
            problems.append(f"reviewed_at={reviewed_at!r} 不是 YYYY-MM-DD")
        else:
            try:
                when = datetime.strptime(reviewed_at, "%Y-%m-%d").date()
                if when > date.today():
                    problems.append(f"reviewed_at={reviewed_at} 在未来")
            except ValueError:
                problems.append(f"reviewed_at={reviewed_at!r} 不是合法日期")

    outcome = str(review.get("correct_final_state", "")).strip()
    valid_outcomes = {o.value for o in ExpectedOutcome}
    if outcome and outcome not in valid_outcomes:
        problems.append(f"correct_final_state={outcome!r} 不在 {sorted(valid_outcomes)} 中")

    # Consistency checks
    expects_no_tools = review.get("expects_no_tools")
    required_tools = review.get("required_tools") or []
    if expects_no_tools is True and required_tools:
        problems.append("expects_no_tools=true 与非空 required_tools 矛盾")

    unique = review.get("correct_tool_is_unique")
    paths = review.get("acceptable_tool_paths") or []
    if unique is True and len(paths) > 1:
        problems.append("correct_tool_is_unique=true 但给出了多条 acceptable_tool_paths")

    score = review.get("score")
    if score is not None and not (isinstance(score, (int, float)) and 1 <= score <= 5):
        problems.append(f"score={score!r} 不在 1-5 区间")

    if review.get("task_is_reasonable") is False:
        # Valid review, but the outcome is "drop", not "promote".
        return False, ["reviewer 判定任务不合理 -> 应从数据集中移除，不予升级"]

    return (not problems), problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and apply human gold review")
    parser.add_argument("--queue", type=Path, default=QUEUE_PATH)
    parser.add_argument("--dataset", type=Path, default=DATASET_DIR / "all_tasks.json")
    parser.add_argument("--apply", action="store_true",
                        help="write promotions back into the dataset")
    args = parser.parse_args()

    if not args.queue.exists():
        raise SystemExit(f"queue not found: {args.queue}; run build_gold_queue.py --write first")

    rows = [json.loads(line) for line in args.queue.read_text(encoding="utf-8").splitlines()
            if line.strip()]

    promotable: list[dict[str, Any]] = []
    rejected: list[tuple[str, list[str]]] = []
    untouched = 0
    to_drop: list[str] = []

    for row in rows:
        ok, problems = _validate_row(row)
        if ok:
            promotable.append(row)
        elif problems == ["未填写（整行为空）"]:
            untouched += 1
        elif problems and problems[0].startswith("reviewer 判定任务不合理"):
            to_drop.append(row["task_id"])
        elif problems == ["shortfall marker, not a review row"]:
            continue
        else:
            rejected.append((row.get("task_id", "?"), problems))

    print(f"队列共 {len(rows)} 行")
    print(f"  未填写           : {untouched}")
    print(f"  可升级           : {len(promotable)}")
    print(f"  填写有问题       : {len(rejected)}")
    print(f"  判定应移除       : {len(to_drop)}")
    for task_id, problems in rejected[:20]:
        print(f"    - {task_id}: {'; '.join(problems)}")

    if not promotable:
        print("\n没有任何任务满足升级条件。数据集中 human_verified 数量保持不变。")
        print("这是预期状态，直到有真人完成审核 —— 系统不会自行升级。")
        if not args.apply:
            return 0

    if not args.apply:
        print("\n(仅校验；加 --apply 才写回数据集)")
        return 0

    dataset = EvalDataset.load(args.dataset)
    by_id = {t.task_id: t for t in dataset.tasks}
    applied = 0
    for row in promotable:
        task = by_id.get(row["task_id"])
        if task is None:
            continue
        review = row["review"]
        task.expected_outcome = ExpectedOutcome(review["correct_final_state"])
        if review.get("required_facts"):
            task.required_facts = [RequiredFact(**f) for f in review["required_facts"]]
        if review.get("required_tools"):
            task.required_tools = list(review["required_tools"])
        if review.get("acceptable_tool_paths"):
            task.acceptable_tool_paths = [list(p) for p in review["acceptable_tool_paths"]]
        if review.get("expects_no_tools") is not None:
            task.expects_no_tools = bool(review["expects_no_tools"])
        if review.get("forbidden_claims"):
            task.forbidden_claims = list(review["forbidden_claims"])
        task.annotator = str(review["annotator"]).strip()
        task.reviewed_at = str(review["reviewed_at"]).strip()
        task.gold_status = GoldStatus.HUMAN_VERIFIED
        applied += 1

    dataset.tasks = [t for t in dataset.tasks if t.task_id not in set(to_drop)]
    dataset.save(args.dataset)
    for split in Split:
        subset = EvalDataset(name=f"{dataset.name}_{split.value}",
                             description=dataset.description, created_at=dataset.created_at,
                             tasks=dataset.by_split(split))
        subset.save(DATASET_DIR / f"{split.value}.json")

    print(f"\n已升级 {applied} 条为 human_verified；移除 {len(to_drop)} 条被判定不合理的任务")
    print(json.dumps(dataset.stats(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
