"""Evaluation runner: multi-trial, resumable, arm-aware.

    python -m evals.runners.run_eval --split dev --trials 3
    python -m evals.runners.run_eval --split dev --resume
    python -m evals.runners.run_eval --smoke
    python -m evals.runners.run_eval --split test --arm no_fallback --live

Properties that matter:

- **Offline by default.** `--live` is required to touch the real network or a
  real model. Without it the run replays fixtures and stubs the LLM, so the
  suite is free, deterministic and runnable in CI.
- **Resumable.** Results are appended to a JSONL file after every trial, and
  `--resume` skips (task_id, trial, arm) triples already recorded. A killed
  8-hour run loses one trial, not the suite. This mirrors the pattern already
  proven in `scripts/run_competition_eval.py`.
- **Multi-trial.** Models are stochastic; the default is 3 trials per task and
  the report keeps mean, stdev and the *worst* trial.
- **Arms.** An arm is a named configuration (baseline / harness / no_fallback /
  no_cache / no_compression / model / concurrency). Every result row records
  the arm and the config hash, so no two numbers can be compared without their
  configurations being visible.
- **Failures are recorded as failures.** A crashed case produces a row with
  `status="failed"` and the traceback summary. Nothing is dropped.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import platform
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from config import config  # noqa: E402
from evals.datasets.schema import EvalDataset, EvalTask, Split  # noqa: E402
from evals.graders.base import RunArtifacts, aggregate  # noqa: E402
from evals.graders.deterministic import DEFAULT_GRADERS, grade_all  # noqa: E402
from evals.graders.llm_judge import LLMJudgeGrader  # noqa: E402
from evals.runners.arms import ARMS, ArmConfig, resolve_arm  # noqa: E402
from evals.runners.replay import RUNNER_DRIVEN, build_library, unknown_scenarios  # noqa: E402
from schemas.request import ResearchRequest  # noqa: E402
from src.observability.trace_store import TraceStore  # noqa: E402
from src.runtime.runner import HarnessConfig, HarnessRunner  # noqa: E402
from src.tools.registry import build_default_registry  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = Path(__file__).resolve().parents[1] / "datasets"
RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"


def repo_path(value) -> str:
    """Store path pointers repo-relative, never absolute.

    An absolute path bakes the producing machine's username and layout into a
    committed result file and resolves to nothing on anyone else's checkout -
    `scripts/replay_canary_error_classes.py` then silently finds no traces.
    Pointers are not measurements, so normalising them loses no evidence.
    """
    text = str(value or "")
    if not text:
        return ""
    candidate = Path(text)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(REPO_ROOT).as_posix()
    except (ValueError, OSError):
        return candidate.name


def resolve_repo_path(value) -> Path:
    """Inverse of `repo_path`: accept relative or absolute, return a real path.

    Relative pointers resolve against the repo root rather than the current
    working directory, so tooling works from any directory.
    """
    candidate = Path(str(value or ""))
    return candidate if candidate.is_absolute() else (REPO_ROOT / candidate)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Offline stubs
# --------------------------------------------------------------------------- #
# The stub deliberately satisfies `ReportAgent._validate_markdown` (>=5 of the
# required headings, >=300 chars) so the offline eval exercises the *real*
# report path rather than silently falling through to the deterministic
# fallback template every single time. All figures come from the fixture
# corpus, so number-grounding graders have something真实 to check against.
_STUB_REPORT = """# {topic} 研究报告

## 执行摘要
基于已检索到的公开资料，{topic}的经营与财务状况整理如下 [s1]。

## 公司概况
研究对象主营业务与所属行业见公开披露资料 [s1]。

## 财务分析
2024年营业收入1200.5亿元 [s1]，同比增长18.3% [s1]；毛利率22.4% [s1]。

## 趋势分析
行业规模约8600亿元、CR5为61.2%，供需与竞争格局仍在变化 [s3]。

## 估值分析
当前市盈率18.5倍 [s2]，市净率2.3倍 [s2]，处于历史中枢下方 [s2]。

## 风险提示
原材料价格波动、海外需求放缓与汇率波动是主要风险 [s4]。

