"""Tool executor behaviour and trace/redaction guarantees.

These run the real `ToolExecutor` against fixture-backed handlers, so argument
validation, retry decisions, circuit breaking, budget accounting, dedup and
artifact externalization are all genuinely exercised.
"""
from __future__ import annotations

import json

import pytest

from evals.fixtures import FixtureError
from src.observability.events import (
    DROPPED,
    REDACTED,
    EventType,
    TraceEvent,
    make_event,
    redact,
    redact_text,
)
from src.observability.metrics import RunMetrics, SuiteMetrics, multi_trial_stats, percentile
from src.observability.trace_store import ArtifactStore, TraceStore
from src.runtime.budget import BudgetLimits, BudgetTracker
from src.runtime.errors import ErrorClass
from src.runtime.policies import CircuitRegistry, RetryPolicy, SideEffectPolicy
from src.runtime.state import StepStatus
from src.tools.schemas import ToolSpec, ToolCategory, apply_defaults, coerce, validate


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #
def test_schema_validator_subset():
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 10},
            "n": {"type": "integer", "minimum": 1, "maximum": 5},
            "mode": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    assert validate({"query": "hi"}, schema) == []
    assert "missing required property" in validate({}, schema)[0]
    assert "maxLength" in validate({"query": "x" * 20}, schema)[0]
    assert "maximum" in validate({"query": "x", "n": 99}, schema)[0]
    assert "enum" in validate({"query": "x", "mode": "z"}, schema)[0]
    assert "unexpected property" in validate({"query": "x", "extra": 1}, schema)[0]


def test_booleans_are_not_integers():
    """`True` must not satisfy an integer field - Python's bool/int subclassing
    would otherwise let `max_results=True` through."""
    assert validate(True, {"type": "integer"}) != []


def test_coerce_and_defaults():
    schema = {"type": "object", "properties": {
        "max_results": {"type": "integer", "default": 8},
        "flag": {"type": "boolean"},
    }}
    assert coerce({"max_results": "12"}, schema)["max_results"] == 12
    assert coerce({"flag": "true"}, schema)["flag"] is True
    assert coerce({"max_results": "abc"}, schema)["max_results"] == "abc"  # left for validate()
    assert apply_defaults({}, schema)["max_results"] == 8


def test_tool_catalog_entry_hides_infrastructure_details():
    from src.tools.registry import SEARCH_SPEC

    entry = SEARCH_SPEC.to_catalog_entry()
    assert set(entry) == {"name", "category", "description", "when_to_use",
                          "when_not_to_use", "input_schema", "returns", "cost"}
    assert "timeout" not in json.dumps(entry)
    assert entry["when_not_to_use"]


# --------------------------------------------------------------------------- #
# Executor: happy path, validation, retry, circuits, budget, dedup
# --------------------------------------------------------------------------- #
def test_successful_call_records_step_and_events(executor, trace, run_state):
    result = executor.call("web_search", {"query": "示例公司 财务分析"},
                           rationale="need candidate sources")
    assert result.ok is True
    assert len(result.content) > 0
    assert result.content[0]["url"].startswith("https://")

    step = run_state.steps[-1]
    assert step.kind == "tool" and step.status == StepStatus.SUCCEEDED
    assert step.decision_rationale == "need candidate sources"
    assert step.idempotency_key

    types = [e.event_type for e in trace.events]
    assert EventType.TOOL_CALL_STARTED in types
    assert EventType.TOOL_CALL_COMPLETED in types
    assert EventType.BUDGET_UPDATED in types


def test_rationale_is_persisted_so_tool_choice_is_auditable(executor, trace):
    executor.call("web_search", {"query": "x"}, rationale="第一轮检索：尚无候选来源")
    started = [e for e in trace.events if e.event_type == EventType.TOOL_CALL_STARTED][-1]
    assert started.payload["rationale"] == "第一轮检索：尚无候选来源"
    assert started.payload["when_to_use"]


def test_invalid_arguments_are_blocked_without_calling_the_tool(executor, library, trace):
    result = executor.call("web_search", {"max_results": 5})  # missing required `query`
    assert result.ok is False
    assert result.error_class == ErrorClass.INVALID_ARGUMENT.value
    assert library.calls_to("web_search") == []
    assert trace.of_type(EventType.TOOL_CALL_BLOCKED)


def test_out_of_range_argument_is_rejected(executor, library):
    result = executor.call("web_search", {"query": "x", "max_results": 999})
    assert result.ok is False and result.error_class == ErrorClass.INVALID_ARGUMENT.value
    assert library.calls_to("web_search") == []


def test_unknown_tool_is_not_found(executor):
    result = executor.call("definitely_not_a_tool", {})
    assert result.ok is False and result.error_class == ErrorClass.NOT_FOUND.value


