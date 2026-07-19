"""Sequential multi-agent workflow orchestrator for the financial research pipeline.

Executes: planning -> research -> browse -> analyze -> report -> evaluate,
respecting task dependencies. Kept deliberately sequential in v1; the task
list + dependency scheduler here is structured so a future version can swap
in an async/queue-based executor without touching the agents.
"""
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Optional

from agents.analyze_agent import AnalyzeAgent
from agents.browser_agent import BrowserAgent
from agents.planning_agent import PlanningAgent, normalize_plan
from agents.report_agent import ReportAgent
from agents.research_agent import ResearchAgent
from config import config
from schemas.report import Report
from schemas.request import ResearchRequest
from schemas.source import Source
from schemas.task import Task, TaskResult
from schemas.trace import TraceLog
from tools.report_renderer import render_html_report, save_report
from utils.file_utils import sanitize_filename, save_json
from utils.logger import logger

_CONTEXT_KEY_BY_TASK_TYPE = {
    "research": "search_results",
    "browse": "sources",
    "analyze": "analysis",
    "report": "report",
    "evaluate": "evaluation",
}

_STEP_LABEL_BY_TASK_TYPE = {
    "research": "Searching sources...",
    "browse": "Reading and scoring sources...",
    "analyze": "Analyzing content...",
    "report": "Generating report...",
    "evaluate": "Evaluating report...",
}

# Dynamic replan (docs/dynamic_planning.md): when browse fails because the
# candidate supply was thin (not because of relevance-threshold issues, which
# have their own relaxed-relevance fallback), re-run research once with
# forced authoritative-site fallback queries and retry browse on the new
# candidate pool. Hard bound - deliberately NOT configurable to unlimited:
# each extra round costs live searches + fetches, and one targeted retry is
# the point of diminishing returns for this failure mode.
_MAX_REPLAN_ROUNDS = 1


