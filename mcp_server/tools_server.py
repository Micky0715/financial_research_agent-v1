"""MCP server exposing this project's stateless research tools.

Run standalone (stdio transport):
    python mcp_server/tools_server.py

Normally you don't start this by hand - tools/tool_gateway.py spawns it as a
subprocess and talks to it over the Model Context Protocol. The tools wrapped
here are the exact same functions the agents used to call directly
(tools/web_search.py, tools/web_reader.py, tools/pdf_reader.py); this file
adds only the protocol boundary, no behavior changes.

Results are returned as JSON strings (json.dumps) rather than relying on the
SDK's structured-content serialization, so the client side can json.loads
them uniformly regardless of MCP SDK version.
"""
import json
import sys
from pathlib import Path

# The server runs as its own subprocess with cwd anywhere - make the project
# importable regardless of how it was launched.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from tools.pdf_reader import read_pdf as _read_pdf  # noqa: E402
from tools.web_reader import read_webpage as _read_webpage  # noqa: E402
from tools.web_search import web_search as _web_search  # noqa: E402

mcp = FastMCP("financial-research-tools")


@mcp.tool()
def web_search(query: str, max_results: int = 8) -> str:
    """Search the web via ddgs. Returns a JSON array of {title, url, snippet}."""
    return json.dumps(_web_search(query, max_results=max_results), ensure_ascii=False)


@mcp.tool()
def read_webpage(url: str, max_chars: int = 8000) -> str:
    """Fetch and extract main content from a web page. Returns a JSON object
    with {success, title, content, ...} or {success: false, error}."""
    return json.dumps(_read_webpage(url, max_chars=max_chars), ensure_ascii=False)


@mcp.tool()
def read_pdf(url: str) -> str:
    """Download and extract full text from a PDF. Returns a JSON object with
    {success, full_text, page_count, ...} or {success: false, error}."""
    return json.dumps(_read_pdf(url), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
