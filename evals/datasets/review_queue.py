"""Human review queue: the only path from `synthetic_draft` to `human_verified`.

There is no automatic promotion, and there is deliberately no flag that grants
it in bulk. A task becomes verified when a named person records a decision, and
`schema.EvalTask` refuses the status without an annotator and a date.

Workflow:

    # 1. export the highest-value unreviewed tasks
    python -m evals.datasets.review_queue export --limit 40

    # 2. a human edits evals/datasets/review_queue.jsonl, setting for each row:
    #      decision: accept | reject | revise
    #      annotator, notes, and (for accept) any required_facts they verified
    #
    # 3. apply the decisions back into the datasets
    python -m evals.datasets.review_queue apply --annotator "your-name"

    # 4. see what the dataset now claims
    python -m evals.datasets.review_queue status

Priority order for review: regression cases derived from real bad cases first
(highest information per minute of reviewer time), then tasks with declared
required facts, then everything else.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from evals.datasets.schema import (  # noqa: E402
    EvalDataset,
    EvalTask,
    GoldStatus,
    RequiredFact,
    Split,
)

DATASET_DIR = Path(__file__).resolve().parent
QUEUE_PATH = DATASET_DIR / "review_queue.jsonl"
ALL_TASKS = DATASET_DIR / "all_tasks.json"


def _priority(task: EvalTask) -> tuple[int, str]:
    if "bad_case" in task.tags:
        return (0, task.task_id)
    if task.required_facts:
        return (1, task.task_id)
    if task.forbidden_claims or task.forbidden_tools:
        return (2, task.task_id)
    return (3, task.task_id)


def export_queue(limit: int, out_path: Path = QUEUE_PATH) -> int:
    dataset = EvalDataset.load(ALL_TASKS)
    pending = [t for t in dataset.tasks if t.gold_status is not GoldStatus.HUMAN_VERIFIED]
    pending.sort(key=_priority)
    selected = pending[:limit]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for task in selected:
            handle.write(json.dumps({
                "task_id": task.task_id,
                "category": task.category.value,
                "query": task.query,
                "report_type": task.report_type,
                "expected_outcome": task.expected_outcome.value,
                "current_gold_status": task.gold_status.value,
                "data_source": task.data_source,
                "source_reference": task.source_reference,
                "existing_required_facts": [f.model_dump() for f in task.required_facts],
                "existing_forbidden_claims": task.forbidden_claims,
                # --- reviewer fills these in ---
                "decision": "",            # accept | reject | revise
                "annotator": "",
                "notes": "",
                "revised_expected_outcome": "",
                "verified_required_facts": [],
            }, ensure_ascii=False) + "\n")
    return len(selected)


def apply_queue(annotator: str, queue_path: Path = QUEUE_PATH) -> dict[str, Any]:
    if not queue_path.exists():
        raise SystemExit(f"queue not found: {queue_path}; run `export` first")

    decisions: dict[str, dict[str, Any]] = {}
    with queue_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("decision"):
                decisions[row["task_id"]] = row

    if not decisions:
        raise SystemExit("no rows carry a `decision`; nothing to apply "
                         "(this is the expected state until a human reviews them)")

    dataset = EvalDataset.load(ALL_TASKS)
    today = date.today().isoformat()
    accepted = rejected = revised = 0
    kept: list[EvalTask] = []

    for task in dataset.tasks:
        row = decisions.get(task.task_id)
        if row is None:
            kept.append(task)
            continue

        who = row.get("annotator") or annotator
        if not who:
            raise SystemExit(f"task {task.task_id} has a decision but no annotator; "
                             "pass --annotator or fill the field")
        decision = row["decision"].strip().lower()

        if decision == "reject":
            rejected += 1
            continue  # dropped from the dataset entirely
        if decision == "revise":
            if row.get("revised_expected_outcome"):
                task.expected_outcome = type(task.expected_outcome)(row["revised_expected_outcome"])
            revised += 1
        if facts := row.get("verified_required_facts"):
            task.required_facts = [RequiredFact(**f) for f in facts]

        task.annotator = who
        task.reviewed_at = today
        task.gold_status = GoldStatus.HUMAN_VERIFIED
        accepted += 1
        kept.append(task)

    dataset.tasks = kept
    dataset.save(ALL_TASKS)
    for split in Split:
        subset = EvalDataset(name=f"{dataset.name}_{split.value}",
                             description=dataset.description, created_at=dataset.created_at,
                             tasks=dataset.by_split(split))
        subset.save(DATASET_DIR / f"{split.value}.json")

    return {"accepted": accepted, "rejected": rejected, "revised": revised,
            "remaining_tasks": len(kept), "annotator": annotator, "reviewed_at": today}


def status() -> dict[str, Any]:
    dataset = EvalDataset.load(ALL_TASKS)
    stats = dataset.stats()
    verified = stats["human_verified"]
    stats["review_progress"] = f"{verified}/{stats['total']}"
    stats["claim_permitted"] = (
        "可以把该数据集上的指标称为 human-verified 结果" if verified == stats["total"] else
        f"**不可以**把该数据集上的指标称为已验证结果：{stats['total'] - verified} 条仍未经人工复核，"
        "报告中必须标注 draft/synthetic")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Human review queue for eval datasets")
    sub = parser.add_subparsers(dest="command", required=True)

    export_cmd = sub.add_parser("export", help="write the review queue file")
    export_cmd.add_argument("--limit", type=int, default=40)
    export_cmd.add_argument("--out", type=Path, default=QUEUE_PATH)

    apply_cmd = sub.add_parser("apply", help="apply reviewer decisions back into the datasets")
    apply_cmd.add_argument("--annotator", default="", help="fallback annotator name")
    apply_cmd.add_argument("--queue", type=Path, default=QUEUE_PATH)

    sub.add_parser("status", help="show how much of the dataset is human-verified")

    args = parser.parse_args()

    if args.command == "export":
        count = export_queue(args.limit, args.out)
        print(f"exported {count} task(s) needing review -> {args.out}")
        print("Fill in `decision` (accept/reject/revise), `annotator` and `notes` per line, "
              "then run: python -m evals.datasets.review_queue apply")
    elif args.command == "apply":
        print(json.dumps(apply_queue(args.annotator, args.queue), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
