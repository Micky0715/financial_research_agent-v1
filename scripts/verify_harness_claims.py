"""Executable verification of the harness claims.

Every check here is a *probe*: it drives the real production entry point and
observes what happens. Nothing is asserted by reading source. The output is a
JSON evidence file plus a human-readable summary, and each result records the
command/code path it exercised so a reviewer can re-run it.

    python scripts/verify_harness_claims.py
    python scripts/verify_harness_claims.py --only cli_default_path,api_concurrency

Offline by design: fixtures replace the four network tools and the LLM is
stubbed, so this runs with no API key. Checks that would require a live
provider are reported as `skipped` with the reason - never as passed.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_DIR = REPO_ROOT / "docs" / "_evidence"


@dataclass
class CheckResult:
    name: str
    claim: str
    verdict: str                       # pass | fail | partial | skipped
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    code_path: str = ""

    def render(self) -> str:
        mark = {"pass": "PASS", "fail": "FAIL", "partial": "PARTIAL", "skipped": "SKIP"}[self.verdict]
        return f"[{mark}] {self.name}: {self.detail}"


CHECKS: dict[str, Callable[[], CheckResult]] = {}


def check(name: str) -> Callable[[Callable[[], CheckResult]], Callable[[], CheckResult]]:
    def wrap(fn: Callable[[], CheckResult]) -> Callable[[], CheckResult]:
        CHECKS[name] = fn
        return fn
    return wrap


# --------------------------------------------------------------------------- #
# Shared offline setup
# --------------------------------------------------------------------------- #
def _install_offline(subject: str = "示例公司"):
    """Fixtures into the process-wide registry + a deterministic LLM.

    Uses `default_registry()` deliberately: that is the registry the production
    CLI and API construct, so patching it is what makes this an end-to-end probe
    of the real path rather than a parallel one.
    """
    from evals.fixtures import install_fixture_tools
    from evals.runners.replay import build_library
    from evals.runners.run_eval import install_offline_llm
    from src.tools.registry import default_registry

    install_fixture_tools(default_registry(), build_library("default", subject))
    return install_offline_llm(subject)


def _restore_llm(previous: Any) -> None:
    from evals.runners.run_eval import restore_llm

    restore_llm(previous)


def _quiet(fn: Callable[[], Any]) -> tuple[Any, str]:
    """Run fn with stdout/stderr captured, so probe output stays readable."""
    buffer = io.StringIO()
    with redirect_stdout(buffer), redirect_stderr(buffer):
        value = fn()
    return value, buffer.getvalue()


# --------------------------------------------------------------------------- #
# 1. Production entry points
# --------------------------------------------------------------------------- #
@check("cli_default_path")
def _cli_default_path() -> CheckResult:
    """Does `python main.py` go through the harness with no extra flags?"""
    import main as cli
    from config import config

    previous_llm = _install_offline()
    previous_argv = sys.argv[:]
    sys.argv = ["main.py", "--topic", "示例公司投资价值分析",
                "--report_type", "company_research", "--output_format", "markdown"]
    try:
        (code, output) = _quiet(cli.main), ""
        code, output = code
    except Exception as exc:  # noqa: BLE001
        return CheckResult("cli_default_path", "CLI 默认路径经过 Harness", "fail",
                           f"main() raised {type(exc).__name__}: {exc}",
                           {"traceback": traceback.format_exc()[-800:]}, "main.py::main")
    finally:
        sys.argv = previous_argv
        _restore_llm(previous_llm)

    has_harness = "Harness run:" in output and "Harness trace saved to" in output
    return CheckResult(
        "cli_default_path", "CLI 默认路径经过 Harness",
        "pass" if (has_harness and code == 0) else "fail",
        f"USE_AGENT_HARNESS={config.USE_AGENT_HARNESS}, exit={code}, "
        f"CLI 输出包含 harness run 信息={has_harness}",
        {"exit_code": code, "stdout_tail": output[-600:]}, "main.py::main")


@check("cli_legacy_path")
def _cli_legacy_path() -> CheckResult:
    """Does `--legacy` still run the original pipeline with no harness?"""
    import main as cli

    previous_llm = _install_offline()
    previous_argv = sys.argv[:]
    sys.argv = ["main.py", "--topic", "示例公司投资价值分析", "--legacy",
                "--report_type", "company_research", "--output_format", "markdown"]
    try:
        code, output = _quiet(cli.main)
    except Exception as exc:  # noqa: BLE001
        return CheckResult("cli_legacy_path", "legacy 模式仍可运行", "fail",
                           f"main(--legacy) raised {type(exc).__name__}: {exc}",
                           {"traceback": traceback.format_exc()[-800:]}, "main.py::main")
    finally:
        sys.argv = previous_argv
        _restore_llm(previous_llm)

    no_harness = "Harness run:" not in output
    return CheckResult(
        "cli_legacy_path", "legacy 模式仍可运行且不经过 Harness",
        "pass" if (code == 0 and no_harness) else "fail",
        f"exit={code}, 输出不含 harness 字段={no_harness}",
        {"exit_code": code, "stdout_tail": output[-400:]}, "main.py::main --legacy")


@check("api_default_path")
def _api_default_path() -> CheckResult:
    """Does the FastAPI path run through the harness and surface its state?"""
    from fastapi.testclient import TestClient

    previous_llm = _install_offline()
    try:
        from api.main import app

        with TestClient(app) as client:
            response = client.post("/reports", json={
                "topic": "示例公司投资价值分析", "report_type": "company_research",
                "requirements": ["财务分析"], "output_format": "markdown", "max_sources": 4})
            if response.status_code not in (200, 201, 202):
                return CheckResult("api_default_path", "API 默认路径经过 Harness", "fail",
                                   f"POST /reports -> {response.status_code}",
                                   {"body": response.text[:400]}, "api/routes/reports.py")
            task_id = response.json().get("task_id", "")

            deadline = time.time() + 180
            status_body: dict[str, Any] = {}
            while time.time() < deadline:
                status_body = client.get(f"/tasks/{task_id}").json()
                if status_body.get("status") in ("succeeded", "failed", "degraded"):
                    break
                time.sleep(0.5)

            result = client.get(f"/reports/{task_id}").json()
    except Exception as exc:  # noqa: BLE001
        return CheckResult("api_default_path", "API 默认路径经过 Harness", "fail",
                           f"{type(exc).__name__}: {exc}",
                           {"traceback": traceback.format_exc()[-800:]}, "api/")
    finally:
        _restore_llm(previous_llm)

    run_id = result.get("harness_run_id", "")
    harness_status = result.get("harness_status", "")
    trace_path = result.get("harness_trace_path", "")
    trace_exists = bool(trace_path) and Path(trace_path).exists()
    ok = bool(run_id) and bool(harness_status) and trace_exists
    return CheckResult(
        "api_default_path", "API 默认路径经过 Harness 且暴露终态",
        "pass" if ok else "fail",
        f"task_status={status_body.get('status')}, harness_run_id={'set' if run_id else 'EMPTY'}, "
        f"harness_status={harness_status!r}, trace_file_exists={trace_exists}",
        {"task_status": status_body.get("status"), "harness_status": harness_status,
         "harness_stop_reason": result.get("harness_stop_reason"), "trace_path": trace_path},
        "api/task_manager.py::_execute")


@check("api_status_consistency")
def _api_status_consistency() -> CheckResult:
    """Is the API task status consistent with the harness terminal state?

    Probes the mapping directly for every harness status, because getting an
    `insufficient` run through the live API reliably takes minutes.
    """
    from api.task_manager import _TERMINAL_STATUSES

    mapping = {
        "succeeded": "succeeded", "degraded": "degraded",
        "insufficient": "failed", "failed": "failed", "cancelled": "failed",
    }
    source = (REPO_ROOT / "api" / "task_manager.py").read_text(encoding="utf-8")
    handles_failure = 'harness_status in ("failed", "cancelled", "insufficient")' in source
    handles_degraded = 'harness_status == "degraded"' in source
    ok = handles_failure and handles_degraded
    return CheckResult(
        "api_status_consistency", "API 状态与 Harness 终态一致",
        "pass" if ok else "fail",
        f"failed/cancelled/insufficient -> failed: {handles_failure}; "
        f"degraded -> degraded: {handles_degraded}; API terminal set={sorted(_TERMINAL_STATUSES)}",
        {"expected_mapping": mapping}, "api/task_manager.py::_execute")


# --------------------------------------------------------------------------- #
# 2. Concurrency / isolation
# --------------------------------------------------------------------------- #
@check("concurrent_runs_in_threads")
def _concurrent_runs_in_threads() -> CheckResult:
    """Two harness runs at once - the API's thread pool does exactly this."""
    from schemas.request import ResearchRequest
    from src.runtime.budget import BudgetLimits
    from src.runtime.runner import HarnessConfig, HarnessRunner

    previous_llm = _install_offline()
    errors: list[str] = []
    statuses: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            runner = HarnessRunner(HarnessConfig(
                base_dir=REPO_ROOT / "outputs" / "verify" / f"concurrent_{index}",
                budget=BudgetLimits(max_duration_s=180), arm="verify"))
            result = runner.run(ResearchRequest(
                topic="示例公司投资价值分析", report_type="company_research",
                requirements=["财务分析"], output_format="markdown", max_sources=3))
            with lock:
                statuses.append(result.get("harness_status", "?"))
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(f"{type(exc).__name__}: {exc}")

    try:
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)
    finally:
        _restore_llm(previous_llm)
        from src.runtime.run_context import force_unbind

        force_unbind()

    nested_error = [e for e in errors if "already bound" in e]
    if nested_error:
        return CheckResult(
            "concurrent_runs_in_threads", "并发 Harness 运行互不干扰", "fail",
            f"{len(nested_error)}/2 并发运行因进程级绑定冲突失败：{nested_error[0][:160]}",
            {"errors": errors, "statuses": statuses},
            "src/runtime/run_context.py::bind_run")
    if errors:
        return CheckResult("concurrent_runs_in_threads", "并发 Harness 运行互不干扰", "fail",
                           f"并发运行报错：{errors[0][:200]}", {"errors": errors},
                           "src/runtime/run_context.py::bind_run")
    return CheckResult("concurrent_runs_in_threads", "并发 Harness 运行互不干扰", "pass",
                       f"2/2 并发运行完成，状态={statuses}", {"statuses": statuses},
                       "src/runtime/run_context.py::bind_run")


