"""Turn raw eval result rows into a Markdown report.

Every number in the output is computed here from the JSONL rows written by
`evals/runners/run_eval.py`. There is no path by which a figure can be typed in
by hand, and each report states the results file, the config hashes and the
gold-status mix it was computed over.

    python -m evals.reports.build_report --results evals/results/dev_harness.jsonl
    python -m evals.reports.build_report --compare evals/results/dev_baseline_v4.jsonl \\
        --results evals/results/dev_harness.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

REPORTS_DIR = Path(__file__).resolve().parent


REPO_ROOT = Path(__file__).resolve().parents[2]


def _repo_relative(path: Path | str) -> str:
    """Render a path relative to the repo root, POSIX-style.

    Reports are committed and read by other people; an absolute local path in
    a shipped artifact leaks the author's machine layout and is meaningless to
    every other reader.
    """
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return Path(path).name


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    import math

    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 4)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _collect_metric(rows: Iterable[dict[str, Any]], key: str) -> list[float]:
    """Pull one grader-contributed metric out of every row that reported it."""
    out: list[float] = []
    for row in rows:
        value = row.get("summary", {}).get("metrics", {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(float(value))
    return out


def aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the full metric set the README and the regression gate consume."""
    if not rows:
        return {"rows": 0}

    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_task[row["task_id"]].append(row)

    # Task-level success uses *all* trials: a task is stable-pass only when
    # every trial passed. Reporting only the mean would hide a 2-of-3 flake.
    task_success_all, task_success_any, flaky = [], [], []
    for task_id, trials in by_task.items():
        passed = [bool(t.get("summary", {}).get("metrics", {}).get("task_success", 0)) for t in trials]
        task_success_all.append(all(passed))
        task_success_any.append(any(passed))
        if any(passed) and not all(passed):
            flaky.append(task_id)

    scores = [r.get("summary", {}).get("overall_score", 0.0) for r in rows]
    durations = [float(r.get("duration_s", 0.0) or 0.0) for r in rows]
    statuses: dict[str, int] = defaultdict(int)
    stop_reasons: dict[str, int] = defaultdict(int)
    for row in rows:
        statuses[row.get("status", "unknown")] += 1
        stop_reasons[row.get("stop_reason", "unknown") or "unknown"] += 1

    # Per-task score stability across trials.
    stdevs = [statistics.pstdev([t.get("summary", {}).get("overall_score", 0.0) for t in trials])
              for trials in by_task.values() if len(trials) > 1]

    gold_mix: dict[str, int] = defaultdict(int)
    for row in rows:
        gold_mix[row.get("gold_status", "unknown")] += 1

    def metric(key: str) -> Optional[float]:
        values = _collect_metric(rows, key)
        return _mean(values) if values else None

    # Per-task mean score, consumed by the regression gate's bad-case check:
    # aggregate movement can hide a specific previously-fixed defect coming back.
    per_task_score = {
        task_id: round(sum(t.get("summary", {}).get("overall_score", 0.0) for t in trials)
                       / len(trials), 4)
        for task_id, trials in by_task.items()
    }

    return {
        "rows": len(rows),
        "tasks": len(by_task),
        "per_task_score": per_task_score,
        "trials_per_task": round(len(rows) / len(by_task), 2),
        "arms": sorted({r.get("arm", "") for r in rows}),
        "config_hashes": sorted({r.get("config_hash", "") for r in rows}),
        "live": any(r.get("live") for r in rows),

        "task_success_rate_all_trials": _mean([1.0 if s else 0.0 for s in task_success_all]),
        "task_success_rate_any_trial": _mean([1.0 if s else 0.0 for s in task_success_any]),
        "flaky_tasks": sorted(flaky),
        "overall_score_mean": _mean(scores),
        "overall_score_worst": round(min(scores), 4) if scores else 0.0,
        "score_stdev_within_task_mean": _mean(stdevs),

        "necessary_tool_recall": metric("necessary_tool_recall"),
        "unnecessary_tool_call_rate": metric("unnecessary_tool_call_rate"),
        "tool_outcome_success": metric("tool_outcome_success"),
        "alternative_valid_path_rate": metric("alternative_valid_path_hit"),
        "forbidden_tool_calls_total": sum(_collect_metric(rows, "forbidden_tool_calls")),
        "tool_selection_precision": metric("tool_precision"),
        "tool_selection_recall": metric("tool_recall"),
        "tool_selection_f1": metric("tool_f1"),
        "tool_argument_accuracy": metric("tool_argument_accuracy"),
        "invalid_tool_call_rate": metric("invalid_tool_call_rate"),
        "redundant_tool_call_rate": metric("redundant_tool_call_rate"),
        # Deprecated but still aggregated so old result files remain readable.
        "recovery_success_rate": metric("recovery_success_rate"),
        # The six metrics that replace it (evals/graders/recovery.py). Each one
        # names a distinct recovery mechanism, so a drop points at one thing.
        "same_tool_retry_recovery": metric("same_tool_retry_recovery"),
        "cross_tool_fallback_recovery": metric("cross_tool_fallback_recovery"),
        "degraded_completion": metric("degraded_completion"),
        "checkpoint_resume_success": metric("checkpoint_resume_success"),
        "false_success": metric("false_success"),
        "success_with_ungrounded_numbers": metric("success_with_ungrounded_numbers"),
        "unresolved_failure": metric("unresolved_failure"),

        "citation_validity": metric("citation_validity"),
        "citation_coverage": metric("citation_coverage"),
        "number_grounding_rate": metric("number_grounding_rate"),
        "unsupported_number_rate": metric("unsupported_number_rate"),

        "budget_exceeded_rate": metric("budget_exceeded"),
        "estimated_cost_usd_mean": metric("estimated_cost_usd"),
        "input_tokens_mean": metric("input_tokens"),
        "output_tokens_mean": metric("output_tokens"),
        "tool_calls_mean": metric("tool_calls"),

        "injection_tool_hijack_total": sum(_collect_metric(rows, "injection_tool_hijack")),
        "injection_leak_total": sum(_collect_metric(rows, "injection_leak")),

        "latency": {"avg_s": _mean(durations), "p50_s": _percentile(durations, 50),
                    "p95_s": _percentile(durations, 95),
                    "max_s": round(max(durations), 4) if durations else 0.0},
        "status_counts": dict(statuses),
        "stop_reasons": dict(stop_reasons),
        "gold_status_mix": dict(gold_mix),
        "errors": [{"task_id": r["task_id"], "error": r["error"][:200]}
                   for r in rows if r.get("error")][:20],
    }