## 投资观点
基于现有公开资料维持中性判断。本报告不构成投资建议。

## 参考来源
详见下方来源列表。
"""

_STUB_ANALYSIS_JSON = """{
  "key_findings": ["2024年营业收入1200.5亿元，同比增长18.3%", "毛利率22.4%"],
  "financial_analysis": "收入与利润同比增长，毛利率改善",
  "risks": ["原材料价格波动", "海外需求放缓"],
  "valuation": "市盈率18.5倍，市净率2.3倍",
  "conclusion": "维持中性判断"
}"""


# Keep the deterministic analysis response aligned with AnalyzeAgent's public
# schema.  Defining it here also makes older fixture text harmless when the
# schema gains fields.
_STUB_ANALYSIS_JSON = json.dumps({
    "overview": "Offline fixture-based research summary.",
    "key_trends": [
        "2024 revenue was 1200.5 with year-on-year growth of 18.3% [s1].",
        "Gross margin was 32.4% [s2].",
    ],
    "financial_analysis": "Revenue, profit and gross margin improved [s1][s2].",
    "valuation_analysis": "The fixture valuation is 18.5x earnings and 2.3x book [s3].",
    "risk_analysis": ["Input-cost volatility [s4].", "Overseas demand slowdown [s4]."],
    "investment_view": "Maintain a neutral view based on the bounded fixture evidence.",
    "data_limitations": "This response is used only for deterministic offline regression tests.",
}, ensure_ascii=False)


def install_offline_llm(topic: str) -> Any:
    """Deterministic LLM stub. Returns the previous callable for restoration.

    Dispatches on what the caller asked for so each agent gets a shape it can
    actually parse: an empty plan (planning is normalized anyway), a structured
    analysis object, or a full markdown report.
    """
    from agents.base_agent import BaseAgent

    previous = BaseAgent.call_llm

    def _fake(prompt: str, system: str = "", temperature: float = 0.3) -> str:
        # Dispatch on the *system* prompt, which is unique per call site. The
        # user prompt is not usable for this: the report prompt embeds the
        # analysis block, so matching on "key_findings" in the prompt body sent
        # JSON to the report agent and every report silently fell back to the
        # deterministic template.
        if "任务规划" in system:
            return "[]"
        if "研报撰写" in system:
            return _STUB_REPORT.format(topic=topic)
        return _STUB_ANALYSIS_JSON

    BaseAgent.call_llm = staticmethod(_fake)  # type: ignore[method-assign]
    return previous


def restore_llm(previous: Any) -> None:
    from agents.base_agent import BaseAgent

    BaseAgent.call_llm = previous  # type: ignore[method-assign]


# --------------------------------------------------------------------------- #
# One trial
# --------------------------------------------------------------------------- #
def _subject_of(task: EvalTask) -> str:
    """The entity the fixture corpus should talk about.

    Uses the task's group key (the company/industry with the analysis-type
    suffix stripped) so fetched pages actually mention the subject and entity
    validation exercises the scenario rather than tripping on a corpus mismatch.
    """
    # Synthetic scenario rows use grouping keys such as
    # ``scenario:tool_timeout`` solely to prevent split leakage.  Those keys
    # are not entities and must never be templated into the source corpus.
    if task.group_key and not task.group_key.startswith("scenario:"):
        return task.group_key.strip()
    from utils.text_utils import normalize_topic

    return normalize_topic(task.query).strip() or "示例公司"


#: Candidate config overrides recognised by the runner. A version's
#: `config_overrides` must use these keys to be measurable; anything else is
#: reported as unapplied rather than silently ignored, so a gate can never
#: score a candidate whose change never actually took effect.
APPLICABLE_OVERRIDES = {
    "SEARCH_MIN_UNIQUE_RESULTS", "SEARCH_TARGET_UNIQUE_RESULTS", "MAX_BROWSE_CANDIDATES",
    "max_supplementary_rounds", "skip_optional_threshold", "finalize_threshold",
    "narrow_search_threshold", "downgrade_model_threshold", "memory_min_score",
}


def apply_candidate_overrides(harness_config, overrides: dict[str, Any]) -> tuple[dict, list[str]]:
    """Apply a candidate's overrides. Returns (legacy config previous values,
    list of override keys that could not be applied)."""
    from config import config as legacy_config

    previous: dict[str, Any] = {}
    unapplied: list[str] = []
    budget_updates: dict[str, Any] = {}

    for key, value in (overrides or {}).items():
        if key not in APPLICABLE_OVERRIDES:
            unapplied.append(key)
            continue
        if hasattr(legacy_config, key):
            previous[key] = getattr(legacy_config, key)
            setattr(legacy_config, key, value)
        elif key in type(harness_config.budget).model_fields:
            budget_updates[key] = value
        else:
            unapplied.append(key)

    if budget_updates:
        harness_config.budget = harness_config.budget.model_copy(update=budget_updates)
    return previous, unapplied


def _run_legacy_no_harness(request: ResearchRequest, base_dir: Path) -> dict[str, Any]:
    """Run the pipeline with no harness bound at all.

    This is the honest legacy baseline for the ablation matrix. Consequences,
    all of them intentional:

    - no trace file, so every event-derived grader reports `applicable=False`
      instead of a misleading zero;
    - no harness terminal state, so status is derived the way the pre-harness
      code did: sources + a report file mean success, zero sources mean
      insufficient, an exception means failed;
    - no budget, no checkpoint, no typed errors, no dedup accounting.

    Anything this arm cannot report is a *capability* difference from the
    harness, and the ablation table says so rather than scoring it 0.
    """
    from orchestrator.workflow import WorkflowOrchestrator

    try:
        result = WorkflowOrchestrator().run(request)
    except Exception as exc:  # noqa: BLE001 - a legacy crash is a legitimate outcome
        return {"harness_status": "failed", "harness_stop_reason": "fatal_error",
                "harness_trace_path": "", "num_sources": 0,
                "_legacy_error": f"{type(exc).__name__}: {exc}"}

    sources = int(result.get("num_sources", 0) or 0)
    status = "succeeded" if sources > 0 else "insufficient"
    return {**result,
            "harness_status": status,
            "harness_stop_reason": "completed" if sources else "no_usable_sources",
            "harness_trace_path": "",
            "harness_run_id": result.get("run_id", ""),
            "harness_degradations": [],
            "harness_evidence_count": sources}


def run_one_trial(task: EvalTask, arm: ArmConfig, trial: int, *, live: bool,
                  base_dir: Path,
                  candidate_overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Execute one (task, trial) under one arm and grade it."""
    started = time.perf_counter()
    library = build_library(task.fixture_scenario, subject=_subject_of(task),
                            report_type=task.report_type)
    registry = build_default_registry()
    if not live:
        from evals.fixtures import install_fixture_tools

        install_fixture_tools(registry, library)

    harness_config = arm.to_harness_config(base_dir=base_dir / arm.name)
    # Task-declared budget caps override the arm's, so an insufficient-budget
    # task is actually starved rather than merely labelled.
    if task.budget_limit.get("tool_calls"):
        harness_config.budget = harness_config.budget.model_copy(
            update={"max_tool_calls": int(task.budget_limit["tool_calls"])})

    runner = HarnessRunner(harness_config, registry=registry)
    previous_llm = None if live else install_offline_llm(task.query)
    # Offline runs must not share the on-disk search cache. It persists between
    # runs, so a cached query short-circuits before the tool layer is reached:
    # searches vanished from the traces entirely and each run's behaviour
    # depended on whatever had run before it. The fixture library is already the
    # deterministic source of search results; cache benefit is a live-mode
    # property and is measured by the `no_cache` arm there.
    previous_cache = None
    previous_semantic = None
    if not live:
        previous_cache = config.ENABLE_SEARCH_CACHE
        previous_semantic = config.ENABLE_SEMANTIC_RANKING
        config.ENABLE_SEARCH_CACHE = False
        # Loading a local embedding model made the first offline case take
        # ~45 seconds and allowed machine-specific model availability to alter
        # the benchmark. Semantic ranking is a live-mode ablation; fixture
        # replay deliberately exercises the deterministic rule scorer.
        config.ENABLE_SEMANTIC_RANKING = False
    # Ablation arms mutate the legacy `config` singleton; restore it in the
    # finally block so one arm's settings never leak into the next.
    previous_config = arm.apply_overrides()
    candidate_previous, unapplied_overrides = apply_candidate_overrides(
        harness_config, candidate_overrides or {})

    request = ResearchRequest(
        topic=task.query, report_type=task.report_type,
        requirements=list(task.task_constraints.get("required_sections", []) or []),
        output_format="markdown",
        max_sources=int(task.task_constraints.get("max_sources", 5) or 5))

    result: dict[str, Any] = {}
    error = ""
    try:
        if live:
            offline_guard = nullcontext()
        else:
            from evals.fixtures import offline_runtime

            offline_guard = offline_runtime(
                macro_failure=task.fixture_scenario == "akshare_down")
        with offline_guard:
            if arm.legacy_mode:
                # No harness bound: the honest pre-harness baseline. Fixtures
                # must be installed at the *gateway* seam here - with no
                # executor bound, the registry patch is inert and the gateway
                # would go out over MCP to the live internet.
                from evals.fixtures import install_gateway_fixtures

                gateway_guard = (nullcontext() if live
                                 else install_gateway_fixtures(library))
                with gateway_guard:
                    result = _run_legacy_no_harness(request, base_dir)
                error = result.pop("_legacy_error", "")
            elif task.fixture_scenario == "interrupt_after_browse":
                result = _run_with_interrupt(runner, request)
            else:
                result = runner.run(request)
    except Exception as exc:  # noqa: BLE001 - one bad case must not stop the suite
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc(limit=3)
    finally:
        if previous_llm is not None:
            restore_llm(previous_llm)
        if previous_cache is not None:
            config.ENABLE_SEARCH_CACHE = previous_cache
        if previous_semantic is not None:
            config.ENABLE_SEMANTIC_RANKING = previous_semantic
        arm.restore_overrides(previous_config)
        arm.restore_overrides(candidate_previous)

    artifacts = _collect_artifacts(task, trial, result, error)
    grades = grade_all(task, artifacts, _graders(arm, live))
    summary = aggregate(grades)

    return {
        "task_id": task.task_id,
        "trial": trial,
        "arm": arm.name,
        "config_hash": harness_config.fingerprint(),
        "category": task.category.value,
        "split": task.split.value,
        "expected_outcome": task.expected_outcome.value,
        "gold_status": task.gold_status.value,
        "fixture_scenario": task.fixture_scenario,
        "live": live,
        "candidate_overrides": dict(candidate_overrides or {}),
        "unapplied_overrides": unapplied_overrides,
        "run_id": artifacts.run_id,
        "status": artifacts.status,
        "stop_reason": artifacts.stop_reason,
        "duration_s": round(time.perf_counter() - started, 4),
        "error": error,
        "trace_path": repo_path(result.get("harness_trace_path", "")),
        "report_path": repo_path(result.get("report_path", "")),
        "num_sources": int(result.get("num_sources", 0) or 0),
        "grades": [g.model_dump(mode="json") for g in grades],
        "summary": summary,
        "recorded_at": _now(),
    }


