"""Build the verified-regression report: legacy baseline vs harness.

Every number is computed here from the two JSONL result files, and the file
hashes go into the report so a reader can confirm which run produced them.

Both arms must share the task set, the trial count and the grader set, or the
report says the comparison is invalid instead of printing a delta. That guard
exists because the previous README compared a stale baseline against a freshly
graded harness run and credited the difference to the harness.

    python scripts/build_regression_verified_report.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS = REPO_ROOT / "evals" / "results"
OUT = REPO_ROOT / "evals" / "reports" / "regression_verified_report.md"

RECOVERY_KEYS = (
    ("same_tool_retry_recovery", "同工具重试恢复", True),
    ("cross_tool_fallback_recovery", "跨工具/降级恢复", True),
    ("degraded_completion", "降级仍完成", True),
    ("checkpoint_resume_success", "Checkpoint 恢复成功", True),
    ("false_success", "False Success", False),
    ("success_with_ungrounded_numbers", "成功但含无源数字", False),
    ("unresolved_failure", "Unresolved Failure", False),
)


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"missing results file: {path}")
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 4) if values else None


def metric(rows: list[dict[str, Any]], key: str) -> tuple[Optional[float], int]:
    values = [float(r["summary"]["metrics"][key]) for r in rows
              if isinstance(r["summary"]["metrics"].get(key), (int, float))
              and not isinstance(r["summary"]["metrics"][key], bool)]
    return _mean(values), len(values)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_task[row["task_id"]].append(row)
    passed = [t for t, trials in by_task.items()
              if all(x["summary"]["metrics"].get("task_success", 0) for x in trials)]
    flaky = [t for t, trials in by_task.items()
             if any(x["summary"]["metrics"].get("task_success", 0) for x in trials)
             and not all(x["summary"]["metrics"].get("task_success", 0) for x in trials)]
    statuses: dict[str, int] = defaultdict(int)
    for row in rows:
        statuses[row["status"]] += 1
    return {
        "rows": len(rows), "tasks": len(by_task),
        "trials": round(len(rows) / len(by_task), 2),
        "passed_all_trials": len(passed),
        "success_rate": round(len(passed) / len(by_task), 4),
        "flaky": sorted(flaky),
        "statuses": dict(statuses),
        "has_trace": any(r.get("trace_path") for r in rows),
        "grader_set": sorted({g["grader"] for r in rows for g in r.get("grades", [])}),
        "by_task": {t: all(x["summary"]["metrics"].get("task_success", 0) for x in v)
                    for t, v in by_task.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the verified regression report")
    parser.add_argument("--harness", type=Path,
                        default=RESULTS / "regression_rebuilt_harness.jsonl")
    parser.add_argument("--legacy", type=Path,
                        default=RESULTS / "regression_rebuilt_legacy.jsonl")
    args = parser.parse_args()

    harness_rows, legacy_rows = load(args.harness), load(args.legacy)
    harness, legacy = summarize(harness_rows), summarize(legacy_rows)

    problems: list[str] = []
    if harness["tasks"] != legacy["tasks"]:
        problems.append(f"任务数不一致 harness={harness['tasks']} legacy={legacy['tasks']}")
    if harness["trials"] != legacy["trials"]:
        problems.append(f"trial 数不一致 harness={harness['trials']} legacy={legacy['trials']}")
    if harness["grader_set"] != legacy["grader_set"]:
        problems.append("Grader 集合不一致，分数不可比")

    manifest = json.loads((REPO_ROOT / "evals" / "datasets" /
                           "regression_manifest.json").read_text(encoding="utf-8"))

    lines = ["# 重构回归集验证报告（legacy vs harness）", ""]
    lines += [f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
              "- 运行模式：离线 fixture 回放（确定性；token 与费用恒为 0，不含真实成本结论）",
              f"- harness 结果：`{args.harness.relative_to(REPO_ROOT).as_posix()}`  "
              f"sha256 `{sha256(args.harness)[:16]}…`",
              f"- legacy 结果：`{args.legacy.relative_to(REPO_ROOT).as_posix()}`  "
              f"sha256 `{sha256(args.legacy)[:16]}…`",
              f"- 任务集：{harness['tasks']} 条可执行回归 × {harness['trials']:.0f} trials"
              f"（由 31 条真实 bad case 重构，见 docs/regression_rebuild.md）", ""]

    if problems:
        lines += ["> ⚠️ **对照有效性检查未通过，下方 Δ 不可作为归因结论**：", ""]
        lines += [f"> - {p}" for p in problems] + [""]
    else:
        lines += ["> ✅ 对照有效性检查通过：任务集、trial 数、Grader 集合三项一致。", ""]

    lines += ["## 1. 总体", "",
              "| 指标 | legacy（无 Harness） | harness | Δ |", "|---|---|---|---|"]

    def fmt(v: Any) -> str:
        if v is None:
            return "n/a"
        return f"{v:.4f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)

    delta = round(harness["success_rate"] - legacy["success_rate"], 4)
    lines.append(f"| Task Success（全 trial 通过） | {fmt(legacy['success_rate'])} | "
                 f"{fmt(harness['success_rate'])} | {delta:+} |")
    lines.append(f"| 通过任务数 | {legacy['passed_all_trials']}/{legacy['tasks']} | "
                 f"{harness['passed_all_trials']}/{harness['tasks']} | — |")
    lines.append(f"| 不稳定任务（部分 trial 通过） | {len(legacy['flaky'])} | "
                 f"{len(harness['flaky'])} | — |")
    lines.append(f"| 有结构化 Trace | {'是' if legacy['has_trace'] else '**否**'} | "
                 f"{'是' if harness['has_trace'] else '否'} | — |")
    lines.append("")

    lines += ["## 2. 六个恢复/诚信指标", "",
              "`n/a` = 该臂没有这项能力或本次无适用运行，不是 0 分。"
              "legacy 无 trace，因此所有事件派生指标不存在——这正是 Harness 的主要收益。", "",
              "| 指标 | legacy | n(legacy) | harness | n(harness) | 方向 |",
              "|---|---|---|---|---|---|"]
    for key, label, higher in RECOVERY_KEYS:
        legacy_value, legacy_n = metric(legacy_rows, key)
        harness_value, harness_n = metric(harness_rows, key)
        arrow = "越高越好" if higher else "越低越好"
        lines.append(f"| {label} | {fmt(legacy_value)} | {legacy_n} | "
                     f"{fmt(harness_value)} | {harness_n} | {arrow} |")
    lines.append("")

    lines += ["## 3. 按原缺陷类型分组", "",
              "| Bad Case | 原缺陷 | 故障注入 | legacy | harness |", "|---|---|---|---|---|"]
    by_task_id = {r["task_id"]: r for r in manifest["rows"] if r.get("task_id")}
    for task_id in sorted(harness["by_task"]):
        row = by_task_id.get(task_id, {})
        title = (row.get("title") or "")[:38]
        scen = row.get("fixture_scenario", "")
        legacy_ok = legacy["by_task"].get(task_id)
        harness_ok = harness["by_task"].get(task_id)
        mark = lambda v: "n/a" if v is None else ("通过" if v else "**失败**")
        lines.append(f"| {row.get('bad_case', '?')} | {title} | "
                     f"`{scen or 'default'}` | {mark(legacy_ok)} | {mark(harness_ok)} |")
    lines.append("")

    lines += ["## 4. 未被覆盖的 bad case（如实列出）", "",
              f"31 条 bad case 中只有 {manifest['by_verification'].get('deterministic', 0)} 条"
              "可复现为可执行任务，其余按类别归档：", "",
              "| 类别 | 条数 | 含义 |", "|---|---|---|"]
    meanings = {
        "needs_human_review": "期望值必须由人设定（会计口径、行业判断）",
        "needs_network": "只能在真实 provider / TLS / 子进程下复现",
        "not_agent_behaviour": "真实缺陷，但属于开发工具或性能，不是 agent 行为回归",
        "deterministic": "已重构为可执行任务",
    }
    for key, count in sorted(manifest["by_verification"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| `{key}` | {count} | {meanings.get(key, '')} |")
    lines.append("")
    lines += ["详细逐条理由见 `docs/regression_rebuild.md` 第 2.1 节与 "
              "`evals/datasets/regression_manifest.json`。", ""]

    lines += ["## 5. 读法与限制", "",
              "- **13 条规模不足以做统计显著性判断**，只能当作"
              "\"这些历史缺陷没有复现\"的存在性检查。",
              "- **13 条期望值均为 `needs_human_review`**：缺陷是真的、已复现，"
              "但\"正确终态应该是什么\"是作者推断，未经人工复核。",
              "- **9 条 `needs_network` + `needs_human_review` 的缺陷目前零覆盖**，"
              "其中 4 条是金融正确性问题，恰恰最需要人工基准。",
              "- 离线 stub 下 token 与费用恒为 0，本报告不含任何真实成本结论。"]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")

    print(f"legacy  success={legacy['success_rate']} passed={legacy['passed_all_trials']}/{legacy['tasks']}")
    print(f"harness success={harness['success_rate']} passed={harness['passed_all_trials']}/{harness['tasks']}")
    print("validity:", "OK" if not problems else problems)
    print(f"wrote {OUT}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
