"""GET /health (v4 stage J).

每项检查都是**廉价、非阻塞**的可用性/配置检查，不在每次 /health 调用时
真的打一次 LLM/网络请求（避免把健康检查变成计费调用）——这意味着"healthy"
表示"已正确配置/依赖可导入"，不保证运行时一定成功（网络抖动仍可能导致
单次任务降级，属已声明限制）。
"""
from pathlib import Path

from fastapi import APIRouter

from api.schemas import HealthCheckItem, HealthResponse
from config import config

router = APIRouter()


def _check_api() -> HealthCheckItem:
    return HealthCheckItem(name="api", healthy=True, detail="FastAPI service running")


def _check_llm_provider() -> HealthCheckItem:
    configured = bool(config.OPENAI_API_KEY or config.DEEPSEEK_API_KEY or config.ANTHROPIC_API_KEY)
    return HealthCheckItem(
        name="llm_provider", healthy=configured,
        detail=f"model={config.MODEL_NAME}, api_key_configured={configured}"
              + ("" if configured else "（未配置任何 LLM API Key，主链路会走确定性 fallback）"))


def _check_mcp() -> HealthCheckItem:
    try:
        import mcp  # noqa: F401

        return HealthCheckItem(name="mcp", healthy=True, detail="mcp SDK importable")
    except Exception as exc:  # noqa: BLE001
        return HealthCheckItem(name="mcp", healthy=False,
                               detail=f"mcp SDK unavailable, gateway degrades to direct calls: {exc!r}")


def _check_akshare() -> HealthCheckItem:
    try:
        import akshare  # noqa: F401

        return HealthCheckItem(name="akshare", healthy=True, detail="akshare importable")
    except Exception as exc:  # noqa: BLE001
        return HealthCheckItem(name="akshare", healthy=False,
                               detail=f"akshare unavailable, structured data degrades: {exc!r}")


def _check_embedding() -> HealthCheckItem:
    try:
        from sentence_transformers import SentenceTransformer  # noqa: F401

        return HealthCheckItem(name="embedding", healthy=True,
                               detail="sentence-transformers importable (semantic ranking + memory)")
    except Exception as exc:  # noqa: BLE001
        return HealthCheckItem(name="embedding", healthy=False,
                               detail=f"unavailable, degrades to keyword-only ranking/search: {exc!r}")


def _check_export_deps() -> HealthCheckItem:
    try:
        import docx  # noqa: F401

        docx_ok = True
    except Exception:  # noqa: BLE001
        docx_ok = False
    edge_paths = [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                 Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]
    edge_ok = any(p.exists() for p in edge_paths)
    healthy = docx_ok  # PDF (Edge) 缺失只降级，不算不健康
    return HealthCheckItem(
        name="export_deps", healthy=healthy,
        detail=f"docx={docx_ok}, pdf_via_edge={edge_ok}" + ("" if edge_ok else "（PDF导出将降级为仅DOCX/HTML）"))


def _check_memory() -> HealthCheckItem:
    try:
        import memory.vector_store  # noqa: F401

        index_exists = (config.BASE_DIR / "memory" / "index" / "records.jsonl").exists()
        return HealthCheckItem(name="memory", healthy=True,
                               detail=f"module importable, index_built={index_exists}（默认不参与主链路）")
    except Exception as exc:  # noqa: BLE001
        return HealthCheckItem(name="memory", healthy=False, detail=f"unavailable: {exc!r}")


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    checks = [_check_api(), _check_llm_provider(), _check_mcp(), _check_akshare(),
             _check_embedding(), _check_export_deps(), _check_memory()]
    # 核心依赖（LLM/AkShare 缺失才算不健康；MCP/embedding/export/memory 都有明确降级路径）
    core_ok = all(c.healthy for c in checks if c.name in ("api", "llm_provider"))
    any_degraded = any(not c.healthy for c in checks)
    status = "healthy" if core_ok and not any_degraded else ("degraded" if core_ok else "unhealthy")
    return HealthResponse(status=status, checks=checks)
