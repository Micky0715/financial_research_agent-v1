"""Checkpoint persistence, resume planning, and layered context compression."""
from __future__ import annotations

import json

import pytest

from src.context.manager import ContextManager, Layer, source_digest, verify_consistency
from src.runtime.checkpoint import (
    SqliteCheckpointStore,
    SqliteRunStore,
    open_stores,
    plan_resume,
    should_skip,
)
from src.runtime.state import (
    AttemptRecord,
    EvidenceRecord,
    FinalOutcome,
    Phase,
    RunState,
    RunStatus,
    StepRecord,
    StepStatus,
    StopReason,
)


@pytest.fixture
def stores(tmp_path):
    runs, checkpoints, artifacts = open_stores(tmp_path)
    yield runs, checkpoints, artifacts
    runs.close()


# --------------------------------------------------------------------------- #
# Checkpoint persistence
# --------------------------------------------------------------------------- #
def test_checkpoints_are_versioned_and_latest_wins(stores):
    _, checkpoints, _ = stores
    state = RunState(topic="比亚迪", report_type="company_research")

    state.set_phase(Phase.RESEARCH)
    assert checkpoints.save(state) == 1
    state.set_phase(Phase.BROWSE)
    state.add_evidence(EvidenceRecord(claim="c", value="1亿元", source_id="s1", url="http://a"))
    assert checkpoints.save(state) == 2

    restored = checkpoints.load_latest(state.run_id)
    assert restored.checkpoint_version == 2
    assert restored.phase is Phase.BROWSE
    assert len(restored.evidence) == 1
    assert checkpoints.versions(state.run_id) == [1, 2]


def test_load_latest_skips_an_incompatible_tail_checkpoint(stores, tmp_path):
    _, checkpoints, _ = stores
    state = RunState(topic="x")
    checkpoints.save(state)          # v1, good
    good_version = state.checkpoint_version

    # Simulate a checkpoint written by an incompatible future build.
    payload = state.to_checkpoint()
    payload["schema_version"] = "99.0"
    checkpoints._conn.execute(
        "INSERT OR REPLACE INTO checkpoints (run_id, version, phase, schema_version, created_at, state_json)"
        " VALUES (?,?,?,?,?,?)",
        (state.run_id, 99, "browse", "99.0", "now", json.dumps(payload)))
    checkpoints._conn.commit()

    restored = checkpoints.load_latest(state.run_id)
    assert restored is not None
    assert restored.checkpoint_version == good_version


def test_load_latest_raises_when_no_checkpoint_is_compatible(stores):
    _, checkpoints, _ = stores
    state = RunState(topic="x")
    payload = state.to_checkpoint()
    payload["schema_version"] = "99.0"
    checkpoints._conn.execute(
        "INSERT INTO checkpoints (run_id, version, phase, schema_version, created_at, state_json)"
        " VALUES (?,?,?,?,?,?)",
        (state.run_id, 1, "browse", "99.0", "now", json.dumps(payload)))
    checkpoints._conn.commit()
    with pytest.raises(ValueError, match="no compatible checkpoint"):
        checkpoints.load_latest(state.run_id)


def test_missing_run_returns_none(stores):
    _, checkpoints, _ = stores
    assert checkpoints.load_latest("does-not-exist") is None


def test_run_store_indexes_outcome(stores):
    runs, _, _ = stores
    state = RunState(topic="宁德时代", report_type="company_research")
    state.finish(FinalOutcome(status=RunStatus.DEGRADED, stop_reason=StopReason.BUDGET_EXHAUSTED,
                              num_sources=3, quality_score=0.71))
    runs.upsert(state)

    row = runs.get(state.run_id)
    assert row["status"] == "degraded"
    assert row["stop_reason"] == "budget_exhausted"
    assert json.loads(row["summary_json"])["num_sources"] == 3
    assert runs.list_runs(status="degraded")[0]["run_id"] == state.run_id


def test_resumable_lists_only_unfinished_runs(stores):
    runs, _, _ = stores
    unfinished = RunState(topic="a")
    runs.upsert(unfinished)
    done = RunState(topic="b")
    done.finish(FinalOutcome(status=RunStatus.SUCCEEDED))
    runs.upsert(done)

    ids = {r["run_id"] for r in runs.resumable()}
    assert unfinished.run_id in ids and done.run_id not in ids


def test_prune_keeps_the_newest_checkpoints(stores):
    _, checkpoints, _ = stores
    state = RunState(topic="x")
    for _ in range(6):
        checkpoints.save(state)
    assert checkpoints.prune(state.run_id, keep_last=2) == 4
    assert checkpoints.versions(state.run_id) == [5, 6]


# --------------------------------------------------------------------------- #
# Resume planning
# --------------------------------------------------------------------------- #
def _step(name, status, key="", side_effect_free=True):
    return StepRecord(name=name, kind="tool", status=status, idempotency_key=key,
                      side_effect_free=side_effect_free)


def test_resume_skips_completed_idempotent_steps_and_reruns_safe_ones():
    state = RunState(topic="x")
    state.add_step(_step("web_search", StepStatus.SUCCEEDED, key="fp-search"))
    state.add_step(_step("read_webpage", StepStatus.SUCCEEDED, key="fp-page"))
    state.add_step(_step("read_pdf", StepStatus.RUNNING, key="fp-pdf"))

    plan = plan_resume(state)
    assert plan.skip_step_keys == {"fp-search", "fp-page"}
    assert len(plan.rerun_step_ids) == 1
    assert plan.needs_human == []


