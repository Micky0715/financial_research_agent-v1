"""CLI entrypoint: python main.py --topic "宁德时代投资分析"."""
import argparse
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from rich.console import Console

from config import config
from orchestrator.workflow import WorkflowOrchestrator
from schemas.request import ResearchRequest
from src.runtime.runner import HarnessConfig, HarnessRunner

console = Console()


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for a single research-report generation run."""
    parser = argparse.ArgumentParser(description="金融研报多智能体自动生成系统")
    parser.add_argument("--topic", required=True, help="研究主题，例如 '宁德时代投资分析'")
    parser.add_argument("--report_type", default="company_research",
                        choices=["company_research", "industry_research", "macro_research",
                                 "risk_research", "valuation_research"])
    parser.add_argument("--output_format", default=config.OUTPUT_FORMAT, choices=["markdown", "html"])
    parser.add_argument("--max_sources", type=int, default=config.TOP_K_SOURCES)
    parser.add_argument("--max_results", type=int, default=config.SEARCH_MAX_RESULTS)
    parser.add_argument(
        "--requirements",
        default="",
        help="逗号分隔的需求列表，例如 '公司概况,财务分析,估值分析,风险提示'",
    )
    parser.add_argument("--language", default="zh")
    parser.add_argument(
        "--local-files",
        default="",
        help="逗号分隔的本地文件路径（PDF/TXT/MD/CSV/Excel），作为额外来源参与分析",
    )
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="Run without the Agent Harness (explicit migration rollback).",
    )
    parser.add_argument(
        "--resume-run-id",
        default="",
        help="Resume a checkpointed Harness run by run id.",
    )
    parser.add_argument(
        "--enable-memory",
        action="store_true",
        help="Enable namespaced long-term memory retrieval and post-run learning.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the full pipeline for one ResearchRequest built from CLI args."""
    args = parse_args()

    requirements = [r.strip() for r in args.requirements.split(",") if r.strip()]
    request = ResearchRequest(
        topic=args.topic,
        report_type=args.report_type,
        requirements=requirements,
        output_format=args.output_format,
        language=args.language,
        max_sources=args.max_sources,
        local_files=[f.strip() for f in args.local_files.split(",") if f.strip()],
    )

    console.print(f"[bold cyan]研究主题:[/bold cyan] {request.topic}")
    console.print(f"[bold cyan]报告类型:[/bold cyan] {request.report_type}")

    def on_progress(step_index: int, total_steps: int, label: str) -> None:
        console.print(f"[{step_index}/{total_steps}] {label}")

    orchestrator = WorkflowOrchestrator()
    try:
        use_harness = config.USE_AGENT_HARNESS and not args.legacy
        if use_harness:
            runner = HarnessRunner(HarnessConfig(
                max_concurrency=config.HARNESS_MAX_CONCURRENCY,
                context_budget_tokens=config.HARNESS_CONTEXT_BUDGET_TOKENS,
                memory_enabled=args.enable_memory,
                namespace="cli",
                arm="production_cli",
            ))
            summary = runner.run(
                request,
                orchestrator=orchestrator,
                resume_from=args.resume_run_id or None,
                on_progress=on_progress,
            )
        else:
            if args.resume_run_id:
                raise ValueError("--resume-run-id requires the Agent Harness; remove --legacy")
            summary = orchestrator.run(request, on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001 - top-level guard, never crash silently
        console.print(f"[bold red]流程执行失败:[/bold red] {exc}")
        return 1

    console.print()
    console.print(f"[bold green]Report saved to[/bold green] {summary.get('report_path', '')}")
    console.print(f"[bold green]Trace saved to[/bold green] {summary.get('trace_path', '')}")
    console.print(f"[bold green]Sources saved to[/bold green] {summary.get('sources_path', '')}")
    console.print(f"[bold green]Evaluation saved to[/bold green] {summary.get('evaluation_path', '')}")
    console.print(f"[bold]来源数量:[/bold] {summary.get('num_sources', 0)}  "
                  f"[bold]总耗时:[/bold] {summary.get('duration', 0)}s")
    if summary.get("harness_run_id"):
        console.print(f"[bold]Harness run:[/bold] {summary['harness_run_id']}  "
                      f"[bold]status:[/bold] {summary.get('harness_status')}  "
                      f"[bold]stop:[/bold] {summary.get('harness_stop_reason')}")
        console.print(f"[bold green]Harness trace saved to[/bold green] "
                      f"{summary.get('harness_trace_path', '')}")

    evaluation = summary.get("evaluation") or {}
    if evaluation:
        console.print(f"[bold]质量总分:[/bold] {evaluation.get('overall_score')}")
        console.print(f"[bold]分项得分:[/bold] {evaluation.get('criteria_scores')}")

    if summary.get("harness_status") in {"failed", "cancelled", "insufficient"}:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
