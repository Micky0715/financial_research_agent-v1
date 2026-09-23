"""Tests for the offline replay of recorded live-run error classes.

The replay is the only way a classifier change can be validated against real
provider output without re-spending money on a live run, so its accounting has
to be trustworthy: it must not count message-less rows as agreement, and it
must report the before/after distributions separately.
"""
from __future__ import annotations

import json
import re

from scripts.replay_canary_error_classes import collect_failures, replay


def test_a_real_empty_content_message_is_reclassified():
    """The exact message the live canary produced 5 times."""
    report = replay([{
        "task_id": "canary_co_byd", "tool": "read_webpage",
        "recorded_class": "UNKNOWN", "message": "empty_content_after_cleaning",
        "trace": "t.jsonl",
    }])
    assert report["reclassified"][0]["current_class"] == "PERMANENT_FAILURE"
    assert report["recorded_distribution"] == {"UNKNOWN": 1}
    assert report["current_distribution"] == {"PERMANENT_FAILURE": 1}


def test_other_real_messages_keep_their_class():
    """No shadowing: the new rule must not steal timeouts, 404s or resets."""
    real_messages = [
        ("TIMEOUT", "TimeoutError: web_search exceeded 20.0s; handler thread abandoned"),
        ("NOT_FOUND", "404 Client Error: Not Found for url: https://example.test/404.html"),
        ("TRANSIENT_NETWORK",
         "HTTPSConnectionPool(host='example.test', port=443): Max retries exceeded with url: /r/1"),
    ]
    report = replay([
        {"task_id": "t", "tool": "read_webpage", "recorded_class": cls,
         "message": msg, "trace": "t.jsonl"}
        for cls, msg in real_messages
    ])
    assert report["reclassified"] == []
    assert report["replayable"] == 3


def test_message_less_rows_are_not_counted_as_agreement():
    """A failure with no persisted message is not evidence either way.

    Counting it as "unchanged" would inflate the agreement rate, which is the
    number a reviewer would use to decide whether the rules are stable.
    """
    report = replay([
        {"task_id": "t", "tool": "x", "recorded_class": "UNKNOWN", "message": "",
         "trace": "t.jsonl"},
        {"task_id": "t", "tool": "x", "recorded_class": "TIMEOUT",
         "message": "timed out", "trace": "t.jsonl"},
    ])
    assert report["total_failures"] == 2
    assert report["replayable"] == 1
    assert report["unclassifiable_no_message"] == 1
    assert report["recorded_distribution"] == {"TIMEOUT": 1}


def test_collect_failures_reads_top_level_event_fields(tmp_path):
    """Guards a real mistake: `error_class` / `error_message` / `name` live at
    the event top level, not inside `payload`. A collector reading `payload`
    silently returns None for every field and the replay looks like a no-op."""
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "event_type": "tool_call_failed", "name": "read_webpage",
        "error_class": "UNKNOWN", "error_message": "empty_content_after_cleaning",
        "payload": {"attempts": 1, "retryable": False},
    }) + "\n" + json.dumps({
        "event_type": "tool_call_completed", "name": "web_search",
    }) + "\n", encoding="utf-8")

    results = tmp_path / "results.jsonl"
    results.write_text(json.dumps({"task_id": "t1", "trace_path": str(trace)}) + "\n",
                       encoding="utf-8")

    failures = collect_failures(results)
    assert len(failures) == 1, "only tool_call_failed events are failures"
    assert failures[0]["recorded_class"] == "UNKNOWN"
    assert failures[0]["message"] == "empty_content_after_cleaning"
    assert failures[0]["tool"] == "read_webpage"


def test_collect_failures_tolerates_missing_traces(tmp_path):
    results = tmp_path / "results.jsonl"
    results.write_text(
        json.dumps({"task_id": "t1", "trace_path": str(tmp_path / "gone.jsonl")}) + "\n"
        + json.dumps({"task_id": "t2"}) + "\n" + "not json\n", encoding="utf-8")
    assert collect_failures(results) == []


def test_replayed_evidence_file_is_repo_relative():
    """No absolute Windows user paths may reach a committed evidence file."""
    from pathlib import Path

    evidence = Path(__file__).resolve().parents[2] / "docs" / "_evidence" / "canary_error_replay.json"
    if not evidence.exists():
        return
    text = evidence.read_text(encoding="utf-8")
    # A drive letter followed by a separator is what an absolute Windows
    # path looks like; plain "Users" can legitimately appear in a URL.
    # A *single-letter* drive plus separator. The lookbehind matters: without
    # it, the "s:/" inside every "https://" URL matches and the test is
    # permanently red for the wrong reason.
    assert not re.search(r"(?<![A-Za-z])[A-Za-z]:[/\\]", text), "absolute path leaked"
    payload = json.loads(text)
    for entry in payload["reclassified"]:
        assert not entry["trace"].startswith(("/", "C:")), entry["trace"]