def test_in_flight_side_effecting_step_escalates_instead_of_guessing():
    """The case a naive resume gets wrong: it writes the file a second time."""
    state = RunState(topic="x")
    state.add_step(_step("export_report_file", StepStatus.RUNNING, key="fp-export",
                         side_effect_free=False))
    plan = plan_resume(state)
    assert plan.rerun_step_ids == []
    assert len(plan.needs_human) == 1
    assert "manual confirmation" in plan.needs_human[0]["reason"]


def test_should_skip_returns_the_reusable_step():
    state = RunState(topic="x")
    done = _step("web_search", StepStatus.SUCCEEDED, key="fp1")
    done.output_summary = "5 hits"
    state.add_step(done)
    assert should_skip(state, "fp1").output_summary == "5 hits"
    assert should_skip(state, "other") is None

    failed = _step("web_search", StepStatus.FAILED, key="fp2")
    state.add_step(failed)
    assert should_skip(state, "fp2") is None, "a failed step must be re-run, not skipped"


def test_resume_preserves_budget_already_spent(stores):
    _, checkpoints, _ = stores
    state = RunState(topic="x")
    state.budget.tool_calls = 17
    state.budget.cost_usd = 0.42
    checkpoints.save(state)

    restored = checkpoints.load_latest(state.run_id)
    plan = plan_resume(restored)
    assert plan.describe()["budget_already_spent"]["tool_calls"] == 17
    assert restored.budget.cost_usd == 0.42


def test_resume_plan_description_is_serializable(stores):
    state = RunState(topic="x")
    state.add_step(_step("web_search", StepStatus.SUCCEEDED, key="fp1"))
    assert json.dumps(plan_resume(state).describe(), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Context management
# --------------------------------------------------------------------------- #
def test_high_priority_layers_survive_a_tight_budget():
    manager = ContextManager(budget_tokens=300)
    manager.add_goal("比亚迪投资价值分析", "company_research", ["财务分析", "估值分析"])
    manager.add_open_questions(["2025Q1 毛利率尚未确认"])
    manager.add_evidence([EvidenceRecord(claim="2024年营收", value="7771亿元",
                                         source_id="s1", url="http://a", title="年报")])
    for i in range(40):
        manager.add_tool_result("read_webpage", "无关内容 " * 60, importance=0.1)

    text, report = manager.assemble()
    assert "比亚迪投资价值分析" in text
    assert "尚未确认" in text
    assert report.dropped_by_layer.get("TOOL_RESULTS", 0) > 0
    assert report.tokens_after <= manager.budget_tokens
    assert report.compression_ratio < 1.0


def test_evidence_number_source_mapping_is_preserved():
    manager = ContextManager(budget_tokens=2000)
    manager.add_goal("t", "company_research", [])
    manager.add_evidence([
        EvidenceRecord(claim="2024年营业收入", value="1200.5亿元", source_id="s1",
                       url="http://a", title="年报", authority_tier="tier1"),
        EvidenceRecord(claim="毛利率", value="22.4%", source_id="s1", url="http://a", title="年报"),
        EvidenceRecord(claim="行业CR5", value="61.2%", source_id="s2", url="http://b", title="行业"),
    ])
    text, report = manager.assemble()
    assert "1200.5亿元" in text and "22.4%" in text and "61.2%" in text
    assert "[s1]" in text and "[s2]" in text
    assert report.consistency_ok


def test_source_digest_groups_by_source_not_by_claim():
    records = [EvidenceRecord(claim=f"指标{i}", value=f"{i}亿元", source_id="s1",
                              url="http://a", title="同一份年报") for i in range(6)]
    lines = source_digest(records, max_per_source=4)
    assert sum(1 for line in lines if line.startswith("[s1]")) == 1
    assert "另有 2 条同源证据" in lines[-1]


def test_memory_has_its_own_hard_cap():
    manager = ContextManager(budget_tokens=10000, memory_budget_tokens=60)
    added = manager.add_memory([{"kind": "semantic", "content": "记忆内容 " * 200,
                                 "memory_id": f"m{i}"} for i in range(10)])
    assert added <= 60
    assert sum(1 for b in manager.blocks if b.layer == Layer.MEMORY) < 10


def test_consistency_check_flags_orphaned_citations():
    from src.context.manager import CompressionReport, ContextBlock

    report = CompressionReport()
    blocks = [ContextBlock(layer=Layer.TASK_GOAL, text="目标"),
              ContextBlock(layer=Layer.RECENT, text="报告引用了 [s9] 的数据")]
    verify_consistency(report, blocks)
    assert report.consistency_ok is False
    assert any("citation markers" in w for w in report.consistency_warnings)


def test_compression_report_records_what_was_dropped():
    manager = ContextManager(budget_tokens=200)
    manager.add_goal("t", "company_research", [])
    for i in range(20):
        manager.add_tool_result("web_search", "x" * 500, importance=0.1)
    _, report = manager.assemble()
    assert report.blocks_before > report.blocks_after
    assert report.tokens_before > report.tokens_after
    assert sum(report.dropped_by_layer.values()) > 0
    assert json.dumps(report.model_dump(mode="json"))
