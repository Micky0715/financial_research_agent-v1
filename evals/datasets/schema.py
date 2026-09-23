"""Evaluation task schema and dataset splitting.

The audit's finding that motivated this file: the existing `eval/topics.json`
and `eval/topics_competition_30.json` contain **no expected values at all**.
They are run lists, not a benchmark - the "evaluation" they support is the
system grading its own output with heuristics. That measures whether a report
looks like a report, not whether it is right, and it cannot measure tool
selection, argument correctness, or recovery.

An `EvalTask` therefore carries what a grader needs to disagree with the system:
required facts, forbidden claims, the expected outcome class, and (optionally)
which tools a competent run would use.

**Gold status is not a formality.** `gold_status` defaults to
`synthetic_draft`. Nothing becomes `human_verified` except by a person
recording their name and the date in `annotator` / `reviewed_at`. Aggregate
metrics report the two populations separately, so a number computed over
unreviewed synthetic tasks can never be presented as a verified result.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel, Field, model_validator

DATASET_SCHEMA_VERSION = "1.0"


class Category(str, Enum):
    """Task categories. The list is driven by the failure modes that actually
    occur in this system, not by a generic taxonomy."""

    COMPANY_RESEARCH = "company_research"
    INDUSTRY_RESEARCH = "industry_research"
    MACRO_RESEARCH = "macro_research"
    MULTI_SOURCE_VERIFICATION = "multi_source_verification"
    PDF_EXTRACTION = "pdf_extraction"
    STRUCTURED_DATA = "structured_data"
    SOURCE_CONFLICT = "source_conflict"
    UNREACHABLE_SOURCE = "unreachable_source"
    TOOL_TIMEOUT = "tool_timeout"
    INCOMPLETE_INFORMATION = "incomplete_information"
    MUST_REFUSE = "must_refuse"
    PROMPT_INJECTION = "prompt_injection"
    LONG_CONTEXT = "long_context"
    RESUME_AFTER_INTERRUPT = "resume_after_interrupt"
    INSUFFICIENT_BUDGET = "insufficient_budget"


class ExpectedOutcome(str, Enum):
    """What a correct run should end in. `INSUFFICIENT` and `REFUSE` are
    first-class successes: a system that fabricates a report for an unverifiable
    entity has failed the task, however polished the output."""

    REPORT_GENERATED = "report_generated"
    REPORT_WITH_CAVEATS = "report_with_caveats"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REFUSE = "refuse"
    PARTIAL_WITH_DEGRADATION = "partial_with_degradation"


class GoldStatus(str, Enum):
    SYNTHETIC_DRAFT = "synthetic_draft"      # generated, nobody has looked at it
    #: Hand-written adversarial probes. Their *expectations* are authored, so
    #: they are stronger than a generated draft, but no person has validated
    #: that the expectation is the right one - they can never become gold
    #: without review.
    SYNTHETIC_ADVERSARIAL = "synthetic_adversarial"
    MACHINE_VERIFIED = "machine_verified"    # deterministic check confirms the expectation
    NEEDS_HUMAN_REVIEW = "needs_human_review"  # queued for a person, not yet reviewed
    HUMAN_VERIFIED = "human_verified"        # a named person reviewed it on a given date


class Split(str, Enum):
    DEV = "dev"
    TEST = "test"
    REGRESSION = "regression"


class RequiredFact(BaseModel):
    """One checkable claim the report must contain.

    `any_of` holds acceptable surface forms - "1200.5亿元" and "1,200.5亿元" are
    the same fact. `tolerance` allows numeric comparison when `value` parses as
    a number.
    """

    key: str
    description: str = ""
    any_of: list[str] = Field(default_factory=list)
    value: Optional[float] = None
    unit: str = ""
    tolerance: float = 0.01
    must_be_cited: bool = True

    def matches(self, text: str) -> bool:
        return any(form and form in text for form in self.any_of)


class EvalTask(BaseModel):
    """One evaluation case."""

    schema_version: str = DATASET_SCHEMA_VERSION
    task_id: str
    category: Category
    query: str
    report_type: str = "company_research"

    #: Constraints the run must respect (max sources, required sections, ...).
    task_constraints: dict[str, Any] = Field(default_factory=dict)
    required_facts: list[RequiredFact] = Field(default_factory=list)
    expected_outcome: ExpectedOutcome = ExpectedOutcome.REPORT_GENERATED
    #: Tools a competent run is expected to use. Optional because for most
    #: tasks several tool sequences are equally valid.
    optional_expected_tools: list[str] = Field(default_factory=list)
    #: Tools that would be wrong here (used for Tool Selection Precision).
    forbidden_tools: list[str] = Field(default_factory=list)

    #: Tools without which the task *cannot* be done correctly. Distinct from
    #: `optional_expected_tools`: missing one of these is a hard failure, and
    #: this is what `Necessary Tool Recall` measures.
    required_tools: list[str] = Field(default_factory=list)
    #: Sets of tools that each represent an acceptable solution. A task with
    #: two viable routes declares both, so a run is not penalised for picking
    #: the other valid one. Feeds `Alternative Valid Path Rate`.
    acceptable_tool_paths: list[list[str]] = Field(default_factory=list)
    #: Upper bound on tool calls a competent run needs. Calls beyond this are
    #: counted by `Unnecessary Tool Call Rate`. None = no expectation.
    max_expected_tool_calls: Optional[int] = None
    #: True when a correct run needs no tool at all (answerable from the
    #: request itself, or must refuse before searching).
    expects_no_tools: bool = False
    #: Statements that must NOT appear - fabricated entities, invented figures,
    #: investment advice phrased as certainty.
    forbidden_claims: list[str] = Field(default_factory=list)

    budget_limit: dict[str, Any] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)

    #: Provenance. `data_source` says where the case came from; a case derived
    #: from a real bad case cites it.
    data_source: str = "synthetic"
    source_reference: str = ""
    gold_status: GoldStatus = GoldStatus.SYNTHETIC_DRAFT
    annotator: str = ""
    reviewed_at: str = ""

    #: Grouping key for leakage-free splitting: company name, industry, or
    #: source domain. Two tasks about the same company must never straddle
    #: the dev/test boundary.
    group_key: str = ""
    split: Split = Split.DEV
    #: Fixture scenario name for offline replay, if this task is replayable.
    fixture_scenario: str = ""

    # Model-level, not field-level: a `@field_validator("gold_status")` runs
    # before `annotator`/`reviewed_at` are populated (they are declared later),
    # so it would reject every human-verified task regardless of its fields.
    @model_validator(mode="after")
    def _human_verified_requires_an_annotator(self) -> "EvalTask":
        if self.gold_status is GoldStatus.HUMAN_VERIFIED and not (self.annotator and self.reviewed_at):
            raise ValueError(
                "gold_status=human_verified requires both `annotator` and `reviewed_at`; "
                "an unreviewed task must stay synthetic_draft")
        return self

    @property
    def is_verified(self) -> bool:
        return self.gold_status is GoldStatus.HUMAN_VERIFIED

    def effective_group(self) -> str:
        return self.group_key or self.query


class EvalDataset(BaseModel):
    """A named collection of tasks with provenance."""

    schema_version: str = DATASET_SCHEMA_VERSION
    name: str
    description: str = ""
    created_at: str = ""
    tasks: list[EvalTask] = Field(default_factory=list)

    def by_split(self, split: Split) -> list[EvalTask]:
        return [t for t in self.tasks if t.split is split]

    def by_category(self, category: Category) -> list[EvalTask]:
        return [t for t in self.tasks if t.category is category]

    def verified_only(self) -> list[EvalTask]:
        return [t for t in self.tasks if t.is_verified]

    def stats(self) -> dict[str, Any]:
        """Counts by split, category and gold status.

        The gold-status breakdown is reported everywhere this dataset is used;
        it is the number that stops a synthetic draft being read as a result.
        """
        by_status: dict[str, int] = {}
        by_split: dict[str, int] = {}
        by_category: dict[str, int] = {}
        for task in self.tasks:
            by_status[task.gold_status.value] = by_status.get(task.gold_status.value, 0) + 1
            by_split[task.split.value] = by_split.get(task.split.value, 0) + 1
            by_category[task.category.value] = by_category.get(task.category.value, 0) + 1
        return {
            "total": len(self.tasks),
            "by_gold_status": by_status,
            "by_split": by_split,
            "by_category": by_category,
            "human_verified": by_status.get(GoldStatus.HUMAN_VERIFIED.value, 0),
            "groups": len({t.effective_group() for t in self.tasks}),
        }

    def save(self, path: Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: Path) -> "EvalDataset":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Leakage-free splitting
# --------------------------------------------------------------------------- #
def _group_bucket(group: str, buckets: int) -> int:
    """Deterministic hash bucket. Stable across runs and Python versions -
    `hash()` is salted per process and would reshuffle the split every run."""
    digest = hashlib.sha256(group.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % buckets


def assign_splits(tasks: Iterable[EvalTask], *, dev_ratio: float = 0.6,
                  test_ratio: float = 0.3) -> list[EvalTask]:
    """Assign dev/test/regression **by group, never by task**.

    Splitting per-task would put "贵州茅台估值分析" in dev and "贵州茅台风险分析"
    in test, and the test score would then partly measure memorisation of the
    same sources. Grouping by company/industry/source makes the test split an
    actual held-out population.

    Tasks already tagged `regression` keep that split regardless of the ratios.
    """
    tasks = list(tasks)
    dev_cut = int(dev_ratio * 100)
    test_cut = int((dev_ratio + test_ratio) * 100)
    for task in tasks:
        if task.split is Split.REGRESSION or "regression" in task.tags:
            task.split = Split.REGRESSION
            continue
        bucket = _group_bucket(task.effective_group(), 100)
        if bucket < dev_cut:
            task.split = Split.DEV
        elif bucket < test_cut:
            task.split = Split.TEST
        else:
            task.split = Split.REGRESSION
    return tasks


def check_no_leakage(tasks: Iterable[EvalTask]) -> list[str]:
    """Return a list of groups that appear in more than one split."""
    seen: dict[str, set[str]] = {}
    for task in tasks:
        seen.setdefault(task.effective_group(), set()).add(task.split.value)
    return sorted(group for group, splits in seen.items() if len(splits) > 1)


def dedup_tasks(tasks: Iterable[EvalTask]) -> tuple[list[EvalTask], int]:
    """Drop tasks whose (category, normalized query) already appeared."""
    seen: set[str] = set()
    out: list[EvalTask] = []
    dropped = 0
    for task in tasks:
        key = json.dumps([task.category.value, "".join(task.query.split()).lower()],
                         ensure_ascii=False)
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(task)
    return out, dropped