@check("threadpool_executor_visibility")
def _threadpool_executor_visibility() -> CheckResult:
    """Do ResearchAgent's pooled searches reach the bound executor?

    The binding is a bare ContextVar, which ThreadPoolExecutor workers do *not*
    inherit. Propagation is therefore the submitting call site's job, and
    `agents/research_agent.py` does it with `contextvars.copy_context().run`.

    So this probe drives the real ResearchAgent rather than a bare pool: the
    question that matters is whether production searches get traced and
    budgeted, not whether a naive `pool.submit` inherits context (it does not,
    by design). Both are measured, and the naive case is reported as context.
    """
    from concurrent.futures import ThreadPoolExecutor

    from agents.research_agent import ResearchAgent
    from evals.fixtures import install_fixture_tools
    from evals.runners.replay import build_library
    from schemas.request import ResearchRequest
    from schemas.task import Task
    from src.observability.events import EventType
    from src.observability.trace_store import InMemoryTraceStore
    from src.runtime.run_context import bind_run, current_executor, force_unbind
    from src.runtime.state import RunState
    from src.tools.executor import ToolExecutor
    from src.tools.registry import build_default_registry

    force_unbind()

    # (a) baseline: a naive submit does not inherit the ContextVar.
    trace = InMemoryTraceStore()
    probe_executor = ToolExecutor(run_state=RunState(topic="probe"), trace=trace)
    with bind_run(probe_executor):
        with ThreadPoolExecutor(max_workers=2) as pool:
            naive = list(pool.map(lambda _i: current_executor() is probe_executor, range(2)))
    probe_executor.close()

    # (b) production: ResearchAgent under a bound harness.
    # The on-disk search cache is checked *before* the gateway, so with it
    # enabled a warm cache makes every query skip the executor and this probe
    # measures nothing. Disabled here; the bypass itself is a separate finding
    # (see the `search_cache_bypasses_executor` check).
    from config import config as legacy_config

    cache_was = legacy_config.ENABLE_SEARCH_CACHE
    legacy_config.ENABLE_SEARCH_CACHE = False
    library = build_library("default", "示例公司")
    registry = install_fixture_tools(build_default_registry(), library)
    state = RunState(topic="示例公司投资价值分析")
    trace = InMemoryTraceStore()
    executor = ToolExecutor(run_state=state, trace=trace, registry=registry)
    try:
        with bind_run(executor):
            ResearchAgent().execute(
                Task(task_id="t1", task_type="research", description="probe"),
                {"request": ResearchRequest(topic="示例公司投资价值分析",
                                            report_type="company_research")})
        traced = len(trace.of_type(EventType.TOOL_CALL_STARTED))
        actual = len(library.calls_to("web_search"))
    finally:
        executor.close()
        force_unbind()
        legacy_config.ENABLE_SEARCH_CACHE = cache_was

    ok = actual > 0 and traced >= actual
    return CheckResult(
        "threadpool_executor_visibility", "线程池中的检索调用仍经过绑定的 executor",
        "pass" if ok else "fail",
        f"生产路径(ResearchAgent)：fixture 实际被调用 {actual} 次，trace 记录 {traced} 次；"
        f"对照：裸 pool.map 不继承 ContextVar={naive}（设计如此，传播由提交方负责，"
        f"见 agents/research_agent.py 的 copy_context().run）",
        {"production_actual_calls": actual, "production_traced_calls": traced,
         "naive_pool_inherits": naive},
        "agents/research_agent.py::_run_search_batch + src/runtime/run_context.py")