def _run_with_interrupt(runner: HarnessRunner, request: ResearchRequest) -> dict[str, Any]:
    """Kill the run after browse, then resume it from its checkpoint.

    This exercises the real resume path end to end inside the eval, rather than
    asserting about it in a unit test only.
    """
    original_end_phase = runner.end_phase

    def end_phase(phase, stage, context, success, error=None):
        original_end_phase(phase, stage, context, success, error)
        if stage == "browse":
            runner.cancel("eval: simulated process kill after browse")

    runner.end_phase = end_phase  # type: ignore[method-assign]
    first = runner.run(request)
    run_id = first["harness_run_id"]

    resumed = HarnessRunner(runner.config, registry=runner.registry,
                            run_store=runner.run_store,
                            checkpoint_store=runner.checkpoint_store,
                            artifacts=runner.artifacts)
    return resumed.run(request, resume_from=run_id)


def _graders(arm: ArmConfig, live: bool) -> list[Any]:
    graders = list(DEFAULT_GRADERS)
    if arm.use_llm_judge and live:
        graders.append(LLMJudgeGrader(model=arm.judge_model or config.MODEL_NAME,
                                      enabled=True, generator_model=config.MODEL_NAME))
    return graders


def _collect_artifacts(task: EvalTask, trial: int, result: dict[str, Any],
                       error: str) -> RunArtifacts:
    events: list[dict[str, Any]] = []
    if trace_path := result.get("harness_trace_path"):
        events = TraceStore.read(Path(trace_path))

    report_markdown = ""
    if report_path := result.get("report_path"):
        path = Path(report_path)
        if path.exists():
            report_markdown = path.read_text(encoding="utf-8", errors="ignore")

    sources: list[dict[str, Any]] = []
    if sources_path := result.get("sources_path"):
        path = Path(sources_path)
        if path.exists():
            try:
                sources = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                sources = []

    return RunArtifacts(
        task_id=task.task_id, trial=trial,
        run_id=str(result.get("harness_run_id", "")),
        status=str(result.get("harness_status", "failed" if error else "failed")),
        stop_reason=str(result.get("harness_stop_reason", "")),
        report_markdown=report_markdown, report_path=str(result.get("report_path", "")),
        sources=sources, evaluation=result.get("evaluation") or {},
        events=events, duration_s=float(result.get("duration", 0.0) or 0.0), error=error)


