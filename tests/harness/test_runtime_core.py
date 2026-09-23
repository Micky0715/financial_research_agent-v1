"""Unit tests for the runtime primitives: error taxonomy, state serialization,
budget accounting, retry/backoff, circuit breaking, dedup and idempotency."""
from __future__ import annotations

import json

import pytest

from src.runtime.budget import (
    BudgetLimits,
    BudgetTracker,
    DegradeAction,
    estimate_cost,
    estimate_tokens,
)
from src.runtime.errors import (
    CIRCUIT_TRIPPING,
    BudgetExceededError,
    ErrorClass,
    HarnessError,
    classify_exception,
    classify_http_status,
    is_retryable,
)
from src.runtime.policies import (
    CancellationToken,
    CircuitBreaker,
    CircuitRegistry,
    CircuitState,
    ConcurrencyLimiter,
    DuplicateDetector,
    RetryPolicy,
    SideEffectPolicy,
)
from src.runtime.state import (
    SCHEMA_VERSION,
    AttemptRecord,
    BudgetState,
    EvidenceRecord,
    FinalOutcome,
    Phase,
    RunState,
    RunStatus,
    StepRecord,
    StepStatus,
    StopReason,
    summarize,
)


# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("slow"), ErrorClass.TIMEOUT),
    (RuntimeError("HTTP 429 Too Many Requests"), ErrorClass.RATE_LIMIT),
    (RuntimeError("401 Unauthorized: invalid api key"), ErrorClass.AUTH_ERROR),
    (RuntimeError("404 not found"), ErrorClass.NOT_FOUND),
    (RuntimeError("Connection reset by peer"), ErrorClass.TRANSIENT_NETWORK),
    (ValueError("bad"), ErrorClass.INVALID_ARGUMENT),
    (json.JSONDecodeError("Expecting value", "", 0), ErrorClass.SCHEMA_ERROR),
    (RuntimeError("something nobody anticipated"), ErrorClass.UNKNOWN),
])
def test_classify_exception(exc, expected):
    assert classify_exception(exc) is expected


def test_unknown_errors_are_not_retryable():
    """An unclassified error must never be retried: silently retrying an
    unrecognised failure is how one provider bug becomes a bill."""
    assert not is_retryable(ErrorClass.UNKNOWN)
    assert not is_retryable(ErrorClass.AUTH_ERROR)
    assert not is_retryable(ErrorClass.INVALID_ARGUMENT)
    assert not is_retryable(ErrorClass.POLICY_BLOCKED)
    assert not is_retryable(ErrorClass.BUDGET_EXCEEDED)
    assert is_retryable(ErrorClass.TIMEOUT)
    assert is_retryable(ErrorClass.RATE_LIMIT)
    assert is_retryable(ErrorClass.TRANSIENT_NETWORK)


def test_harness_errors_carry_their_own_class():
    err = BudgetExceededError("out of money", details={"dimension": "cost_usd"})
    assert classify_exception(err) is ErrorClass.BUDGET_EXCEEDED
    assert err.to_dict()["retryable"] is False
    assert err.to_dict()["details"]["dimension"] == "cost_usd"


@pytest.mark.parametrize("status,expected", [
    (401, ErrorClass.AUTH_ERROR), (403, ErrorClass.AUTH_ERROR),
    (404, ErrorClass.NOT_FOUND), (429, ErrorClass.RATE_LIMIT),
    (500, ErrorClass.TRANSIENT_NETWORK), (400, ErrorClass.INVALID_ARGUMENT),
])
def test_classify_http_status(status, expected):
    assert classify_http_status(status) is expected


