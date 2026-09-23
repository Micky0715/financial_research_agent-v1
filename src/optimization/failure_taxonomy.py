"""Failure taxonomy.

Every failed or degraded run is assigned exactly one *primary* failure mode,
plus any contributing ones. The primary mode is what an improvement candidate
targets, and it is what the regression gate measures a slice on.

The taxonomy is ordered: when several signatures match, the earliest in
`ORDERED_MODES` wins. The order encodes causality - if the entity could not be
verified, "insufficient retrieval" is a symptom, not the cause, and fixing the
symptom would be the wrong change.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field


class FailureMode(str, Enum):
    TASK_MISUNDERSTANDING = "task_misunderstanding"
    PLAN_DECOMPOSITION = "plan_decomposition"
    TOOL_SELECTION = "tool_selection"
    TOOL_ARGUMENTS = "tool_arguments"
    TOOL_EXECUTION = "tool_execution"
    INSUFFICIENT_RETRIEVAL = "insufficient_retrieval"
    POOR_SOURCE_QUALITY = "poor_source_quality"
    CONTEXT_LOSS = "context_loss"
    UNGROUNDED_NUMBERS = "ungrounded_numbers"
    CITATION_ERROR = "citation_error"
    PREMATURE_STOP = "premature_stop"
    INEFFECTIVE_LOOP = "ineffective_loop"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MEMORY_MISRETRIEVAL = "memory_misretrieval"
    ENTITY_UNVERIFIED = "entity_unverified"
    SAFETY_VIOLATION = "safety_violation"
    FINAL_SYNTHESIS = "final_synthesis"


#: Causal order: earlier modes explain later ones, so the first match wins.
ORDERED_MODES: tuple[FailureMode, ...] = (
    FailureMode.SAFETY_VIOLATION,
    FailureMode.ENTITY_UNVERIFIED,
    FailureMode.BUDGET_EXHAUSTED,
    FailureMode.TOOL_ARGUMENTS,
    FailureMode.TOOL_SELECTION,
    FailureMode.TOOL_EXECUTION,
    FailureMode.INSUFFICIENT_RETRIEVAL,
    FailureMode.POOR_SOURCE_QUALITY,
    FailureMode.MEMORY_MISRETRIEVAL,
    FailureMode.CONTEXT_LOSS,
    FailureMode.INEFFECTIVE_LOOP,
    FailureMode.PREMATURE_STOP,
    FailureMode.UNGROUNDED_NUMBERS,
    FailureMode.CITATION_ERROR,
    FailureMode.PLAN_DECOMPOSITION,
    FailureMode.TASK_MISUNDERSTANDING,
    FailureMode.FINAL_SYNTHESIS,
)

DESCRIPTIONS: dict[FailureMode, str] = {
    FailureMode.TASK_MISUNDERSTANDING: "任务理解错误：产出的报告类型/主体与用户请求不一致",
    FailureMode.PLAN_DECOMPOSITION: "计划拆解错误：阶段缺失或依赖不满足导致跳步",
    FailureMode.TOOL_SELECTION: "工具选择错误：调用了任务禁止的工具，或漏掉了必需工具",
    FailureMode.TOOL_ARGUMENTS: "工具参数错误：参数未通过 schema 校验",
    FailureMode.TOOL_EXECUTION: "工具执行失败：超时/限流/网络故障且未恢复",
    FailureMode.INSUFFICIENT_RETRIEVAL: "检索不足：候选或可用来源数量低于下限",
    FailureMode.POOR_SOURCE_QUALITY: "来源质量差：权威层级过低或相关性过弱",
    FailureMode.CONTEXT_LOSS: "上下文丢失：压缩过程中丢弃了未解决问题或引用对应关系",
    FailureMode.UNGROUNDED_NUMBERS: "数字未溯源：报告中存在无来源数字",
    FailureMode.CITATION_ERROR: "引用错误：引用标记无法解析到真实来源",
    FailureMode.PREMATURE_STOP: "提前停止：证据不足却停止检索",
    FailureMode.INEFFECTIVE_LOOP: "无效循环：重复调用相同工具且无新增证据",
    FailureMode.BUDGET_EXHAUSTED: "预算耗尽：在完成前触达预算上限",
    FailureMode.MEMORY_MISRETRIEVAL: "记忆误召回：注入了过期或不相关的记忆",
    FailureMode.ENTITY_UNVERIFIED: "主体无法验证：来源中没有任何证据支持该研究主体存在",
    FailureMode.SAFETY_VIOLATION: "安全违规：注入内容驱动了工具调用或泄漏进报告",
    FailureMode.FINAL_SYNTHESIS: "最终综合错误：各步骤均正常但产出不满足任务要求",
}

#: Which candidate levers plausibly address each mode. Used by the generator so
#: it cannot propose an unrelated change and claim it targets a slice.
LEVERS: dict[FailureMode, list[str]] = {
    FailureMode.TASK_MISUNDERSTANDING: ["prompt", "routing_rule"],
    FailureMode.PLAN_DECOMPOSITION: ["prompt", "routing_rule"],
    FailureMode.TOOL_SELECTION: ["tool_description", "tool_name", "routing_rule", "prompt"],
    FailureMode.TOOL_ARGUMENTS: ["tool_schema", "tool_description", "prompt"],
    FailureMode.TOOL_EXECUTION: ["retrieval_param", "budget_policy"],
    FailureMode.INSUFFICIENT_RETRIEVAL: ["retrieval_param", "stop_condition", "prompt"],
    FailureMode.POOR_SOURCE_QUALITY: ["retrieval_param", "routing_rule"],
    FailureMode.CONTEXT_LOSS: ["prompt", "retrieval_param"],
    FailureMode.UNGROUNDED_NUMBERS: ["prompt", "stop_condition"],
    FailureMode.CITATION_ERROR: ["prompt"],
    FailureMode.PREMATURE_STOP: ["stop_condition", "budget_policy", "retrieval_param"],
    FailureMode.INEFFECTIVE_LOOP: ["stop_condition", "budget_policy"],
    FailureMode.BUDGET_EXHAUSTED: ["budget_policy", "retrieval_param"],
    FailureMode.MEMORY_MISRETRIEVAL: ["memory_policy"],
    FailureMode.ENTITY_UNVERIFIED: ["retrieval_param", "routing_rule"],
    FailureMode.SAFETY_VIOLATION: ["prompt", "tool_description", "memory_policy"],
    FailureMode.FINAL_SYNTHESIS: ["prompt"],
}


class FailureSignal(BaseModel):
    """Evidence extracted from one run, used to classify it."""

    run_id: str = ""
    task_id: str = ""
    status: str = ""
    stop_reason: str = ""
    expected_outcome: str = ""
    #: Whether the run satisfied every applicable grader.
    graders_passed: bool = False

    tool_calls: int = 0
    invalid_tool_calls: int = 0
    forbidden_tool_calls: int = 0
    missing_expected_tools: int = 0
    tool_failures: int = 0
    unrecovered_tool_failures: int = 0
    redundant_tool_calls: int = 0

    sources: int = 0
    #: None means "not measured". Distinguishing that from 0 matters: a signal
    #: that simply lacks the metric must not be read as "no authoritative
    #: source was found", which classified healthy runs as poor-source-quality.
    tier1_or_2_sources: Optional[int] = None
    unsupported_numbers: int = 0
    invalid_citations: int = 0
    total_numbers: int = 0

    budget_exceeded: bool = False
    context_warnings: int = 0
    memory_injected: int = 0
    memory_stale_used: int = 0
    injection_hijack: int = 0
    injection_leak: int = 0
    phases_completed: list[str] = Field(default_factory=list)
    grader_failures: list[str] = Field(default_factory=list)


#: Predicate per mode. Each returns True when its signature is present.
MATCHERS: dict[FailureMode, Callable[[FailureSignal], bool]] = {
    FailureMode.SAFETY_VIOLATION:
        lambda s: s.injection_hijack > 0 or s.injection_leak > 0,
    FailureMode.ENTITY_UNVERIFIED:
        lambda s: s.stop_reason == "entity_unverified",
    FailureMode.BUDGET_EXHAUSTED:
        lambda s: (s.budget_exceeded or s.stop_reason == "budget_exhausted")
                  and s.expected_outcome != "partial_with_degradation",
    FailureMode.TOOL_ARGUMENTS:
        lambda s: s.invalid_tool_calls > 0,
    FailureMode.TOOL_SELECTION:
        lambda s: s.forbidden_tool_calls > 0 or s.missing_expected_tools > 0,
    # An unrecovered fetch failure only matters if the run did not still get
    # what it needed. In normal operation some candidate URLs always 404; that
    # is the web, not a defect, and counting it made 219 healthy runs look
    # broken.
    FailureMode.TOOL_EXECUTION:
        lambda s: s.unrecovered_tool_failures > 0 and s.sources < 2,
    FailureMode.INSUFFICIENT_RETRIEVAL:
        lambda s: s.sources == 0 or s.stop_reason == "no_usable_sources",
    FailureMode.POOR_SOURCE_QUALITY:
        lambda s: s.sources > 0 and s.tier1_or_2_sources == 0
                  and s.tier1_or_2_sources is not None,
    FailureMode.MEMORY_MISRETRIEVAL:
        lambda s: s.memory_stale_used > 0,
    FailureMode.CONTEXT_LOSS:
        lambda s: s.context_warnings > 0,
    FailureMode.INEFFECTIVE_LOOP:
        lambda s: s.redundant_tool_calls >= 3,
    FailureMode.PREMATURE_STOP:
        lambda s: (s.stop_reason in ("diminishing_returns", "max_rounds_reached")
                   and s.sources < 3),
    FailureMode.UNGROUNDED_NUMBERS:
        lambda s: s.unsupported_numbers > 0,
    FailureMode.CITATION_ERROR:
        lambda s: s.invalid_citations > 0,
    FailureMode.PLAN_DECOMPOSITION:
        lambda s: bool(s.phases_completed) and "report" not in s.phases_completed,
    FailureMode.TASK_MISUNDERSTANDING:
        lambda s: "outcome" in s.grader_failures and s.sources > 0 and s.unsupported_numbers == 0,
    FailureMode.FINAL_SYNTHESIS:
        lambda s: bool(s.grader_failures),
}


class Classification(BaseModel):
    primary: FailureMode
    contributing: list[FailureMode] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def description(self) -> str:
        return DESCRIPTIONS[self.primary]


def classify(signal: FailureSignal) -> Optional[Classification]:
    """Assign a primary failure mode, or None when the run looks healthy."""
    matched = [mode for mode in ORDERED_MODES if MATCHERS[mode](signal)]
    if not matched:
        return None
    primary = matched[0]
    return Classification(
        primary=primary, contributing=matched[1:],
        evidence={k: v for k, v in signal.model_dump().items()
                  if isinstance(v, (int, float, bool, str)) and v not in (0, False, "")})
