"""Budget accounting and the degradation ladder.

Two jobs:

1. **Count** what a run spends (tokens, money, wall clock, model calls, tool
   calls, per-tool calls, supplementary retrieval rounds). The legacy pipeline
   counted none of this - there was no token or cost number anywhere in the
   repo.
2. **Decide what to give up first** when the budget runs low. Hitting a limit
   must never mean an infinite loop or a hard crash: it means the run takes a
   cheaper path and says so. `BudgetPolicy.recommend()` returns that decision
   and the reason goes into the trace, so "why did it stop here" has an answer.

Cost estimation is explicitly approximate: `PRICE_TABLE` holds public list
prices for a few models and every derived cost is labelled `estimated_cost_usd`.
An unpriced model contributes 0.0 and is recorded in `unpriced_models` rather
than silently guessed - a fabricated cost is worse than a missing one.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.runtime.state import BudgetState

#: USD per 1M tokens, (input, output). Public list prices, used only for
#: order-of-magnitude cost reporting. Unknown models -> no cost attributed.
PRICE_TABLE: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "deepseek-chat": (0.27, 1.10),
    "deepseek-v3": (0.27, 1.10),
    "deepseek-v4-pro": (0.55, 2.20),
    "qwen-plus": (0.11, 0.28),
    "qwen-turbo": (0.05, 0.14),
    "qwen3-1.7b": (0.0, 0.0),  # local/self-hosted
}


def normalize_model_name(model: str) -> str:
    """Strip LiteLLM provider prefixes ('openai/deepseek-v4-pro' -> 'deepseek-v4-pro')."""
    name = (model or "").strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> tuple[float, bool]:
    """Return (usd, priced). `priced` is False when the model is not in the table."""
    prices = PRICE_TABLE.get(normalize_model_name(model))
    if prices is None:
        return 0.0, False
    in_rate, out_rate = prices
    usd = (input_tokens / 1_000_000) * in_rate + (output_tokens / 1_000_000) * out_rate
    return round(usd, 6), True


def estimate_tokens(text: str) -> int:
    """Rough token count used when a provider returns no usage block.

    CJK text is roughly 1 token/char while ASCII is roughly 1 token per 4
    chars, so the two are counted separately. This is an estimate and is
    labelled as such wherever it surfaces.
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return int(cjk + other / 4) + 1


class BudgetLimits(BaseModel):
    """Hard ceilings for one run. `None` means "no limit for this dimension"."""

    max_total_tokens: Optional[int] = 400_000
    max_cost_usd: Optional[float] = 1.0
    max_duration_s: Optional[float] = 1800.0
    max_model_calls: Optional[int] = 40
    max_tool_calls: Optional[int] = 120
    #: Per-tool ceiling, e.g. {"web_search": 20, "read_webpage": 40}.
    max_calls_per_tool: dict[str, int] = Field(default_factory=dict)
    #: Per-phase wall-clock ceilings, e.g. {"browse": 600}.
    max_phase_duration_s: dict[str, float] = Field(default_factory=dict)
    #: Supplementary retrieval rounds after the first pass.
    max_supplementary_rounds: int = 1

    #: Utilization at which each rung of the degradation ladder engages.
    #: Configurable rather than hard-coded so an optimization candidate can
    #: propose moving a threshold and the change is actually runnable.
    narrow_search_threshold: float = 0.45
    downgrade_model_threshold: float = 0.60
    skip_optional_threshold: float = 0.75
    finalize_threshold: float = 0.90

    @classmethod
    def smoke(cls) -> "BudgetLimits":
        """Tight limits for fixture-backed smoke runs."""
        return cls(
            max_total_tokens=20_000, max_cost_usd=0.05, max_duration_s=120.0,
            max_model_calls=6, max_tool_calls=20, max_supplementary_rounds=0,
        )


class DegradeAction(str, Enum):
    """The degradation ladder, cheapest concession first."""

    CONTINUE = "continue"
    NARROW_SEARCH = "narrow_search"            # fewer queries / fewer candidates
    REDUCE_PARALLELISM = "reduce_parallelism"
    DOWNGRADE_MODEL = "downgrade_model"
    SKIP_OPTIONAL = "skip_optional"            # drop charts/revision/deep chains
    FINALIZE_WITH_CURRENT = "finalize_with_current"  # write report from evidence in hand
    RETURN_INSUFFICIENT = "return_insufficient"      # not enough to report at all
    ABORT = "abort"


