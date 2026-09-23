"""One offline end-to-end run, printing the trace story as it goes.

    python scripts/demo_harness_run.py
    python scripts/demo_harness_run.py --scenario search_timeout
    python scripts/demo_harness_run.py --scenario interrupt_after_browse

Needs no API key and no network. The point is to show, in about a minute, what
the harness records: tool selection with its stated reason, retries with their
error class and backoff, budget movement, checkpoints, evidence, and the
explicit stop decision at the end.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from evals.fixtures import install_fixture_tools  # noqa: E402
from evals.runners.replay import SCENARIOS, build_library  # noqa: E402
from evals.runners.run_eval import install_offline_llm, restore_llm  # noqa: E402
from schemas.request import ResearchRequest  # noqa: E402
from src.observability.metrics import RunMetrics  # noqa: E402
from src.observability.trace_store import TraceStore  # noqa: E402
from src.runtime.budget import BudgetLimits  # noqa: E402
from src.runtime.checkpoint import plan_resume  # noqa: E402
from src.runtime.runner import HarnessConfig, HarnessRunner  # noqa: E402
from src.tools.registry import build_default_registry  # noqa: E402

INTERESTING = {
    "run_started", "plan_created", "phase_started", "tool_call_started",
    "tool_call_completed", "tool_call_failed", "tool_call_blocked", "tool_call_deduped",
    "retry_scheduled", "circuit_opened", "degraded", "budget_exceeded",
    "checkpoint_saved", "checkpoint_restored", "evidence_added", "context_compressed",
    "stop_decision", "run_completed", "run_failed",
}


def _line(event: dict) -> str:
    kind = event.get("event_type", "")
    name = event.get("name", "")
    payload = event.get("payload", {}) or {}
    prefix = f"  {event.get('seq', 0):>3}  {kind:<22}"

    if kind == "tool_call_started":
        return f"{prefix} {name}  args={json.dumps(payload.get('arguments', {}), ensure_ascii=False)}\n" \
               f"          why: {payload.get('rationale', '(no rationale recorded)')}"
    if kind == "tool_call_completed":
        return f"{prefix} {name}  {event.get('duration_s', 0)}s  attempts={payload.get('attempts')}" \
               f"{'  (recovered)' if payload.get('recovered') else ''}"
    if kind in ("tool_call_failed", "tool_call_blocked"):
        return f"{prefix} {name}  {event.get('error_class', '')}: {(event.get('error_message') or '')[:80]}"
    if kind == "retry_scheduled":
        return f"{prefix} {name}  {event.get('error_class')} -> retry #{payload.get('next_attempt')}" \
               f" after {payload.get('backoff_s')}s"
    if kind == "budget_exceeded":
        return f"{prefix} {payload.get('dimension')}: {payload.get('reason', '')[:80]}"
    if kind == "checkpoint_saved":
        return f"{prefix} v{payload.get('version')}  evidence={payload.get('evidence')}" \
               f" steps={payload.get('steps')}"
    if kind == "evidence_added":
        return f"{prefix} +{payload.get('added')} (total {payload.get('total')})"
    if kind == "context_compressed":
        return f"{prefix} {payload.get('tokens_before')} -> {payload.get('tokens_after')} tokens," \
               f" dropped={payload.get('dropped_by_layer')}, ok={payload.get('consistency_ok')}"
    if kind == "stop_decision":
        return f"{prefix} {payload.get('reason')}: {payload.get('detail', '')[:80]}"
    if kind in ("run_completed", "run_failed"):
        return f"{prefix} status={payload.get('status')} sources={payload.get('num_sources')}" \
               f" degradations={payload.get('degradations')}"
    if kind == "plan_created":
        return f"{prefix} {payload.get('tasks')}"
    if kind == "phase_started":
        return f"{prefix} {name}  headroom={payload.get('budget_headroom')}"
    return f"{prefix} {name}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline end-to-end harness demo")
    parser.add_argument("--topic", default="示例公司投资价值分析")
    parser.add_argument("--report-type", default="company_research")
    parser.add_argument("--scenario", default="default", choices=sorted(SCENARIOS))
    parser.add_argument("--tool-calls", type=int, default=None,
                        help="cap tool calls, to watch the budget ladder engage")
    parser.add_argument("--out-dir", type=Path, default=Path("outputs/demo"))
    args = parser.parse_args()

    subject = args.topic.replace("投资价值分析", "").strip() or "示例公司"
    registry = install_fixture_tools(build_default_registry(),
                                     build_library(args.scenario, subject))
    budget = BudgetLimits(max_duration_s=300)
    if args.tool_calls:
        budget = budget.model_copy(update={"max_tool_calls": args.tool_calls})

    runner = HarnessRunner(HarnessConfig(base_dir=args.out_dir, arm="demo", budget=budget),
                           registry=registry)
    previous_llm = install_offline_llm(args.topic)

    print(f"scenario={args.scenario}  topic={args.topic}  (offline fixtures, no network)\n")
    interrupted_run_id = ""
    try:
        if args.scenario == "interrupt_after_browse":
            original_end_phase = runner.end_phase

            def end_phase(phase, stage, context, success, error=None):
                original_end_phase(phase, stage, context, success, error)
                if stage == "browse":
                    print("  !! simulating a process kill right after browse\n")
                    runner.cancel("demo: simulated kill")

            runner.end_phase = end_phase  # type: ignore[method-assign]
            first = runner.run(ResearchRequest(topic=args.topic, report_type=args.report_type,
                                               requirements=["财务分析", "风险提示"],
                                               output_format="markdown", max_sources=4))
            interrupted_run_id = first["harness_run_id"]

            restored = runner.checkpoint_store.load_latest(interrupted_run_id)
            plan = plan_resume(restored)
            print("  resume plan:", json.dumps(plan.describe(), ensure_ascii=False), "\n")

            runner = HarnessRunner(runner.config, registry=registry,
                                   run_store=runner.run_store,
                                   checkpoint_store=runner.checkpoint_store,
                                   artifacts=runner.artifacts)
            result = runner.run(ResearchRequest(topic=args.topic, report_type=args.report_type,
                                                requirements=["财务分析", "风险提示"],
                                                output_format="markdown", max_sources=4),
                                resume_from=interrupted_run_id)
        else:
            result = runner.run(ResearchRequest(topic=args.topic, report_type=args.report_type,
                                                requirements=["财务分析", "风险提示"],
                                                output_format="markdown", max_sources=4))
    finally:
        restore_llm(previous_llm)

    trace_path = Path(result["harness_trace_path"])
    events = TraceStore.read(trace_path)
    print("--- trace ---")
    for event in events:
        if event.get("event_type") in INTERESTING:
            print(_line(event))

    print("\n--- metrics ---")
    print(json.dumps(RunMetrics(events).summary(), ensure_ascii=False, indent=2, default=str))
    print(f"\nrun_id     : {result['harness_run_id']}")
    print(f"status     : {result['harness_status']}  (stop_reason={result['harness_stop_reason']})")
    print(f"report     : {result.get('report_path')}")
    print(f"trace      : {trace_path}")
    print(f"evidence   : {result['harness_evidence_count']} records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