# --------------------------------------------------------------------------- #
# Suite
# --------------------------------------------------------------------------- #
def _done_keys(path: Path) -> set[tuple[str, int, str]]:
    keys: set[tuple[str, int, str]] = set()
    if not path.exists():
        return keys
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            keys.add((row.get("task_id", ""), int(row.get("trial", 0)), row.get("arm", "")))
    return keys


def run_suite(tasks: list[EvalTask], arm: ArmConfig, *, trials: int, live: bool,
              results_path: Path, resume: bool, base_dir: Path,
              limit: Optional[int] = None,
              candidate_overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    results_path.parent.mkdir(parents=True, exist_ok=True)
    already = _done_keys(results_path) if resume else set()
    if not resume and results_path.exists():
        results_path.unlink()

    if limit:
        tasks = tasks[:limit]

    unknown = unknown_scenarios([t.fixture_scenario for t in tasks])
    if unknown:
        print(f"[warn] unknown fixture scenarios (falling back to default world): {unknown}")

    total = len(tasks) * trials
    executed = skipped = 0
    started = time.perf_counter()

    for index, task in enumerate(tasks, 1):
        for trial in range(1, trials + 1):
            if (task.task_id, trial, arm.name) in already:
                skipped += 1
                continue
            print(f"[{index}/{len(tasks)}] {task.task_id} trial {trial}/{trials} "
                  f"arm={arm.name} scenario={task.fixture_scenario or 'default'}", flush=True)
            row = run_one_trial(task, arm, trial, live=live, base_dir=base_dir,
                                candidate_overrides=candidate_overrides)
            with results_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            executed += 1
            print(f"    -> status={row['status']} score={row['summary']['overall_score']} "
                  f"stop={row['stop_reason']} {row['duration_s']}s", flush=True)

    return {
        "arm": arm.name,
        "tasks": len(tasks),
        "trials_per_task": trials,
        "planned": total,
        "executed": executed,
        "skipped_resumed": skipped,
        "wall_time_s": round(time.perf_counter() - started, 2),
        "results_path": str(results_path),
        "live": live,
        "candidate_overrides": dict(candidate_overrides or {}),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "model": config.MODEL_NAME if live else "offline-stub",
            # The *effective* values for this run, not the ambient config.
            # `run_one_trial` disables both for offline replay and restores them
            # in its finally block, so reading `config` here reports the
            # restored value and claims the eval ran with a shared search cache
            # when it did not. That is a provenance lie in a result file.
            "semantic_ranking": config.ENABLE_SEMANTIC_RANKING if live else False,
            "search_cache": config.ENABLE_SEARCH_CACHE if live else False,
            "offline_overrides_applied": not live,
            "use_mcp": config.USE_MCP_TOOLS,
        },
        "recorded_at": _now(),
    }


