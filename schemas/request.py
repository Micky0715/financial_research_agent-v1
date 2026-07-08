"""Schema describing an incoming user research request."""
from pydantic import BaseModel, Field


class ResearchRequest(BaseModel):
    """A normalized user request for a financial research report."""

    topic: str = Field(..., description="Research topic, e.g. '宁德时代投资分析'")
    report_type: str = Field(
        default="company_research",
        description="'company_research' or 'industry_research'",
    )
    requirements: list[str] = Field(
        default_factory=list,
        description="Sections/aspects the user explicitly asked for",
    )
    output_format: str = Field(default="markdown", description="'markdown' or 'html'")
    language: str = Field(default="zh")
    max_sources: int = Field(default=5, ge=1, le=50)
