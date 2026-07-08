"""Schemas describing planned tasks and their execution results."""
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

TASK_TYPES = ("planning", "research", "browse", "analyze", "report", "evaluate")

TaskType = Literal["planning", "research", "browse", "analyze", "report", "evaluate"]


class Task(BaseModel):
    """A single unit of work produced by the PlanningAgent."""

    task_id: str
    task_type: TaskType
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    priority: int = Field(default=1, description="Lower runs first")
    max_retries: int = Field(default=2, ge=0)
    status: str = Field(default="pending", description="pending|running|success|failed")


class TaskResult(BaseModel):
    """Outcome of executing a single Task."""

    task_id: str
    task_type: TaskType
    success: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    execution_time: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
