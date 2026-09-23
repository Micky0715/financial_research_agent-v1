"""Tests for the offline optimization loop: failure taxonomy, mining,
candidate constraints, the regression gate and version lifecycle."""
from __future__ import annotations

import json

import pytest

from src.optimization.candidate_generator import (
    Candidate,
    CandidateGenerator,
    ChangeType,
    render_candidates_markdown,
)
from src.optimization.failure_miner import FailureMiner, FailureSlice, build_signal
from src.optimization.failure_taxonomy import (
    LEVERS,
    ORDERED_MODES,
    FailureMode,
    FailureSignal,
    classify,
)
from src.optimization.regression_gate import GateThresholds, RegressionGate
from src.optimization.version_store import VersionState, VersionStore


# --------------------------------------------------------------------------- #
# Taxonomy
# --------------------------------------------------------------------------- #
def test_healthy_run_is_not_classified_as_a_failure():
    assert classify(FailureSignal(status="succeeded", stop_reason="completed",
                                  sources=5, graders_passed=True)) is None


def test_causal_ordering_picks_the_root_cause_not_the_symptom():
    """Entity unverified explains zero sources; fixing 'retrieval' would be
    the wrong change."""
    signal = FailureSignal(status="insufficient", stop_reason="entity_unverified", sources=0)
    result = classify(signal)
    assert result.primary is FailureMode.ENTITY_UNVERIFIED
    assert FailureMode.INSUFFICIENT_RETRIEVAL in result.contributing


def test_safety_violation_outranks_everything():
    signal = FailureSignal(status="succeeded", injection_hijack=1, invalid_tool_calls=3,
                           sources=0, stop_reason="entity_unverified")
    assert classify(signal).primary is FailureMode.SAFETY_VIOLATION


def test_routine_fetch_failures_do_not_count_as_execution_failures():
    """Some candidate URLs always 404. Counting that as a failure mode made
    219 healthy runs look broken."""
    noisy_but_fine = FailureSignal(status="succeeded", stop_reason="completed",
                                   unrecovered_tool_failures=2, sources=4,
                                   grader_failures=["citation_validity"])
    assert classify(noisy_but_fine).primary is not FailureMode.TOOL_EXECUTION

    actually_broken = FailureSignal(status="insufficient", unrecovered_tool_failures=2,
                                    sources=0)
    assert classify(actually_broken).primary is FailureMode.TOOL_EXECUTION


def test_every_mode_has_a_matcher_a_description_and_levers():
    from src.optimization.failure_taxonomy import DESCRIPTIONS, MATCHERS

    for mode in FailureMode:
        assert mode in MATCHERS, f"{mode} has no matcher"
        assert mode in DESCRIPTIONS, f"{mode} has no description"
        assert LEVERS.get(mode), f"{mode} has no levers"
        assert mode in ORDERED_MODES, f"{mode} missing from the causal ordering"


# --------------------------------------------------------------------------- #
# Mining
# --------------------------------------------------------------------------- #
def _row(**kwargs):
    base = {
        "run_id": "r1", "task_id": "t1", "trial": 1, "arm": "harness",
        "category": "company_research", "split": "dev", "status": "succeeded",
        "stop_reason": "completed", "num_sources": 4, "trace_path": "", "error": "",
        "grades": [], "summary": {"overall_score": 0.85, "passed": True, "metrics": {}},
    }
    base.update(kwargs)
    return base


def test_miner_skips_runs_that_passed_every_grader():
    assert FailureMiner(load_traces=False).mine([_row()]) == []


def test_miner_flags_an_integrity_defect_even_on_a_passing_run():
    """An unsupported number must not be tolerated because the run passed."""
    row = _row(summary={"overall_score": 0.95, "passed": True,
                        "metrics": {"unsupported_numbers": 2}})
    failures = FailureMiner(load_traces=False).mine([row])
    assert len(failures) == 1
    assert failures[0].classification.primary is FailureMode.UNGROUNDED_NUMBERS