def test_transient_failure_is_retried_then_succeeds(executor, library, trace):
    library.script("web_search", [{"raise": "read timed out"}])
    result = executor.call("web_search", {"query": "示例公司"})
    assert result.ok is True
    assert result.attempts == 2
    assert trace.of_type(EventType.RETRY_SCHEDULED)


def test_auth_failure_is_not_retried(executor, library):
    library.script("web_search", [{"raise": "401 Unauthorized: invalid api key"}] * 3)
    result = executor.call("web_search", {"query": "示例公司"})
    assert result.ok is False
    assert result.error_class == ErrorClass.AUTH_ERROR.value
    assert result.attempts == 1, "auth errors must not burn retry budget"


def test_in_band_failure_envelope_is_classified_as_an_error(executor):
    """`{"success": false}` is how the legacy tools report failure. It must not
    be mistaken for a successful call."""
    result = executor.call("read_webpage", {"url": "https://unknown.invalid/page.html"})
    assert result.ok is False
    assert result.error_class == ErrorClass.NOT_FOUND.value


def test_pdf_parse_failure_is_permanent_not_retried(executor, library):
    result = executor.call("read_pdf", {"url": "https://example.invalid/scan.pdf"})
    assert result.ok is False
    assert result.attempts == 1
    assert len(library.calls_to("read_pdf")) == 1


def test_duplicate_call_is_served_from_memo(executor, library, trace, run_state):
    executor.call("web_search", {"query": "示例公司"})
    second = executor.call("web_search", {"query": "示例公司"})
    assert second.deduped is True and second.ok is True
    assert len(library.calls_to("web_search")) == 1
    assert trace.of_type(EventType.TOOL_CALL_DEDUPED)
    assert run_state.redundant_tool_calls() == 0  # skipped steps are not counted twice


def test_budget_exhaustion_blocks_further_tool_calls(run_state, trace, registry, artifacts):
    from src.tools.executor import ToolExecutor

    executor = ToolExecutor(run_state=run_state, trace=trace, registry=registry,
                            budget=BudgetTracker(BudgetLimits(max_tool_calls=1)),
                            artifacts=artifacts, sleep=lambda _s: None)
    assert executor.call("web_search", {"query": "a"}).ok is True
    blocked = executor.call("web_search", {"query": "b"})
    assert blocked.ok is False
    assert blocked.error_class == ErrorClass.BUDGET_EXCEEDED.value
    assert trace.of_type(EventType.BUDGET_EXCEEDED)
    executor.close()


def test_circuit_opens_after_repeated_provider_failures(run_state, trace, registry, artifacts, library):
    from src.tools.executor import ToolExecutor

    executor = ToolExecutor(
        run_state=run_state, trace=trace, registry=registry,
        budget=BudgetTracker(BudgetLimits()), artifacts=artifacts,
        circuits=CircuitRegistry(failure_threshold=2, cooldown_s=300.0),
        retry_policy=RetryPolicy(max_attempts=1, jitter=False), sleep=lambda _s: None)

    library.script("web_search", [{"raise": "connection reset by peer"}] * 6)
    executor.call("web_search", {"query": "a"})
    executor.call("web_search", {"query": "b"})
    third = executor.call("web_search", {"query": "c"})
    assert third.ok is False
    assert "circuit open" in third.blocked_reason
    assert trace.of_type(EventType.CIRCUIT_OPENED)
    executor.close()


def test_side_effecting_tool_requires_approval(run_state, trace, registry, artifacts, tmp_path):
    from src.tools.executor import ToolExecutor

    executor = ToolExecutor(run_state=run_state, trace=trace, registry=registry,
                            budget=BudgetTracker(BudgetLimits()), artifacts=artifacts,
                            side_effects=SideEffectPolicy(approval_required=True),
                            sleep=lambda _s: None)
    target = tmp_path / "out.md"
    blocked = executor.call("export_report_file", {"content": "# hi", "path": str(target)})
    assert blocked.ok is False and blocked.error_class == ErrorClass.POLICY_BLOCKED.value
    assert not target.exists()
    assert trace.of_type(EventType.APPROVAL_REQUESTED)

    executor.side_effects.granted["export_report_file"] = True
    approved = executor.call("export_report_file", {"content": "# hi", "path": str(target)})
    assert approved.ok is True and target.exists()
    executor.close()


def test_cancellation_blocks_new_calls(executor):
    executor.cancellation.cancel("stopped")
    result = executor.call("web_search", {"query": "x"})
    assert result.ok is False and result.error_class == ErrorClass.POLICY_BLOCKED.value