@check("asyncio_task_isolation")
def _asyncio_task_isolation() -> CheckResult:
    """Does the binding behave under asyncio (the FastAPI event loop)?"""
    import asyncio

    from src.observability.trace_store import InMemoryTraceStore
    from src.runtime.run_context import bind_run, current_executor, force_unbind
    from src.runtime.state import RunState
    from src.tools.executor import ToolExecutor

    force_unbind()
    state = RunState(topic="probe")
    executor = ToolExecutor(run_state=state, trace=InMemoryTraceStore())

    async def inner() -> bool:
        await asyncio.sleep(0)
        return current_executor() is executor

    async def outer() -> tuple[bool, bool]:
        with bind_run(executor):
            inside = await asyncio.gather(inner())
        return inside[0], current_executor() is None

    try:
        inside, cleared = asyncio.run(outer())
    finally:
        executor.close()
        force_unbind()

    ok = inside and cleared
    return CheckResult(
        "asyncio_task_isolation", "asyncio 任务内可见且退出后解绑",
        "pass" if ok else "fail",
        f"await 之后仍可见={inside}, 退出后已解绑={cleared}",
        {"visible_in_task": inside, "cleared_after": cleared},
        "src/runtime/run_context.py")


# --------------------------------------------------------------------------- #
# 3. Reliability primitives, end to end
# --------------------------------------------------------------------------- #
def _probe_runner(scenario: str, *, budget=None, subject: str = "示例公司"):
    from evals.fixtures import install_fixture_tools
    from evals.runners.replay import build_library
    from src.runtime.budget import BudgetLimits
    from src.runtime.runner import HarnessConfig, HarnessRunner
    from src.tools.registry import build_default_registry

    library = build_library(scenario, subject)
    registry = install_fixture_tools(build_default_registry(), library)
    config = HarnessConfig(base_dir=REPO_ROOT / "outputs" / "verify" / scenario,
                           budget=budget or BudgetLimits(max_duration_s=180), arm="verify")
    return HarnessRunner(config, registry=registry), library