def test_slices_are_ranked_by_count_times_severity():
    """8 total failures should outrank 20 near-misses."""
    many_near_misses = [_row(run_id=f"a{i}", task_id=f"a{i}", status="succeeded",
                             summary={"overall_score": 0.95, "passed": False,
                                      "metrics": {"invalid_citations": 1}})
                        for i in range(20)]
    few_disasters = [_row(run_id=f"b{i}", task_id=f"b{i}", status="insufficient",
                          stop_reason="no_usable_sources", num_sources=0,
                          summary={"overall_score": 0.05, "passed": False, "metrics": {}})
                     for i in range(8)]
    miner = FailureMiner(load_traces=False)
    slices = miner.slices(miner.mine(many_near_misses + few_disasters))
    assert slices[0].mode is FailureMode.INSUFFICIENT_RETRIEVAL
    assert slices[0].count == 8


def test_build_signal_reads_events_and_metrics():
    signal = build_signal(
        _row(summary={"overall_score": 0.5, "passed": False,
                      "metrics": {"invalid_tool_calls": 2, "redundant_tool_calls": 4}}),
        events=[{"event_type": "tool_call_started", "name": "web_search"},
                {"event_type": "tool_call_failed", "step_id": "s1"},
                {"event_type": "phase_completed", "name": "browse", "status": "ok"},
                {"event_type": "context_compressed",
                 "payload": {"consistency_warnings": ["open questions dropped"]}}])
    assert signal.invalid_tool_calls == 2
    assert signal.redundant_tool_calls == 4
    assert signal.unrecovered_tool_failures == 1
    assert signal.context_warnings == 1
    assert signal.phases_completed == ["browse"]


# --------------------------------------------------------------------------- #
# Candidate constraints
# --------------------------------------------------------------------------- #
def test_a_candidate_must_use_a_lever_that_matches_its_target_mode():
    with pytest.raises(ValueError, match="not a recognised lever"):
        Candidate(target_failure_mode=FailureMode.CITATION_ERROR,
                  change_type=ChangeType.BUDGET_POLICY, target="x",
                  rationale="unrelated", expected_metric_gains={"a": 1.0})


def test_a_candidate_must_declare_rationale_and_expected_gains():
    with pytest.raises(ValueError, match="why it should fix"):
        Candidate(target_failure_mode=FailureMode.CITATION_ERROR,
                  change_type=ChangeType.PROMPT, target="x", rationale="",
                  expected_metric_gains={"citation_validity": 0.1})
    with pytest.raises(ValueError, match="metrics it expects to move"):
        Candidate(target_failure_mode=FailureMode.CITATION_ERROR,
                  change_type=ChangeType.PROMPT, target="x", rationale="because",
                  expected_metric_gains={})


def test_change_type_is_a_closed_set_with_no_code_editing_lever():
    values = {c.value for c in ChangeType}
    assert values == {"prompt", "tool_name", "tool_description", "tool_schema",
                      "routing_rule", "retrieval_param", "stop_condition",
                      "budget_policy", "memory_policy"}
    assert not any("code" in v or "file" in v or "patch" in v for v in values)


def test_generator_produces_candidates_with_diffs_and_regressions():
    slices = [FailureSlice(mode=FailureMode.UNGROUNDED_NUMBERS, count=12, mean_severity=0.4,
                           priority=4.8)]
    candidates = CandidateGenerator().generate(slices, current_state={})
    assert candidates
    candidate = candidates[0]
    assert candidate.change_type is ChangeType.PROMPT
    assert candidate.diff()
    assert candidate.possible_regressions
    assert candidate.expected_metric_gains


def test_candidates_render_as_proposals_not_applied_changes():
    slices = [FailureSlice(mode=FailureMode.BUDGET_EXHAUSTED, count=5, mean_severity=0.3)]
    markdown = render_candidates_markdown(CandidateGenerator().generate(slices))
    assert "未应用" in markdown
    assert "没有任何一条被自动应用" in markdown


# --------------------------------------------------------------------------- #
# Regression gate
# --------------------------------------------------------------------------- #
def _report(**kwargs):
    base = {
        "task_success_rate_all_trials": 0.80,
        "unsupported_number_rate": 0.0,
        "estimated_cost_usd_mean": 0.01,
        "latency": {"p95_s": 10.0},
        "injection_tool_hijack_total": 0.0,
        "injection_leak_total": 0.0,
        "cross_user_leakage_rate": 0.0,
        "number_grounding_rate": 0.9,
        "per_task_score": {"reg_1": 0.9, "reg_2": 0.9},
    }
    base.update(kwargs)
    return base


