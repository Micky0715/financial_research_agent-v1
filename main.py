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

console = Console()


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for a single research-report generation run."""
    parser = argparse.ArgumentParser(description="金融研报多智能体自动生成系统")
    parser.add_argument("--topic", required=True, help="研究主题，例如 '宁德时代投资分析'")
    parser.add_argument("--report_type", default="company_research", choices=["company_research", "industry_research"])
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
        summary = orchestrator.run(request, on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001 - top-level guard, never crash silently
        console.print(f"[bold red]流程执行失败:[/bold red] {exc}")
        return 1

    console.print()
    console.print(f"[bold green]Report saved to[/bold green] {summary['report_path']}")
    console.print(f"[bold green]Trace saved to[/bold green] {summary['trace_path']}")
    console.print(f"[bold green]Sources saved to[/bold green] {summary['sources_path']}")
    console.print(f"[bold green]Evaluation saved to[/bold green] {summary['evaluation_path']}")
    console.print(f"[bold]来源数量:[/bold] {summary['num_sources']}  [bold]总耗时:[/bold] {summary['duration']}s")

    evaluation = summary.get("evaluation") or {}
    if evaluation:
        console.print(f"[bold]质量总分:[/bold] {evaluation.get('overall_score')}")
        console.print(f"[bold]分项得分:[/bold] {evaluation.get('criteria_scores')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
