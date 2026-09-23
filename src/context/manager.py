"""Layered context assembly under a token budget.

The naive approach - "summarize when the prompt gets too long" - loses exactly
the things that matter: which numbers came from where, and which questions are
still open. This module instead assembles the context from typed layers, each
with its own budget share and its own eviction rule, and it verifies after
compression that nothing load-bearing was dropped.

Layers, in priority order (first to be kept, last to be dropped):

    1. TASK_GOAL       what we were asked to produce
    2. PLAN            the current plan and which step we are on
    3. STAGE_STATE     phase, budget headroom, degradations in force
    4. OPEN_QUESTIONS  unresolved sub-questions - never evicted, only compacted
    5. EVIDENCE        the ledger: claim -> value -> source, per-source digest
    6. RECENT          the last few interactions
    7. TOOL_RESULTS    compact tool outputs (full payloads live in artifacts)
    8. MEMORY          retrieved long-term memory, hard-capped separately

`assemble()` returns both the rendered text and a `CompressionReport` recording
what was kept, what was dropped and whether the consistency checks passed. The
report goes into the trace as a `context_compressed` event, so "上下文过长时保
留了什么、丢弃了什么" is answerable from the trace alone.
"""
from __future__ import annotations

import re
from enum import IntEnum
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field

from src.runtime.budget import estimate_tokens
from src.runtime.state import EvidenceRecord


class Layer(IntEnum):
    """Lower value = higher priority = evicted last."""

    TASK_GOAL = 1
    PLAN = 2
    STAGE_STATE = 3
    OPEN_QUESTIONS = 4
    EVIDENCE = 5
    RECENT = 6
    TOOL_RESULTS = 7
    MEMORY = 8


#: Share of the total budget each layer may claim before eviction starts.
#: Sums to slightly under 1.0 to leave slack for the rendered scaffolding.
DEFAULT_SHARES: dict[Layer, float] = {
    Layer.TASK_GOAL: 0.05,
    Layer.PLAN: 0.07,
    Layer.STAGE_STATE: 0.04,
    Layer.OPEN_QUESTIONS: 0.08,
    Layer.EVIDENCE: 0.35,
    Layer.RECENT: 0.12,
    Layer.TOOL_RESULTS: 0.20,
    Layer.MEMORY: 0.07,
}

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:亿元|万元|亿|万|元|%|倍)")
_CITATION_RE = re.compile(r"\[s\d+\]|\[source_\d+\]")


class ContextBlock(BaseModel):
    """One addressable piece of context."""

    layer: Layer
    key: str = ""
    text: str = ""
    #: Higher wins when the layer has to shed blocks.
    importance: float = 0.5
    #: Blocks marked pinned survive eviction inside their layer.
    pinned: bool = False
    #: Pointer to the full payload, when this block is a digest of something big.
    artifact_id: str = ""

    def tokens(self) -> int:
        return estimate_tokens(self.text)


class CompressionReport(BaseModel):
    """What compression actually did. Emitted as a trace event."""

    budget_tokens: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    blocks_before: int = 0
    blocks_after: int = 0
    dropped_by_layer: dict[str, int] = Field(default_factory=dict)
    truncated_blocks: int = 0
    #: Consistency checks - see verify_consistency().
    numbers_before: int = 0
    numbers_after: int = 0
    citations_before: int = 0
    citations_after: int = 0
    evidence_before: int = 0
    evidence_after: int = 0
    open_questions_preserved: bool = True
    consistency_ok: bool = True
    consistency_warnings: list[str] = Field(default_factory=list)

    @property
    def compression_ratio(self) -> float:
        return round(self.tokens_after / self.tokens_before, 4) if self.tokens_before else 1.0


def source_digest(evidence: Iterable[EvidenceRecord], max_per_source: int = 4) -> list[str]:
    """Group the ledger by source and render one bounded digest per source.

    Per-source rather than per-claim: five figures from one annual report
    should cost one header and five lines, not five repetitions of the title
    and URL.
    """
    by_source: dict[str, list[EvidenceRecord]] = {}
    for record in evidence:
        by_source.setdefault(record.source_id or record.origin, []).append(record)

    lines: list[str] = []
    for source_id, records in by_source.items():
        head = records[0]
        label = head.title or head.url or head.derived_from or source_id
        tier = f" tier={head.authority_tier}" if head.authority_tier else ""
        lines.append(f"[{source_id}] {label[:90]} ({head.origin}{tier})")
        for record in records[:max_per_source]:
            value = f" = {record.value}" if record.value else ""
            lines.append(f"    - {record.claim[:110]}{value}")
        if len(records) > max_per_source:
            lines.append(f"    - ... 另有 {len(records) - max_per_source} 条同源证据（见 evidence ledger）")
    return lines