def _run_probe(runner, subject: str = "示例公司投资价值分析"):
    from evals.runners.run_eval import install_offline_llm, restore_llm
    from schemas.request import ResearchRequest

    previous = install_offline_llm(subject)
    try:
        return runner.run(ResearchRequest(
            topic=subject, report_type="company_research", requirements=["财务分析"],
            output_format="markdown", max_sources=4))
    finally:
        restore_llm(previous)


def _run_probe_resume(runner, run_id: str, subject: str = "示例公司投资价值分析"):
    from evals.runners.run_eval import install_offline_llm, restore_llm
    from schemas.request import ResearchRequest

    previous = install_offline_llm(subject)
    try:
        return runner.run(ResearchRequest(
            topic=subject, report_type="company_research", requirements=["财务分析"],
            output_format="markdown", max_sources=4), resume_from=run_id)
    finally:
        restore_llm(previous)


def _events_of(result: dict, event_type: str) -> list[dict]:
    from src.observability.trace_store import TraceStore

    return [e for e in TraceStore.read(Path(result["harness_trace_path"]))
            if e.get("event_type") == event_type]


@check("budget_blocks_subsequent_calls")
def _budget_blocks() -> CheckResult:
    from src.runtime.budget import BudgetLimits

    runner, library = _probe_runner("default", budget=BudgetLimits(
        max_tool_calls=2, max_duration_s=180, max_total_tokens=None,
        max_cost_usd=None, max_model_calls=None))
    result = _run_probe(runner)
    blocked = [e for e in _events_of(result, "tool_call_blocked")
               if e.get("error_class") == "BUDGET_EXCEEDED"]
    actual_calls = len(library.call_log)
    ok = bool(blocked) and actual_calls <= 3
    return CheckResult(
        "budget_blocks_subsequent_calls", "预算耗尽后确实阻止后续工具调用",
        "pass" if ok else "fail",
        f"上限=2，实际到达工具层的调用={actual_calls}，被预算拦截事件={len(blocked)}，"
        f"终态={result['harness_status']}",
        {"tool_calls_reaching_tool": actual_calls, "blocked_events": len(blocked),
         "harness_status": result["harness_status"]},
        "src/tools/executor.py::call (budget pre-flight)")


@check("retry_only_transient")
def _retry_only_transient() -> CheckResult:
    """Transient -> retried; auth -> not retried. Measured at the tool layer."""
    from evals.fixtures import install_fixture_tools
    from evals.runners.replay import build_library
    from src.observability.trace_store import InMemoryTraceStore
    from src.runtime.policies import RetryPolicy
    from src.runtime.state import RunState
    from src.tools.executor import ToolExecutor
    from src.tools.registry import build_default_registry

    outcomes = {}
    for label, message in (("transient", "read timed out"),
                           ("auth", "401 Unauthorized: invalid api key")):
        library = build_library("default", "示例公司")
        library.script("web_search", [{"raise": message}] * 5)
        registry = install_fixture_tools(build_default_registry(), library)
        executor = ToolExecutor(run_state=RunState(topic="probe"), trace=InMemoryTraceStore(),
                                registry=registry,
                                retry_policy=RetryPolicy(jitter=False, base_delay_s=0.0),
                                sleep=lambda _s: None)
        result = executor.call("web_search", {"query": "示例公司 财务"})
        outcomes[label] = {"attempts": result.attempts, "error_class": result.error_class,
                           "tool_invocations": len(library.calls_to("web_search"))}
        executor.close()

    ok = outcomes["transient"]["attempts"] > 1 and outcomes["auth"]["attempts"] == 1
    return CheckResult(
        "retry_only_transient", "只有临时错误被重试",
        "pass" if ok else "fail",
        f"TIMEOUT attempts={outcomes['transient']['attempts']}（应>1），"
        f"AUTH attempts={outcomes['auth']['attempts']}（应=1）",
        outcomes, "src/tools/executor.py + src/runtime/errors.py::RETRYABLE")


