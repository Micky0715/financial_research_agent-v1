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
    source_type: str = Field(default="web", description="'web', 'pdf' or 'local_file'")
    score: float = 0.0
    quality_details: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    # v3 stage C: authority tier classification (tools/source_tier.py).
    # Heuristic labels - "tier1" does not mean guaranteed correctness.
    authority_tier: str = Field(default="", description="tier1|tier2|tier3|unknown|local_user_file")
    authority_reason: str = ""
    domain: str = ""
    is_official_source: bool = False
    is_financial_media: bool = False
    is_user_generated_content: bool = False