def test_large_payload_is_externalized_not_inlined(executor, artifacts):
    result = executor.call("read_webpage",
                           {"url": "https://www.cninfo.com.cn/fixture/annual.html"})
    assert result.ok is True
    assert result.artifact_id
    assert "content" not in result.content            # compact projection only
    assert result.content["content_chars"] > 0
    full = artifacts.get(result.artifact_id)
    assert full["content"].startswith("示例公司2024年")


def test_deduped_result_still_resolves_to_the_full_payload(executor, artifacts):
    """A dedup hit must hand callers the same thing a fresh call would.

    The memo stores the *compact projection*, but `tools/tool_gateway._dispatch`
    gives callers the full payload via `artifact_id`. When the dedup branch
    returned no artifact id, `_dispatch` fell through to the projection - a dict
    with no `content` key - and BrowserAgent, which reads `parsed["content"]`,
    silently dropped every source. On a resumed run that turned every fetch into
    a lost source and the run ended `no_usable_sources`.
    """
    url = "https://www.cninfo.com.cn/fixture/annual.html"
    first = executor.call("read_webpage", {"url": url})
    assert first.ok and first.artifact_id

    second = executor.call("read_webpage", {"url": url})
    assert second.deduped is True
    assert second.artifact_id == first.artifact_id, \
        "a deduped result must still point at the full payload"

    full = artifacts.get(second.artifact_id)
    assert "content" in full and full["content"], \
        "callers read parsed['content']; the dedup path must not lose it"


def test_gateway_returns_full_payload_on_a_dedup_hit(run_state, trace, registry, artifacts):
    """End-to-end through the real gateway hook, not just the executor."""
    from src.runtime.run_context import bind_run, force_unbind
    from src.tools.executor import ToolExecutor
    from tools.tool_gateway import read_webpage

    url = "https://www.cninfo.com.cn/fixture/annual.html"
    ex = ToolExecutor(run_state=run_state, trace=trace, registry=registry, artifacts=artifacts)
    try:
        with bind_run(ex):
            first = read_webpage(url)
            second = read_webpage(url)  # dedup hit
    finally:
        ex.close()
        force_unbind()

    assert first.get("content"), "first fetch should return real content"
    assert second.get("content") == first.get("content"), \
        "a cached/deduped fetch must look identical to the caller"


def test_model_call_accounting(executor, run_state, trace):
    executor.record_model_call(model="gpt-4o-mini", input_text="a" * 4000,
                               output_text="b" * 400, duration_s=1.2)
    assert run_state.budget.model_calls == 1
    assert run_state.budget.tokens_in > 0
    event = trace.of_type(EventType.MODEL_CALL_COMPLETED)[-1]
    assert event.payload["tokens_estimated"] is True
    assert event.payload["estimated_cost_usd"] >= 0.0

    executor.record_model_call(model="gpt-4o-mini", input_tokens=100, output_tokens=50,
                               duration_s=0.5)
    assert trace.of_type(EventType.MODEL_CALL_COMPLETED)[-1].payload["tokens_estimated"] is False


def test_model_call_failure_is_recorded(executor, trace):
    executor.record_model_call(model="gpt-4o-mini", input_text="x",
                               error=TimeoutError("request timed out"))
    event = trace.of_type(EventType.MODEL_CALL_FAILED)[-1]
    assert event.error_class == ErrorClass.TIMEOUT.value


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
def test_secrets_are_redacted_by_key_and_by_shape():
    payload = {
        "api_key": "sk-abcdefghijklmnopqrstuvwxyz",
        "Authorization": "Bearer abcdefghijklmnopqrstuvwx",
        "cookie": "session=xyz",
        "nested": {"openai_api_key": "sk-zzzz", "safe": "ok"},
        "free_text": "curl -H 'Authorization: Bearer abcdefghijklmnopqrstuv' https://x",
        "list": ["sk-aaaaaaaaaaaaaaaaaaaaaa", "harmless"],
    }
    cleaned = redact(payload)
    assert cleaned["api_key"] == REDACTED
    assert cleaned["Authorization"] == REDACTED
    assert cleaned["cookie"] == REDACTED
    assert cleaned["nested"]["openai_api_key"] == REDACTED
    assert cleaned["nested"]["safe"] == "ok"
    assert "sk-abcdefghij" not in json.dumps(cleaned)
    assert REDACTED in cleaned["free_text"]
    assert cleaned["list"][0] == REDACTED and cleaned["list"][1] == "harmless"


def test_reasoning_fields_are_dropped_not_stored():
    """This project must not persist chain-of-thought."""
    cleaned = redact({"reasoning_content": "step 1 ... step 2 ...",
                      "chain_of_thought": "hidden", "thinking": "hidden",
                      "output_summary": "visible"})
    assert cleaned["reasoning_content"] == DROPPED
    assert cleaned["chain_of_thought"] == DROPPED
    assert cleaned["thinking"] == DROPPED
    assert cleaned["output_summary"] == "visible"


