"""POST /reports, GET /reports/{task_id} (v4 stage J)."""
import json
from collections import Counter
from pathlib import Path

from fastapi import APIRouter, HTTPException

from api.schemas import CreateReportRequest, CreateReportResponse, ReportResultResponse
from api.task_manager import task_manager

router = APIRouter()


@router.post("/reports", response_model=CreateReportResponse)
def create_report(request: CreateReportRequest) -> CreateReportResponse:
    task_id = task_manager.submit(request)
    return CreateReportResponse(task_id=task_id, status="queued")


def _sources_summary(sources_path: str) -> dict:
    try:
        sources = json.loads(Path(sources_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    tiers = Counter(s.get("authority_tier", "unknown") for s in sources)
    return {"count": len(sources), "tier_breakdown": dict(tiers)}


@router.get("/reports/{task_id}", response_model=ReportResultResponse)
def get_report(task_id: str) -> ReportResultResponse:
    record = task_manager.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    if record.status in ("queued", "running"):
        raise HTTPException(status_code=202, detail=f"task {task_id} is still {record.status}; "
                            f"poll GET /tasks/{task_id} until it reaches a terminal status")

    result = record.result or {}
    evaluation = result.get("evaluation", {}) or {}
    sources_summary = _sources_summary(result.get("sources_path", "")) if result.get("sources_path") else {}

    return ReportResultResponse(
        task_id=task_id, status=record.status,
        title=f"{record.topic} 研究报告", report_path=result.get("report_path", ""),
        output_format=Path(result.get("report_path", "")).suffix.lstrip(".") or "markdown",
        export_paths=record.export_paths, evaluation=evaluation,
        sources_summary=sources_summary, warnings=record.warnings, error=record.error,
        harness_run_id=result.get("harness_run_id", ""),
        harness_status=result.get("harness_status", ""),
        harness_stop_reason=result.get("harness_stop_reason", ""),
        harness_trace_path=result.get("harness_trace_path", ""),
    )
