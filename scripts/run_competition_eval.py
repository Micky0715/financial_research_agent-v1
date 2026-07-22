"""Run the 30-case layered competition eval suite (v4 stage K).

Usage:
    python scripts/run_competition_eval.py --run
    python scripts/run_competition_eval.py --collect-only

30 个人工分层构建的固定 case（eval/topics_competition_30.json：公司10/行业10/
宏观10，每类内含至少1个季度跟踪题/1个年度跟踪题/1个风险分析题/1个图表题，
用 tags 字段标注）——不是随机抽样，不能宣称代表所有金融任务泛化能力。

--run 用真实 pipeline 跑完全部 30 个 case（每个 case 就是正常一次
WorkflowOrchestrator.run()，产物落盘方式与 main.py 完全一致），对带
tracking tag 的公司 case 额外调用真实的 tracking_report_builder。一个 case
失败绝不影响其余 case——禁止伪造未完成 case，失败的就如实记 failed。

结果写入 outputs/eval/competition_30_runs.json（run_id/tracking 结果索引），
供 scripts/build_competition_report.py 聚合。
"""
import argparse
import sys
import time
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from config import config  # noqa: E402
from orchestrator.workflow import WorkflowOrchestrator  # noqa: E402
from schemas.request import ResearchRequest  # noqa: E402
from utils.file_utils import load_json, save_json  # noqa: E402
from utils.logger import logger  # noqa: E402

_TOPICS_PATH = config.BASE_DIR / "eval" / "topics_competition_30.json"
_RUNS_PATH = config.EVAL_DIR / "competition_30_runs.json"


def load_competition_topics() -> list[dict[str, Any]]:
    try:
        entries = load_json(_TOPICS_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] could not load {_TOPICS_PATH}: {exc}")
        return []
    return [e for e in entries if isinstance(e, dict) and e.get("topic")]


def _run_tracking_if_tagged(entry: dict[str, Any]) -> dict[str, Any]:
    """公司类跟踪题额外跑一次真实 tracking_report_builder（阶段D 的工具本身
    是公司三表专用；行业/宏观的跟踪题走常规 report_type 管线验证，见报告说明）。"""
    tags = entry.get("tags", [])
    if entry.get("category") != "company" or not entry.get("tracking_symbol"):
        return {"attempted": False}
    period = entry.get("tracking_period", "annual")
    try:
        from tracking.tracking_report_builder import build_tracking_report

        result = build_tracking_report(entry["tracking_symbol"], period)
        return {
            "attempted": True, "period_type": period,
            "insufficient_history": result.insufficient_history,
            "changes": len(result.changes), "report_path": result.report_path,
        }
    except Exception as exc:  # noqa: BLE001 - one case's tracking add-on must not abort the suite
        logger.warning(f"competition eval tracking add-on failed for {entry['topic']}: {exc!r}")
        return {"attempted": True, "error": f"{type(exc).__name__}: {str(exc)[:150]}"}


def execute_topics(topics: list[dict[str, Any]],
                   already_done: Optional[dict[str, dict]] = None) -> list[dict[str, Any]]:
    """already_done: {topic: run_entry} from a prior partial run (see --resume).
    Those topics are skipped entirely (not re-executed) and their existing
    entry is carried over verbatim into the returned list, in original order.
    Saves incrementally after every case so a killed process loses at most
    one in-flight case, not the whole suite."""
    already_done = already_done or {}
    orchestrator = WorkflowOrchestrator()
    runs: list[dict[str, Any]] = []
    for i, entry in enumerate(topics, 1):
        topic = entry["topic"]
        if topic in already_done:
            print(f"[{i}/{len(topics)}] {topic} - already completed in a prior run, skipping (--resume)")
            runs.append(already_done[topic])
            save_json(_RUNS_PATH, runs)
            continue

        print(f"[{i}/{len(topics)}] {topic} ({entry.get('category')}/{entry.get('report_type')}) ...")
        request = ResearchRequest(
            topic=topic, report_type=entry.get("report_type", "company_research"),
            requirements=[r.strip() for r in entry.get("requirements", "").split(",") if r.strip()],
            output_format=entry.get("output_format", "html"),
            max_sources=entry.get("max_sources", config.TOP_K_SOURCES),
        )
        t0 = time.perf_counter()
        run_id, error = None, None
        try:
            result = orchestrator.run(request)
            run_id = result.get("run_id")
        except Exception as exc:  # noqa: BLE001 - one bad case must not stop the suite
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            logger.warning(f"competition eval case failed: {topic}: {exc!r}")

        tracking_result = _run_tracking_if_tagged(entry)
        runs.append({
            "topic": topic, "category": entry.get("category"),
            "report_type": entry.get("report_type"), "tags": entry.get("tags", []),
            "run_id": run_id, "error": error,
            "wall_time": round(time.perf_counter() - t0, 2),
            "tracking_result": tracking_result,
        })
        print(f"    -> run_id={run_id} error={error} wall_time={runs[-1]['wall_time']}s "
              f"tracking={tracking_result.get('attempted')}")
        save_json(_RUNS_PATH, runs)  # incremental: a killed process keeps all progress so far
    return runs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="30-case competition eval runner")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true", help="真实执行全部 30 个 case")
    mode.add_argument("--collect-only", action="store_true",
                      help="不执行，只重新读取上次 --run 写下的 competition_30_runs.json")
    parser.add_argument("--resume", action="store_true",
                        help="与 --run 搭配：跳过 competition_30_runs.json 里已有 run_id 且无 error 的 topic，"
                             "只继续跑剩余的（进程被中断后用这个续跑，不重新烧一遍已完成的 case）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    topics = load_competition_topics()
    if not topics:
        print(f"[error] no topics loaded from {_TOPICS_PATH}")
        return 1
    print(f"loaded {len(topics)} competition topics "
         f"(company={sum(1 for t in topics if t.get('category')=='company')}, "
         f"industry={sum(1 for t in topics if t.get('category')=='industry')}, "
         f"macro={sum(1 for t in topics if t.get('category')=='macro')})")

    if args.run:
        already_done = {}
        if args.resume and _RUNS_PATH.exists():
            try:
                prior = load_json(_RUNS_PATH)
                already_done = {r["topic"]: r for r in prior
                               if r.get("run_id") and not r.get("error")}
                print(f"--resume: found {len(already_done)} already-completed case(s) in {_RUNS_PATH}")
            except Exception as exc:  # noqa: BLE001 - corrupt partial file -> just start fresh
                print(f"[warn] could not read prior run index for --resume: {exc}")
        runs = execute_topics(topics, already_done)
        save_json(_RUNS_PATH, runs)
        print(f"saved run index to {_RUNS_PATH}")
    else:
        if not _RUNS_PATH.exists():
            print(f"[error] {_RUNS_PATH} not found; run with --run first")
            return 1
        runs = load_json(_RUNS_PATH)
        print(f"loaded existing run index ({len(runs)} entries), executed nothing")

    succeeded = sum(1 for r in runs if r.get("run_id") and not r.get("error"))
    print(f"completed: {succeeded}/{len(runs)} cases produced a run_id without error "
         "(this is a raw execution count, not the final quality metrics - "
         "see scripts/build_competition_report.py for the full metric set)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