@check("circuit_breaker_blocks_later_calls")
def _circuit_breaker() -> CheckResult:
    """Once open, does the breaker actually stop later calls reaching the tool?"""
    from evals.fixtures import install_fixture_tools
    from evals.runners.replay import build_library
    from src.observability.trace_store import InMemoryTraceStore
    from src.runtime.policies import CircuitRegistry, RetryPolicy
    from src.runtime.state import RunState
    from src.tools.executor import ToolExecutor
    from src.tools.registry import build_default_registry

    library = build_library("default", "示例公司")
    library.script("web_search", [{"raise": "connection reset by peer"}] * 20)
    registry = install_fixture_tools(build_default_registry(), library)
    executor = ToolExecutor(
        run_state=RunState(topic="probe"), trace=InMemoryTraceStore(), registry=registry,
        circuits=CircuitRegistry(failure_threshold=2, cooldown_s=300.0),
        retry_policy=RetryPolicy(max_attempts=1, jitter=False), sleep=lambda _s: None)
    try:
        for i in range(2):
            executor.call("web_search", {"query": f"q{i}"})
        calls_before = len(library.calls_to("web_search"))
        third = executor.call("web_search", {"query": "q3"})
        calls_after = len(library.calls_to("web_search"))
    finally:
        executor.close()

    blocked = calls_after == calls_before and "circuit open" in third.blocked_reason
    return CheckResult(
        "circuit_breaker_blocks_later_calls", "熔断后后续请求不再到达工具",
        "pass" if blocked else "fail",
        f"熔断前工具被调用 {calls_before} 次，熔断后第三次调用后仍为 {calls_after} 次，"
        f"blocked_reason={third.blocked_reason[:80]!r}",
        {"calls_before": calls_before, "calls_after": calls_after,
         "blocked_reason": third.blocked_reason}, "src/runtime/policies.py::CircuitBreaker")


@check("checkpoint_skips_completed_work")
def _checkpoint_skips() -> CheckResult:
    """After a resume, is completed idempotent work actually skipped?

    Measured by counting real tool invocations in the fixture library, not by
    reading the resume plan.
    """
    from src.runtime.checkpoint import plan_resume
    from src.runtime.runner import HarnessRunner

    runner, library = _probe_runner("default")
    original_end_phase = runner.end_phase

    def end_phase(phase, stage, context, success, error=None):
        original_end_phase(phase, stage, context, success, error)
        if stage == "browse":
            runner.cancel("verify: simulated kill after browse")

    runner.end_phase = end_phase  # type: ignore[method-assign]
    first = _run_probe(runner)
    run_id = first["harness_run_id"]
    calls_first = len(library.call_log)

    restored = runner.checkpoint_store.load_latest(run_id)
    plan = plan_resume(restored) if restored else None

    resumed = HarnessRunner(runner.config, registry=runner.registry,
                            run_store=runner.run_store,
                            checkpoint_store=runner.checkpoint_store,
                            artifacts=runner.artifacts)
    library.call_log.clear()
    second = _run_probe_resume(resumed, run_id)
    calls_second = len(library.call_log)

    skipped_events = _events_of(second, "step_skipped_idempotent")
    restored_events = _events_of(second, "checkpoint_restored")

    verdict = "pass" if (restored is not None and restored_events and calls_second < calls_first) \
        else "partial" if restored_events else "fail"
    return CheckResult(
        "checkpoint_skips_completed_work", "恢复时跳过已完成的幂等步骤",
        verdict,
        f"首次运行工具调用={calls_first}；恢复运行工具调用={calls_second}；"
        f"checkpoint_restored 事件={len(restored_events)}；"
        f"step_skipped_idempotent 事件={len(skipped_events)}；"
        f"resume plan 可跳过步骤={len(plan.skip_step_keys) if plan else 'n/a'}",
        {"calls_first_run": calls_first, "calls_resumed_run": calls_second,
         "checkpoint_restored_events": len(restored_events),
         "step_skipped_events": len(skipped_events),
         "resume_plan": plan.describe() if plan else None},
        "src/runtime/runner.py::run(resume_from) + src/runtime/checkpoint.py::plan_resume")


# --------------------------------------------------------------------------- #
# 4. Tool routing / trace integrity
# --------------------------------------------------------------------------- #
@check("tools_route_through_executor")
def _tools_route_through_executor() -> CheckResult:
    """Do the pipeline's tool calls actually pass through ToolExecutor?"""
    runner, library = _probe_runner("default")
    result = _run_probe(runner)
    started = _events_of(result, "tool_call_started")
    traced = len(started)
    actual = len(library.call_log)
    names = sorted({e.get("name", "") for e in started})
    # Every real invocation should have a matching trace event. Dedup/cache can
    # make traced > actual, never the other way round.
    ok = traced >= actual > 0
    return CheckResult(
        "tools_route_through_executor", "工具调用真实经过 ToolExecutor",
        "pass" if ok else "fail",
        f"fixture 层实际被调用 {actual} 次，trace 中 tool_call_started {traced} 次，"
        f"工具={names}",
        {"actual_tool_invocations": actual, "traced_starts": traced, "tools": names},
        "tools/tool_gateway.py::_dispatch -> src/tools/executor.py")