def _regression(**kwargs):
    base = {"task_success_rate_all_trials": 0.9,
            "per_task_score": {"reg_1": 0.9, "reg_2": 0.9}}
    base.update(kwargs)
    return base


def test_gate_passes_a_genuine_improvement():
    result = RegressionGate().evaluate(
        candidate_id="c1", target_metric="number_grounding_rate",
        baseline_dev=_report(), candidate_dev=_report(number_grounding_rate=0.96),
        baseline_regression=_regression(), candidate_regression=_regression())
    assert result.passed is True
    assert "未自动部署" in result.recommendation


def test_gate_rejects_a_candidate_that_did_not_move_its_own_slice():
    result = RegressionGate().evaluate(
        candidate_id="c1", target_metric="number_grounding_rate",
        baseline_dev=_report(), candidate_dev=_report(number_grounding_rate=0.901),
        baseline_regression=_regression(), candidate_regression=_regression())
    assert result.passed is False
    assert any("targeted_slice" in c.name for c in result.failed_checks)


def test_gate_rejects_a_slice_win_that_costs_overall_success():
    result = RegressionGate().evaluate(
        candidate_id="c1", target_metric="number_grounding_rate",
        baseline_dev=_report(),
        candidate_dev=_report(number_grounding_rate=0.99, task_success_rate_all_trials=0.60),
        baseline_regression=_regression(), candidate_regression=_regression())
    assert result.passed is False
    assert any("overall_task_success" in c.name for c in result.failed_checks)


def test_gate_rejects_any_increase_in_unsupported_numbers():
    """A change that improves scores by inventing better-sounding numbers."""
    result = RegressionGate().evaluate(
        candidate_id="c1", target_metric="number_grounding_rate",
        baseline_dev=_report(),
        candidate_dev=_report(number_grounding_rate=0.99, unsupported_number_rate=0.02),
        baseline_regression=_regression(), candidate_regression=_regression())
    assert result.passed is False
    assert any("unsupported_number_rate" in c.name for c in result.failed_checks)


def test_gate_has_zero_tolerance_for_safety_regressions():
    for metric in ("injection_tool_hijack_total", "injection_leak_total",
                   "cross_user_leakage_rate"):
        result = RegressionGate().evaluate(
            candidate_id="c1", target_metric="number_grounding_rate",
            baseline_dev=_report(), candidate_dev=_report(**{"number_grounding_rate": 0.99,
                                                             metric: 1.0}),
            baseline_regression=_regression(), candidate_regression=_regression())
        assert result.passed is False, metric
        assert any(metric in c.name for c in result.failed_checks)


def test_gate_rejects_a_candidate_with_no_regression_evidence():
    result = RegressionGate().evaluate(
        candidate_id="c1", target_metric="number_grounding_rate",
        baseline_dev=_report(), candidate_dev=_report(number_grounding_rate=0.99))
    assert result.passed is False
    assert any("regression_set" in c.name for c in result.failed_checks)


def test_gate_detects_a_previously_fixed_bad_case_coming_back():
    result = RegressionGate().evaluate(
        candidate_id="c1", target_metric="number_grounding_rate",
        baseline_dev=_report(), candidate_dev=_report(number_grounding_rate=0.99),
        baseline_regression=_regression(per_task_score={"reg_1": 0.9, "reg_2": 0.9,
                                                        "reg_3": 0.9, "reg_4": 0.9}),
        candidate_regression=_regression(per_task_score={"reg_1": 0.2, "reg_2": 0.1,
                                                         "reg_3": 0.2, "reg_4": 0.1}))
    assert result.passed is False
    assert any("bad_case" in c.name for c in result.failed_checks)


