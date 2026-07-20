"""Pydantic request/response models for the FastAPI service (v4 stage J)."""
from typing import Any, Optional

from pydantic import BaseModel, Field


class CreateReportRequest(BaseModel):
    topic: str = Field(..., description="研究主题，如 '贵州茅台投资价值分析'")
    report_type: str = Field(default="company_research",
                             description="company_research/industry_research/macro_research/"
                                         "risk_research/valuation_research")
    requirements: list[str] = Field(default_factory=list)
    local_files: list[str] = Field(default_factory=list, description="服务器本地可访问的文件路径")
    output_format: str = Field(default="html", description="markdown 或 html")
    max_sources: int = Field(default=5, ge=1, le=50)
    export_formats: list[str] = Field(default_factory=list, description="子集: docx, pdf")
    enable_memory: bool = Field(default=False, description="任务完成后是否重建轻量记忆索引")
    enable_revision: bool = Field(default=True, description="是否允许有界(<=1轮)自检改稿")


class CreateReportResponse(BaseModel):
    task_id: str
    status: str


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str  # queued / running / succeeded / failed / degraded
    progress: float = 0.0
    current_stage: str = ""
    error: Optional[str] = None
    topic: str = ""
    report_type: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""


class ReportResultResponse(BaseModel):
    task_id: str
    status: str
    title: str = ""
    report_path: str = ""
    output_format: str = ""
    export_paths: dict[str, Any] = Field(default_factory=dict)
    evaluation: dict[str, Any] = Field(default_factory=dict)
    sources_summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: Optional[str] = None


class HealthCheckItem(BaseModel):
    name: str
    healthy: bool
    detail: str = ""


class HealthResponse(BaseModel):
    status: str  # healthy / degraded / unhealthy
    checks: list[HealthCheckItem]
