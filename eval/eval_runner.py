"""Evaluation-suite runner for the fixed eval/topics.json topic list.

Usage:
    python eval/eval_runner.py --help
        Show this help and exit. Never executes anything.

    python eval/eval_runner.py --run
        Actually execute every topic in eval/topics.json through the full
        pipeline (agents/orchestrator - reports/traces/sources/evaluations
        are saved exactly as a normal `python main.py` run would), then
        summarize the results.

    python eval/eval_runner.py --collect-only
        Execute nothing. Just summarize the latest existing outputs/traces
        run for each topic in eval/topics.json (reuses
        scripts/collect_eval_summary.py's logic).

    python eval/eval_runner.py --run --topics eval/topics.json --output-dir outputs/eval
        --topics overrides which topics file to read (default eval/topics.json).
        --output-dir overrides where eval_suite_summary.json is written
        (default outputs/eval, NOT outputs/evaluations).

--run and --collect-only both end by writing outputs/eval/eval_suite_summary.json
via the same aggregation logic, so the two modes produce directly comparable
output - the only difference is whether new runs are executed first.
"""
import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from rich.console import Console  # noqa: E402
from tqdm import tqdm  # noqa: E402

from config import config  # noqa: E402
from orchestrator.workflow import WorkflowOrchestrator  # noqa: E402
from schemas.request import ResearchRequest  # noqa: E402
from scripts.collect_eval_summary import build_rows, load_topics  # noqa: E402
from utils.file_utils import save_json  # noqa: E402
from utils.logger import logger  # noqa: E402

console = Console()

_DEFAULT_TOPICS_PATH = config.BASE_DIR / "eval" / "topics.json"


def parse_args() -> argparse.Namespace:
    """Parse CLI args. `--help` is handled entirely by argparse - it prints usage and calls
    sys.exit() before this function (or anything after it) ever runs, so `--help` can never
    trigger an eval run."""
    parser = argparse.ArgumentParser(
        prog="eval_runner.py",
        description="Run or summarize the fixed eval/topics.json evaluation suite.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--run",
        action="store_true",
        help="Execute every topic in the topics file through the full pipeline.",
    )
    mode.add_argument(
        "--collect-only",
        action="store_true",
        help="Execute nothing - summarize the latest existing run per topic.",
    )
    parser.add_argument(
        "--topics",
        type=Path,
        default=_DEFAULT_TOPICS_PATH,
        help=f"Path to the topics JSON file (default: {_DEFAULT_TOPICS_PATH}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=config.EVAL_DIR,
        help=f"Directory to write eval_suite_summary.json into (default: {config.EVAL_DIR}).",
    )
    return parser.parse_args()


def _parse_requirements(value: Any) -> list[str]:
    """eval/topics.json stores `requirements` as a comma-joined string (matching main.py's
    --requirements CLI flag), not a JSON list - accept either so a hand-edited topics file in
    list form still works."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return []


def execute_topics(topics: list[dict[str, Any]]) -> None:
    """Run every topic through the real pipeline. One bad topic must not stop the suite -
    the orchestrator already persists reports/traces/sources/evaluations per run as it goes,
    so a failure here just means that topic's outputs reflect the failure (see
    scripts/collect_eval_summary.py's no_sources_aborted / missing_run handling)."""
    orchestrator = WorkflowOrchestrator()
    for entry in tqdm(topics, desc="Running eval topics"):
        request = ResearchRequest(
            topic=entry["topic"],
            report_type=entry.get("report_type", "company_research"),
            requirements=_parse_requirements(entry.get("requirements")),
            output_format=entry.get("output_format", config.OUTPUT_FORMAT),
            max_sources=entry.get("max_sources", config.TOP_K_SOURCES),
        )
        try:
            orchestrator.run(request)
        except Exception as exc:  # noqa: BLE001 - one bad topic must not stop the suite
            logger.warning(f"eval topic failed: {request.topic}: {exc}")


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate scripts/collect_eval_summary.py-style rows into an eval_suite_summary.json."""
    total = len(rows)

    def _num(row: dict[str, Any], key: str) -> Any:
        value = row.get(key)
        if isinstance(value, bool):
            return None
        return value if isinstance(value, (int, float)) else None

    ok_rows = [r for r in rows if r.get("main_issue") != "missing_run" and r.get("source_count")]
    n = len(ok_rows)

    durations = [d for r in ok_rows if (d := _num(r, "total_time")) is not None]
    source_counts = [d for r in ok_rows if (d := _num(r, "source_count")) is not None]
    quality_scores = [d for r in ok_rows if (d := _num(r, "quality_score")) is not None]

    return {
        "total_cases": total,
        "succeeded": n,
        "report_generated_rate": round(n / total, 3) if total else 0.0,
        "average_duration": round(sum(durations) / len(durations), 3) if durations else 0.0,
        "average_quality_score": round(sum(quality_scores) / len(quality_scores), 3) if quality_scores else 0.0,
        "average_source_count": round(sum(source_counts) / len(source_counts), 3) if source_counts else 0.0,
        "failed_cases": [
            {"topic": r.get("topic"), "issue": r.get("main_issue")}
            for r in rows
            if r.get("main_issue") == "missing_run" or not r.get("source_count")
        ],
        "results": rows,
    }


def main() -> None:
    args = parse_args()

    topics = load_topics(args.topics)
    if not topics:
        console.print(f"[bold red]No topics loaded from[/bold red] {args.topics}")
        sys.exit(1)

    if args.run:
        console.print(f"Running {len(topics)} eval topic(s) from {args.topics} ...")
        execute_topics(topics)
    else:
        console.print(
            f"--collect-only: summarizing existing outputs for {len(topics)} eval topic(s), running nothing."
        )

    rows = build_rows(args.topics)
    summary = summarize(rows)

    out_path = args.output_dir / "eval_suite_summary.json"
    save_json(out_path, summary)

    console.print(f"[bold green]Eval suite summary saved to[/bold green] {out_path}")
    console.print(
        f"succeeded={summary['succeeded']}/{summary['total_cases']}  "
        f"avg_quality_score={summary['average_quality_score']}  "
        f"avg_source_count={summary['average_source_count']}  "
        f"avg_duration={summary['average_duration']}s"
    )


if __name__ == "__main__":
    main()
