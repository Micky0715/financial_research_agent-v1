"""Experiment arms: the named configurations an eval can be run under.

An arm is the unit of comparison. Every result row records its arm name *and*
the resolved config hash, so a metric can never be quoted without the
configuration that produced it being recoverable.

The ablation arms exist to answer "did this component actually earn its
place?": turn off the fallback round, the cache, or context compression and see
what the metrics do. `baseline_v4` runs the pipeline as it was before the
harness (no budget gate, no checkpointing) so the harness itself is measured
rather than assumed to be an improvement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.runtime.budget import BudgetLimits
from src.runtime.policies import RetryPolicy


@dataclass
class ArmConfig:
    """One named configuration."""

    name: str
    description: str
    #: Legacy `config` attributes to override for the duration of the arm.
    config_overrides: dict[str, Any] = field(default_factory=dict)
    budget: Optional[BudgetLimits] = None
    retry: Optional[RetryPolicy] = None
    max_concurrency: int = 8
    context_budget_tokens: int = 12000
    checkpoint_enabled: bool = True
    memory_enabled: bool = False
    use_llm_judge: bool = False
    judge_model: str = ""
    #: Run `WorkflowOrchestrator.run()` with no harness bound at all. This is
    #: the only honest "legacy" baseline: it produces no trace, so every
    #: event-derived grader is correctly not-applicable rather than zero.
    legacy_mode: bool = False

    def to_harness_config(self, base_dir: Path):
        from src.runtime.runner import HarnessConfig

        return HarnessConfig(
            base_dir=base_dir,
            budget=self.budget or BudgetLimits(),
            retry=self.retry or RetryPolicy(),
            max_concurrency=self.max_concurrency,
            checkpoint_enabled=self.checkpoint_enabled,
            context_budget_tokens=self.context_budget_tokens,
            memory_enabled=self.memory_enabled,
            arm=self.name,
        )

    def apply_overrides(self) -> dict[str, Any]:
        """Apply `config_overrides` to the legacy config, returning the previous
        values so the caller can restore them."""
        from config import config

        previous: dict[str, Any] = {}
        for key, value in self.config_overrides.items():
            previous[key] = getattr(config, key, None)
            setattr(config, key, value)
        return previous

    @staticmethod
    def restore_overrides(previous: dict[str, Any]) -> None:
        from config import config

        for key, value in previous.items():
            setattr(config, key, value)


ARMS: dict[str, ArmConfig] = {
    "harness": ArmConfig(
        name="harness",
        description="v5 统一 Harness：预算门禁 + checkpoint + 工具校验 + 结构化重试",
    ),
    "baseline_v4": ArmConfig(
        name="baseline_v4",
        description="v4 基线：不启用预算门禁与 checkpoint（对照 Harness 本身的收益）",
        budget=BudgetLimits(max_total_tokens=None, max_cost_usd=None, max_duration_s=None,
                            max_model_calls=None, max_tool_calls=None,
                            max_supplementary_rounds=1),
        checkpoint_enabled=False,
    ),
    "no_fallback": ArmConfig(
        name="no_fallback",
        description="消融：关闭权威站点 fallback 检索轮（SEARCH_MIN_UNIQUE_RESULTS=0）",
        config_overrides={"SEARCH_MIN_UNIQUE_RESULTS": 0},
        budget=BudgetLimits(max_supplementary_rounds=0),
    ),
    "no_cache": ArmConfig(
        name="no_cache",
        description="消融：关闭搜索缓存，衡量缓存对时延与冗余调用的贡献",
        config_overrides={"ENABLE_SEARCH_CACHE": False},
    ),
    "no_compression": ArmConfig(
        name="no_compression",
        description="消融：上下文预算放到极大，等价于不压缩",
        context_budget_tokens=1_000_000,
    ),
    "no_semantic": ArmConfig(
        name="no_semantic",
        description="消融：关闭语义排序，仅用规则打分",
        config_overrides={"ENABLE_SEMANTIC_RANKING": False},
    ),
    "tight_budget": ArmConfig(
        name="tight_budget",
        description="预算压力测试：工具调用上限 6，观察降级阶梯是否按序生效",
        budget=BudgetLimits(max_tool_calls=6, max_duration_s=300),
    ),
    "low_concurrency": ArmConfig(
        name="low_concurrency",
        description="并发消融：全局并发降到 2",
        max_concurrency=2,
        config_overrides={"SEARCH_MAX_WORKERS": 2},
    ),
    "max_rounds_0": ArmConfig(
        name="max_rounds_0",
        description="停止条件消融：不允许任何补充检索轮",
        budget=BudgetLimits(max_supplementary_rounds=0),
    ),
    "with_memory": ArmConfig(
        name="with_memory",
        description="记忆 A/B：启用长期记忆注入",
        memory_enabled=True,
    ),
    "no_memory": ArmConfig(
        name="no_memory",
        description="记忆 A/B 对照：不注入任何记忆",
        memory_enabled=False,
    ),
# ----------------------------------------------------------------- #
    # Single-variable ablation arms.
    #
    # `baseline_v4` above disables budget *and* checkpointing at once, so any
    # difference against `harness` cannot be attributed to either one. These
    # arms change exactly one thing each, which is what makes the ablation
    # table readable.
    # ----------------------------------------------------------------- #
    "legacy_no_harness": ArmConfig(
        name="legacy_no_harness",
        description="真 legacy：完全不绑定 Harness，直接跑 WorkflowOrchestrator（无 trace）",
        legacy_mode=True,
    ),
    "abl_no_retry": ArmConfig(
        name="abl_no_retry",
        description="单变量消融：只关重试（max_attempts=1，无退避）",
        retry=RetryPolicy(max_attempts=1, jitter=False, base_delay_s=0.0),
    ),
    "abl_no_checkpoint": ArmConfig(
        name="abl_no_checkpoint",
        description="单变量消融：只关 checkpoint（预算门禁保持开启）",
        checkpoint_enabled=False,
    ),
    "abl_no_budget": ArmConfig(
        name="abl_no_budget",
        description="单变量消融：只关预算门禁（checkpoint 保持开启）",
        budget=BudgetLimits(max_total_tokens=None, max_cost_usd=None, max_duration_s=None,
                            max_model_calls=None, max_tool_calls=None,
                            max_supplementary_rounds=1),
    ),
    "abl_no_context_mgmt": ArmConfig(
        name="abl_no_context_mgmt",
        description="单变量消融：只关上下文分层压缩（预算放到极大，等价不压缩）",
        context_budget_tokens=1_000_000,
    ),
    "harness_full": ArmConfig(
        name="harness_full",
        description="完整 Harness（与 harness 同配置，作为消融矩阵的参照臂）",
    ),
    "with_judge": ArmConfig(
        name="with_judge",
        description="附加 LLM Judge（仅 --live 时生效，需要真实模型）",
        use_llm_judge=True,
    ),
}


def resolve_arm(name: str) -> ArmConfig:
    if name not in ARMS:
        raise SystemExit(f"unknown arm {name!r}; available: {sorted(ARMS)}")
    return ARMS[name]


def ablation_arms() -> list[str]:
    """The strict single-variable matrix required by the audit brief.

    Every arm differs from `harness_full` in exactly one dimension, so a delta
    is attributable. `baseline_v4` is deliberately absent: it bundles two
    changes and cannot support an attribution claim.
    """
    return ["legacy_no_harness", "harness_full", "abl_no_retry", "abl_no_checkpoint",
            "abl_no_budget", "abl_no_context_mgmt", "no_fallback"]
