"""Planning agent: turns a ResearchRequest into a validated list of Tasks."""
from typing import Any

from config import config
from schemas.request import ResearchRequest
from schemas.task import TASK_TYPES, Task
from utils.logger import logger

from .base_agent import BaseAgent

_PROMPT_PATH = config.PROMPTS_DIR / "planning_prompt.txt"

# The pipeline is a fixed 5-stage chain - normalize_plan() is the single
# source of truth for task_id/dependencies/priority/max_retries per stage,
# used both to build the deterministic default plan and to collapse whatever
# the LLM planner produced (which has been observed emitting 9-11 tasks with
# duplicate research/browse stages) back down to exactly these 5.
_STAGE_ORDER = ["research", "browse", "analyze", "report", "evaluate"]
_STAGE_TASK_ID = {"research": "t1", "browse": "t2", "analyze": "t3", "report": "t4", "evaluate": "t5"}
_STAGE_DEPENDENCIES = {
    "research": [],
    "browse": ["t1"],
    "analyze": ["t2"],
    "report": ["t3"],
    "evaluate": ["t4"],
}
_STAGE_DESCRIPTION = {
    "research": "搜索主题相关资料",
    "browse": "读取网页正文和PDF并做质量评分",
    "analyze": "做财务、趋势、估值和风险分析",
    "report": "生成 Markdown/HTML 研报",
    "evaluate": "评估报告质量",
}
# browse gets fewer retries than the rest: retrying a fetch failure (all
# candidates errored - likely transient network) is worth one extra attempt,
# but retrying because the *same* URL batch scored zero relevance just repeats
# the identical outcome. See agents/browser_agent.py's NoUsableSourcesError.
_STAGE_MAX_RETRIES = {"research": 2, "browse": 1, "analyze": 2, "report": 2, "evaluate": 2}


def default_task_plan() -> list[Task]:
    """Deterministic 5-stage plan used whenever LLM planning fails or is skipped."""
    return normalize_plan([])[0]


def normalize_plan(tasks: list[Task]) -> tuple[list[Task], dict[str, Any]]:
    """Collapse whatever the planner produced into exactly the fixed 5-stage
    pipeline: research -> browse -> analyze -> report -> evaluate.

    The LLM planner has been observed emitting anywhere from 5 to 11 tasks,
    including duplicate research/browse stages that made the orchestrator
    actually re-run research or browse multiple times (see the 半导体国产替代
    and 低空经济 bad cases). Rather than trying to make LLM output reliable,
    every plan - LLM-generated or the deterministic default - is normalized
    through here before execution: at most one task per stage, fixed
    task_id/dependencies/priority/max_retries, fixed order. Only
    `description`/`parameters` are carried over from the first task of each
    type the planner produced (if any); everything else is forced.
    """
    raw_task_types = [t.task_type for t in tasks]
    first_by_type: dict[str, Task] = {}
    for t in tasks:
        if t.task_type in _STAGE_ORDER and t.task_type not in first_by_type:
            first_by_type[t.task_type] = t

    normalized: list[Task] = []
    for stage in _STAGE_ORDER:
        source = first_by_type.get(stage)
        normalized.append(
            Task(
                task_id=_STAGE_TASK_ID[stage],
                task_type=stage,
                description=(source.description if source and source.description else _STAGE_DESCRIPTION[stage]),
                parameters=(source.parameters if source else {}),
                dependencies=_STAGE_DEPENDENCIES[stage],
                priority=_STAGE_ORDER.index(stage) + 1,
                max_retries=_STAGE_MAX_RETRIES[stage],
            )
        )

    planner_metrics = {
        "raw_task_count": len(tasks),
        "normalized_task_count": len(normalized),
        "raw_task_types": raw_task_types,
        "normalized_task_types": [t.task_type for t in normalized],
        "deduplicated_task_types": [stage for stage in _STAGE_ORDER if raw_task_types.count(stage) > 1],
    }
    logger.info(f"Plan normalized: raw {len(tasks)} tasks -> {len(normalized)} tasks")
    return normalized, planner_metrics


class PlanningAgent(BaseAgent):
    """Uses the LLM to decompose a ResearchRequest into an executable task plan."""

    name = "planning_agent"
    description = "理解用户需求并拆解为可执行任务计划"
    tools: list[str] = []

    def execute(self, task: Task, context: dict[str, Any]) -> dict[str, Any]:
        """Generate a task plan for the ResearchRequest stored in context['request']."""
        request: ResearchRequest = context["request"]
        tasks = self.plan(request)
        return {"tasks": [t.model_dump() for t in tasks]}

    def plan(self, request: ResearchRequest) -> list[Task]:
        """Produce a validated list[Task] for `request`, falling back on any failure."""
        try:
            prompt_template = _PROMPT_PATH.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning(f"planning prompt missing, using default plan: {exc}")
            return default_task_plan()

        prompt = prompt_template.format(
            topic=request.topic,
            report_type=request.report_type,
            requirements="、".join(request.requirements) or "未指定",
            output_format=request.output_format,
        )

        try:
            raw = self.call_llm(prompt, system="你是严谨的任务规划助手，只输出JSON。")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"planning LLM call failed, using default plan: {exc}")
            return default_task_plan()

        parsed = self.parse_json_response(raw)
        tasks = self._validate_plan(parsed)
        if tasks is None:
            logger.warning("planning LLM output failed validation, using default plan")
            return default_task_plan()
        return tasks

    @staticmethod
    def _validate_plan(parsed: Any) -> list[Task] | None:
        """Validate LLM JSON output against schema + task_type/dependency rules."""
        if not isinstance(parsed, list) or not parsed:
            return None

        tasks: list[Task] = []
        seen_ids: set[str] = set()
        try:
            for item in parsed:
                if item.get("task_type") not in TASK_TYPES:
                    return None
                task = Task(
                    task_id=str(item["task_id"]),
                    task_type=item["task_type"],
                    description=str(item.get("description", "")),
                    dependencies=[str(d) for d in item.get("dependencies", [])],
                    priority=int(item.get("priority", 1)),
                )
                tasks.append(task)
                seen_ids.add(task.task_id)
        except (KeyError, TypeError, ValueError):
            return None

        for task in tasks:
            if any(dep not in seen_ids for dep in task.dependencies):
                return None

        required_stages = {"research", "browse", "analyze", "report"}
        present_stages = {t.task_type for t in tasks}
        if not required_stages.issubset(present_stages):
            return None

        return tasks
