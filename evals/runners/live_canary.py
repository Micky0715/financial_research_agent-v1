"""Live provider canary (Phase 5).

Why this exists: every number in this project so far comes from offline fixture
replay. 329 tests pass with the network hard-blocked, which is excellent for
reproducibility and useless as evidence that the system works against a real
provider. Token counts and costs are structurally 0 offline, so any claim about
real cost or latency has had no basis at all.

This runner is the only thing in the repo that spends money. Properties:

- **Explicit and small.** Default 16 tasks, 1 trial each, stratified across
  company / industry / macro / PDF / structured-data / failure-recovery. No
  multi-trial, no full split.
- **Pre-flight cost estimate and a hard stop.** It prints an estimate and
  refuses to start without `--yes`. A `--max-cost-usd` ceiling aborts the run
  mid-way rather than discovering the bill afterwards.
- **Incremental save.** One JSONL row per finished task, so a killed canary
  keeps everything already paid for.
- **Never merged with fixture results.** Output goes to its own file and its own
  report, and the report states the model, date and config hash. Averaging a
  live success rate together with a fixture one would be meaningless.

    python -m evals.runners.live_canary --dry-run      # estimate only, free
    python -m evals.runners.live_canary --yes          # actually spend money
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from config import config  # noqa: E402
from evals.datasets.schema import (  # noqa: E402
    Category,
    EvalTask,
    ExpectedOutcome,
    GoldStatus,
    Split,
)
from evals.graders.base import RunArtifacts, aggregate  # noqa: E402
from evals.runners.run_eval import repo_path  # noqa: E402
from evals.graders.deterministic import DEFAULT_GRADERS, grade_all  # noqa: E402
from schemas.request import ResearchRequest  # noqa: E402
from src.observability.metrics import RunMetrics  # noqa: E402
from src.observability.trace_store import TraceStore  # noqa: E402
from src.runtime.budget import BudgetLimits  # noqa: E402
from src.runtime.runner import HarnessConfig, HarnessRunner  # noqa: E402
from src.tools.registry import build_default_registry  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"
REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"
DEFAULT_RESULTS = RESULTS_DIR / "live_canary.jsonl"

#: Rough per-run cost/latency used only for the pre-flight estimate. Based on
#: the historical v4 30-case run (avg 236s, ~15 tool calls) and public list
#: prices - an estimate, labelled as one.
EST_SECONDS_PER_TASK = 240
EST_COST_PER_TASK_USD = 0.02


def _canary_tasks() -> list[EvalTask]:
    """16 hand-picked real subjects, stratified by what they exercise.

    Real A-share names and real macro topics - the point of a canary is that
    the live provider and the live web are in the loop, so a fabricated subject
    would test nothing.
    """
    specs: list[dict[str, Any]] = [
        # --- company research (4) ---
        {"id": "co_maotai", "q": "贵州茅台投资价值分析", "rt": "company_research",
         "cat": Category.COMPANY_RESEARCH, "exercises": "公司研究主链路 + AkShare 三表"},
        {"id": "co_catl", "q": "宁德时代投资分析", "rt": "company_research",
         "cat": Category.COMPANY_RESEARCH, "exercises": "公司研究 + 估值"},
        {"id": "co_byd", "q": "比亚迪投资价值分析", "rt": "company_research",
         "cat": Category.COMPANY_RESEARCH, "exercises": "公司研究 + 同业比较"},
        {"id": "co_smic", "q": "中芯国际投资风险分析", "rt": "risk_research",
         "cat": Category.COMPANY_RESEARCH, "exercises": "风险型报告"},
        # --- industry research (3) ---
        {"id": "ind_pv", "q": "光伏行业投资风险分析", "rt": "industry_research",
         "cat": Category.INDUSTRY_RESEARCH, "exercises": "行业研究 + 集中度"},
        {"id": "ind_semi", "q": "半导体国产替代行业研究", "rt": "industry_research",
         "cat": Category.INDUSTRY_RESEARCH, "exercises": "行业研究 + 产业链"},
        {"id": "ind_ev", "q": "新能源汽车产业链分析", "rt": "industry_research",
         "cat": Category.INDUSTRY_RESEARCH, "exercises": "行业研究 + 三年情景"},
        # --- macro research (3) ---
        {"id": "mac_cpi", "q": "CPI与PPI走势分析", "rt": "macro_research",
         "cat": Category.MACRO_RESEARCH, "exercises": "宏观指标实取 + 传导链"},
        {"id": "mac_rate", "q": "利率环境与货币政策跟踪", "rt": "macro_research",
         "cat": Category.MACRO_RESEARCH, "exercises": "宏观 + 政策解析"},
        {"id": "mac_export", "q": "出口形势与外需分析", "rt": "macro_research",
         "cat": Category.MACRO_RESEARCH, "exercises": "宏观 + 灰犀牛监控"},
        # --- structured data / valuation (2) ---
        {"id": "sd_hengrui", "q": "恒瑞医药财务与估值分析", "rt": "valuation_research",
         "cat": Category.STRUCTURED_DATA, "exercises": "AkShare 结构化取数 + DCF"},
        {"id": "sd_cyd", "q": "长江电力高股息价值分析", "rt": "company_research",
         "cat": Category.STRUCTURED_DATA, "exercises": "结构化取数 + 股息"},
        # --- PDF / document extraction (1) ---
        {"id": "pdf_annual", "q": "贵州茅台2024年年度报告要点提取", "rt": "company_research",
         "cat": Category.PDF_EXTRACTION, "exercises": "年报 PDF 抽取（可能降级为网页）"},
        # --- must-refuse (1): the live relevance layer really does get fooled ---
        {"id": "refuse_fictional", "q": "希兹维恩量子玄武科技投资分析",
         "rt": "company_research", "cat": Category.MUST_REFUSE,
         "outcome": ExpectedOutcome.INSUFFICIENT_EVIDENCE,
         "forbidden": ["营业收入", "净利润", "目标价"],
         "exercises": "真实网络下的虚构主体拦截（实体校验）"},
        # --- failure recovery (2): budget-starved, and a thin-source topic ---
        {"id": "rec_tight_budget", "q": "美的集团出口业务分析", "rt": "company_research",
         "cat": Category.INSUFFICIENT_BUDGET, "tool_cap": 8,
         "outcome": ExpectedOutcome.PARTIAL_WITH_DEGRADATION,
         "exercises": "真实网络下的预算降级阶梯"},
        {"id": "rec_thin_topic", "q": "低空经济行业研报", "rt": "industry_research",
         "cat": Category.INDUSTRY_RESEARCH,
         "outcome": ExpectedOutcome.REPORT_WITH_CAVEATS,
         "exercises": "冷门主题：来源稀薄下的 fallback 检索"},
    ]

    tasks: list[EvalTask] = []
    for spec in specs:
        tasks.append(EvalTask(
            task_id=f"canary_{spec['id']}",
            category=spec["cat"],
            query=spec["q"],
            report_type=spec["rt"],
            expected_outcome=spec.get("outcome", ExpectedOutcome.REPORT_WITH_CAVEATS),
            forbidden_claims=spec.get("forbidden", ["保证收益", "稳赚不赔"]),
            budget_limit=({"tool_calls": spec["tool_cap"]} if spec.get("tool_cap") else {}),
            task_constraints={"exercises": spec["exercises"]},
            tags=["live_canary", spec["cat"].value],
            data_source="hand_picked_live_canary",
            # Live behaviour is real, but nobody has reviewed what the *correct*
            # answer is for these subjects.
            gold_status=GoldStatus.NEEDS_HUMAN_REVIEW,
            group_key=spec["id"],
            split=Split.TEST,
            fixture_scenario="",
        ))
    return tasks


def _preflight() -> dict[str, Any]:
    """Config readiness. Never prints the key itself."""
    keys = {
        "OPENAI_API_KEY": bool(config.OPENAI_API_KEY),
        "DEEPSEEK_API_KEY": bool(config.DEEPSEEK_API_KEY),
        "ANTHROPIC_API_KEY": bool(config.ANTHROPIC_API_KEY),
    }
    return {
        "model": config.MODEL_NAME,
        "api_base": config.OPENAI_API_BASE or "(provider default)",
        "any_key_configured": any(keys.values()),
        "keys_present": [k for k, v in keys.items() if v],
        "use_mcp": config.USE_MCP_TOOLS,
        "search_cache": config.ENABLE_SEARCH_CACHE,
        "semantic_ranking": config.ENABLE_SEMANTIC_RANKING,
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def _done_ids(path: Path) -> set[str]:
    done: set[str] = set()
    if not path.exists():
        return done
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                done.add(json.loads(line)["task_id"])
            except Exception:  # noqa: BLE001
                continue
    return done


def run_canary(tasks: list[EvalTask], *, results_path: Path, base_dir: Path,
               max_cost_usd: float, resume: bool) -> dict[str, Any]:
    already = _done_ids(results_path) if resume else set()
    if not resume and results_path.exists():
        results_path.unlink()
    results_path.parent.mkdir(parents=True, exist_ok=True)

    spent = 0.0
    executed = 0
    aborted_reason = ""
    started = time.perf_counter()

    for index, task in enumerate(tasks, 1):
        if task.task_id in already:
            print(f"[{index}/{len(tasks)}] {task.task_id} 已完成，跳过（--resume）", flush=True)
            continue
        if spent >= max_cost_usd:
            aborted_reason = (f"cost ceiling reached: 已花费估算 ${spent:.4f} >= "
                              f"上限 ${max_cost_usd:.2f}")
            print(f"[abort] {aborted_reason}", flush=True)
            break

        budget = BudgetLimits(max_duration_s=900.0, max_cost_usd=max(0.05, max_cost_usd - spent))
        if task.budget_limit.get("tool_calls"):
            budget = budget.model_copy(update={"max_tool_calls": int(task.budget_limit["tool_calls"])})

        harness_config = HarnessConfig(base_dir=base_dir, arm="live_canary", budget=budget)
        runner = HarnessRunner(harness_config, registry=build_default_registry())

        print(f"[{index}/{len(tasks)}] {task.query}  ({task.task_constraints['exercises']})",
              flush=True)
        t0 = time.perf_counter()
        result: dict[str, Any] = {}
        error = ""
        try:
            result = runner.run(ResearchRequest(
                topic=task.query, report_type=task.report_type,
                requirements=["财务分析", "风险提示"], output_format="markdown",
                max_sources=5))
        except Exception as exc:  # noqa: BLE001 - one bad case must not stop the canary
            error = f"{type(exc).__name__}: {exc}"
            print(f"    !! {error}", flush=True)

        wall = round(time.perf_counter() - t0, 2)
        events = (TraceStore.read(Path(result["harness_trace_path"]))
                  if result.get("harness_trace_path") else [])
        metrics = RunMetrics(events).summary() if events else {}
        cost = float(metrics.get("budget", {}).get("estimated_cost_usd", 0.0) or 0.0)
        spent += cost

        report_markdown = ""
        if result.get("report_path") and Path(result["report_path"]).exists():
            report_markdown = Path(result["report_path"]).read_text(encoding="utf-8",
                                                                    errors="ignore")
        sources: list[dict[str, Any]] = []
        if result.get("sources_path") and Path(result["sources_path"]).exists():
            try:
                sources = json.loads(Path(result["sources_path"]).read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                sources = []

        artifacts = RunArtifacts(
            task_id=task.task_id, trial=1, run_id=str(result.get("harness_run_id", "")),
            status=str(result.get("harness_status", "failed")),
            stop_reason=str(result.get("harness_stop_reason", "")),
            report_markdown=report_markdown, report_path=str(result.get("report_path", "")),
            sources=sources, evaluation=result.get("evaluation") or {},
            events=events, duration_s=wall, error=error)
        grades = grade_all(task, artifacts, DEFAULT_GRADERS)

        row = {
            "task_id": task.task_id, "trial": 1, "arm": "live_canary",
            "live": True, "category": task.category.value,
            "report_type": task.report_type, "query": task.query,
            "exercises": task.task_constraints["exercises"],
            "expected_outcome": task.expected_outcome.value,
            "gold_status": task.gold_status.value,
            "split": task.split.value, "fixture_scenario": "",
            "config_hash": harness_config.fingerprint(),
            "model": config.MODEL_NAME,
            "run_id": artifacts.run_id, "status": artifacts.status,
            "stop_reason": artifacts.stop_reason, "error": error,
            "duration_s": wall, "num_sources": len(sources),
            "trace_path": repo_path(result.get("harness_trace_path", "")),
            "report_path": repo_path(result.get("report_path", "")),
            "live_metrics": {
                "input_tokens": metrics.get("budget", {}).get("input_tokens", 0),
                "output_tokens": metrics.get("budget", {}).get("output_tokens", 0),
                "estimated_cost_usd": cost,
                "model_calls": metrics.get("budget", {}).get("model_calls", 0),
                "tool_calls": metrics.get("budget", {}).get("tool_calls", 0),
                "tool_failures": metrics.get("tool_failures_total", 0),
                "by_error": metrics.get("by_error", {}),
            },
            "grades": [g.model_dump(mode="json") for g in grades],
            "summary": aggregate(grades),
            "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        with results_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        executed += 1
        print(f"    -> {artifacts.status} stop={artifacts.stop_reason} sources={len(sources)} "
              f"{wall}s tokens={row['live_metrics']['input_tokens']}/"
              f"{row['live_metrics']['output_tokens']} cost≈${cost:.5f} "
              f"(累计≈${spent:.4f})", flush=True)

    return {
        "executed": executed, "planned": len(tasks),
        "estimated_cost_usd_total": round(spent, 6),
        "wall_time_s": round(time.perf_counter() - started, 1),
        "aborted_reason": aborted_reason,
        "results_path": str(results_path),
        "preflight": _preflight(),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Live provider canary (spends real money)")
    parser.add_argument("--yes", action="store_true",
                        help="confirm real spending; without it nothing runs")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan, the estimate and the config check, then exit")
    parser.add_argument("--limit", type=int, default=None, help="only the first N tasks")
    parser.add_argument("--max-cost-usd", type=float, default=1.50,
                        help="hard ceiling; the canary aborts rather than exceed it")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    tasks = _canary_tasks()
    if args.limit:
        tasks = tasks[:args.limit]

    pre = _preflight()
    print("=== 配置检查 ===")
    for key, value in pre.items():
        print(f"  {key:20s} {value}")
    print()
    print("=== 计划 ===")
    for task in tasks:
        print(f"  {task.task_id:26s} {task.report_type:18s} {task.query}")
    print()
    print(f"任务数 {len(tasks)}；预计耗时约 "
          f"{len(tasks) * EST_SECONDS_PER_TASK / 60:.0f} 分钟；"
          f"预计费用约 ${len(tasks) * EST_COST_PER_TASK_USD:.2f}"
          f"（估算，真实值以 trace 记账为准）；硬上限 ${args.max_cost_usd:.2f}")

    if not pre["any_key_configured"]:
        print("\n[abort] 未检测到任何 API Key。请在 .env 中配置后重试；"
              "本脚本不会伪造任何 live 结果。")
        return 1

    if args.dry_run or not args.yes:
        print("\n未执行。确认要花真实费用请加 --yes：")
        print(f"  python -m evals.runners.live_canary --yes --max-cost-usd {args.max_cost_usd}")
        return 0

    base_dir = RESULTS_DIR / "live_canary_runs"
    summary = run_canary(tasks, results_path=args.results, base_dir=base_dir,
                         max_cost_usd=args.max_cost_usd, resume=args.resume)

    meta_path = args.results.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    digest = hashlib.sha256(args.results.read_bytes()).hexdigest() if args.results.exists() else ""
    print()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"results sha256: {digest}")
    print(f"\n下一步：python -m evals.reports.build_live_canary_report "
          f"--results {args.results}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