def test_not_found_does_not_trip_the_circuit():
    """A 404 is a fact about the web, not evidence the provider is down."""
    assert ErrorClass.NOT_FOUND not in CIRCUIT_TRIPPING
    assert ErrorClass.INVALID_ARGUMENT not in CIRCUIT_TRIPPING
    assert ErrorClass.TIMEOUT in CIRCUIT_TRIPPING


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def test_run_state_round_trips_through_a_checkpoint():
    state = RunState(topic="比亚迪", report_type="company_research", requirements=["财务分析"])
    step = StepRecord(name="web_search", kind="tool", phase=Phase.RESEARCH,
                      idempotency_key="k1", status=StepStatus.SUCCEEDED)
    step.attempts.append(AttemptRecord(attempt=1, tool_name="web_search", tool_status="ok"))
    state.add_step(step)
    state.add_evidence(EvidenceRecord(claim="营收", value="1200亿元", source_id="s1", url="http://a"))
    state.budget.tokens_in = 100

    payload = state.to_checkpoint()
    assert json.loads(json.dumps(payload, ensure_ascii=False))  # must be JSON-safe

    restored = RunState.from_checkpoint(payload)
    assert restored.run_id == state.run_id
    assert restored.topic == "比亚迪"
    assert len(restored.steps) == 1
    assert restored.steps[0].idempotency_key == "k1"
    assert len(restored.evidence) == 1
    assert restored.budget.tokens_in == 100
    assert restored.checkpoint_version == 1


def test_incompatible_checkpoint_schema_is_refused():
    """Silently reinterpreting an old checkpoint is how resume produces a
    subtly wrong report - it must fail loudly instead."""
    payload = RunState(topic="x").to_checkpoint()
    payload["schema_version"] = "99.0"
    with pytest.raises(ValueError, match="incompatible"):
        RunState.from_checkpoint(payload)
    assert SCHEMA_VERSION.startswith("1.")


def test_evidence_dedup():
    state = RunState(topic="x")
    record = EvidenceRecord(claim="营收 2024", value="1200.5亿元", source_id="s1", url="http://a")
    assert state.add_evidence(record) is True
    assert state.add_evidence(record.model_copy()) is False
    assert len(state.evidence) == 1
    other = EvidenceRecord(claim="营收 2024", value="1300.0亿元", source_id="s1", url="http://a")
    assert state.add_evidence(other) is True


def test_redundant_tool_call_counting_and_recovery():
    state = RunState(topic="x")
    for _ in range(3):
        state.add_step(StepRecord(kind="tool", name="web_search", idempotency_key="same",
                                  status=StepStatus.SUCCEEDED))
    assert state.redundant_tool_calls() == 2

    recovered = StepRecord(kind="tool", name="read_webpage", status=StepStatus.SUCCEEDED)
    recovered.attempts = [AttemptRecord(attempt=1, status=StepStatus.FAILED),
                          AttemptRecord(attempt=2, status=StepStatus.SUCCEEDED)]
    state.add_step(recovered)
    assert recovered.recovered is True
    assert state.recovery_stats() == (1, 1)


def test_summarize_truncates_and_marks_truncation():
    text = summarize("x" * 5000, limit=100)
    assert len(text) < 200
    assert "truncated" in text


def test_failed_run_is_never_recorded_as_success():
    outcome = FinalOutcome(status=RunStatus.INSUFFICIENT, stop_reason=StopReason.NO_USABLE_SOURCES)
    assert outcome.is_success is False
    assert FinalOutcome(status=RunStatus.DEGRADED).is_success is True
    assert FinalOutcome(status=RunStatus.FAILED).is_success is False


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #
def test_budget_charges_and_blocks():
    tracker = BudgetTracker(BudgetLimits(max_tool_calls=3, max_model_calls=2,
                                         max_total_tokens=1000, max_cost_usd=None,
                                         max_duration_s=None))
    for _ in range(3):
        assert tracker.can_call_tool("web_search")[0] is True
        tracker.charge_tool("web_search")
    allowed, reason = tracker.can_call_tool("web_search")
    assert allowed is False and "tool_calls" in reason
    assert "tool_calls" in tracker.state.exceeded
    assert tracker.state.tool_calls_by_name["web_search"] == 3


def test_per_tool_budget_limit():
    tracker = BudgetTracker(BudgetLimits(max_calls_per_tool={"read_pdf": 1}, max_tool_calls=100))
    tracker.charge_tool("read_pdf")
    allowed, reason = tracker.can_call_tool("read_pdf")
    assert allowed is False and "read_pdf" in reason
    assert tracker.can_call_tool("web_search")[0] is True