class BudgetDecision(BaseModel):
    action: DegradeAction = DegradeAction.CONTINUE
    reason: str = ""
    #: Which limit drove this, e.g. "cost_usd" / "duration_s".
    dimension: str = ""
    utilization: float = 0.0  # 0..1+ against the binding limit


class BudgetTracker:
    """Mutates a `BudgetState` and answers "can I afford this / what now?".

    Not thread-safe by itself; the executor holds a lock around charge calls
    because ResearchAgent runs searches in a thread pool.
    """

    def __init__(self, limits: Optional[BudgetLimits] = None, state: Optional[BudgetState] = None) -> None:
        self.limits = limits or BudgetLimits()
        self.state = state or BudgetState()
        self.unpriced_models: set[str] = set()
        self._phase_elapsed: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # Charging
    # ------------------------------------------------------------------ #
    def charge_model(self, model: str, input_tokens: int, output_tokens: int) -> float:
        usd, priced = estimate_cost(model, input_tokens, output_tokens)
        if not priced:
            self.unpriced_models.add(normalize_model_name(model))
        self.state.tokens_in += max(0, input_tokens)
        self.state.tokens_out += max(0, output_tokens)
        self.state.cost_usd = round(self.state.cost_usd + usd, 6)
        self.state.model_calls += 1
        return usd

    def charge_tool(self, tool_name: str, count: int = 1) -> None:
        self.state.tool_calls += count
        self.state.tool_calls_by_name[tool_name] = (
            self.state.tool_calls_by_name.get(tool_name, 0) + count
        )

    def charge_time(self, elapsed_s: float, phase: str = "") -> None:
        self.state.elapsed_s = round(self.state.elapsed_s + max(0.0, elapsed_s), 3)
        if phase:
            self._phase_elapsed[phase] = round(self._phase_elapsed.get(phase, 0.0) + elapsed_s, 3)

    def set_elapsed(self, elapsed_s: float) -> None:
        """Wall-clock is authoritative when available (concurrency makes the
        sum of step durations exceed real time)."""
        self.state.elapsed_s = round(max(0.0, elapsed_s), 3)

    def note_supplementary_round(self) -> None:
        self.state.supplementary_rounds += 1

    # ------------------------------------------------------------------ #
    # Checking
    # ------------------------------------------------------------------ #
    def _record_exceeded(self, dimension: str) -> None:
        if dimension not in self.state.exceeded:
            self.state.exceeded.append(dimension)

    def can_call_tool(self, tool_name: str) -> tuple[bool, str]:
        """Pre-flight check. Returns (allowed, reason-if-not)."""
        lim = self.limits
        if lim.max_tool_calls is not None and self.state.tool_calls >= lim.max_tool_calls:
            self._record_exceeded("tool_calls")
            return False, f"tool_calls {self.state.tool_calls} >= limit {lim.max_tool_calls}"
        per_tool = lim.max_calls_per_tool.get(tool_name)
        if per_tool is not None and self.state.tool_calls_by_name.get(tool_name, 0) >= per_tool:
            self._record_exceeded(f"tool_calls:{tool_name}")
            return False, f"{tool_name} calls {self.state.tool_calls_by_name.get(tool_name, 0)} >= limit {per_tool}"
        ok, reason = self._check_shared()
        return ok, reason

    def can_call_model(self) -> tuple[bool, str]:
        lim = self.limits
        if lim.max_model_calls is not None and self.state.model_calls >= lim.max_model_calls:
            self._record_exceeded("model_calls")
            return False, f"model_calls {self.state.model_calls} >= limit {lim.max_model_calls}"
        return self._check_shared()

    def _check_shared(self) -> tuple[bool, str]:
        lim = self.limits
        if lim.max_total_tokens is not None and self.state.total_tokens >= lim.max_total_tokens:
            self._record_exceeded("total_tokens")
            return False, f"total_tokens {self.state.total_tokens} >= limit {lim.max_total_tokens}"
        if lim.max_cost_usd is not None and self.state.cost_usd >= lim.max_cost_usd:
            self._record_exceeded("cost_usd")
            return False, f"cost_usd {self.state.cost_usd} >= limit {lim.max_cost_usd}"
        if lim.max_duration_s is not None and self.state.elapsed_s >= lim.max_duration_s:
            self._record_exceeded("duration_s")
            return False, f"elapsed {self.state.elapsed_s}s >= limit {lim.max_duration_s}s"
        return True, ""

    def can_supplement(self) -> bool:
        return self.state.supplementary_rounds < self.limits.max_supplementary_rounds

    def phase_over_budget(self, phase: str) -> bool:
        cap = self.limits.max_phase_duration_s.get(phase)
        return cap is not None and self._phase_elapsed.get(phase, 0.0) >= cap

    # ------------------------------------------------------------------ #
    # Utilization / degradation
    # ------------------------------------------------------------------ #
    def utilization(self) -> dict[str, float]:
        """Fraction of each configured limit consumed so far."""
        lim, st = self.limits, self.state
        out: dict[str, float] = {}
        if lim.max_total_tokens:
            out["total_tokens"] = st.total_tokens / lim.max_total_tokens
        if lim.max_cost_usd:
            out["cost_usd"] = st.cost_usd / lim.max_cost_usd
        if lim.max_duration_s:
            out["duration_s"] = st.elapsed_s / lim.max_duration_s
        if lim.max_model_calls:
            out["model_calls"] = st.model_calls / lim.max_model_calls
        if lim.max_tool_calls:
            out["tool_calls"] = st.tool_calls / lim.max_tool_calls
        return out

    def headroom(self) -> float:
        """1.0 = untouched, 0.0 = a limit is fully consumed."""
        used = self.utilization()
        return round(max(0.0, 1.0 - max(used.values())), 4) if used else 1.0

    def recommend(self, *, has_evidence: bool = False) -> BudgetDecision:
        """The degradation ladder.

        Thresholds are pressure bands, not cliffs: the run starts giving things
        up well before it would be forced to, so it can still *finish* rather
        than being cut off mid-report.
        """
        used = self.utilization()
        if not used:
            return BudgetDecision(action=DegradeAction.CONTINUE, reason="no limits configured")

        dimension, ratio = max(used.items(), key=lambda kv: kv[1])
        base = {"dimension": dimension, "utilization": round(ratio, 4)}

        if ratio >= 1.0:
            self._record_exceeded(dimension)
            if has_evidence:
                return BudgetDecision(
                    action=DegradeAction.FINALIZE_WITH_CURRENT,
                    reason=f"{dimension} exhausted ({ratio:.2f}); writing report from evidence already gathered",
                    **base)
            return BudgetDecision(
                action=DegradeAction.RETURN_INSUFFICIENT,
                reason=f"{dimension} exhausted ({ratio:.2f}) with no usable evidence gathered",
                **base)
        if ratio >= self.limits.finalize_threshold:
            return BudgetDecision(
                action=DegradeAction.FINALIZE_WITH_CURRENT if has_evidence else DegradeAction.SKIP_OPTIONAL,
                reason=f"{dimension} at {ratio:.0%}; stop gathering and finalize",
                **base)
        if ratio >= self.limits.skip_optional_threshold:
            return BudgetDecision(
                action=DegradeAction.SKIP_OPTIONAL,
                reason=f"{dimension} at {ratio:.0%}; skipping optional enrichment (charts/revision/deep chains)",
                **base)
        if ratio >= self.limits.downgrade_model_threshold:
            return BudgetDecision(
                action=DegradeAction.DOWNGRADE_MODEL,
                reason=f"{dimension} at {ratio:.0%}; downgrade to the cheaper model for remaining calls",
                **base)
        if ratio >= self.limits.narrow_search_threshold:
            return BudgetDecision(
                action=DegradeAction.NARROW_SEARCH,
                reason=f"{dimension} at {ratio:.0%}; narrowing search breadth",
                **base)
        return BudgetDecision(action=DegradeAction.CONTINUE, reason="within budget", **base)

    def snapshot(self) -> dict[str, Any]:
        return {
            **self.state.model_dump(mode="json"),
            "utilization": {k: round(v, 4) for k, v in self.utilization().items()},
            "headroom": self.headroom(),
            "unpriced_models": sorted(self.unpriced_models),
            "limits": self.limits.model_dump(mode="json"),
        }