# --------------------------------------------------------------------------- #
def load_tasks(split: Optional[str], dataset_dir: Path = DATASET_DIR) -> list[EvalTask]:
    if split == "regression_rebuilt":
        path = dataset_dir / "regression_rebuilt.json"
        if not path.exists():
            raise SystemExit(f"{path} missing; run python -m evals.datasets.regression_rebuild --write")
        return EvalDataset.load(path).tasks
    if split == "adversarial":
        path = dataset_dir / "adversarial_tools.json"
        if not path.exists():
            raise SystemExit(f"{path} missing; run python -m evals.datasets.adversarial_tools --write")
        return EvalDataset.load(path).tasks
    if split and split != "all":
        path = dataset_dir / f"{split}.json"
        if not path.exists():
            raise SystemExit(f"dataset not found: {path}; run python -m evals.datasets.build_datasets --write")
        return EvalDataset.load(path).tasks
    return EvalDataset.load(dataset_dir / "all_tasks.json").tasks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the agent evaluation suite")
    parser.add_argument("--split", default="dev",
                        choices=[s.value for s in Split] + ["all", "adversarial", "regression_rebuilt"])
    parser.add_argument("--arm", default="harness", choices=sorted(ARMS))
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None, help="only the first N tasks")
    parser.add_argument("--live", action="store_true",
                        help="use the real model and real network (costs money; off by default)")
    parser.add_argument("--resume", action="store_true",
                        help="skip (task, trial, arm) triples already in the results file")
    parser.add_argument("--smoke", action="store_true",
                        help="tiny offline sanity run: 5 dev tasks, 1 trial")
    parser.add_argument("--results", type=Path, default=None)
    parser.add_argument("--base-dir", type=Path, default=None)
    parser.add_argument("--candidate-version", default=None,
                        help="apply an optimization candidate's config_overrides "
                             "(from outputs/optimization/versions) before running")
    return parser.parse_args()


