"""Schema describing a full execution trace for one run of the pipeline."""
from typing import Any

from pydantic import BaseModel, Field


class TraceLog(BaseModel):
    """Structured record of every step taken during a run, for debugging bad cases."""

    run_id: str
    topic: str
    started_at: str
    finished_at: str = ""
    total_duration: float = 0.0
    steps: list[dict[str, Any]] = Field(default_factory=list)
    selected_sources: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    final_report_path: str = ""

    # Performance/quality diagnostics surfaced by planning_agent, research_agent,
    # browser_agent, analyze_agent, report_agent - see orchestrator/workflow.py
    # for how these get populated.
    planner_metrics: dict[str, Any] = Field(default_factory=dict)
    research_metrics: dict[str, Any] = Field(default_factory=dict)
    replan_metrics: dict[str, Any] = Field(default_factory=dict)
    browser_metrics: dict[str, Any] = Field(default_factory=dict)
    performance_metrics: dict[str, Any] = Field(default_factory=dict)
    compression_metrics: dict[str, Any] = Field(default_factory=dict)
    evaluation_diagnostics: dict[str, Any] = Field(default_factory=dict)