class ContextManager:
    """Assembles layered context under a token budget."""

    def __init__(self, budget_tokens: int = 12000,
                 shares: Optional[dict[Layer, float]] = None,
                 memory_budget_tokens: Optional[int] = None) -> None:
        self.budget_tokens = budget_tokens
        self.shares = shares or dict(DEFAULT_SHARES)
        # Memory gets its own hard cap so a large memory store cannot crowd out
        # evidence. Defaults to its layer share.
        self.memory_budget_tokens = memory_budget_tokens or int(
            budget_tokens * self.shares[Layer.MEMORY])
        self.blocks: list[ContextBlock] = []

    # ------------------------------------------------------------------ #
    def add(self, layer: Layer, text: str, *, key: str = "", importance: float = 0.5,
            pinned: bool = False, artifact_id: str = "") -> ContextBlock:
        block = ContextBlock(layer=layer, key=key, text=text, importance=importance,
                             pinned=pinned, artifact_id=artifact_id)
        self.blocks.append(block)
        return block

    def add_goal(self, topic: str, report_type: str, requirements: list[str]) -> None:
        self.add(Layer.TASK_GOAL, pinned=True, importance=1.0, key="goal",
                 text=f"研究主题：{topic}\n报告类型：{report_type}\n"
                      f"必须覆盖的章节：{'、'.join(requirements) or '未指定'}")

    def add_plan(self, steps: list[str], current: str = "") -> None:
        rendered = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
        marker = f"\n当前步骤：{current}" if current else ""
        self.add(Layer.PLAN, text=f"当前计划：\n{rendered}{marker}", pinned=True,
                 importance=0.9, key="plan")

    def add_stage_state(self, phase: str, headroom: float, degradations: list[str]) -> None:
        text = f"当前阶段：{phase}\n预算余量：{headroom:.0%}"
        if degradations:
            text += "\n已生效的降级：" + "；".join(degradations[:5])
        self.add(Layer.STAGE_STATE, text=text, pinned=True, importance=0.85, key="stage")

    def add_open_questions(self, questions: list[str]) -> None:
        """Unresolved questions. Pinned: dropping these is how a run silently
        stops looking for the thing it was missing."""
        if not questions:
            return
        rendered = "\n".join(f"- {q}" for q in questions)
        self.add(Layer.OPEN_QUESTIONS, text=f"尚未解决的问题：\n{rendered}", pinned=True,
                 importance=0.95, key="open_questions")

    def add_evidence(self, evidence: list[EvidenceRecord]) -> None:
        if not evidence:
            return
        lines = source_digest(evidence)
        self.add(Layer.EVIDENCE, text="证据账本（数字与来源的对应关系，报告只能引用这里的内容）：\n"
                                      + "\n".join(lines),
                 importance=0.9, key="evidence_ledger")

    def add_tool_result(self, tool_name: str, summary: str, *, artifact_id: str = "",
                        importance: float = 0.5) -> None:
        self.add(Layer.TOOL_RESULTS, key=f"tool:{tool_name}", importance=importance,
                 artifact_id=artifact_id,
                 text=f"[{tool_name}] {summary}"
                      + (f"\n(完整结果见 artifact:{artifact_id})" if artifact_id else ""))

    def add_recent(self, entries: list[str]) -> None:
        for i, entry in enumerate(entries):
            # Later entries matter more; importance rises toward the end.
            self.add(Layer.RECENT, text=entry, key=f"recent:{i}",
                     importance=0.3 + 0.5 * (i + 1) / max(1, len(entries)))

    def add_memory(self, items: list[dict[str, Any]]) -> int:
        """Add retrieved memory under its own hard cap. Returns tokens used."""
        used = 0
        for item in items:
            text = f"[记忆:{item.get('kind', '?')}] {item.get('content', '')}"
            cost = estimate_tokens(text)
            if used + cost > self.memory_budget_tokens:
                break
            self.add(Layer.MEMORY, text=text, key=f"memory:{item.get('memory_id', '')}",
                     importance=float(item.get("importance", 0.5)))
            used += cost
        return used

    # ------------------------------------------------------------------ #
    def assemble(self) -> tuple[str, CompressionReport]:
        """Fit the blocks into the budget and render.

        Two passes: per-layer eviction by importance, then a global tail trim
        if the total still overflows (which can only bite the lowest-priority
        layers, since pinned high-priority blocks are never candidates).
        """
        report = CompressionReport(budget_tokens=self.budget_tokens,
                                   blocks_before=len(self.blocks))
        before_text = "\n\n".join(b.text for b in self.blocks)
        report.tokens_before = estimate_tokens(before_text)
        report.numbers_before = len(_NUMBER_RE.findall(before_text))
        report.citations_before = len(set(_CITATION_RE.findall(before_text)))
        report.evidence_before = sum(
            1 for b in self.blocks if b.layer == Layer.EVIDENCE for _ in b.text.splitlines()
            if _.strip().startswith("- "))

        kept: list[ContextBlock] = []
        dropped: dict[str, int] = {}

        for layer in sorted({b.layer for b in self.blocks}):
            layer_blocks = [b for b in self.blocks if b.layer == layer]
            cap = int(self.budget_tokens * self.shares.get(layer, 0.05))
            # Pinned first, then by importance; within equal importance keep
            # insertion order so "recent" stays chronological.
            ordered = sorted(layer_blocks, key=lambda b: (not b.pinned, -b.importance))
            used = 0
            for block in ordered:
                cost = block.tokens()
                if block.pinned or used + cost <= cap:
                    kept.append(block)
                    used += cost
                elif used < cap and cost > 0:
                    # Partially fits: truncate rather than drop, so a long tool
                    # result still contributes its head.
                    allowance = max(0, cap - used)
                    if allowance >= 40:
                        ratio = allowance / cost
                        cut = max(40, int(len(block.text) * ratio))
                        block.text = block.text[:cut] + "\n...[truncated for context budget]"
                        kept.append(block)
                        used += block.tokens()
                        report.truncated_blocks += 1
                    else:
                        dropped[layer.name] = dropped.get(layer.name, 0) + 1
                else:
                    dropped[layer.name] = dropped.get(layer.name, 0) + 1

        kept.sort(key=lambda b: (b.layer, -b.importance))
        rendered = "\n\n".join(b.text for b in kept if b.text.strip())

        # Global overflow guard: trim the lowest-priority tail until it fits.
        while estimate_tokens(rendered) > self.budget_tokens and kept:
            evictable = [b for b in kept if not b.pinned]
            if not evictable:
                break
            victim = max(evictable, key=lambda b: (b.layer, -b.importance))
            kept.remove(victim)
            dropped[victim.layer.name] = dropped.get(victim.layer.name, 0) + 1
            rendered = "\n\n".join(b.text for b in kept if b.text.strip())

        report.blocks_after = len(kept)
        report.tokens_after = estimate_tokens(rendered)
        report.dropped_by_layer = dropped
        report.numbers_after = len(_NUMBER_RE.findall(rendered))
        report.citations_after = len(set(_CITATION_RE.findall(rendered)))
        report.evidence_after = sum(
            1 for line in rendered.splitlines() if line.strip().startswith("- "))
        verify_consistency(report, kept)
        return rendered, report


