"""Drive the offline optimization loop end to end.

    python -m src.optimization.experiment_runner mine   --results evals/results/dev_harness.jsonl
    python -m src.optimization.experiment_runner propose --results evals/results/dev_harness.jsonl
    python -m src.optimization.experiment_runner gate    --version v001 \\
        --baseline evals/reports/dev_harness_report.json \\
        --candidate evals/reports/dev_candidate_report.json \\
        --baseline-regression ... --candidate-regression ...
    python -m src.optimization.experiment_runner status

Flow: mine failures -> rank slices -> generate candidates -> record them as
proposals -> (a human or CI runs the candidate arm) -> gate the measured results
-> a passing candidate becomes an approvable version.

The one thing this deliberately does **not** do is run the candidate itself and
then judge its own output in the same breath. Running a candidate means
executing an eval arm with the override applied, and that is
`evals/runners/run_eval.py` - kept separate so the numbers the gate reads always
come from the same runner that produces every other number in the project.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from src.optimization.candidate_generator import (  # noqa: E402
    CandidateGenerator,
    render_candidates_markdown,
)
from src.optimization.failure_miner import FailureMiner, render_markdown  # noqa: E402
from src.optimization.failure_taxonomy import LEVERS, FailureMode  # noqa: E402
from src.optimization.regression_gate import (  # noqa: E402
    GateThresholds,
    RegressionGate,
    render_gate_markdown,
)
from src.optimization.version_store import VersionStore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_VERSION_DIR = REPO_ROOT / "outputs" / "optimization" / "versions"
DEFAULT_REPORT_DIR = REPO_ROOT / "outputs" / "optimization"

#: Which aggregate metric each failure mode's improvement is judged on.
SLICE_METRIC: dict[FailureMode, tuple[str, bool]] = {
    FailureMode.TOOL_ARGUMENTS: ("tool_argument_accuracy", True),
    FailureMode.TOOL_SELECTION: ("tool_selection_f1", True),
    FailureMode.INSUFFICIENT_RETRIEVAL: ("task_success_rate_all_trials", True),
    FailureMode.PREMATURE_STOP: ("task_success_rate_all_trials", True),
    FailureMode.BUDGET_EXHAUSTED: ("budget_exceeded_rate", False),
    FailureMode.UNGROUNDED_NUMBERS: ("number_grounding_rate", True),
    FailureMode.CITATION_ERROR: ("citation_validity", True),
    FailureMode.MEMORY_MISRETRIEVAL: ("stale_memory_error_rate", False),
    FailureMode.ENTITY_UNVERIFIED: ("task_success_rate_all_trials", True),
    FailureMode.SAFETY_VIOLATION: ("injection_tool_hijack_total", False),
    FailureMode.TOOL_EXECUTION: ("recovery_success_rate", True),
    FailureMode.POOR_SOURCE_QUALITY: ("overall_score_mean", True),
    FailureMode.CONTEXT_LOSS: ("overall_score_mean", True),
    FailureMode.INEFFECTIVE_LOOP: ("redundant_tool_call_rate", False),
    FailureMode.PLAN_DECOMPOSITION: ("task_success_rate_all_trials", True),
    FailureMode.TASK_MISUNDERSTANDING: ("task_success_rate_all_trials", True),
    FailureMode.FINAL_SYNTHESIS: ("overall_score_mean", True),
}


def _current_state() -> dict[str, Any]:
    """Current values of the settings candidates may propose changing."""
    from config import config

    return {
        "SEARCH_MIN_UNIQUE_RESULTS": config.SEARCH_MIN_UNIQUE_RESULTS,
        "SEARCH_TARGET_UNIQUE_RESULTS": config.SEARCH_TARGET_UNIQUE_RESULTS,
        "MAX_BROWSE_CANDIDATES": config.MAX_BROWSE_CANDIDATES,
        "max_supplementary_rounds": 1,
        "memory_min_score": 0.35,
    }


def cmd_mine(args: argparse.Namespace) -> int:
    miner = FailureMiner()
    report = miner.report(args.results)
    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = DEFAULT_REPORT_DIR / "failure_report.json"
    md_path = DEFAULT_REPORT_DIR / "failure_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(f"rows={report['rows_examined']} failures={report['failing_runs']} "
          f"({report['failure_rate']:.1%})")
    for item in report["slices"][:8]:
        print(f"  {item['mode']:26s} count={item['count']:3d} "
              f"severity={item['mean_severity']:.3f} priority={item['priority']:.2f}")
    print(f"wrote {json_path}\nwrote {md_path}")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    miner = FailureMiner()
    report = miner.report(args.results)
    slices = miner.slices(miner.mine(miner.load_rows(args.results)))
    if not slices:
        print("no failure slices found; nothing to propose")
        return 0

    candidates = CandidateGenerator(max_per_slice=args.max_per_slice).generate(
        slices, current_state=_current_state())
    if not candidates:
        print(f"failure slices found ({[s.mode.value for s in slices[:5]]}) but no candidate "
              "builder covers them; add a builder in candidate_generator.py")
        return 0

    store = VersionStore(args.version_dir)
    records = [store.propose(c, notes=f"auto-generated from {args.results}") for c in candidates]

    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = DEFAULT_REPORT_DIR / "candidates.md"
    md_path.write_text(render_candidates_markdown([r.candidate for r in records]),
                       encoding="utf-8")

    print(f"proposed {len(records)} candidate(s):")
    for record in records:
        metric, higher = SLICE_METRIC.get(record.candidate.target_failure_mode,
                                          ("overall_score_mean", True))
        print(f"  {record.version} {record.candidate.change_type.value:18s} "
              f"target={record.candidate.target_failure_mode.value:22s} "
              f"gate_metric={metric}{'↑' if higher else '↓'}")
    print(f"wrote {md_path}")
    print("\nNothing has been applied. Next: run the candidate arm, then `gate` its results.")
    return 0


def _load_report(path: Optional[Path]) -> Optional[dict[str, Any]]:
    if path is None:
        return None
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"report not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_gate(args: argparse.Namespace) -> int:
    store = VersionStore(args.version_dir)
    record = store.get(args.version)
    if record is None:
        raise SystemExit(f"unknown version {args.version!r}")

    metric, higher_is_better = SLICE_METRIC.get(
        record.candidate.target_failure_mode, ("overall_score_mean", True))
    gate = RegressionGate(GateThresholds())
    result = gate.evaluate(
        candidate_id=record.candidate.candidate_id, target_metric=metric,
        baseline_dev=_load_report(args.baseline) or {},
        candidate_dev=_load_report(args.candidate) or {},
        baseline_test=_load_report(args.baseline_test),
        candidate_test=_load_report(args.candidate_test),
        baseline_regression=_load_report(args.baseline_regression),
        candidate_regression=_load_report(args.candidate_regression),
        evidence={k: str(v) for k, v in {
            "baseline_dev": args.baseline, "candidate_dev": args.candidate,
            "baseline_test": args.baseline_test, "candidate_test": args.candidate_test,
            "baseline_regression": args.baseline_regression,
            "candidate_regression": args.candidate_regression}.items() if v},
        higher_is_better=higher_is_better)

    store.record_gate(args.version, result)
    DEFAULT_REPORT_DIR.mkdir(parents=True, exist_ok=True)
    md_path = DEFAULT_REPORT_DIR / f"gate_{args.version}.md"
    md_path.write_text(render_gate_markdown(result), encoding="utf-8")

    print(render_gate_markdown(result))
    print(f"\nwrote {md_path}")
    if result.passed:
        print(f"\nversion {args.version} is now 'gated'. To adopt it a person must run:\n"
              f"  python -m src.optimization.experiment_runner approve --version {args.version} "
              f"--approver <name>")
    return 0 if result.passed else 1


def cmd_approve(args: argparse.Namespace) -> int:
    store = VersionStore(args.version_dir)
    record = store.approve(args.version, args.approver)
    overlay = store.apply_version(args.version)
    print(f"version {record.version} approved by {record.approved_by}")
    print(f"overlay written to {overlay}")
    print("The overlay is NOT loaded automatically; wiring it in is a deliberate manual step.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = VersionStore(args.version_dir)
    print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
    for record in store.list_versions():
        gate = ("gate=passed" if record.gate_result and record.gate_result.passed
                else "gate=failed" if record.gate_result else "gate=not-run")
        print(f"  {record.version} {record.state.value:12s} {gate:16s} "
              f"{record.candidate.change_type.value} -> {record.candidate.target}")
    return 0


def cmd_levers(args: argparse.Namespace) -> int:
    print("允许的修改杠杆（按失败模式）：")
    for mode, levers in LEVERS.items():
        print(f"  {mode.value:26s} {levers}")
    print("\n候选只能修改上述杠杆；没有任何杠杆可以编辑任意代码。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline, regression-gated optimization loop")
    parser.add_argument("--version-dir", type=Path, default=DEFAULT_VERSION_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    mine = sub.add_parser("mine", help="classify failures and rank slices")
    mine.add_argument("--results", type=Path, required=True)
    mine.set_defaults(func=cmd_mine)

    propose = sub.add_parser("propose", help="generate improvement candidates (proposals only)")
    propose.add_argument("--results", type=Path, required=True)
    propose.add_argument("--max-per-slice", type=int, default=2)
    propose.set_defaults(func=cmd_propose)

    gate = sub.add_parser("gate", help="evaluate a candidate's measured results")
    gate.add_argument("--version", required=True)
    gate.add_argument("--baseline", type=Path, required=True)
    gate.add_argument("--candidate", type=Path, required=True)
    gate.add_argument("--baseline-test", type=Path, default=None)
    gate.add_argument("--candidate-test", type=Path, default=None)
    gate.add_argument("--baseline-regression", type=Path, default=None)
    gate.add_argument("--candidate-regression", type=Path, default=None)
    gate.set_defaults(func=cmd_gate)

    approve = sub.add_parser("approve", help="human sign-off on a gated version")
    approve.add_argument("--version", required=True)
    approve.add_argument("--approver", required=True)
    approve.set_defaults(func=cmd_approve)

    sub.add_parser("status", help="list versions and their states").set_defaults(func=cmd_status)
    sub.add_parser("levers", help="show what a candidate is allowed to change").set_defaults(
        func=cmd_levers)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