def test_unpriced_model_contributes_no_fabricated_cost():
    tracker = BudgetTracker(BudgetLimits())
    cost = tracker.charge_model("some-unlisted-model", 1000, 500)
    assert cost == 0.0
    assert "some-unlisted-model" in tracker.unpriced_models
    priced, known = estimate_cost("gpt-4o-mini", 1_000_000, 0)
    assert known is True and priced == pytest.approx(0.15)


def test_degradation_ladder_orders_concessions():
    limits = BudgetLimits(max_total_tokens=1000, max_cost_usd=None, max_duration_s=None,
                          max_model_calls=None, max_tool_calls=None)
    tracker = BudgetTracker(limits)

    tracker.state.tokens_in = 100
    assert tracker.recommend().action is DegradeAction.CONTINUE
    tracker.state.tokens_in = 500
    assert tracker.recommend().action is DegradeAction.NARROW_SEARCH
    tracker.state.tokens_in = 650
    assert tracker.recommend().action is DegradeAction.DOWNGRADE_MODEL
    tracker.state.tokens_in = 800
    assert tracker.recommend().action is DegradeAction.SKIP_OPTIONAL
    tracker.state.tokens_in = 950
    assert tracker.recommend(has_evidence=True).action is DegradeAction.FINALIZE_WITH_CURRENT
    tracker.state.tokens_in = 1200
    assert tracker.recommend(has_evidence=True).action is DegradeAction.FINALIZE_WITH_CURRENT
    assert tracker.recommend(has_evidence=False).action is DegradeAction.RETURN_INSUFFICIENT


def test_supplementary_round_cap():
    tracker = BudgetTracker(BudgetLimits(max_supplementary_rounds=1))
    assert tracker.can_supplement() is True
    tracker.note_supplementary_round()
    assert tracker.can_supplement() is False


def test_estimate_tokens_handles_cjk_and_ascii():
    assert estimate_tokens("中文字符测试") >= 6
    assert estimate_tokens("a" * 400) == pytest.approx(101, abs=2)
    assert estimate_tokens("") == 0


# --------------------------------------------------------------------------- #
# Retry / backoff
# --------------------------------------------------------------------------- #
def test_retry_policy_only_retries_transient_classes():
    policy = RetryPolicy(max_attempts=3)
    assert policy.should_retry(ErrorClass.TIMEOUT, 1) is True
    assert policy.should_retry(ErrorClass.AUTH_ERROR, 1) is False
    assert policy.should_retry(ErrorClass.INVALID_ARGUMENT, 1) is False
    assert policy.should_retry(ErrorClass.TIMEOUT, 3) is False  # attempt cap


def test_backoff_is_exponential_capped_and_rate_limit_aware():
    policy = RetryPolicy(base_delay_s=1.0, multiplier=2.0, max_delay_s=8.0, jitter=False)
    assert [policy.backoff(n) for n in (1, 2, 3, 4, 5)] == [1.0, 2.0, 4.0, 8.0, 8.0]
    assert policy.backoff(1, ErrorClass.RATE_LIMIT) >= policy.rate_limit_min_delay_s


def test_jitter_stays_within_the_computed_bound():
    import random

    policy = RetryPolicy(base_delay_s=4.0, multiplier=1.0, max_delay_s=4.0, jitter=True)
    rng = random.Random(7)
    samples = [policy.backoff(1, rng=rng) for _ in range(50)]
    assert all(0.0 <= s <= 4.0 for s in samples)
    assert len(set(samples)) > 1  # actually jittered


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
def test_circuit_opens_after_threshold_then_half_opens_and_closes():
    now = {"t": 0.0}
    breaker = CircuitBreaker("web_search", failure_threshold=3, cooldown_s=10.0,
                             clock=lambda: now["t"])

    assert breaker.allow()[0] is True
    for _ in range(2):
        assert breaker.record_failure(ErrorClass.TIMEOUT) is False
    assert breaker.record_failure(ErrorClass.TIMEOUT) is True
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow()[0] is False

    now["t"] = 11.0
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.allow()[0] is True
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.open_count == 1


def test_half_open_failure_reopens_immediately():
    now = {"t": 0.0}
    breaker = CircuitBreaker("t", failure_threshold=2, cooldown_s=5.0, clock=lambda: now["t"])
    breaker.record_failure(ErrorClass.TIMEOUT)
    breaker.record_failure(ErrorClass.TIMEOUT)
    now["t"] = 6.0
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.record_failure(ErrorClass.TIMEOUT) is True
    assert breaker.state is CircuitState.OPEN


