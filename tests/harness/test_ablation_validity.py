"""Tests for the ablation matrix's comparison-validity guard.

This guard exists because of a real incident: the README presented a
Task Success delta of +12.5pp as "the Harness's benefit" while comparing a
stale baseline (graded with the old degenerate tool grader) against a freshly
graded harness run. The delta was later located to 11 tasks that all required
checkpoint resume, which the baseline arm had disabled.

So the matrix must refuse to print a delta unless the arms share their task
set, their trial count and their grader set. These tests make that refusal a
tested property rather than a promise in a docstring.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.build_ablation_matrix import (
    REFERENCE_ARM,
    TRACE_DERIVED,
    render,
    summarize_arm,
    validate,
)


def row(*, arm="harness_full", task_id="t1", trial=1, success=1, grader_set=None,
        trace="x.jsonl", duration=1.0, **metrics):
    graders = grader_set or ["outcome", "false_success", "unresolved_failure"]
    base_metrics = {"task_success": success}
    base_metrics.update(metrics)
    return {
        "task_id": task_id, "trial": trial, "arm": arm, "config_hash": f"cfg_{arm}",
        "trace_path": trace, "duration_s": duration,
        "grades": [{"grader": g, "applicable": True, "passed": True} for g in graders],
        "summary": {"overall_score": 0.9, "metrics": base_metrics},
    }


def arm_rows(arm, n=4, **kwargs):
    return [row(arm=arm, task_id=f"t{i}", **kwargs) for i in range(n)]


# --------------------------------------------------------------------------- #
def test_matching_arms_pass_validation():
    arms = {
        REFERENCE_ARM: summarize_arm(arm_rows(REFERENCE_ARM)),
        "abl_no_retry": summarize_arm(arm_rows("abl_no_retry")),
    }
    assert validate(arms) == []


def test_different_task_counts_are_rejected():
    arms = {
        REFERENCE_ARM: summarize_arm(arm_rows(REFERENCE_ARM, n=4)),
        "abl_no_retry": summarize_arm(arm_rows("abl_no_retry", n=3)),
    }
    problems = validate(arms)
    assert problems and any("任务数" in p for p in problems)


def test_different_trial_counts_are_rejected():
    reference = arm_rows(REFERENCE_ARM, n=4)
    doubled = arm_rows("abl_no_retry", n=4) + arm_rows("abl_no_retry", n=4)
    arms = {REFERENCE_ARM: summarize_arm(reference),
            "abl_no_retry": summarize_arm(doubled)}
    problems = validate(arms)
    assert problems and any("trial" in p for p in problems)


def test_different_grader_sets_are_rejected():
    """The exact shape of the incident this guard was written for."""
    stale = arm_rows("baseline_stale", grader_set=["outcome", "tool_selection"])
    fresh = arm_rows(REFERENCE_ARM,
                     grader_set=["outcome", "tool_selection", "false_success",
                                 "necessary_tool_recall"])
    problems = validate({REFERENCE_ARM: summarize_arm(fresh),
                         "baseline_stale": summarize_arm(stale)})
    assert problems
    assert any("Grader" in p for p in problems)


def test_a_single_arm_cannot_be_compared():
    problems = validate({REFERENCE_ARM: summarize_arm(arm_rows(REFERENCE_ARM))})
    assert problems and "少于 2 个" in problems[0]


def test_report_refuses_to_present_deltas_when_invalid():
    arms = {
        REFERENCE_ARM: summarize_arm(arm_rows(REFERENCE_ARM, n=4)),
        "abl_no_retry": summarize_arm(arm_rows("abl_no_retry", n=2)),
    }
    problems = validate(arms)
    markdown = render(arms, problems, "test")
    assert "对照有效性检查未通过" in markdown
    assert "不可作为归因结论" in markdown


def test_report_marks_validity_ok_when_arms_match():
    arms = {
        REFERENCE_ARM: summarize_arm(arm_rows(REFERENCE_ARM)),
        "abl_no_retry": summarize_arm(arm_rows("abl_no_retry")),
    }
    markdown = render(arms, validate(arms), "test")
    assert "对照有效性检查通过" in markdown


# --------------------------------------------------------------------------- #
def test_a_traceless_arm_reports_na_not_zero():
    """A missing capability must never look like a bad score.

    The legacy arm has no trace, so event-derived metrics do not exist for it.
    Printing 0 would say "legacy has a false-success rate of 0", which reads as
    *better* than the harness rather than "unmeasurable".
    """
    legacy = summarize_arm(arm_rows("legacy_no_harness", trace=""))
    harness = summarize_arm(arm_rows(REFERENCE_ARM, false_success=0.01))
    assert legacy["has_trace"] is False
    assert harness["has_trace"] is True

    markdown = render({"legacy_no_harness": legacy, REFERENCE_ARM: harness},
                      [], "test")
    lines = [line for line in markdown.splitlines() if line.startswith("| False Success")]
    assert lines, "False Success row missing from the matrix"
    assert "n/a" in lines[0], f"traceless arm should read n/a, got: {lines[0]}"


def test_trace_derived_set_covers_the_event_only_metrics():
    for key in ("false_success_rate", "unresolved_failure_rate",
                "same_tool_retry_recovery", "cross_tool_fallback_recovery",
                "checkpoint_resume_success", "redundant_tool_call_rate"):
        assert key in TRACE_DERIVED, f"{key} is event-derived but not marked as such"


def test_cost_metrics_are_not_judged_as_better_or_worse():
    """Tool-call count and latency are trade-offs. Labelling a rise in tool
    calls a "regression" would penalise exactly the capabilities that spend
    calls to gain reliability."""
    from scripts.build_ablation_matrix import METRICS

    directions = {key: direction for key, _label, direction in METRICS}
    assert directions["tool_calls_mean"] is None
    assert directions["p95_latency_s"] is None
    assert directions["task_success_rate_all_trials"] is True
    assert directions["false_success_rate"] is False


def test_summarize_reports_all_trials_pass_not_any():
    """A task passing 1 of 2 trials must not count as a pass."""
    flaky = [row(arm="a", task_id="t1", trial=1, success=1),
             row(arm="a", task_id="t1", trial=2, success=0)]
    summary = summarize_arm(flaky)
    assert summary["tasks"] == 1
    assert summary["task_success_rate_all_trials"] == 0.0


def test_real_ablation_results_pass_validation_if_present():
    """Guards the committed artifacts: if a future change makes the shipped
    matrix invalid, this fails instead of the invalid table being published."""
    from pathlib import Path

    report = Path(__file__).resolve().parents[2] / "evals" / "reports" / "ablation_matrix_dev.json"
    if not report.exists():
        pytest.skip("ablation matrix not built in this checkout")
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["validity_problems"] == [], payload["validity_problems"]
    assert len(payload["arms"]) >= 2


def test_tool_call_count_is_trace_derived():
    """The Tool Executor counts tool calls, so a legacy run has no counter.

    Before this was marked trace-derived the reg matrix printed
    `tool_calls_mean = 0.0` for `legacy_no_harness`, which reads as "legacy
    made zero tool calls". It made real calls; they were simply uncounted.
    """
    assert "tool_calls_mean" in TRACE_DERIVED

    legacy = summarize_arm(arm_rows("legacy_no_harness", trace=""))
    harness = summarize_arm(arm_rows(REFERENCE_ARM, tool_calls=15.0))
    markdown = render({"legacy_no_harness": legacy, REFERENCE_ARM: harness}, [], "test")
    line = next(l for l in markdown.splitlines() if l.startswith("| 平均工具调用数"))
    assert line.split("|")[2].strip() == "n/a", line


def test_a_genuine_zero_tool_call_mean_is_not_swallowed():
    """`a or b` would replace a real 0.0 mean with the fallback key's value.

    A harness arm that truly made no tool calls (everything served from
    checkpoint) must report 0, not fall through to `total_tool_calls`.
    """
    rows = [row(arm=REFERENCE_ARM, task_id=f"t{i}", tool_calls=0, total_tool_calls=99)
            for i in range(3)]
    assert summarize_arm(rows)["tool_calls_mean"] == 0.0


def test_tool_call_mean_falls_back_only_when_the_key_is_absent():
    rows = [row(arm=REFERENCE_ARM, task_id=f"t{i}", total_tool_calls=7) for i in range(3)]
    assert summarize_arm(rows)["tool_calls_mean"] == 7.0


# --------------------------------------------------------------------------- #
# Run-summary provenance
# --------------------------------------------------------------------------- #
def test_offline_run_summary_reports_the_effective_config_not_the_restored_one(tmp_path):
    """Behavioural, not a source grep: actually run one offline task.

    `run_one_trial` disables the shared search cache and semantic ranking for
    offline replay, then restores them in its finally block. The summary is
    built *after* that restore, so reading `config` directly reported
    `search_cache: true` for a run that had executed with the cache off - a
    result summary claiming the benchmark was cache-contaminated when it was
    not. (A shared on-disk cache would make each run depend on whatever ran
    before it, which is the one thing an offline benchmark may not do.)
    """
    from config import config as legacy_config
    from evals.runners.arms import resolve_arm
    from evals.runners.run_eval import load_tasks, run_suite

    tasks = load_tasks("dev")[:1]
    assert tasks, "dev split must have at least one task"

    prev_cache = legacy_config.ENABLE_SEARCH_CACHE
    prev_semantic = legacy_config.ENABLE_SEMANTIC_RANKING
    legacy_config.ENABLE_SEARCH_CACHE = True       # ambient state to be restored
    legacy_config.ENABLE_SEMANTIC_RANKING = True
    try:
        # `run_one_trial` installs the fixture tools itself for offline runs.
        summary = run_suite(tasks, resolve_arm("harness_full"), trials=1, live=False,
                            results_path=tmp_path / "r.jsonl", base_dir=tmp_path,
                            resume=False)
        env = summary["environment"]
        assert env["search_cache"] is False, (
            "offline summary must report the cache as disabled, not the restored value")
        assert env["semantic_ranking"] is False
        assert env["offline_overrides_applied"] is True
        assert summary["live"] is False
        # And the ambient config really was restored, so the lie was plausible.
        assert legacy_config.ENABLE_SEARCH_CACHE is True
    finally:
        legacy_config.ENABLE_SEARCH_CACHE = prev_cache
        legacy_config.ENABLE_SEMANTIC_RANKING = prev_semantic