def test_recorded_and_current_distributions_cover_the_same_calls():
    """The two columns of the canary report's 4.2 table must be subtractable.

    The first version of that table put per-attempt counts
    (`live_metrics.by_error`) next to per-failed-call counts (trace events) and
    showed TRANSIENT_NETWORK as 10 vs 5 - a pure denominator artefact. Both
    columns now come from `replay()`, so their totals must match exactly.
    """
    failures = [
        {"task_id": "t", "tool": "read_webpage", "recorded_class": "UNKNOWN",
         "message": "empty_content_after_cleaning", "trace": ""},
        {"task_id": "t", "tool": "web_search", "recorded_class": "TIMEOUT",
         "message": "TimeoutError: web_search exceeded 20.0s", "trace": ""},
        {"task_id": "t", "tool": "read_webpage", "recorded_class": "NOT_FOUND",
         "message": "404 Client Error: Not Found for url: https://example.test/x", "trace": ""},
    ]
    report = replay(failures)
    assert sum(report["recorded_distribution"].values()) == 3
    assert sum(report["current_distribution"].values()) == 3
    # And the only difference is the rule that was actually changed.
    before, after = report["recorded_distribution"], report["current_distribution"]
    moved = {k: after.get(k, 0) - before.get(k, 0)
             for k in set(before) | set(after) if after.get(k, 0) != before.get(k, 0)}
    assert moved == {"UNKNOWN": -1, "PERMANENT_FAILURE": 1}


def test_canary_report_builder_keeps_the_two_denominators_apart():
    """Guards the report text itself, not just the data."""
    from pathlib import Path

    report = Path(__file__).resolve().parents[2] / "evals" / "reports" / "live_canary_report.md"
    if not report.exists():
        return
    text = report.read_text(encoding="utf-8")
    assert "### 4.1 按**尝试**计（含重试）" in text
    assert "### 4.2 按**失败的工具调用**计" in text
    assert "分母相同、可以相减" in text


def test_a_nonstandard_4xx_from_a_real_waf_is_permanent():
    """The live canary produced a real `468 Client Error` from an anti-bot front
    end. 468 is not a registered status code, so nothing matched it and it fell
    to UNKNOWN. A 4xx means the request as sent is unacceptable, so replaying it
    unchanged cannot succeed."""
    report = replay([{
        "task_id": "canary_ind_lowalt", "tool": "read_webpage", "recorded_class": "UNKNOWN",
        "message": "468 Client Error:  for url: https://info.example.test/open/article/detail/516910670613135360.html",
        "trace": "",
    }])
    assert report["current_distribution"] == {"PERMANENT_FAILURE": 1}


def test_the_generic_4xx_rule_does_not_shadow_the_retryable_ones():
    """429 and 408 are 4xx but retryable, and 401/403/404 have their own class.

    The generic rule sits last in `_MESSAGE_RULES` precisely so it can only
    catch what nothing more specific claimed. If someone reorders the tuple,
    this fails.
    """
    expectations = {
        "429 Client Error: Too Many Requests for url: https://x.test/c": "RATE_LIMIT",
        "408 Client Error: Request Timeout for url: https://x.test/d": "TIMEOUT",
        "404 Client Error: Not Found for url: https://x.test/a": "NOT_FOUND",
        "403 Client Error: Forbidden for url: https://x.test/b": "AUTH_ERROR",
        "500 Server Error: Internal Server Error for url: https://x.test/e": "TRANSIENT_NETWORK",
    }
    for message, expected in expectations.items():
        report = replay([{"task_id": "t", "tool": "read_webpage",
                          "recorded_class": expected, "message": message, "trace": ""}])
        assert report["reclassified"] == [], f"{message} moved to {report['current_distribution']}"


def test_the_4xx_rule_does_not_fire_on_a_bare_number():
    """`4\d\d` alone would match stray numbers; the rule requires the
    "client error" phrasing so ordinary messages stay UNKNOWN."""
    report = replay([{"task_id": "t", "tool": "x", "recorded_class": "UNKNOWN",
                      "message": "read 4096 bytes then failed for unknown reasons",
                      "trace": ""}])
    assert report["current_distribution"] == {"UNKNOWN": 1}
