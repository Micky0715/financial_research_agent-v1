"""FastAPI service entrypoint (v4 stage J).

Run:
    uvicorn api.main:app --host 0.0.0.0 --port 8000

Docs at /docs, health at /health. API key(s) are read from environment
variables via config.py / .env — never hardcoded here or in any route.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402

from api.routes import health, reports, tasks  # noqa: E402
from config import config  # noqa: E402

app = FastAPI(
    title="Financial Research Agent API",
    description=(
        "自动化金融研究报告生成服务（v4-competition）。数据来自免费公开来源，"
        "不接入 Wind，不使用付费金融数据 API；所有报告不构成投资建议，"
        "仅供技术研究参考。详见 README.md 与 docs/competition_alignment.md。"
    ),
    version="4.0.0",
)

app.include_router(health.router, tags=["health"])
app.include_router(reports.router, tags=["reports"])
app.include_router(tasks.router, tags=["tasks"])


@app.get("/", tags=["health"])
def root() -> dict:
    return {
        "service": "financial_research_agent",
        "version": "4.0.0",
        "docs": "/docs",
        "health": "/health",
        "max_concurrent_tasks": config.API_MAX_CONCURRENT_TASKS,
        "disclaimer": "本服务生成的报告不构成投资建议，仅供技术研究参考。",
    }
