"""Schema describing the final generated report."""
from typing import Any, Optional

from pydantic import BaseModel, Field

from .source import Source


class Report(BaseModel):
    """Final research report along with its grounding sources and QA score."""

    topic: str
    title: str
    content: str
    output_format: str = "markdown"
    sources: list[Source] = Field(default_factory=list)
    quality_score: Optional[dict[str, Any]] = None
    created_at: str = ""