def _candidate_overrides(version: Optional[str]) -> dict[str, Any]:
    if not version:
        return {}
    from src.optimization.version_store import VersionStore

    store = VersionStore(Path(__file__).resolve().parents[2] / "outputs" / "optimization" / "versions")
    record = store.get(version)
    if record is None:
        raise SystemExit(f"unknown candidate version {version!r}")
    print(f"applying candidate {version}: {record.candidate.change_type.value} -> "
          f"{record.candidate.target}; overrides={record.candidate.config_overrides}")
    return dict(record.candidate.config_overrides)


def main() -> int:
    args = parse_args()
    arm = resolve_arm(args.arm)

    if args.smoke:
        tasks, trials, split_label = load_tasks("dev")[:5], 1, "smoke"
    else:
        tasks, trials, split_label = load_tasks(args.split), args.trials, args.split

    results_path = args.results or (RESULTS_DIR / f"{split_label}_{arm.name}.jsonl")
    base_dir = args.base_dir or (RESULTS_DIR / "runs")

    print(f"tasks={len(tasks)} split={split_label} arm={arm.name} trials={trials} "
          f"live={args.live} results={results_path}")
    if not args.live:
        print("offline mode: fixture tools + stubbed LLM (no network, no cost, deterministic)")

    overrides = _candidate_overrides(args.candidate_version)
    summary = run_suite(tasks, arm, trials=trials, live=args.live, results_path=results_path,
                        resume=args.resume, base_dir=base_dir, limit=args.limit,
                        candidate_overrides=overrides)
    if summary.get("candidate_overrides"):
        print(f"candidate overrides applied: {summary['candidate_overrides']}")

    meta_path = results_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # Keep the human-readable report transactionally aligned with the raw
    # JSONL.  Previously a later eval could replace dev_harness.jsonl while an
    # older Markdown report remained beside it, making a truthful but stale
    # metric table look current.
    from evals.reports.build_report import write_report

    baseline_path = RESULTS_DIR / "dev_baseline_v4.jsonl"
    compare = (baseline_path if split_label == "dev" and arm.name == "harness"
               and baseline_path.exists() and results_path.resolve() != baseline_path.resolve()
               else None)
    report_path, report_json_path = write_report(results_path, compare=compare)
    print(f"report: {report_path}")
    print(f"report metrics: {report_json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
