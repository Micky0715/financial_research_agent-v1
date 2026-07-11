"""Tool invocation gateway: routes search/fetch tool calls through MCP.

Agents import `web_search` / `read_webpage` / `read_pdf` from here instead of
from the tool modules directly. The signatures and return shapes are identical
to the original functions, so nothing above this layer changes.

When config.USE_MCP_TOOLS is true, calls go through a real Model Context
Protocol client session: a persistent background thread runs an asyncio event
loop that spawns mcp_server/tools_server.py as a stdio subprocess and issues
`call_tool` requests over the protocol. Multiple pipeline threads (e.g.
ResearchAgent's ThreadPoolExecutor workers) share the one session - the MCP
client multiplexes concurrent requests by request id.

Degradation policy: if the MCP SDK is missing, the server fails to start, or
a call errors at the protocol level, the gateway logs a warning and falls
back to calling the underlying function directly - permanently for the rest
of the process (no retry storm). A network error *inside* a tool (e.g. a 403
while fetching a page) is NOT an MCP failure: it comes back as a normal
{success: false} payload through the protocol, exactly as the direct call
would return it.
"""
import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from config import config
from utils.logger import logger

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SERVER_SCRIPT = _PROJECT_ROOT / "mcp_server" / "tools_server.py"


class _McpGateway:
    """Lazy singleton owning the background event loop + MCP client session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._session = None
        self._started = False
        self._failed = False  # once true, all calls fall back to direct

    # ---------------------------------------------------------------- #
    # Session lifecycle
    # ---------------------------------------------------------------- #
    def _start_locked(self) -> None:
        """Spawn the event-loop thread and open the MCP session. Called once.

        The stdio transport and ClientSession use anyio task groups whose
        cancel scopes are bound to the task that entered them - if that task
        returns, the transport is torn down and the next call_tool fails with
        "Connection closed". So a single keeper task owns both context
        managers for the whole process lifetime and parks on an Event that is
        never set; call_tool requests are scheduled onto the same loop from
        pipeline threads via run_coroutine_threadsafe.
        """
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, name="mcp-gateway-loop", daemon=True).start()

        ready = threading.Event()
        startup_error: list[Exception] = []

        async def _session_keeper():
            try:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[str(_SERVER_SCRIPT)],
                    cwd=str(_PROJECT_ROOT),
                )
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        self._session = session
                        ready.set()
                        await asyncio.Event().wait()  # park forever; daemon thread dies with process
            except Exception as exc:  # noqa: BLE001 - surfaced to the waiting thread below
                startup_error.append(exc)
                ready.set()

        asyncio.run_coroutine_threadsafe(_session_keeper(), loop)
        if not ready.wait(timeout=30) or startup_error or self._session is None:
            raise RuntimeError(
                f"MCP session startup failed: {startup_error[0] if startup_error else 'timeout'}"
            )
        self._loop = loop
        logger.info("MCP tool gateway: session established with mcp_server/tools_server.py")

    def _ensure_session(self) -> bool:
        """Return True if an MCP session is (now) available."""
        if self._failed:
            return False
        if self._started:
            return self._session is not None
        with self._lock:
            if self._started:
                return self._session is not None
            try:
                self._start_locked()
            except Exception as exc:  # noqa: BLE001 - MCP unavailability must not break the pipeline
                logger.warning(f"MCP tool gateway unavailable, falling back to direct calls: {exc}")
                self._failed = True
            finally:
                self._started = True
        return self._session is not None

    # ---------------------------------------------------------------- #
    # Invocation
    # ---------------------------------------------------------------- #
    def call(self, tool_name: str, arguments: dict[str, Any]) -> Optional[Any]:
        """Call a tool over MCP. Returns the parsed JSON payload, or None on
        protocol failure (caller then falls back to the direct function)."""
        if not config.USE_MCP_TOOLS or not self._ensure_session():
            return None
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(tool_name, arguments), self._loop
            )
            result = future.result(timeout=config.MCP_TOOL_TIMEOUT)
            if result.isError:
                raise RuntimeError(f"tool returned error: {result.content}")
            text = next(
                (c.text for c in result.content if getattr(c, "text", None)), None
            )
            if text is None:
                raise RuntimeError("no text content in MCP tool result")
            return json.loads(text)
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash the pipeline
            # repr() not str(): a bare TimeoutError stringifies to "" and the
            # log line becomes undiagnosable (learned the hard way - a 4x
            # same-second degradation with empty reason turned out to be the
            # 90s future timeout during a slow live-search batch).
            logger.warning(
                f"MCP call_tool({tool_name}) failed, falling back to direct calls: {exc!r}"
            )
            self._failed = True
            return None


_gateway = _McpGateway()

_MCP_MISS = object()  # sentinel: MCP unavailable, use direct call


def _via_mcp(tool_name: str, arguments: dict[str, Any]) -> Any:
    result = _gateway.call(tool_name, arguments)
    return _MCP_MISS if result is None else result


# -------------------------------------------------------------------- #
# Public API - same signatures as the underlying tool functions
# -------------------------------------------------------------------- #
def web_search(query: str, max_results: int = 8) -> list[dict]:
    result = _via_mcp("web_search", {"query": query, "max_results": max_results})
    if result is not _MCP_MISS:
        return result
    from tools.web_search import web_search as direct
    return direct(query, max_results=max_results)


def read_webpage(url: str, max_chars: int = 8000) -> dict:
    result = _via_mcp("read_webpage", {"url": url, "max_chars": max_chars})
    if result is not _MCP_MISS:
        return result
    from tools.web_reader import read_webpage as direct
    return direct(url, max_chars=max_chars)


def read_pdf(url: str) -> dict:
    result = _via_mcp("read_pdf", {"url": url})
    if result is not _MCP_MISS:
        return result
    from tools.pdf_reader import read_pdf as direct
    return direct(url)
