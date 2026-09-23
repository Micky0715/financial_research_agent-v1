"""Build the strict single-variable ablation matrix (Phase 6).

Each arm differs from `harness_full` in exactly one dimension, so every delta
is attributable to one capability. That constraint is the whole point: the
previous comparison disabled budget *and* checkpointing at once and credited
the difference to "the Harness", which no reviewer should accept.

Two guards run before any number is printed, and the report refuses to claim a
comparison is valid unless both pass:

  1. **Same grader set.** If two arms were graded by different grader
     populations, their scores are not comparable. The earlier README table
     compared a run graded with the old degenerate tool grader against one
     graded with the fixed set.
  2. **Same tasks and trial count.** Different denominators, different number.

Capability arms that *cannot* report a metric (legacy has no trace, so no
event-derived metric exists) are shown as `n/a`, never as 0 - a missing
capability must not look like a bad score.

    python scripts/build_ablation_matrix.py
    python scripts/build_ablation_matrix.py --split reg
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS = REPO_ROOT / "evals" / "results"
OUT_DIR = REPO_ROOT / "evals" / "reports"

REFERENCE_ARM = "harness_full"

#: (metric key, label, higher_is_better). `None` for direction means "report
#: the number, do not judge it" - cost metrics are trade-offs, not regressions.
METRICS: tuple[tuple[str, str, Optional[bool]], ...] = (
    ("task_success_rate_all_trials", "Task Success", True),
    ("false_success_rate", "False Success", False),
    ("unresolved_failure_rate", "Unresolved Failure", False),
    ("same_tool_retry_recovery", "同工具重试恢复", True),
    ("cross_tool_fallback_recovery", "跨工具/降级恢复", True),
    ("degraded_completion", "降级仍完成", True),
    ("checkpoint_resume_success", "Checkpoint 恢复成功", True),
    ("citation_coverage", "Citation Coverage", True),
    ("number_grounding_rate", "Number Grounding", True),
    ("unsupported_number_rate", "Unsupported Number", False),
    ("success_with_ungrounded_numbers", "成功但含无源数字", False),
    ("tool_calls_mean", "平均工具调用数", None),
    ("redundant_tool_call_rate", "冗余调用率", False),
    ("p95_latency_s", "P95 时延(s)", None),
)

#: Metrics that only exist when a trace exists. A legacy arm reporting these
#: as 0 would be a lie about its capability.
TRACE_DERIVED = {
    "false_success_rate", "unresolved_failure_rate", "same_tool_retry_recovery",
    "cross_tool_fallback_recovery", "degraded_completion",
    "checkpoint_resume_success", "redundant_tool_call_rate",
    # `tool_calls` is counted by the Tool Executor, so a legacy run has no
    # counter at all. It surfaced as `0.0` on the reg split (both candidate
    # keys absent -> `_metric_mean` returned None -> `or` fell through to a
    # second None -> printed 0), which reads as "legacy made zero tool calls".
    # It made real calls; we simply cannot count them without the executor.
    "tool_calls_mean",
}


def load_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 4) if values else None


def _metric_mean(rows: list[dict[str, Any]], key: str) -> Optional[float]:
    values = [r["summary"]["metrics"][key] for r in rows
              if isinstance(r.get("summary", {}).get("metrics", {}).get(key), (int, float))
              and not isinstance(r["summary"]["metrics"][key], bool)]
    return _mean([float(v) for v in values])


def summarize_arm(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    by_task: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], []).append(row)

    passed = sum(1 for trials in by_task.values()
                 if all(t["summary"]["metrics"].get("task_success", 0) for t in trials))
    durations = sorted(float(r.get("duration_s", 0.0) or 0.0) for r in rows)
    import math

    p95 = durations[max(0, math.ceil(0.95 * len(durations)) - 1)] if durations else 0.0

    graders_seen = sorted({g["grader"] for r in rows for g in r.get("grades", [])})
    has_trace = any(r.get("trace_path") for r in rows)

    out: dict[str, Any] = {
        "arm": rows[0].get("arm", "?"),
        "rows": len(rows),
        "tasks": len(by_task),
        "trials": round(len(rows) / len(by_task), 2),
        "config_hash": sorted({r.get("config_hash", "") for r in rows}),
        "grader_set": graders_seen,
        "has_trace": has_trace,
        "task_success_rate_all_trials": round(passed / len(by_task), 4),
        "p95_latency_s": round(p95, 4),
    }
    for key in ("false_success", "unresolved_failure", "same_tool_retry_recovery",
                "cross_tool_fallback_recovery", "degraded_completion",
                "checkpoint_resume_success", "success_with_ungrounded_numbers",
                "citation_coverage", "number_grounding_rate", "unsupported_number_rate",
                "redundant_tool_call_rate"):
        out[key] = _metric_mean(rows, key)
    out["false_success_rate"] = out.pop("false_success")
    out["unresolved_failure_rate"] = out.pop("unresolved_failure")
    # Not `a or b`: a genuine mean of 0.0 is falsy and would be replaced by the
    # fallback, and two absent keys would collapse to 0 instead of None.
    # Not `a or b`: a genuine mean of 0.0 is falsy and would be replaced by the
    # fallback, and two absent keys would collapse to 0 instead of None.
    calls = _metric_mean(rows, "tool_calls")
    out["tool_calls_mean"] = calls if calls is not None else _metric_mean(rows, "total_tool_calls")
    return out


def validate(arms: dict[str, dict[str, Any]]) -> list[str]:
    """Refuse to present an invalid comparison. Returns blocking problems."""
    problems: list[str] = []
    present = {k: v for k, v in arms.items() if v}
    if len(present) < 2:
        return ["少于 2 个可用臂，无法比较"]

    task_counts = {k: v["tasks"] for k, v in present.items()}
    if len(set(task_counts.values())) > 1:
        problems.append(f"各臂任务数不一致：{task_counts}")

    trial_counts = {k: v["trials"] for k, v in present.items()}
    if len(set(trial_counts.values())) > 1:
        problems.append(f"各臂 trial 数不一致：{trial_counts}")

    # The grader set must match across arms, except that a trace-less arm
    # legitimately lacks nothing - grade_all always runs every grader, it just
    # marks them not-applicable - so the *set* should still be identical.
    sets = {k: tuple(v["grader_set"]) for k, v in present.items()}
    distinct = set(sets.values())
    if len(distinct) > 1:
        problems.append(
            "各臂 Grader 集合不一致，分数不可比（这正是旧 README 对照失效的原因）："
            + json.dumps({k: len(v) for k, v in sets.items()}, ensure_ascii=False))
    return problems


def render(arms: dict[str, dict[str, Any]], problems: list[str], label: str) -> str:
    from datetime import datetime, timezone

    order = [a for a in ["legacy_no_harness", REFERENCE_ARM, "abl_no_retry",
                         "abl_no_checkpoint", "abl_no_budget", "abl_no_context_mgmt",
                         "no_fallback"] if arms.get(a)]
    ref = arms.get(REFERENCE_ARM, {})

    lines = [f"# 单变量消融矩阵（{label}）", "",
             f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
             "- 运行模式：离线 fixture 回放，全部臂共用同一份任务、同一 stub、同一 Grader 集合",
             f"- 参照臂：`{REFERENCE_ARM}`（完整 Harness）",
             "- 每个臂与参照臂**只差一个变量**，因此每个 Δ 可归因到单一能力。", ""]

    if problems:
        lines += ["> ⚠️ **对照有效性检查未通过，以下数字不可作为归因结论**：", ""]
        lines += [f"> - {p}" for p in problems] + [""]
    else:
        lines += ["> ✅ 对照有效性检查通过：任务数、trial 数、Grader 集合三项全部一致。", ""]

    lines += ["## 各臂配置与可观测性", "",
              "| 臂 | 差异 | 任务 | Trials | 有 Trace | config_hash |",
              "|---|---|---|---|---|---|"]
    diffs = {
        "legacy_no_harness": "**完全不绑定 Harness**",
        REFERENCE_ARM: "（参照：全部能力开启）",
        "abl_no_retry": "只关重试",
        "abl_no_checkpoint": "只关 checkpoint",
        "abl_no_budget": "只关预算门禁",
        "abl_no_context_mgmt": "只关上下文压缩",
        "no_fallback": "只关权威站点 fallback 轮",
    }
    for arm in order:
        row = arms[arm]
        lines.append(f"| `{arm}` | {diffs.get(arm, '—')} | {row['tasks']} | {row['trials']} | "
                     f"{'是' if row['has_trace'] else '**否**'} | "
                     f"`{(row['config_hash'] or [''])[0][:12]}` |")
    lines.append("")

    lines += ["## 指标矩阵", "",
              "`n/a` 表示该臂**没有这项能力**（例如 legacy 无 trace，因此所有事件派生指标不存在），"
              "不是 0 分。", ""]
    header = "| 指标 | " + " | ".join(f"`{a}`" for a in order) + " |"
    lines += [header, "|" + "---|" * (len(order) + 1)]

    def fmt(value: Any) -> str:
        if value is None:
            return "n/a"
        return f"{value:.4f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)

    for key, label_cn, _direction in METRICS:
        cells = []
        for arm in order:
            row = arms[arm]
            if key in TRACE_DERIVED and not row["has_trace"]:
                cells.append("n/a")
            else:
                cells.append(fmt(row.get(key)))
        lines.append(f"| {label_cn} | " + " | ".join(cells) + " |")
    lines.append("")

    lines += ["## 相对参照臂的 Δ（只对有方向的指标判优劣）", "",
              "| 臂 | 指标 | 参照 | 该臂 | Δ | 解读 |", "|---|---|---|---|---|---|"]
    for arm in order:
        if arm == REFERENCE_ARM:
            continue
        row = arms[arm]
        for key, label_cn, direction in METRICS:
            if direction is None:
                continue
            if key in TRACE_DERIVED and not row["has_trace"]:
                continue
            ref_value, arm_value = ref.get(key), row.get(key)
            if not isinstance(ref_value, (int, float)) or not isinstance(arm_value, (int, float)):
                continue
            delta = round(arm_value - ref_value, 4)
            if abs(delta) < 1e-9:
                continue
            better = (delta > 0) if direction else (delta < 0)
            verdict = "关掉后**更好**（该能力未证明收益）" if better else "关掉后**变差**（该能力有收益）"
            lines.append(f"| `{arm}` | {label_cn} | {fmt(ref_value)} | {fmt(arm_value)} | "
                         f"{delta:+} | {verdict} |")
    lines.append("")

    lines += ["## 读法与限制", "",
              "- **1 trial**。离线 fixture 下同任务多 trial 的评分标准差实测约 0.0007（近确定性），"
              "因此消融矩阵用单 trial 换取可接受的运行时长；dev/test 主结果仍用 3 trials。",
              "- **成本类指标（平均工具调用数、P95 时延）不判优劣**，它们是权衡而非回归："
              "关掉预算门禁通常会让调用数上升而成功率不变，这正是需要显示的取舍。",
              "- **离线 stub 下 token 与费用恒为 0**，本矩阵不包含任何真实成本结论。",
              "- legacy 臂的 `n/a` 不是缺失数据，是**缺失能力**：它没有 trace，"
              "所以无法回答恢复、冗余、虚假成功这些问题——这本身就是 Harness 的主要收益。"]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the single-variable ablation matrix")
    parser.add_argument("--split", default="dev", choices=["dev", "reg"])
    args = parser.parse_args()

    prefix = f"abl_{args.split}_"
    arms: dict[str, dict[str, Any]] = {}
    for path in sorted(RESULTS.glob(f"{prefix}*.jsonl")):
        arm = path.stem[len(prefix):]
        arms[arm] = summarize_arm(load_rows(path))

    if not arms:
        raise SystemExit(f"no ablation results found matching {RESULTS / (prefix + '*.jsonl')}")

    problems = validate(arms)
    label = "dev 集" if args.split == "dev" else "重构回归集"
    markdown = render(arms, problems, label)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = OUT_DIR / f"ablation_matrix_{args.split}.md"
    md_path.write_text(markdown, encoding="utf-8")
    (OUT_DIR / f"ablation_matrix_{args.split}.json").write_text(
        json.dumps({"validity_problems": problems, "arms": arms}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    print(f"arms: {sorted(arms)}")
    print("validity:", "OK" if not problems else problems)
    for arm, row in arms.items():
        print(f"  {arm:22s} tasks={row['tasks']:>3} succ={row['task_success_rate_all_trials']} "
              f"trace={'y' if row['has_trace'] else 'n'} "
              f"false_succ={row.get('false_success_rate')}")
    print(f"wrote {md_path}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
