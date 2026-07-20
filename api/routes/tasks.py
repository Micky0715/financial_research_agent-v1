"""GET /tasks/{task_id} (v4 stage J)."""
from fastapi import APIRouter, HTTPException

from api.schemas import TaskStatusResponse
from api.task_manager import task_manager

router = APIRouter()


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
def get_task(task_id: str) -> TaskStatusResponse:
    record = task_manager.get(task_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found "
                            "(unknown id, or the service restarted and lost in-memory state)")
    return TaskStatusResponse(
        task_id=record.task_id, status=record.status, progress=record.progress,
        current_stage=record.current_stage, error=record.error, topic=record.topic,
        report_type=record.report_type, created_at=record.created_at,
        started_at=record.started_at, finished_at=record.finished_at,
    )
