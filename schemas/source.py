"""Schema describing a single information source (web page or PDF)."""
from typing import Any

from pydantic import BaseModel, Field


class Source(BaseModel):
    """A scored, content-bearing source used to ground the final report."""

    source_id: str
    title: str = ""
    url: str = ""
    snippet: str = ""
    content: str = ""
    source_type: str = Field(default="web", description="'web' or 'pdf'")
    score: float = 0.0
    quality_details: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
