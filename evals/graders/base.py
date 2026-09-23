"""Grader contract.

A grader takes (task, run result, trace events) and returns a `GradeResult`.
Design rules:

- **Deterministic graders come first.** Anything a rule can decide is decided
  by a rule. The LLM judge only sees what is left over (writing quality,
  analytical coherence), and its output is always labelled as judge-derived.
- **A grader never mutates the run.** It reads artifacts and events.
- **Scores carry their evidence.** `details` holds the specific facts that
  produced the score, so a failing case can be diagnosed without re-running it.
- **Missing input is not a pass.** A grader that cannot find what it needs
  returns `applicable=False`, and the metric excludes it, rather than silently
  awarding 1.0 - the failure mode that makes a broken pipeline look perfect.
"""
from __future__ import annotations

from typing import Any, Optional, Protocol

from pydantic import BaseModel, Field

from evals.datasets.schema import EvalTask


class GradeResult(BaseModel):
    """One grader's verdict on one run."""

    grader: str
    #: 0.0-1.0. Meaningless when `applicable` is False.
    score: float = 0.0
    passed: bool = False
    #: False when this grader had nothing to judge (e.g. no report produced).
    applicable: bool = True
    weight: float = 1.0
    #: Machine-readable metrics this grader contributes to the suite totals.
    metrics: dict[str, Any] = Field(default_factory=dict)
    #: Human-readable evidence for the score.
    details: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    #: Set by LLM judges so judge-derived numbers are never mixed with
    #: deterministic ones without being visible as such.
    judge_model: str = ""
    judge_prompt_version: str = ""

    @property
    def effective_weight(self) -> float:
        return self.weight if self.applicable else 0.0


class RunArtifacts(BaseModel):
    """Everything a grader may read about one run."""

    task_id: str
    trial: int = 1
    run_id: str = ""
    status: str = ""
    stop_reason: str = ""
    report_markdown: str = ""
    report_path: str = ""
    sources: list[dict[str, Any]] = Field(default_factory=list)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    events: list[dict[str, Any]] = Field(default_factory=list)
    duration_s: float = 0.0
    error: str = ""

    def source_ids(self) -> list[str]:
        return [str(s.get("source_id", "")) for s in self.sources if s.get("source_id")]

    def tool_calls(self) -> list[dict[str, Any]]:
        """Every tool call the run *started*, in order."""
        return [e for e in self.events if e.get("event_type") == "tool_call_started"]

    def tool_names(self) -> list[str]:
        return [e.get("name", "") for e in self.tool_calls()]

    def succeeded_tool_names(self) -> list[str]:
        return [e.get("name", "") for e in self.events
                if e.get("event_type") == "tool_call_completed"]


class Grader(Protocol):
    name: str

    def grade(self, task: EvalTask, run: RunArtifacts) -> GradeResult: ...


class BaseGrader:
    """Convenience base with a uniform not-applicable helper."""

    name = "base"
    weight = 1.0

    def not_applicable(self, reason: str, **details: Any) -> GradeResult:
        return GradeResult(grader=self.name, applicable=False, weight=self.weight,
                           reason=reason, details=details)

    def result(self, score: float, passed: bool, reason: str = "", *,
               metrics: Optional[dict[str, Any]] = None, **details: Any) -> GradeResult:
        return GradeResult(grader=self.name, score=round(float(score), 4), passed=bool(passed),
                           weight=self.weight, reason=reason, metrics=metrics or {},
                           details=details)

    def grade(self, task: EvalTask, run: RunArtifacts) -> GradeResult:  # pragma: no cover
        raise NotImplementedError


def aggregate(results: list[GradeResult]) -> dict[str, Any]:
    """Weighted overall score plus a per-grader breakdown.

    Non-applicable graders are excluded from both the numerator and the
    denominator, so they neither reward nor penalise the run.
    """
    applicable = [r for r in results if r.applicable]
    total_weight = sum(r.effective_weight for r in applicable)
    overall = (sum(r.score * r.effective_weight for r in applicable) / total_weight
               if total_weight else 0.0)
    metrics: dict[str, Any] = {}
    for result in applicable:
        metrics.update(result.metrics)
    return {
        "overall_score": round(overall, 4),
        "passed": all(r.passed for r in applicable) if applicable else False,
        "graders_applied": len(applicable),
        "graders_skipped": [r.grader for r in results if not r.applicable],
        "by_grader": {r.grader: {"score": r.score, "passed": r.passed, "reason": r.reason}
                      for r in results},
        "metrics": metrics,
        "judge_used": any(r.judge_model for r in results),
    }