def aggregate_by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key, "unknown"))].append(row)
    return {name: aggregate_rows(group) for name, group in sorted(grouped.items())}


# --------------------------------------------------------------------------- #
def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "—"
    return str(value)


_HEADLINE_METRICS: tuple[tuple[str, str], ...] = (
    ("task_success_rate_all_trials", "Task Success Rate（全部 trial 均通过）"),
    ("task_success_rate_any_trial", "Task Success Rate（至少一次通过）"),
    ("overall_score_mean", "综合评分均值"),
    ("overall_score_worst", "综合评分最差值"),
    ("score_stdev_within_task_mean", "同任务多 trial 评分标准差均值"),
    ("tool_selection_precision", "Tool Selection Precision"),
    ("necessary_tool_recall", "Necessary Tool Recall"),
    ("unnecessary_tool_call_rate", "Unnecessary Tool Call Rate"),
    ("tool_outcome_success", "Tool Outcome Success"),
    ("alternative_valid_path_rate", "Alternative Valid Path Rate"),
    ("tool_selection_recall", "Tool Selection Recall"),
    ("tool_selection_f1", "Tool Selection F1"),
    ("tool_argument_accuracy", "Tool Argument Accuracy"),
    ("invalid_tool_call_rate", "Invalid Tool Call Rate"),
    ("redundant_tool_call_rate", "Redundant Tool Call Rate"),
    # DEPRECATED. Kept only so historical result files stay readable. Its
    # definition ("some step failed and the run later succeeded") cannot
    # distinguish a same-tool retry from a cross-tool fallback from a
    # degraded finish, so a change in it is not actionable. Superseded by
    # the six graders in `evals/graders/recovery.py`, reported below it.
    ("recovery_success_rate", "~~Recovery Success Rate~~（已废弃，见下方六项恢复指标）"),
    ("same_tool_retry_recovery", "同工具重试恢复率"),
    ("cross_tool_fallback_recovery", "跨工具/降级恢复率"),
    ("degraded_completion", "降级仍完成率"),
    ("checkpoint_resume_success", "Checkpoint 恢复成功率"),
    ("false_success", "False Success Rate"),
    ("success_with_ungrounded_numbers", "成功但含无源数字率"),
    ("unresolved_failure", "Unresolved Failure Rate"),
    ("citation_validity", "Citation Validity"),
    ("citation_coverage", "Citation Coverage"),
    ("number_grounding_rate", "Number Grounding Rate"),
    ("unsupported_number_rate", "Unsupported Number Rate"),
    ("budget_exceeded_rate", "Budget Exceeded Rate"),
    ("estimated_cost_usd_mean", "单次运行估算费用(USD)"),
    ("input_tokens_mean", "平均输入 Token"),
    ("output_tokens_mean", "平均输出 Token"),
    ("tool_calls_mean", "平均工具调用数"),
)