def verify_consistency(report: CompressionReport, kept: list[ContextBlock]) -> CompressionReport:
    """Post-compression checks. Warnings, not exceptions - a degraded context
    is still usable, but the run must record that it was degraded.

    Checks:
      1. Open questions survived (they are pinned, so failure means a bug).
      2. The goal survived.
      3. No citation marker was orphaned - if `[s3]` still appears, source s3
         must still be described somewhere in the context, or the model will
         cite a source it can no longer see.
    """
    layers_present = {b.layer for b in kept}
    warnings: list[str] = []

    if Layer.TASK_GOAL not in layers_present:
        warnings.append("task goal was dropped from context (should be impossible: pinned)")
    report.open_questions_preserved = (
        Layer.OPEN_QUESTIONS in layers_present
        or report.dropped_by_layer.get(Layer.OPEN_QUESTIONS.name, 0) == 0
    )
    if not report.open_questions_preserved:
        warnings.append("open questions were dropped during compression")

    rendered = "\n".join(b.text for b in kept)
    cited = {m.strip("[]").replace("source_", "s") for m in _CITATION_RE.findall(rendered)}
    # A source counts as *described* only when the ledger still carries its
    # header line (`[s1] <title> (web tier=...)`, produced by source_digest).
    # Matching bare `[sN]` markers anywhere would make this check tautological -
    # every citation would "describe" itself.
    described = set(re.findall(r"^\[(s\d+)\]\s+\S", rendered, flags=re.MULTILINE))
    orphaned = cited - described
    if orphaned:
        warnings.append(f"citation markers with no visible source description: {sorted(orphaned)[:5]}")

    if report.numbers_before and report.numbers_after == 0:
        warnings.append("every substantive number was compressed away")

    report.consistency_warnings = warnings
    report.consistency_ok = not warnings
    return report