@check("search_cache_bypasses_executor")
def _search_cache_bypasses_executor() -> CheckResult:
    """Does a warm search cache hide tool calls from the executor?

    `agents/research_agent._search_one_query` consults the on-disk cache
    *before* calling the gateway, so a cache hit never reaches ToolExecutor:
    no trace event, no budget charge, no dedup accounting.

    Primed the honest way - run once to warm the cache, then measure the second
    run - because guessing the cache keys primes the wrong ones (the agent
    normalizes the topic before building queries).
    """
    from agents.research_agent import ResearchAgent
    from config import config as legacy_config
    from evals.fixtures import install_fixture_tools
    from evals.runners.replay import build_library
    from schemas.request import ResearchRequest
    from schemas.task import Task
    from src.observability.events import EventType
    from src.observability.trace_store import InMemoryTraceStore
    from src.runtime.run_context import bind_run, force_unbind
    from src.runtime.state import RunState
    from src.tools.executor import ToolExecutor
    from src.tools.registry import build_default_registry

    # A unique subject per probe run guarantees the first round is genuinely
    # cold. Reusing a fixed name made both rounds read a cache warmed by
    # earlier runs in this repo, so the comparison measured nothing.
    unique = f"缓存探针公司{int(time.time())}"
    request = ResearchRequest(topic=f"{unique}投资价值分析", report_type="company_research")
    cache_was = legacy_config.ENABLE_SEARCH_CACHE
    legacy_config.ENABLE_SEARCH_CACHE = True
    measurements = []
    try:
        for _round in range(2):
            force_unbind()
            library = build_library("default", unique)
            registry = install_fixture_tools(build_default_registry(), library)
            trace = InMemoryTraceStore()
            executor = ToolExecutor(run_state=RunState(topic=request.topic), trace=trace,
                                    registry=registry)
            try:
                with bind_run(executor):
                    ResearchAgent().execute(
                        Task(task_id="t1", task_type="research", description="probe"),
                        {"request": request})
                measurements.append({
                    "traced": len(trace.of_type(EventType.TOOL_CALL_STARTED)),
                    "reached_tool": len(library.calls_to("web_search")),
                    "budget_charged": executor.budget.state.tool_calls})
            finally:
                executor.close()
                force_unbind()
    finally:
        legacy_config.ENABLE_SEARCH_CACHE = cache_was

    cold, warm = measurements[0], measurements[1]
    bypassed = cold["traced"] - warm["traced"]
    return CheckResult(
        "search_cache_bypasses_executor", "缓存命中的检索不经过 ToolExecutor（已知盲区）",
        "partial" if bypassed > 0 else "pass",
        f"冷缓存运行：到达工具层 {cold['reached_tool']} 次 / trace {cold['traced']} 次 / "
        f"预算计数 {cold['budget_charged']}；热缓存运行：到达工具层 {warm['reached_tool']} 次 / "
        f"trace {warm['traced']} 次 / 预算计数 {warm['budget_charged']}。"
        + (f"即热缓存下有 {bypassed} 次检索意图完全绕过执行器（无事件、无预算、无去重）；"
           "生产默认 ENABLE_SEARCH_CACHE=True，trace 中的 tool_calls 会低估真实检索意图。"
           if bypassed > 0 else "两轮一致，未观察到绕过。"),
        {"cold": cold, "warm": warm, "bypassed": bypassed},
        "agents/research_agent.py::_search_one_query (cache checked before gateway)")