class WorkflowOrchestrator:
    """Coordinates PlanningAgent -> ResearchAgent -> BrowserAgent -> AnalyzeAgent -> ReportAgent."""

    def __init__(self) -> None:
        self.planning_agent = PlanningAgent()
        self.research_agent = ResearchAgent()
        self.browser_agent = BrowserAgent()
        self.analyze_agent = AnalyzeAgent()
        self.report_agent = ReportAgent()
        self._agent_by_task_type = {
            "research": self.research_agent,
            "browse": self.browser_agent,
            "analyze": self.analyze_agent,
            "report": self.report_agent,
            "evaluate": self.report_agent,
        }

    def _schedule(self, tasks: list[Task]) -> list[Task]:
        """Order tasks by dependency-satisfaction then priority (simple topological sort)."""
        remaining = list(tasks)
        by_id = {t.task_id: t for t in tasks}
        ordered: list[Task] = []
        done_ids: set[str] = set()

        while remaining:
            ready = [t for t in remaining if all(d in done_ids for d in t.dependencies)]
            if not ready:
                # Circular or unresolved dependency: append the rest as-is so
                # the executor can surface a clear dependency error per task.
                ordered.extend(remaining)
                break
            ready.sort(key=lambda t: t.priority)
            picked = ready[0]
            ordered.append(picked)
            done_ids.add(picked.task_id)
            remaining.remove(picked)

        return ordered

    def run(
        self,
        request: ResearchRequest,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict[str, Any]:
        """Execute the full pipeline for `request` and persist all four output artifacts.

        `on_progress(step_index, total_steps, label)` is called once per stage
        (planning, research, browse, analyze, report, evaluate) right before
        that stage runs, so a CLI can print live progress.
        """
        total_steps = 6

        def notify(step_index: int, label: str) -> None:
            if on_progress:
                on_progress(step_index, total_steps, label)

        run_id = uuid.uuid4().hex[:10]
        started_at = datetime.now().isoformat(timespec="seconds")
        run_start = time.perf_counter()

        trace = TraceLog(run_id=run_id, topic=request.topic, started_at=started_at)
        context: dict[str, Any] = {
            "request": request,
            "tasks": [],
            "search_results": {},
            "sources": {},
            "analysis": {},
            "report": {},
            "evaluation": {},
        }

        # 1. Planning
        notify(1, "Planning task...")
        plan_start = time.perf_counter()
        raw_tasks = self.planning_agent.plan(request)
        # Defensive normalization at the execution boundary: even though
        # PlanningAgent falls back to a clean 5-task plan on its own failure
        # paths, the LLM-generated plan (the common success path) has been
        # observed emitting 9-11 tasks with duplicate research/browse stages,
        # which made the scheduler below actually re-run those stages
        # multiple times. This is the one place that must never be skipped.
        tasks, planner_metrics = normalize_plan(raw_tasks)
        plan_elapsed = round(time.perf_counter() - plan_start, 4)
        context["tasks"] = tasks
        trace.planner_metrics = planner_metrics
        trace.steps.append(
            {
                "agent": "planning_agent",
                "task_id": "planning",
                "task_type": "planning",
                "success": True,
                "detail": f"generated {len(tasks)} tasks",
                "duration": plan_elapsed,
            }
        )
        logger.info(f"run={run_id} plan has {len(tasks)} tasks: {[t.task_type for t in tasks]}")

        # 2-6: research / browse / analyze / report / evaluate
        completed_ids: set[str] = set()
        replan_metrics: dict[str, Any] = {"triggered": False, "reason": None, "rounds": 0, "recovered": False}
        for planned_task in self._schedule(tasks):
            if not all(dep in completed_ids for dep in planned_task.dependencies):
                error_msg = f"unresolved dependency for task {planned_task.task_id}"
                logger.warning(error_msg)
                trace.errors.append({"task_id": planned_task.task_id, "error": error_msg})
                continue

            step_index = list(_STEP_LABEL_BY_TASK_TYPE.keys()).index(planned_task.task_type) + 2
            notify(step_index, _STEP_LABEL_BY_TASK_TYPE.get(planned_task.task_type, planned_task.task_type))

            agent = self._agent_by_task_type.get(planned_task.task_type)
            if agent is None:
                error_msg = f"no agent registered for task_type={planned_task.task_type}"
                logger.warning(error_msg)
                trace.errors.append({"task_id": planned_task.task_id, "error": error_msg})
                continue

            task_result: TaskResult = agent.run(planned_task, context)
            trace.steps.append(
                {
                    "agent": agent.name,
                    "task_id": planned_task.task_id,
                    "task_type": planned_task.task_type,
                    "success": task_result.success,
                    "detail": task_result.error or "ok",
                    "duration": task_result.execution_time,
                }
            )

            # Dynamic replan: browse failed on a thin candidate supply -> jump
            # back to research once with forced fallback queries, then retry
            # browse on the new pool (docs/dynamic_planning.md). Deterministic
            # trigger, hard round limit, failure-path only - the fixed 5-stage
            # plan stays authoritative for every run that succeeds first try.
            if (
                planned_task.task_type == "browse"
                and not task_result.success
                and replan_metrics["rounds"] < _MAX_REPLAN_ROUNDS
                and (task_result.result or {}).get("browser_metrics", {}).get("browser_fallback_triggered")
            ):
                replan_metrics.update(
                    {"triggered": True, "reason": "browse_failed_insufficient_candidates"}
                )
                replan_metrics["rounds"] += 1
                logger.info("Replan round 1: re-running research with forced fallback queries")
                notify(2, "Re-searching sources (replan)...")

                research_retry = Task(
                    task_id="t1_replan",
                    task_type="research",
                    description="重检索：browse 候选不足，追加权威站点 fallback query",
                    parameters={"force_fallback": True},
                    dependencies=[],
                    priority=1,
                    max_retries=0,
                )
                retry_research_result = self.research_agent.run(research_retry, context)
                trace.steps.append(
                    {
                        "agent": self.research_agent.name,
                        "task_id": research_retry.task_id,
                        "task_type": "research",
                        "success": retry_research_result.success,
                        "detail": retry_research_result.error or "ok (replan)",
                        "duration": retry_research_result.execution_time,
                    }
                )
                if retry_research_result.success:
                    context["search_results"] = retry_research_result.result
                    notify(3, "Reading and scoring sources (replan)...")
                    task_result = agent.run(planned_task, context)
                    trace.steps.append(
                        {
                            "agent": agent.name,
                            "task_id": f"{planned_task.task_id}_replan",
                            "task_type": "browse",
                            "success": task_result.success,
                            "detail": task_result.error or "ok (replan)",
                            "duration": task_result.execution_time,
                        }
                    )
                    replan_metrics["recovered"] = task_result.success

            # v3 阶段H：browse 成功后做实体真实性校验（bad_cases 16 修复）。
            # failed -> 清空来源并把 browse 判为逻辑失败，走既有的"资料不足"
            # 终止路径，拒绝为无法验证的主体硬生成研报；weak -> 继续执行但
            # 记录状态，evaluation 里可见。
            if planned_task.task_type == "browse" and task_result.success:
                from tools.entity_validator import validate_entity

                src_objs = [Source(**s) for s in task_result.result.get("sources", [])]
                validation = validate_entity(request.topic, request.report_type, src_objs)
                context["entity_validation"] = validation
                trace.entity_validation = validation
                if validation["validation_status"] == "failed":
                    error_msg = f"insufficient_entity_evidence: {validation['reason']}"
                    task_result = TaskResult(
                        task_id=planned_task.task_id,
                        task_type="browse",
                        success=False,
                        result={"sources": [], "browser_metrics": task_result.result.get("browser_metrics", {})},
                        error=error_msg,
                        execution_time=task_result.execution_time,
                    )
                    logger.warning(error_msg)

            context_key = _CONTEXT_KEY_BY_TASK_TYPE.get(planned_task.task_type)
            if context_key:
                context[context_key] = task_result.result

            if task_result.success:
                completed_ids.add(planned_task.task_id)
            else:
                trace.errors.append({"task_id": planned_task.task_id, "error": task_result.error})
                logger.warning(f"task {planned_task.task_id} ({planned_task.task_type}) failed: {task_result.error}")

        # Persist sources
        source_dicts = context.get("sources", {}).get("sources", [])
        trace.selected_sources = source_dicts

        # Build final Report object
        report_data = context.get("report", {})
        evaluation = context.get("evaluation", {}).get("evaluation", {})

        if not source_dicts:
            # browse failed to find any usable, on-topic source (see
            # BrowserAgent.execute), so analyze/report/evaluate were all
            # skipped by the dependency scheduler above. Refuse to emit
            # something that looks like a real report - an empty analysis
            # dict rendered through the report template would otherwise
            # produce boilerplate "资料不足" text dressed up with all the
            # normal headings, which is misleading and defeats the whole
            # anti-hallucination point of this pipeline.
            entity_validation = context.get("entity_validation", {})
            entity_failed = entity_validation.get("validation_status") == "failed"
            trace.errors.append({"task_id": "report", "error": (
                "aborted: insufficient_entity_evidence" if entity_failed
                else "aborted: no usable sources, report generation skipped")})
            abort_title = (
                f"{request.topic} 研究报告（未生成：" + ("实体无法验证" if entity_failed else "资料不足") + "）"
            )
            if entity_failed:
                # v3 阶段H：主体无法验证 -> 明确说明，不产出泛"资料不足"文案，
                # 并写入带 entity_validation_failed 标记的最小 evaluation。
                abort_markdown = (
                    f"# {request.topic} 研究报告 - 未生成\n\n"
                    "**未找到足够可信来源验证该研究主体的真实性**（insufficient_entity_evidence）。\n\n"
                    f"验证结论：{entity_validation.get('reason', '')}\n\n"
                    "已抓取的候选内容虽包含金融关键词，但没有任何来源正文真实提到该主体，"
                    "也未在 A 股上市公司列表中找到对应实体。为避免基于无关内容生成看似有据的报告，"
                    "本次运行在分析阶段之前终止。\n\n"
                    "若该主体确实存在（如未上市公司/新设实体），请通过 --local-files 提供本地资料后重试。"
                )
                evaluation = {
                    "overall_score": 0.0,
                    "entity_validation_failed": True,
                    "entity_validation": entity_validation,
                    "criteria_scores": {},
                    "diagnostics": {"source_count": 0, "score_cap_reason": "entity_validation_failed"},
                    "weaknesses": ["研究主体无法通过实体真实性校验"],
                    "recommendations": ["确认主体名称拼写，或通过 --local-files 提供本地资料文件"],
                }
            else:
                abort_markdown = (
                    f"# {request.topic} 研究报告 - 未生成\n\n"
                    "本次运行未能获取到与该主题相关、且通过质量与相关性筛选的有效资料来源，"
                    "因此未进入分析与报告生成阶段，避免在无资料依据的情况下输出内容。\n\n"
                    "可能原因：搜索后端无结果或不稳定、候选来源被域名黑名单/相关性预过滤全部排除、"
                    "或所有来源经内容抓取后主题相关度过低。\n\n"
                    "建议：检查网络与搜索配置后重试，或放宽 `max_sources`/`max_results` 参数。"
                )
            abort_content = (
                render_html_report(abort_markdown, abort_title)
                if request.output_format == "html"
                else abort_markdown
            )
            report = Report(
                topic=request.topic,
                title=abort_title,
                content=abort_content,
                output_format=request.output_format,
                sources=[],
                quality_score=None,
                created_at=started_at,
            )
        else:
            report = Report(
                topic=request.topic,
                title=report_data.get("title", f"{request.topic} 研究报告"),
                content=report_data.get("final_content", report_data.get("markdown_content", "")),
                output_format=report_data.get("output_format", request.output_format),
                sources=[Source(**s) for s in source_dicts],
                quality_score=evaluation or None,
                created_at=started_at,
            )

        report_path = save_report(
            content=report.content,
            output_format=report.output_format,
            run_id=run_id,
            topic=request.topic,
            output_dir=config.REPORTS_DIR,
        )

        safe_topic = sanitize_filename(request.topic)
        sources_path = config.SOURCES_DIR / f"{run_id}_{safe_topic}_sources.json"
        evaluation_path = config.EVALUATIONS_DIR / f"{run_id}_{safe_topic}_evaluation.json"
        trace_path = config.TRACES_DIR / f"{run_id}_{safe_topic}_trace.json"

        save_json(sources_path, source_dicts)
        save_json(evaluation_path, evaluation)

        trace.finished_at = datetime.now().isoformat(timespec="seconds")
        trace.total_duration = round(time.perf_counter() - run_start, 4)
        trace.final_report_path = str(report_path)

        # Diagnostics gathered from each agent's result dict (see
        # browser_agent/analyze_agent/report_agent), surfaced into the trace
        # for perf/quality debugging without changing any existing output.
        duration_by_task_type: dict[str, float] = {}
        for step in trace.steps:
            duration_by_task_type.setdefault(step["task_type"], step["duration"])
        trace.performance_metrics = {
            "research_duration": duration_by_task_type.get("research", 0.0),
            "browser_duration": duration_by_task_type.get("browse", 0.0),
            "analyze_duration": duration_by_task_type.get("analyze", 0.0),
            "report_duration": duration_by_task_type.get("report", 0.0),
            "total_duration": trace.total_duration,
        }

        trace.research_metrics = context.get("search_results", {}).get("research_metrics", {})
        trace.browser_metrics = context.get("sources", {}).get("browser_metrics", {})
        trace.replan_metrics = replan_metrics
        trace.structured_data_metrics = context.get("analysis", {}).get("akshare_metrics", {})

        analysis_compression = context.get("analysis", {}).get("compression_metrics", {})
        report_compression = context.get("report", {}).get("report_compression", {})
        trace.compression_metrics = {
            **analysis_compression,
            "report_context_chars": report_compression.get("report_context_chars", 0),
        }

        eval_criteria = evaluation.get("criteria_scores", {})
        trace.evaluation_diagnostics = {
            **evaluation.get("diagnostics", {}),
            "financial_depth_score": eval_criteria.get("financial_depth_score"),
            "valuation_depth_score": eval_criteria.get("valuation_depth_score"),
        }

        save_json(trace_path, trace.model_dump())

        # v4 阶段D：成功的公司研报 run 追加跟踪记录（best-effort，失败不影响主流程）
        if source_dicts and request.report_type == "company_research":
            try:
                from tracking.periodic_data_store import append_record, record_from_run

                record = record_from_run(request, run_id, str(report_path),
                                         context.get("analysis", {}), evaluation)
                if record is not None and record.symbol:
                    append_record(record)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"tracking record append skipped: {exc!r}")

        return {
            "run_id": run_id,
            "report_path": str(report_path),
            "trace_path": str(trace_path),
            "sources_path": str(sources_path),
            "evaluation_path": str(evaluation_path),
            "evaluation": evaluation,
            "num_sources": len(source_dicts),
            "duration": trace.total_duration,
        }
