"""Mine failures out of eval results and traces.

Input: the JSONL rows produced by `evals/runners/run_eval.py` (each carries its
grader breakdown and a pointer to the run's trace). Output: per-run
classifications plus slices - groups of runs sharing a primary failure mode,
ranked by how much fixing them would be worth.

Ranking is `count x mean_severity`, where severity is 1 minus the run's overall
score. A slice of 20 near-misses ranks below a slice of 8 total failures, which
is the right priority when the goal is to stop the system being wrong rather
than to nudge a score.
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field

from src.observability.trace_store import TraceStore
from src.optimization.failure_taxonomy import (
    DESCRIPTIONS,
    Classification,
    FailureMode,
    FailureSignal,
    classify,
)


class RunFailure(BaseModel):
    run_id: str
    task_id: str
    trial: int = 1
    arm: str = ""
    category: str = ""
    split: str = ""
    status: str = ""
    stop_reason: str = ""
    score: float = 0.0
    classification: Classification
    trace_path: str = ""

    @property
    def severity(self) -> float:
        return round(max(0.0, 1.0 - self.score), 4)


class FailureSlice(BaseModel):
    mode: FailureMode
    description: str = ""
    count: int = 0
    mean_severity: float = 0.0
    priority: float = 0.0
    affected_tasks: list[str] = Field(default_factory=list)
    affected_categories: dict[str, int] = Field(default_factory=dict)
    example_run_ids: list[str] = Field(default_factory=list)
    contributing_modes: dict[str, int] = Field(default_factory=dict)


def _grader(row: dict[str, Any], name: str) -> dict[str, Any]:
    for grade in row.get("grades", []):
        if grade.get("grader") == name:
            return grade
    return {}


def _metric(row: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = row.get("summary", {}).get("metrics", {}).get(key, default)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def build_signal(row: dict[str, Any], events: Optional[list[dict[str, Any]]] = None) -> FailureSignal:
    """Assemble the classification evidence for one eval result row."""
    events = events or []
    tool_started = [e for e in events if e.get("event_type") == "tool_call_started"]
    tool_failed = [e for e in events if e.get("event_type") == "tool_call_failed"]
    tool_ok_ids = {e.get("step_id") for e in events
                   if e.get("event_type") == "tool_call_completed"}
    unrecovered = [e for e in tool_failed if e.get("step_id") not in tool_ok_ids]

    tool_selection = _grader(row, "tool_selection").get("details", {})
    context_warnings = sum(
        len(e.get("payload", {}).get("consistency_warnings", []) or [])
        for e in events if e.get("event_type") == "context_compressed")

    sources = row.get("num_sources", 0) or 0
    grader_failures = [g.get("grader") for g in row.get("grades", [])
                       if g.get("applicable") and not g.get("passed")]

    return FailureSignal(
        run_id=row.get("run_id", ""), task_id=row.get("task_id", ""),
        status=row.get("status", ""), stop_reason=row.get("stop_reason", ""),
        expected_outcome=str(row.get("expected_outcome", "")),
        graders_passed=bool(row.get("summary", {}).get("passed", False)),
        tool_calls=len(tool_started),
        invalid_tool_calls=int(_metric(row, "invalid_tool_calls")),
        forbidden_tool_calls=int(_metric(row, "forbidden_tool_calls")),
        missing_expected_tools=len(tool_selection.get("missing", []) or []),
        tool_failures=len(tool_failed), unrecovered_tool_failures=len(unrecovered),
        redundant_tool_calls=int(_metric(row, "redundant_tool_calls")),
        sources=int(sources),
        tier1_or_2_sources=(int(_metric(row, "tier1_or_2_sources"))
                            if "tier1_or_2_sources" in row.get("summary", {}).get("metrics", {})
                            else None),
        unsupported_numbers=int(_metric(row, "unsupported_numbers")),
        invalid_citations=int(_metric(row, "invalid_citations")),
        total_numbers=int(_metric(row, "total_numbers")),
        budget_exceeded=bool(_metric(row, "budget_exceeded")),
        context_warnings=context_warnings,
        memory_injected=len([e for e in events if e.get("event_type") == "memory_read"]),
        injection_hijack=int(_metric(row, "injection_tool_hijack")),
        injection_leak=int(_metric(row, "injection_leak")),
        phases_completed=[e.get("name", "") for e in events
                          if e.get("event_type") == "phase_completed"
                          and e.get("status") == "ok"],
        grader_failures=[g for g in grader_failures if g],
    )


class FailureMiner:
    """Reads eval results, classifies failures and produces ranked slices."""

    def __init__(self, *, load_traces: bool = True) -> None:
        self.load_traces = load_traces

    def load_rows(self, results_path: Path) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with Path(results_path).open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return rows

    def mine(self, rows: Iterable[dict[str, Any]]) -> list[RunFailure]:
        failures: list[RunFailure] = []
        for row in rows:
            score = float(row.get("summary", {}).get("overall_score", 0.0) or 0.0)
            passed = bool(row.get("summary", {}).get("passed", False))

            # A run that satisfied every applicable grader is healthy, whatever
            # its weighted score is. Requiring score >= 0.99 instead classified
            # 98.9% of a clean suite as failing, because the weighted average
            # almost never reaches 1.0 - which made the whole ranking useless.
            #
            # The exception is an integrity defect: an unsupported number, an
            # unresolvable citation or a safety hit is mined even on a passing
            # run, because those must never be tolerated silently.
            integrity_defect = any(_metric(row, key) > 0 for key in (
                "unsupported_numbers", "invalid_citations",
                "injection_tool_hijack", "injection_leak"))
            if passed and not row.get("error") and not integrity_defect:
                continue

            events: list[dict[str, Any]] = []
            if self.load_traces and (trace_path := row.get("trace_path")):
                path = Path(trace_path)
                if path.exists():
                    events = TraceStore.read(path)

            classification = classify(build_signal(row, events))
            if classification is None:
                continue
            failures.append(RunFailure(
                run_id=row.get("run_id", ""), task_id=row.get("task_id", ""),
                trial=int(row.get("trial", 1)), arm=row.get("arm", ""),
                category=row.get("category", ""), split=row.get("split", ""),
                status=row.get("status", ""), stop_reason=row.get("stop_reason", ""),
                score=score, classification=classification,
                trace_path=row.get("trace_path", "")))
        return failures

    @staticmethod
    def slices(failures: list[RunFailure]) -> list[FailureSlice]:
        grouped: dict[FailureMode, list[RunFailure]] = defaultdict(list)
        for failure in failures:
            grouped[failure.classification.primary].append(failure)

        out: list[FailureSlice] = []
        for mode, members in grouped.items():
            categories: dict[str, int] = defaultdict(int)
            contributing: dict[str, int] = defaultdict(int)
            for member in members:
                categories[member.category or "unknown"] += 1
                for contributor in member.classification.contributing:
                    contributing[contributor.value] += 1
            mean_severity = round(sum(m.severity for m in members) / len(members), 4)
            out.append(FailureSlice(
                mode=mode, description=DESCRIPTIONS[mode], count=len(members),
                mean_severity=mean_severity,
                priority=round(len(members) * mean_severity, 4),
                affected_tasks=sorted({m.task_id for m in members})[:20],
                affected_categories=dict(categories),
                example_run_ids=[m.run_id for m in members if m.run_id][:5],
                contributing_modes=dict(contributing)))
        out.sort(key=lambda s: s.priority, reverse=True)
        return out

    def report(self, results_path: Path) -> dict[str, Any]:
        rows = self.load_rows(results_path)
        failures = self.mine(rows)
        slices = self.slices(failures)
        return {
            "results_path": str(results_path),
            "rows_examined": len(rows),
            "failing_runs": len(failures),
            "failure_rate": round(len(failures) / len(rows), 4) if rows else 0.0,
            "slices": [s.model_dump(mode="json") for s in slices],
            "top_slice": slices[0].mode.value if slices else None,
            "failures": [f.model_dump(mode="json") for f in failures[:200]],
        }


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# 失败归因报告", "",
             f"- 原始结果：`{report['results_path']}`",
             f"- 检查运行数：{report['rows_examined']}，判定为失败/降级：{report['failing_runs']}"
             f"（{report['failure_rate']:.1%}）", "",
             "## 失败切片（按 count x 平均严重度 排序）", "",
             "| 失败模式 | 说明 | 次数 | 平均严重度 | 优先级 | 受影响类别 |",
             "|---|---|---|---|---|---|"]
    for item in report["slices"]:
        categories = "、".join(f"{k}×{v}" for k, v in sorted(item["affected_categories"].items()))
        lines.append(f"| `{item['mode']}` | {item['description']} | {item['count']} | "
                     f"{item['mean_severity']} | {item['priority']} | {categories} |")
    lines += ["", "## 说明", "",
              "- 每次运行只归入**一个**主因；次因单独列出。归因顺序编码了因果关系："
              "例如主体无法验证时，“检索不足”是症状而不是原因。",
              "- 优先级 = 出现次数 × 平均严重度（1 - 综合评分），"
              "因此 8 个彻底失败排在 20 个擦边通过之前。",
              "- 本报告只做归因，不做修改；改进候选由 candidate_generator 生成，"
              "并且必须通过回归门禁才会被建议采纳。"]
    return "\n".join(lines)