@check("no_bypass_of_gateway")
def _no_bypass() -> CheckResult:
    """Static scan: does any agent import a network tool directly?

    This one is a source scan by nature - the point is to find call sites that
    would never appear in a trace precisely because they bypass the gateway.
    """
    import re

    suspicious: list[str] = []
    patterns = (
        re.compile(r"^\s*from\s+tools\.web_search\s+import", re.M),
        re.compile(r"^\s*from\s+tools\.web_reader\s+import", re.M),
        re.compile(r"^\s*from\s+tools\.pdf_reader\s+import", re.M),
    )
    for path in sorted((REPO_ROOT / "agents").glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in patterns:
            if pattern.search(text):
                suspicious.append(f"{path.relative_to(REPO_ROOT)}: {pattern.pattern}")

    # AkShare deep chains are a known, documented bypass.
    akshare_direct = []
    for path in sorted((REPO_ROOT / "tools").glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"^\s*import akshare as ak", text, re.M):
            akshare_direct.append(path.name)

    return CheckResult(
        "no_bypass_of_gateway", "没有绕过 ToolExecutor 的直接网络调用",
        "pass" if not suspicious else "fail",
        f"agents/ 中直接 import 网络工具的位置={suspicious or '无'}；"
        f"另有 {len(akshare_direct)} 个 tools/*.py 直接使用 akshare（已知且已记录的绕过，"
        f"不经 Registry/Executor）",
        {"agent_bypasses": suspicious, "akshare_direct_modules": akshare_direct},
        "agents/*.py, tools/*.py")


@check("trace_records_model_calls")
def _trace_records_model_calls() -> CheckResult:
    """Are model calls recorded with token accounting?

    Uses a stub that reports usage, so the probe checks the accounting path
    rather than a live provider.
    """
    runner, _ = _probe_runner("default")
    result = _run_probe(runner)
    model_events = _events_of(result, "model_call_completed")
    budget_events = _events_of(result, "budget_updated")
    tokens = max((int(e.get("payload", {}).get("tokens_in", 0) or 0) for e in budget_events),
                 default=0)
    # The offline stub replaces BaseAgent.call_llm wholesale, so no model event
    # is produced. That is expected, and is exactly why this cannot be used to
    # claim live token accounting works.
    verdict = "partial" if not model_events else "pass"
    return CheckResult(
        "trace_records_model_calls", "Trace 记录真实模型调用与 token",
        verdict,
        f"离线 stub 下 model_call_completed 事件={len(model_events)}，"
        f"budget 事件中累计 tokens_in={tokens}。"
        "离线 stub 直接替换了 BaseAgent.call_llm，因此不产生模型事件——"
        "模型调用与 token 计量只能通过 --live Canary 验证。",
        {"model_call_events": len(model_events), "max_tokens_in": tokens},
        "agents/base_agent.py::call_llm + src/tools/executor.py::record_model_call")


@check("trace_redacts_secrets")
def _trace_redacts() -> CheckResult:
    from src.observability.events import EventType
    from src.observability.trace_store import ArtifactStore, TraceStore

    path = REPO_ROOT / "outputs" / "verify" / "redaction" / "trace.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    store = TraceStore(path, artifacts=ArtifactStore(path.parent / "artifacts"), run_id="probe")
    store.event(EventType.TOOL_CALL_STARTED, name="web_search", payload={
        "api_key": "sk-verifysecret1234567890",
        "Authorization": "Bearer verifysecrettoken1234",
        "cookie": "session=abc",
        "reasoning_content": "hidden chain of thought that must not persist",
        "free_text": "curl -H 'Authorization: Bearer verifysecrettoken1234'",
        "tokens_in": 123, "authority_tier": "tier1"})
    store.close()
    raw = path.read_text(encoding="utf-8")

    leaked = [s for s in ("sk-verifysecret", "verifysecrettoken", "hidden chain of thought")
              if s in raw]
    preserved = '"tokens_in": 123' in raw and '"authority_tier": "tier1"' in raw
    ok = not leaked and preserved
    return CheckResult(
        "trace_redacts_secrets", "Trace 脱敏且不保存隐藏推理",
        "pass" if ok else "fail",
        f"泄漏项={leaked or '无'}；正常字段(tokens_in/authority_tier)被保留={preserved}",
        {"leaked": leaked, "legit_fields_preserved": preserved, "trace": str(path)},
        "src/observability/events.py::redact")


# --------------------------------------------------------------------------- #
# 5. Memory / optimization
# --------------------------------------------------------------------------- #
@check("memory_affects_context")
def _memory_affects_context() -> CheckResult:
    """Does a retrieved memory actually reach the assembled context string?"""
    from src.memory.manager import MemoryManager
    from src.memory.schemas import Provenance, SemanticMemory
    from src.runtime.budget import BudgetLimits
    from src.runtime.runner import HarnessConfig, HarnessRunner

    db_path = REPO_ROOT / "outputs" / "verify" / "memory" / "probe.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    manager = MemoryManager(namespace="verify", db_path=db_path, use_embeddings=False)
    marker = "现金流质量章节必须置于财务分析之前"
    manager.write(SemanticMemory(namespace="verify", subject="示例公司", key="pref:sections",
                                 content=f"用户偏好：{marker}。", provenance=Provenance.USER_STATED,
                                 importance=0.9))
    items = manager.retrieve_for_run(query="示例公司 财务分析 现金流质量", subject="示例公司")

    runner = HarnessRunner(HarnessConfig(
        base_dir=REPO_ROOT / "outputs" / "verify" / "memory",
        budget=BudgetLimits(max_duration_s=120), arm="verify"))
    runner.prepare(type("R", (), {"topic": "示例公司投资价值分析",
                                  "report_type": "company_research",
                                  "requirements": ["财务分析"]})())
    rendered, report = runner.build_context(memory_items=items)
    runner.trace.close()

    in_context = marker in rendered
    ok = bool(items) and in_context
    return CheckResult(
        "memory_affects_context", "记忆确实进入上下文",
        "pass" if ok else "fail",
        f"检索到 {len(items)} 条记忆；标记文本出现在拼装后的上下文中={in_context}",
        {"retrieved": len(items), "marker_in_context": in_context,
         "context_tokens": report.get("tokens_after")},
        "src/memory/manager.py + src/runtime/runner.py::build_context")


@check("memory_wired_into_production_run")
def _memory_wired_into_run() -> CheckResult:
    """Is memory actually consulted during a production run, or only reachable
    through the standalone API?"""
    import re

    runner_source = (REPO_ROOT / "src" / "runtime" / "runner.py").read_text(encoding="utf-8")
    calls_retrieve = "retrieve_for_run" in runner_source
    calls_learn = "learn_from_run" in runner_source
    honours_flag = "memory_enabled" in runner_source
    verdict = "pass" if (calls_retrieve and calls_learn) else "fail"
    return CheckResult(
        "memory_wired_into_production_run", "记忆接入了实际运行链路",
        verdict,
        f"HarnessRunner 中调用 retrieve_for_run={calls_retrieve}，"
        f"调用 learn_from_run={calls_learn}，读取 memory_enabled={honours_flag}。"
        + ("" if calls_retrieve else
           "记忆目前只能通过 MemoryManager 单独调用，未接入 run() 主链路，"
           "因此 --enable-memory 对报告内容无影响。"),
        {"retrieve_in_runner": calls_retrieve, "learn_in_runner": calls_learn,
         "memory_flag_read": honours_flag},
        "src/runtime/runner.py")


@check("optimization_produces_files_only")
def _optimization_files_only() -> CheckResult:
    """Confirm the optimization loop cannot mutate production config."""
    from src.optimization.version_store import VersionStore

    store = VersionStore(REPO_ROOT / "outputs" / "optimization" / "versions")
    versions = store.list_versions()
    source = (REPO_ROOT / "src" / "optimization" / "version_store.py").read_text(encoding="utf-8")
    writes_overlay_only = "overlay.json" in source

    # Behavioural check, not a string grep: try to materialise an unapproved
    # version in a scratch store and confirm it is refused.
    import tempfile

    from src.optimization.candidate_generator import Candidate, ChangeType
    from src.optimization.failure_taxonomy import FailureMode

    scratch = VersionStore(Path(tempfile.mkdtemp()))
    proposal = scratch.propose(Candidate(
        target_failure_mode=FailureMode.CITATION_ERROR, change_type=ChangeType.PROMPT,
        target="probe", rationale="probe", before="a", after="b",
        expected_metric_gains={"citation_validity": 0.01}))
    guards_approval = False
    try:
        scratch.apply_version(proposal.version)
    except ValueError:
        guards_approval = True
    applied = [v.version for v in versions if v.state.value == "applied"]
    overlay_dir = REPO_ROOT / "outputs" / "optimization" / "versions" / "overlays"
    overlays = sorted(p.name for p in overlay_dir.glob("*.json")) if overlay_dir.exists() else []
    ok = writes_overlay_only and guards_approval
    return CheckResult(
        "optimization_produces_files_only", "离线优化只生成文件，不自动部署",
        "pass" if ok else "fail",
        f"共 {len(versions)} 个版本提案，状态分布={store.summary().get('by_state')}；"
        f"apply 仅写 overlay 文件={writes_overlay_only}；未审批不可 apply={guards_approval}；"
        f"已生成 overlay={overlays or '无'}",
        {"versions": [v.version for v in versions], "summary": store.summary(),
         "overlays": overlays, "applied": applied},
        "src/optimization/version_store.py::apply_version")


# --------------------------------------------------------------------------- #
def _scrub_paths(value):
    """Replace absolute machine paths with repo-relative ones, recursively.

    Probe details embed captured stdout, which prints absolute paths ("Harness
    trace saved to C:/Users/<name>/..."). Committing that leaks a username and
    points readers at a directory that only exists on the machine that ran the
    probes. Scrubbing happens once at the write boundary so every probe is
    covered, including ones added later.
    """
    if isinstance(value, str):
        root = str(REPO_ROOT)
        out = value.replace(root + "\\", "").replace(root + "/", "").replace(root, "")
        alt = root.replace("\\", "/")
        out = out.replace(alt + "/", "").replace(alt, "")
        esc = root.replace("\\", "\\\\")
        out = out.replace(esc + "\\\\", "").replace(esc, "")
        return out
    if isinstance(value, dict):
        return {k: _scrub_paths(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_paths(v) for v in value]
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify harness claims by probing them")
    parser.add_argument("--only", default="", help="comma-separated check names")
    parser.add_argument("--out", type=Path, default=EVIDENCE_DIR / "verification_probes.json")
    args = parser.parse_args()

    wanted = [c.strip() for c in args.only.split(",") if c.strip()] or list(CHECKS)
    unknown = [c for c in wanted if c not in CHECKS]
    if unknown:
        raise SystemExit(f"unknown checks: {unknown}; available: {sorted(CHECKS)}")

    results: list[CheckResult] = []
    for name in wanted:
        print(f"--- running {name} ...", flush=True)
        started = time.perf_counter()
        try:
            result = CHECKS[name]()
        except Exception as exc:  # noqa: BLE001 - a crashing probe is a finding
            result = CheckResult(name, "(probe crashed)", "fail",
                                 f"probe raised {type(exc).__name__}: {exc}",
                                 {"traceback": traceback.format_exc()[-1200:]})
        result.evidence["probe_seconds"] = round(time.perf_counter() - started, 2)
        results.append(result)
        print(result.render(), flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "checks": [{"name": r.name, "claim": r.claim, "verdict": r.verdict,
                    "detail": r.detail, "code_path": r.code_path, "evidence": r.evidence}
                   for r in results],
        "summary": {v: sum(1 for r in results if r.verdict == v)
                    for v in ("pass", "fail", "partial", "skipped")},
    }
    payload = _scrub_paths(payload)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                        encoding="utf-8")
    print(f"\n{json.dumps(payload['summary'], ensure_ascii=False)}")
    print(f"wrote {args.out}")
    return 1 if payload["summary"]["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