def render_report(rows: list[dict[str, Any]], results_path: Path,
                  baseline_rows: Optional[list[dict[str, Any]]] = None,
                  baseline_path: Optional[Path] = None) -> str:
    agg = aggregate_rows(rows)
    lines: list[str] = []
    add = lines.append

    add("# Agent 评测报告")
    add("")
    add(f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    add(f"- 原始结果文件：`{_repo_relative(results_path)}`（{agg['rows']} 行，{agg['tasks']} 个任务，"
        f"每任务 {agg['trials_per_task']} 次 trial）")
    if results_path.exists():
        digest = hashlib.sha256(results_path.read_bytes()).hexdigest()
        add(f"- 原始结果 SHA-256：`{digest}`")
    add(f"- 实验臂：{_fmt(agg['arms'])}")
    add(f"- 配置哈希：{_fmt(agg['config_hashes'])}")
    add(f"- 运行模式：{'**live**（真实模型/网络）' if agg['live'] else '离线 fixture 回放（确定性，无网络、无费用）'}")
    add("")

    verified = agg["gold_status_mix"].get("human_verified", 0)
    add("> **数据性质声明**：本报告的任务集 gold_status 分布为 "
        f"`{json.dumps(agg['gold_status_mix'], ensure_ascii=False)}`，其中 "
        f"human_verified = **{verified}**。"
        + ("" if verified else
           "**没有任何任务经过人工复核**，因此以下全部指标只能作为 draft/synthetic 结果，"
           "不得作为已验证的效果结论对外引用。"))
    add("")

    add("## 总体指标")
    add("")
    add("| 指标 | 数值 |")
    add("|---|---|")
    for key, label in _HEADLINE_METRICS:
        add(f"| {label} | {_fmt(agg.get(key))} |")
    latency = agg["latency"]
    add(f"| 时延 avg / p50 / p95 / max (s) | {_fmt(latency['avg_s'])} / {_fmt(latency['p50_s'])}"
        f" / {_fmt(latency['p95_s'])} / {_fmt(latency['max_s'])} |")
    add("")

    add("### 安全")
    add("")
    add(f"- Prompt Injection 导致的工具劫持次数：**{int(agg['injection_tool_hijack_total'])}**")
    add(f"- 注入内容泄漏进报告的次数：**{int(agg['injection_leak_total'])}**")
    add("")

    add("### 运行结束状态分布")
    add("")
    add("| 状态 | 次数 |")
    add("|---|---|")
    for status, count in sorted(agg["status_counts"].items()):
        add(f"| {status} | {count} |")
    add("")
    add("停止原因分布（回答“为什么在这里停止”）：")
    add("")
    add("| stop_reason | 次数 |")
    add("|---|---|")
    for reason, count in sorted(agg["stop_reasons"].items()):
        add(f"| {reason} | {count} |")
    add("")

    if agg["flaky_tasks"]:
        add("### 不稳定任务（部分 trial 通过、部分失败）")
        add("")
        for task_id in agg["flaky_tasks"]:
            add(f"- `{task_id}`")
        add("")

    add("## 分类别")
    add("")
    add("| 类别 | 任务数 | 全通过率 | 综合评分 | 数字溯源率 | 无源数字率 | p95 时延(s) |")
    add("|---|---|---|---|---|---|---|")
    for name, sub in aggregate_by(rows, "category").items():
        add(f"| {name} | {sub['tasks']} | {_fmt(sub['task_success_rate_all_trials'])} | "
            f"{_fmt(sub['overall_score_mean'])} | {_fmt(sub['number_grounding_rate'])} | "
            f"{_fmt(sub['unsupported_number_rate'])} | {_fmt(sub['latency']['p95_s'])} |")
    add("")

    add("## 分 split")
    add("")
    add("| split | 任务数 | 全通过率 | 综合评分 |")
    add("|---|---|---|---|")
    for name, sub in aggregate_by(rows, "split").items():
        add(f"| {name} | {sub['tasks']} | {_fmt(sub['task_success_rate_all_trials'])} | "
            f"{_fmt(sub['overall_score_mean'])} |")
    add("")

    if baseline_rows:
        add("## 对照实验（Baseline vs 当前）")
        add("")
        base = aggregate_rows(baseline_rows)
        add(f"Baseline 原始结果：`{_repo_relative(baseline_path)}`（臂：{_fmt(base['arms'])}）")
        add("")
        add("| 指标 | Baseline | 当前 | Δ |")
        add("|---|---|---|---|")
        for key, label in _HEADLINE_METRICS:
            b, c = base.get(key), agg.get(key)
            delta = (f"{c - b:+.4f}" if isinstance(b, (int, float)) and isinstance(c, (int, float))
                     else "n/a")
            add(f"| {label} | {_fmt(b)} | {_fmt(c)} | {delta} |")
        base_p95 = base["latency"]["p95_s"]
        current_p95 = latency["p95_s"]
        add(f"| p95 时延(s) | {_fmt(base_p95)} | {_fmt(current_p95)} | {current_p95 - base_p95:+.4f} |")
        add("")

    if agg["errors"]:
        add("## 执行错误（如实列出，不隐藏）")
        add("")
        for item in agg["errors"]:
            add(f"- `{item['task_id']}`: {item['error']}")
        add("")

    add("## 方法与限制")
    add("")
    add("- 所有数字由 `evals/reports/build_report.py` 从原始 JSONL 行计算，无人工填写路径。")
    add("- Citation Validity 的定义是“报告中的 `[sN]` 标记都能解析到真实 source_id”，"
        "**不校验被引页面是否真的支持该句结论**——这是工程引用检查，不是事实核查。")
    add("- Number Grounding 把数字分为 sourced / akshare / assumption / calculated / unsourced 五类，"
        "假设与测算如实归类，不洗成“有来源”。")
    add("- Tool Selection 指标只在任务显式声明了期望/禁用工具时计算，其余任务不计入分母。")
    add("- 离线模式下模型被确定性 stub 替换，因此 token/费用为 0，"
        "**不能**据此宣称真实成本；真实成本必须用 `--live` 跑出来。")
    return "\n".join(lines)


def write_report(results_path: Path, *, compare: Optional[Path] = None,
                 out: Optional[Path] = None,
                 json_out: Optional[Path] = None) -> tuple[Path, Path]:
    """Build both report artifacts from one immutable read of the JSONL."""
    results_path = Path(results_path)
    if not results_path.exists():
        raise FileNotFoundError(f"results file not found: {results_path}")
    rows = load_rows(results_path)
    if not rows:
        raise ValueError(f"no usable rows in {results_path}")
    baseline_rows = load_rows(compare) if compare and Path(compare).exists() else None
    markdown = render_report(rows, results_path, baseline_rows, compare)
    out_path = Path(out) if out else (REPORTS_DIR / f"{results_path.stem}_report.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    json_path = Path(json_out) if json_out else out_path.with_suffix(".json")
    metrics = aggregate_rows(rows)
    metrics["results_sha256"] = hashlib.sha256(results_path.read_bytes()).hexdigest()
    json_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path, json_path


# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a Markdown eval report from raw results")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--compare", type=Path, default=None,
                        help="baseline results file to diff against")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.results.exists():
        raise SystemExit(f"results file not found: {args.results}")

    try:
        out_path, json_path = write_report(
            args.results, compare=args.compare, out=args.out, json_out=args.json_out)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    print(f"wrote {out_path}")
    print(f"wrote {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
