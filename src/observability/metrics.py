"""Metric aggregation over trace event streams.

Everything reported anywhere in this project - the eval report, the regression
gate, the README's numbers - is computed here from JSONL events. There is no
second path where a number can be typed in by hand.

Percentiles use the nearest-rank method on the sorted sample, which is exact
for the small sample sizes here (tens of runs) and needs no interpolation
convention to explain.
"""
from __future__ import annotations

import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from src.observability.events import EventType
from src.observability.trace_store import TraceStore


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile. `p` in [0, 100]. Empty sample -> 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return round(ordered[min(rank, len(ordered)) - 1], 4)


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = sum(values) / len(values)
    return round(math.sqrt(sum((v - mu) ** 2 for v in values) / (len(values) - 1)), 4)


class RunMetrics:
    """Metrics for a single run, derived from its event stream."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.run_id = next((e.get("run_id", "") for e in events if e.get("run_id")), "")

    # ------------------------------------------------------------------ #
    def _of(self, *types: EventType) -> list[dict[str, Any]]:
        wanted = {t.value for t in types}
        return [e for e in self.events if e.get("event_type") in wanted]

    @property
    def succeeded(self) -> bool:
        """A run counts as succeeded only on an explicit run_completed event
        whose payload says so. Absence of a failure event is not success -
        a killed process leaves neither."""
        for event in reversed(self._of(EventType.RUN_COMPLETED)):
            return bool(event.get("payload", {}).get("is_success", False))
        return False

    @property
    def stop_reason(self) -> str:
        for event in reversed(self._of(EventType.STOP_DECISION)):
            return str(event.get("payload", {}).get("reason", ""))
        for event in reversed(self._of(EventType.RUN_COMPLETED, EventType.RUN_FAILED)):
            return str(event.get("payload", {}).get("stop_reason", ""))
        return "unknown"

    @property
    def duration_s(self) -> float:
        for event in reversed(self._of(EventType.RUN_COMPLETED, EventType.RUN_FAILED)):
            return float(event.get("payload", {}).get("duration_s", 0.0) or 0.0)
        return 0.0

    # ------------------------------------------------------------------ #
    def tool_stats(self) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"calls": 0, "ok": 0, "failed": 0, "blocked": 0, "deduped": 0,
                     "cached": 0, "durations": [], "errors": defaultdict(int)})
        for event in self._of(EventType.TOOL_CALL_COMPLETED):
            row = stats[event.get("name", "?")]
            row["calls"] += 1
            row["ok"] += 1
            row["durations"].append(float(event.get("duration_s", 0.0) or 0.0))
            if event.get("payload", {}).get("from_cache"):
                row["cached"] += 1
        for event in self._of(EventType.TOOL_CALL_FAILED):
            row = stats[event.get("name", "?")]
            row["calls"] += 1
            row["failed"] += 1
            row["durations"].append(float(event.get("duration_s", 0.0) or 0.0))
            row["errors"][event.get("error_class", "UNKNOWN")] += 1
        for event in self._of(EventType.TOOL_CALL_BLOCKED):
            stats[event.get("name", "?")]["blocked"] += 1
        for event in self._of(EventType.TOOL_CALL_DEDUPED):
            stats[event.get("name", "?")]["deduped"] += 1

        out: dict[str, dict[str, Any]] = {}
        for name, row in stats.items():
            durations = row.pop("durations")
            errors = dict(row.pop("errors"))
            out[name] = {
                **row,
                "errors": errors,
                "success_rate": round(row["ok"] / row["calls"], 4) if row["calls"] else 0.0,
                "avg_latency_s": _mean(durations),
                "p50_latency_s": percentile(durations, 50),
                "p95_latency_s": percentile(durations, 95),
            }
        return out

    def model_stats(self) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"calls": 0, "ok": 0, "failed": 0, "input_tokens": 0, "output_tokens": 0,
                     "cost_usd": 0.0, "durations": [], "errors": defaultdict(int)})
        for event in self._of(EventType.MODEL_CALL_COMPLETED):
            payload = event.get("payload", {})
            row = stats[event.get("name", "?")]
            row["calls"] += 1
            row["ok"] += 1
            row["input_tokens"] += int(payload.get("input_tokens", 0) or 0)
            row["output_tokens"] += int(payload.get("output_tokens", 0) or 0)
            row["cost_usd"] += float(payload.get("estimated_cost_usd", 0.0) or 0.0)
            row["durations"].append(float(event.get("duration_s", 0.0) or 0.0))
        for event in self._of(EventType.MODEL_CALL_FAILED):
            row = stats[event.get("name", "?")]
            row["calls"] += 1
            row["failed"] += 1
            row["errors"][event.get("error_class", "UNKNOWN")] += 1

        out: dict[str, dict[str, Any]] = {}
        for name, row in stats.items():
            durations = row.pop("durations")
            errors = dict(row.pop("errors"))
            out[name] = {
                **row, "errors": errors,
                "cost_usd": round(row["cost_usd"], 6),
                "avg_latency_s": _mean(durations),
                "p95_latency_s": percentile(durations, 95),
            }
        return out

    def error_stats(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for event in self._of(EventType.TOOL_CALL_FAILED, EventType.MODEL_CALL_FAILED,
                              EventType.RUN_FAILED, EventType.TOOL_CALL_BLOCKED):
            if cls := event.get("error_class"):
                counts[cls] += 1
        return dict(counts)

    def recovery_stats(self) -> dict[str, Any]:
        """Recovery = a step that failed at least once and later succeeded.

        Keyed on step_id so a retried call is one recovery, not N.
        """
        failed_steps = {e.get("step_id") for e in self._of(EventType.TOOL_CALL_FAILED,
                                                           EventType.MODEL_CALL_FAILED)
                        if e.get("step_id")}
        ok_steps = {e.get("step_id") for e in self._of(EventType.TOOL_CALL_COMPLETED,
                                                       EventType.MODEL_CALL_COMPLETED)
                    if e.get("step_id")}
        recovered = failed_steps & ok_steps
        return {
            "steps_with_failure": len(failed_steps),
            "recovered_steps": len(recovered),
            "recovery_rate": round(len(recovered) / len(failed_steps), 4) if failed_steps else None,
            "retries_scheduled": len(self._of(EventType.RETRY_SCHEDULED)),
            "circuits_opened": len(self._of(EventType.CIRCUIT_OPENED)),
            "degradations": len(self._of(EventType.DEGRADED)),
        }

    def budget_stats(self) -> dict[str, Any]:
        last = None
        for event in reversed(self._of(EventType.BUDGET_UPDATED)):
            last = event.get("payload", {})
            break
        exceeded = self._of(EventType.BUDGET_EXCEEDED)
        return {
            "input_tokens": int((last or {}).get("tokens_in", 0) or 0),
            "output_tokens": int((last or {}).get("tokens_out", 0) or 0),
            "estimated_cost_usd": round(float((last or {}).get("cost_usd", 0.0) or 0.0), 6),
            "model_calls": int((last or {}).get("model_calls", 0) or 0),
            "tool_calls": int((last or {}).get("tool_calls", 0) or 0),
            "budget_exceeded": bool(exceeded),
            "exceeded_dimensions": sorted({
                str(e.get("payload", {}).get("dimension", "")) for e in exceeded
            } - {""}),
        }

    def redundant_tool_calls(self) -> int:
        return len(self._of(EventType.TOOL_CALL_DEDUPED))

    def checkpoint_stats(self) -> dict[str, int]:
        return {
            "checkpoints_saved": len(self._of(EventType.CHECKPOINT_SAVED)),
            "checkpoints_restored": len(self._of(EventType.CHECKPOINT_RESTORED)),
            "steps_skipped_idempotent": len(self._of(EventType.STEP_SKIPPED_IDEMPOTENT)),
        }

    def memory_stats(self) -> dict[str, int]:
        return {
            "memory_reads": len(self._of(EventType.MEMORY_READ)),
            "memory_writes": len(self._of(EventType.MEMORY_WRITE)),
            "memory_rejected": len(self._of(EventType.MEMORY_REJECTED)),
        }

    def grader_results(self) -> list[dict[str, Any]]:
        return [e.get("payload", {}) for e in self._of(EventType.GRADER_RESULT)]

    def summary(self) -> dict[str, Any]:
        tools = self.tool_stats()
        models = self.model_stats()
        return {
            "run_id": self.run_id,
            "succeeded": self.succeeded,
            "stop_reason": self.stop_reason,
            "duration_s": self.duration_s,
            "event_count": len(self.events),
            "tool_calls_total": sum(t["calls"] for t in tools.values()),
            "tool_failures_total": sum(t["failed"] for t in tools.values()),
            "invalid_tool_calls": self.error_stats().get("INVALID_ARGUMENT", 0)
                                  + self.error_stats().get("SCHEMA_ERROR", 0),
            "redundant_tool_calls": self.redundant_tool_calls(),
            "model_calls_total": sum(m["calls"] for m in models.values()),
            "by_tool": tools,
            "by_model": models,
            "by_error": self.error_stats(),
            "recovery": self.recovery_stats(),
            "budget": self.budget_stats(),
            "checkpoints": self.checkpoint_stats(),
            "memory": self.memory_stats(),
        }

    @classmethod
    def from_file(cls, path: Path) -> "RunMetrics":
        return cls(TraceStore.read(path))


class SuiteMetrics:
    """Aggregate many runs (one eval suite, or one A/B arm)."""

    def __init__(self, runs: Iterable[RunMetrics]) -> None:
        self.runs = list(runs)

    @classmethod
    def from_dir(cls, directory: Path, pattern: str = "*.jsonl") -> "SuiteMetrics":
        return cls(RunMetrics(events) for _, events in TraceStore.iter_dir(Path(directory), pattern))

    def aggregate(self) -> dict[str, Any]:
        if not self.runs:
            return {"runs": 0}
        summaries = [r.summary() for r in self.runs]
        durations = [s["duration_s"] for s in summaries if s["duration_s"] > 0]
        successes = [s for s in summaries if s["succeeded"]]

        by_tool: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"calls": 0, "ok": 0, "failed": 0, "blocked": 0, "deduped": 0, "cached": 0})
        for summary in summaries:
            for name, row in summary["by_tool"].items():
                agg = by_tool[name]
                for key in ("calls", "ok", "failed", "blocked", "deduped", "cached"):
                    agg[key] += row.get(key, 0)
        for name, agg in by_tool.items():
            agg["success_rate"] = round(agg["ok"] / agg["calls"], 4) if agg["calls"] else 0.0

        by_error: dict[str, int] = defaultdict(int)
        for summary in summaries:
            for cls_name, count in summary["by_error"].items():
                by_error[cls_name] += count

        recoveries = [s["recovery"] for s in summaries]
        with_failure = sum(r["steps_with_failure"] for r in recoveries)
        recovered = sum(r["recovered_steps"] for r in recoveries)

        stop_reasons: dict[str, int] = defaultdict(int)
        for summary in summaries:
            stop_reasons[summary["stop_reason"] or "unknown"] += 1

        return {
            "runs": len(summaries),
            "task_success_rate": round(len(successes) / len(summaries), 4),
            "latency": {
                "avg_s": _mean(durations),
                "p50_s": percentile(durations, 50),
                "p95_s": percentile(durations, 95),
                "max_s": round(max(durations), 4) if durations else 0.0,
            },
            "tokens": {
                "input": sum(s["budget"]["input_tokens"] for s in summaries),
                "output": sum(s["budget"]["output_tokens"] for s in summaries),
            },
            "estimated_cost_usd": round(sum(s["budget"]["estimated_cost_usd"] for s in summaries), 6),
            "budget_exceeded_rate": round(
                sum(1 for s in summaries if s["budget"]["budget_exceeded"]) / len(summaries), 4),
            "tool_calls_total": sum(s["tool_calls_total"] for s in summaries),
            "invalid_tool_call_rate": round(
                sum(s["invalid_tool_calls"] for s in summaries)
                / max(1, sum(s["tool_calls_total"] for s in summaries)), 4),
            "redundant_tool_calls": sum(s["redundant_tool_calls"] for s in summaries),
            "redundant_tool_call_rate": round(
                sum(s["redundant_tool_calls"] for s in summaries)
                / max(1, sum(s["tool_calls_total"] for s in summaries)), 4),
            "recovery_success_rate": round(recovered / with_failure, 4) if with_failure else None,
            "steps_with_failure": with_failure,
            "by_tool": dict(by_tool),
            "by_error": dict(by_error),
            "stop_reasons": dict(stop_reasons),
        }


def multi_trial_stats(values: list[float]) -> dict[str, Any]:
    """Stability view over repeated trials of the same task.

    Reports the worst trial explicitly: an average that hides one catastrophic
    run out of three is exactly the number that makes a system look shippable
    when it is not.
    """
    clean = [v for v in values if v is not None]
    return {
        "trials": len(clean),
        "mean": _mean(clean),
        "stdev": _stdev(clean),
        "min": round(min(clean), 4) if clean else 0.0,
        "max": round(max(clean), 4) if clean else 0.0,
        "worst": round(min(clean), 4) if clean else 0.0,
    }


def success_stability(successes: list[bool]) -> dict[str, Any]:
    total = len(successes)
    passed = sum(1 for s in successes if s)
    return {
        "trials": total,
        "successes": passed,
        "success_rate": round(passed / total, 4) if total else 0.0,
        "all_passed": total > 0 and passed == total,
        "any_passed": passed > 0,
        "flaky": 0 < passed < total,
    }


def compare_arms(baseline: dict[str, Any], candidate: dict[str, Any],
                 keys: Optional[list[str]] = None) -> dict[str, Any]:
    """Flat baseline-vs-candidate delta table for the regression gate/report."""
    keys = keys or ["task_success_rate", "invalid_tool_call_rate", "redundant_tool_call_rate",
                    "recovery_success_rate", "budget_exceeded_rate", "estimated_cost_usd"]
    out: dict[str, Any] = {}
    for key in keys:
        base_value, cand_value = baseline.get(key), candidate.get(key)
        if isinstance(base_value, (int, float)) and isinstance(cand_value, (int, float)):
            out[key] = {"baseline": base_value, "candidate": cand_value,
                        "delta": round(cand_value - base_value, 6)}
        else:
            out[key] = {"baseline": base_value, "candidate": cand_value, "delta": None}
    for section in ("latency",):
        base_section, cand_section = baseline.get(section, {}), candidate.get(section, {})
        for key, base_value in base_section.items():
            cand_value = cand_section.get(key)
            if isinstance(base_value, (int, float)) and isinstance(cand_value, (int, float)):
                out[f"{section}.{key}"] = {"baseline": base_value, "candidate": cand_value,
                                           "delta": round(cand_value - base_value, 4)}
    return out