def test_jwt_and_cloud_keys_are_masked():
    text = "token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U key AKIAIOSFODNN7EXAMPLE"
    masked = redact_text(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in masked
    assert "AKIAIOSFODNN7EXAMPLE" not in masked


def test_trace_store_redacts_before_writing(tmp_path):
    path = tmp_path / "trace.jsonl"
    store = TraceStore(path, artifacts=ArtifactStore(tmp_path / "a"), run_id="r1")
    store.event(EventType.TOOL_CALL_STARTED, name="web_search",
                payload={"api_key": "sk-shouldnotappear1234567", "query": "ok"})
    store.close()
    raw = path.read_text(encoding="utf-8")
    assert "sk-shouldnotappear" not in raw
    assert REDACTED in raw

    events = TraceStore.read(path)
    assert len(events) == 1 and events[0]["seq"] == 1 and events[0]["run_id"] == "r1"


def test_trace_reader_skips_a_truncated_tail(tmp_path):
    """A killed writer leaves a partial last line; it must not poison the read."""
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps({"event_type": "run_started", "run_id": "r"}) + "\n{\"event_type\": \"tru",
        encoding="utf-8")
    assert len(TraceStore.read(path)) == 1


def test_oversized_payload_is_moved_to_the_artifact_store(tmp_path):
    artifacts = ArtifactStore(tmp_path / "a")
    store = TraceStore(tmp_path / "t.jsonl", artifacts=artifacts, run_id="r")
    event = store.event(EventType.TOOL_CALL_COMPLETED, name="read_webpage",
                        payload={"body": "x" * 9000})
    assert "externalized" in event.payload["body"]
    assert artifacts.get(event.artifact_id)["body"].startswith("x")


def test_artifact_store_is_content_addressed(tmp_path):
    store = ArtifactStore(tmp_path)
    first = store.put({"a": 1}, kind="k")
    second = store.put({"a": 1}, kind="k")
    assert first == second and store.count() == 1
    assert store.put({"a": 2}, kind="k") != first


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def test_percentile_nearest_rank():
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 5
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10
    assert percentile([], 95) == 0.0


def test_run_metrics_derive_everything_from_events(executor, trace, library):
    library.script("web_search", [{"raise": "read timed out"}])
    executor.call("web_search", {"query": "a"})
    executor.call("read_webpage", {"url": "https://unknown.invalid/x.html"})
    executor.call("web_search", {"query": "a"})  # dedup
    trace.event(EventType.RUN_COMPLETED, payload={"is_success": True, "duration_s": 12.5,
                                                  "stop_reason": "completed"})

    metrics = RunMetrics(trace.as_dicts()).summary()
    assert metrics["succeeded"] is True
    assert metrics["stop_reason"] == "completed"
    assert metrics["duration_s"] == 12.5
    assert metrics["by_tool"]["web_search"]["ok"] == 1
    assert metrics["by_tool"]["read_webpage"]["failed"] == 1
    assert metrics["redundant_tool_calls"] == 1
    assert metrics["recovery"]["retries_scheduled"] == 1
    assert metrics["by_error"]["NOT_FOUND"] == 1


def test_absence_of_a_failure_event_is_not_success(trace):
    """A killed process leaves no run_completed event; that must read as
    'not succeeded', never as success by default."""
    trace.event(EventType.RUN_STARTED)
    trace.event(EventType.TOOL_CALL_COMPLETED, name="web_search")
    assert RunMetrics(trace.as_dicts()).succeeded is False


def test_suite_metrics_aggregate_across_runs(trace):
    trace.event(EventType.RUN_COMPLETED, payload={"is_success": True, "duration_s": 10,
                                                  "stop_reason": "completed"})
    one = RunMetrics(trace.as_dicts())

    other = RunMetrics([
        {"event_type": "tool_call_failed", "name": "read_pdf", "error_class": "TIMEOUT",
         "duration_s": 3, "step_id": "s1"},
        {"event_type": "run_failed", "payload": {"is_success": False, "duration_s": 30,
                                                 "stop_reason": "no_usable_sources"}},
    ])
    agg = SuiteMetrics([one, other]).aggregate()
    assert agg["runs"] == 2
    assert agg["task_success_rate"] == 0.5
    assert agg["stop_reasons"]["no_usable_sources"] == 1
    assert agg["by_error"]["TIMEOUT"] == 1


def test_multi_trial_stats_expose_the_worst_run():
    stats = multi_trial_stats([0.9, 0.85, 0.2])
    assert stats["trials"] == 3
    assert stats["worst"] == 0.2
    assert stats["stdev"] > 0