def test_non_provider_errors_never_trip_the_circuit():
    breaker = CircuitBreaker("t", failure_threshold=1)
    assert breaker.record_failure(ErrorClass.NOT_FOUND) is False
    assert breaker.record_failure(ErrorClass.INVALID_ARGUMENT) is False
    assert breaker.state is CircuitState.CLOSED


def test_circuit_registry_tracks_open_keys():
    registry = CircuitRegistry(failure_threshold=1, cooldown_s=100.0)
    registry.get("read_pdf").record_failure(ErrorClass.TIMEOUT)
    assert registry.open_keys() == ["read_pdf"]
    assert registry.get("read_pdf") is registry.get("read_pdf")  # same instance


# --------------------------------------------------------------------------- #
# Dedup / idempotency / side effects / cancellation
# --------------------------------------------------------------------------- #
def test_duplicate_detector_counts_waste_not_first_calls():
    detector = DuplicateDetector()
    detector.remember("fp1", {"x": 1})
    assert detector.duplicate_count() == 0
    detector.get("fp1")
    detector.get("fp1")
    assert detector.duplicate_count() == 2
    assert detector.snapshot()["fp1"] == 3


def test_side_effect_policy_gates_only_mutating_tools():
    policy = SideEffectPolicy(approval_required=True)
    assert policy.check("web_search", side_effect_free=True) == (True, "")
    allowed, reason = policy.check("export_report_file", side_effect_free=False)
    assert allowed is False and "approval" in reason
    policy.granted["export_report_file"] = True
    assert policy.check("export_report_file", side_effect_free=False)[0] is True


def test_dry_run_blocks_writes_even_when_approved():
    policy = SideEffectPolicy(dry_run=True, granted={"export_report_file": True})
    allowed, reason = policy.check("export_report_file", side_effect_free=False)
    assert allowed is False and "dry_run" in reason


def test_cancellation_token():
    token = CancellationToken()
    assert token.cancelled is False
    token.raise_if_cancelled()
    token.cancel("user pressed stop")
    assert token.cancelled is True
    with pytest.raises(HarnessError, match="user pressed stop"):
        token.raise_if_cancelled()


def test_concurrency_limiter_releases_slots_on_exception():
    limiter = ConcurrencyLimiter(max_global=1)
    with pytest.raises(RuntimeError):
        with limiter.acquire("web_search", timeout=1):
            raise RuntimeError("boom")
    with limiter.acquire("web_search", timeout=1):
        pass  # slot was released despite the exception


def test_budget_state_totals():
    state = BudgetState(tokens_in=10, tokens_out=5)
    assert state.total_tokens == 15


def test_empty_content_is_a_permanent_failure_not_unknown():
    """Found by the live canary, not by a fixture.

    `tools/web_reader.py` returns `empty_content_after_cleaning` when a page
    fetches fine but yields no text; the PDF reader returns "no text layer" for
    scans. Retrying cannot create text, so both are PERMANENT_FAILURE. They
    used to fall through to UNKNOWN - non-retryable for the right reason by
    accident, but mislabelled in every metric that groups by error class.
    """
    for message in ("empty_content_after_cleaning",
                    "pdf parse failed: no text layer",
                    "content is empty"):
        error_class = classify_exception(RuntimeError(message))
        assert error_class is ErrorClass.PERMANENT_FAILURE, message
        assert not is_retryable(error_class), message


def test_the_new_empty_content_rule_does_not_shadow_other_classes():
    """The rule sits first in _MESSAGE_RULES, so verify it stays narrow."""
    assert classify_exception(RuntimeError("HTTP 429 rate limit")) is ErrorClass.RATE_LIMIT
    assert classify_exception(RuntimeError("403 Forbidden")) is ErrorClass.AUTH_ERROR
    assert classify_exception(RuntimeError("read timed out")) is ErrorClass.TIMEOUT
    assert classify_exception(RuntimeError("connection reset by peer")) is ErrorClass.TRANSIENT_NETWORK