def test_gate_honours_lower_is_better_metrics():
    result = RegressionGate().evaluate(
        candidate_id="c1", target_metric="unsupported_number_rate",
        baseline_dev=_report(unsupported_number_rate=0.10),
        candidate_dev=_report(unsupported_number_rate=0.02),
        baseline_regression=_regression(), candidate_regression=_regression(),
        higher_is_better=False)
    slice_check = next(c for c in result.checks if "targeted_slice" in c.name)
    assert slice_check.passed is True


# --------------------------------------------------------------------------- #
# Version lifecycle
# --------------------------------------------------------------------------- #
def _candidate() -> Candidate:
    return Candidate(
        target_failure_mode=FailureMode.CITATION_ERROR, change_type=ChangeType.PROMPT,
        target="prompts/report_prompt.txt", rationale="约束引用编号必须来自来源列表",
        before="旧的引用说明", after="新的引用说明，编号必须真实存在",
        expected_metric_gains={"citation_validity": 0.03})


def test_version_lifecycle_requires_gate_then_human_approval(tmp_path):
    store = VersionStore(tmp_path)
    record = store.propose(_candidate())
    assert record.state is VersionState.PROPOSED
    assert record.version == "v001"
    assert record.candidate.candidate_id.startswith("cand_")

    with pytest.raises(ValueError, match="passed the regression gate"):
        store.approve(record.version, "reviewer-a")

    gate_result = RegressionGate().evaluate(
        candidate_id=record.candidate.candidate_id, target_metric="number_grounding_rate",
        baseline_dev=_report(), candidate_dev=_report(number_grounding_rate=0.96),
        baseline_regression=_regression(), candidate_regression=_regression())
    store.record_gate(record.version, gate_result)
    assert store.get(record.version).state is VersionState.GATED

    with pytest.raises(ValueError, match="named approver"):
        store.approve(record.version, "")

    approved = store.approve(record.version, "reviewer-a")
    assert approved.state is VersionState.APPROVED and approved.approved_by == "reviewer-a"


def test_a_failed_gate_marks_the_version_rejected_and_blocks_approval(tmp_path):
    store = VersionStore(tmp_path)
    record = store.propose(_candidate())
    failed = RegressionGate().evaluate(
        candidate_id="c", target_metric="number_grounding_rate",
        baseline_dev=_report(), candidate_dev=_report())  # no regression evidence
    store.record_gate(record.version, failed)
    assert store.get(record.version).state is VersionState.REJECTED
    with pytest.raises(ValueError):
        store.approve(record.version, "reviewer-a")


def test_applying_writes_an_overlay_and_never_touches_production_config(tmp_path):
    store = VersionStore(tmp_path)
    record = store.propose(_candidate())
    store.record_gate(record.version, RegressionGate().evaluate(
        candidate_id="c", target_metric="number_grounding_rate",
        baseline_dev=_report(), candidate_dev=_report(number_grounding_rate=0.96),
        baseline_regression=_regression(), candidate_regression=_regression()))
    store.approve(record.version, "reviewer-a")

    overlay = store.apply_version(record.version)
    assert overlay.exists()
    payload = json.loads(overlay.read_text(encoding="utf-8"))
    assert payload["approved_by"] == "reviewer-a"
    assert "不会自动加载" in payload["note"]
    assert store.get(record.version).state is VersionState.APPLIED


def test_unapproved_version_cannot_be_materialised(tmp_path):
    store = VersionStore(tmp_path)
    record = store.propose(_candidate())
    with pytest.raises(ValueError, match="only an approved version"):
        store.apply_version(record.version)


def test_rollback_marks_the_version_and_records_the_reason(tmp_path):
    store = VersionStore(tmp_path)
    record = store.propose(_candidate())
    rolled = store.rollback(record.version, reason="p95 latency regressed in production")
    assert rolled.state is VersionState.ROLLED_BACK
    assert "p95 latency regressed" in rolled.notes


def test_versions_increment_and_index_is_queryable(tmp_path):
    store = VersionStore(tmp_path)
    store.propose(_candidate())
    second = _candidate()
    second.after = "另一种引用说明写法"
    store.propose(second)
    assert [r.version for r in store.list_versions()] == ["v001", "v002"]
    assert store.summary()["total"] == 2
    assert store.summary()["by_state"]["proposed"] == 2
