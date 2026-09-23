"""Build the live canary report (Phase 5).

Kept strictly separate from every fixture report. Averaging a live success rate
together with an offline one produces a number that describes neither, so this
script reads only `live_canary.jsonl` and refuses to merge anything else.

What it can say that no offline report can: real token counts, real estimated
cost, real end-to-end latency, and which failure classes actually occur against
a live provider and the live web.

    python -m evals.reports.build_live_canary_report
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS = REPO_ROOT / "evals" / "results" / "live_canary.jsonl"
OUT = REPO_ROOT / "evals" / "reports" / "live_canary_report.md"

#: Offline dev-split reference, for a *side-by-side* that is explicitly not a
#: delta table. Loaded from the report JSON so it cannot drift.
OFFLINE_REFERENCE = REPO_ROOT / "evals" / "reports" / "dev_harness_report.json"


def _replay_recorded_classes(
    rows: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int]]:
    """Re-classify the recorded error messages under the *current* rules.

    Returns `(recorded, current)` - **both derived from the same trace events**,
    one entry per failed tool call. This matters: `live_metrics.by_error` counts
    one entry per *attempt*, so mixing the two sources into one table compares
    different denominators (the first draft of this table showed
    TRANSIENT_NETWORK as 10 vs 5 for exactly that reason).

    Returns two empty dicts when traces are unavailable, so the report prints
    `n/a` instead of implying the rules agree.
    """
    try:
        from scripts.replay_canary_error_classes import collect_failures, replay
    except Exception:  # pragma: no cover - the report must still build
        return {}, {}

    failures: list[dict[str, Any]] = []
    for row in rows:
        trace = row.get("trace_path")
        if not trace or not Path(trace).exists():
            continue
        for line in Path(trace).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event_type") != "tool_call_failed":
                continue
            failures.append({
                "task_id": row.get("task_id", ""),
                "tool": event.get("name", ""),
                "recorded_class": event.get("error_class"),
                "message": event.get("error_message") or "",
                "trace": "",
            })
    if not failures:
        return {}, {}
    _ = collect_failures  # imported for symmetry with the standalone script
    report = replay(failures)
    return report["recorded_distribution"], report["current_distribution"]


def load(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SystemExit(f"missing {path}; run `python -m evals.runners.live_canary --yes` first")
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 2)


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 4) if values else None


def _metric(rows: list[dict[str, Any]], key: str) -> tuple[Optional[float], int]:
    values = [float(r["summary"]["metrics"][key]) for r in rows
              if isinstance(r["summary"]["metrics"].get(key), (int, float))
              and not isinstance(r["summary"]["metrics"][key], bool)]
    return _mean(values), len(values)


def fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the live canary report")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    args = parser.parse_args()

    rows = load(args.results)
    meta_path = args.results.with_suffix(".meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    statuses = Counter(r["status"] for r in rows)
    stop_reasons = Counter(r.get("stop_reason") or "unknown" for r in rows)
    durations = [float(r.get("duration_s", 0.0) or 0.0) for r in rows]

    tok_in = sum(int(r["live_metrics"]["input_tokens"] or 0) for r in rows)
    tok_out = sum(int(r["live_metrics"]["output_tokens"] or 0) for r in rows)
    cost = sum(float(r["live_metrics"]["estimated_cost_usd"] or 0.0) for r in rows)
    tool_calls = sum(int(r["live_metrics"]["tool_calls"] or 0) for r in rows)
    tool_failures = sum(int(r["live_metrics"]["tool_failures"] or 0) for r in rows)
    model_calls = sum(int(r["live_metrics"]["model_calls"] or 0) for r in rows)

    errors: Counter = Counter()
    for row in rows:
        for cls, count in (row["live_metrics"].get("by_error") or {}).items():
            errors[cls] += count

    succeeded = [r for r in rows if r["status"] in ("succeeded", "degraded")]
    passed_outcome = [r for r in rows
                      if r["summary"]["metrics"].get("task_success", 0)]

    lines = ["# 真实 Provider Canary 报告（Phase 5）", ""]
    lines += ["> **这是本项目唯一一份真实链路结果。** 此前所有数字都来自离线 fixture 回放，",
              "> 离线模式下 token 与费用结构性为 0，因此关于真实成本与时延的任何说法此前都没有依据。",
              "> 本报告与任何 fixture 报告**分开呈现**，不做合并平均。", ""]

    model = rows[0].get("model", "?") if rows else "?"
    config_hashes = sorted({r.get("config_hash", "") for r in rows})
    lines += [f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}",
              f"- 模型：`{model}`",
              f"- 任务数：{len(rows)}（每条 1 trial，人工分层挑选的真实主体）",
              f"- 原始结果：`{args.results.relative_to(REPO_ROOT).as_posix()}`",
              f"- SHA-256：`{hashlib.sha256(args.results.read_bytes()).hexdigest()}`",
              f"- 配置哈希：{', '.join(f'`{h[:12]}`' for h in config_hashes if h)}"]
    if meta:
        lines += [f"- 本次墙钟耗时：{meta.get('wall_time_s')}s",
                  f"- 硬费用上限：${meta.get('preflight', {}).get('max_cost_usd', 'n/a')}"
                  if meta.get("preflight", {}).get("max_cost_usd") else
                  "- 硬费用上限：见 live_canary.meta.json",
                  f"- 中止原因：{meta.get('aborted_reason') or '无（跑完全部计划任务）'}"]
    lines.append("")

    lines += ["## 1. 真实成本与时延（本项目首次实测）", "",
              "| 指标 | 实测值 |", "|---|---|",
              f"| 总输入 Token | {tok_in:,} |",
              f"| 总输出 Token | {tok_out:,} |",
              f"| 平均输入 Token / 运行 | {tok_in // max(1, len(rows)):,} |",
              f"| 平均输出 Token / 运行 | {tok_out // max(1, len(rows)):,} |",
              f"| **估算总费用** | **${cost:.4f}** |",
              f"| **估算单次费用** | **${cost / max(1, len(rows)):.4f}** |",
              f"| 模型调用总数 | {model_calls} |",
              f"| 工具调用总数 | {tool_calls}（平均 {tool_calls / max(1, len(rows)):.1f} 次/运行） |",
              f"| 工具失败总数 | {tool_failures} |",
              f"| 时延 平均 | {_mean(durations)}s |",
              f"| 时延 P50 | {percentile(durations, 50)}s |",
              f"| 时延 P95 | {percentile(durations, 95)}s |",
              f"| 时延 最大 | {round(max(durations), 2) if durations else 0}s |", ""]
    lines += ["费用口径：由 trace 中每次模型调用的 usage 累加，按 `src/runtime/budget.py::PRICE_TABLE` "
              "的公开列表价换算。**未在价表中的模型贡献 0**，并会在 trace 的 "
              "`unpriced_models` 中列出——宁可缺失也不编造。", ""]

    lines += ["## 2. 质量指标", "",
              "| 指标 | 实测 | n |", "|---|---|---|"]
    for key, label in (("task_success", "Task Success（符合预期终态）"),
                       ("citation_validity", "Citation Validity"),
                       ("citation_coverage", "Citation Coverage"),
                       ("number_grounding_rate", "Number Grounding"),
                       ("unsupported_number_rate", "Unsupported Number"),
                       ("false_success", "False Success"),
                       ("success_with_ungrounded_numbers", "成功但含无源数字"),
                       ("unresolved_failure", "Unresolved Failure"),
                       ("same_tool_retry_recovery", "同工具重试恢复"),
                       ("cross_tool_fallback_recovery", "跨工具/降级恢复"),
                       ("tool_argument_accuracy", "Tool Argument Accuracy"),
                       ("invalid_tool_call_rate", "Invalid Tool Call Rate")):
        value, n = _metric(rows, key)
        lines.append(f"| {label} | {fmt(value)} | {n} |")
    lines.append("")

    lines += ["## 3. 终态与停止原因分布", "",
              "| 终态 | 次数 |", "|---|---|"]
    for status, count in statuses.most_common():
        lines.append(f"| {status} | {count} |")
    lines += ["", "| stop_reason | 次数 |", "|---|---|"]
    for reason, count in stop_reasons.most_common():
        lines.append(f"| `{reason}` | {count} |")
    lines.append("")

    if errors:
        lines += ["## 4. 真实错误类型分布（离线 fixture 无法产生的信息）", "",
                  "### 4.1 按**尝试**计（含重试）", "",
                  "来源：每个 run 的 `live_metrics.by_error`。一次工具调用重试 3 次"
                  "失败会计 3 次，所以这张表反映的是**真实付出的代价**。", "",
                  "| 错误类型 | 尝试次数 |", "|---|---|"]
        for cls, count in errors.most_common():
            lines.append(f"| `{cls}` | {count} |")
        lines.append("")

        recorded_tc, replayed_tc = _replay_recorded_classes(rows)
        lines += ["### 4.2 按**失败的工具调用**计：运行时分类 vs 当前规则重放", "",
                  "两列**都来自同一批 trace `tool_call_failed` 事件**，一条失败调用一行，"
                  "因此分母相同、可以相减。（这张表的第一版把 4.1 的按尝试计数和"
                  "按调用计数混在一起对比，TRANSIENT_NETWORK 显示成 10 vs 5，"
                  "是分母不同造成的假差异。）", "",
                  "分类规则改动后，已跑完 run 的 trace **不会被重写**"
                  "（重写历史证据不可接受），所以用当前规则重放同一批真实错误消息，"
                  "差异即为规则改动的实际影响。重放脚本："
                  "`scripts/replay_canary_error_classes.py`。", ""]
        if not recorded_tc and not replayed_tc:
            lines += ["> trace 不可用，无法重放。", ""]
        else:
            lines += ["| 错误类型 | 运行时记录 | 按当前规则重放 | Δ |", "|---|---|---|---|"]
            for cls in sorted(set(recorded_tc) | set(replayed_tc),
                              key=lambda c: -max(recorded_tc.get(c, 0), replayed_tc.get(c, 0))):
                before, after = recorded_tc.get(cls, 0), replayed_tc.get(cls, 0)
                delta = after - before
                lines.append(f"| `{cls}` | {before or '—'} | {after or '—'} | "
                             f"{('%+d' % delta) if delta else '0'} |")
            lines.append("")
            assert sum(recorded_tc.values()) == sum(replayed_tc.values()), (
                "recorded and replayed must cover the same failed calls")
            lines += [f"两列合计均为 {sum(recorded_tc.values())} 次失败调用。", ""]
            if recorded_tc != replayed_tc:
                lines += ["> 两列不一致说明分类规则在本批 run 之后被修正过。"
                          "本报告**不修改**原始 trace，只并列展示。", ""]

    lines += ["## 5. 逐条结果", "",
              "| 任务 | 考察点 | 终态 | stop_reason | 来源 | 时延(s) | in/out tok | 费用 |",
              "|---|---|---|---|---|---|---|---|"]
    for row in rows:
        live = row["live_metrics"]
        lines.append(
            f"| {row['query']} | {row['exercises']} | {row['status']} | "
            f"`{row.get('stop_reason') or '-'}` | {row['num_sources']} | {row['duration_s']} | "
            f"{live['input_tokens']}/{live['output_tokens']} | "
            f"${float(live['estimated_cost_usd'] or 0):.5f} |")
    lines.append("")

    # Side-by-side, explicitly not a delta table.
    if OFFLINE_REFERENCE.exists():
        offline = json.loads(OFFLINE_REFERENCE.read_text(encoding="utf-8"))
        lines += ["## 6. 与离线 fixture 结果并列（**不是对照实验**）", "",
                  "两者任务集不同、来源不同、模型行为不同，**不可相减**。",
                  "并列的唯一目的是显示离线结论在哪些维度上没有代表性。", "",
                  "| 指标 | 离线 dev（88 任务×3） | 真实 canary（本次） |", "|---|---|---|"]
        live_success, _ = _metric(rows, "task_success")
        pairs = [
            ("Task Success", offline.get("task_success_rate_all_trials"), live_success),
            ("Citation Validity", offline.get("citation_validity"), _metric(rows, "citation_validity")[0]),
            ("Number Grounding", offline.get("number_grounding_rate"), _metric(rows, "number_grounding_rate")[0]),
            ("平均工具调用数", offline.get("tool_calls_mean"), round(tool_calls / max(1, len(rows)), 2)),
            ("P95 时延(s)", (offline.get("latency") or {}).get("p95_s"), percentile(durations, 95)),
            ("平均输入 Token", offline.get("input_tokens_mean"), tok_in // max(1, len(rows))),
            ("估算单次费用(USD)", offline.get("estimated_cost_usd_mean"), round(cost / max(1, len(rows)), 5)),
        ]
        for label, off, live_value in pairs:
            lines.append(f"| {label} | {fmt(off)} | {fmt(live_value)} |")
        lines += ["", "离线 token 与费用为 0 是**结构性的**（stub 替换了 `call_llm`），"
                  "不是测量误差。时延差两个数量级同理：fixture 不经过网络。", ""]

    lines += ["## 7. 能说与不能说", "", "**这份报告支持的结论：**",
              f"- 系统在真实 provider + 真实网络下可以端到端跑完，{len(succeeded)}/{len(rows)} "
              f"条产出了带来源的报告；",
              f"- 单次运行的真实成本约 **${cost / max(1, len(rows)):.4f}**，"
              f"真实 P95 时延约 **{percentile(durations, 95)}s**；",
              "- 真实链路会出现离线 fixture 造不出来的错误类型（见第 4 节）。", "",
              "**这份报告不支持的结论：**",
              f"- 不能作为成功率的统计结论：**n={len(rows)}，每条仅 1 trial**，无置信区间；",
              "- 不能作为「线上」指标：单机、单次、无并发、无持续观测；",
              "- 不能声称成本可外推：主体、来源可得性和检索轮数都会显著改变 token 量；",
              "- 任务期望值仍为 `needs_human_review`，没有人工 Gold。", ""]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines), encoding="utf-8")

    print(f"rows={len(rows)} succeeded/degraded={len(succeeded)} outcome_pass={len(passed_outcome)}")
    print(f"tokens in/out = {tok_in:,}/{tok_out:,}  cost=${cost:.4f}  "
          f"avg=${cost / max(1, len(rows)):.4f}/run")
    print(f"latency avg={_mean(durations)}s p95={percentile(durations, 95)}s")
    print(f"statuses={dict(statuses)}")
    print(f"errors={dict(errors)}")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
