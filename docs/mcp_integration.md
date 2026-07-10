# MCP 协议封装说明

v2 模块1：把 `web_search` / `read_webpage` / `read_pdf` 三个无状态工具函数封装为标准 MCP（Model Context Protocol）Server，`ResearchAgent` / `BrowserAgent` 作为 MCP Client 通过协议调用，替代原来的直接函数调用。

## 组件

| 文件 | 角色 |
|---|---|
| [mcp_server/tools_server.py](../mcp_server/tools_server.py) | MCP Server（官方 `mcp` SDK 的 FastMCP，stdio 传输），用 `@mcp.tool()` 暴露三个工具 |
| [tools/tool_gateway.py](../tools/tool_gateway.py) | Agent 侧调用网关：持久 MCP Client session（后台线程 + asyncio loop + 子进程 stdio），对上层提供与原函数完全相同的同步签名 |
| `config.USE_MCP_TOOLS` | 开关（默认 true）；`MCP_TOOL_TIMEOUT` 单次协议调用超时 |

## 改造前后的调用方式对比

**改造前（直接函数调用）**：

```python
# agents/research_agent.py
from tools.web_search import web_search
hits = web_search(query, max_results=8)          # 同进程内普通函数调用

# agents/browser_agent.py
from tools.web_reader import read_webpage
from tools.pdf_reader import read_pdf
parsed = read_webpage(url, max_chars=8000)       # 同进程内普通函数调用
```

**改造后（MCP 协议调用）**：

```python
# agents/research_agent.py —— 只改 import，调用代码零改动
from tools.tool_gateway import web_search
hits = web_search(query, max_results=8)
# 实际链路：
#   tool_gateway.web_search()
#     -> MCP ClientSession.call_tool("web_search", {"query": ..., "max_results": 8})
#     -> stdio 子进程 mcp_server/tools_server.py
#     -> @mcp.tool() def web_search(...) -> 原 tools/web_search.py 函数
#     -> JSON 结果经协议返回，网关反序列化为原返回类型

# agents/browser_agent.py —— 同样只改 import
from tools.tool_gateway import read_pdf, read_webpage
```

Server 端注册方式：

```python
# mcp_server/tools_server.py
mcp = FastMCP("financial-research-tools")

@mcp.tool()
def web_search(query: str, max_results: int = 8) -> str:
    return json.dumps(_web_search(query, max_results=max_results), ensure_ascii=False)
```

## 设计决策

1. **网关签名与原函数完全一致**——Agent 主链路逻辑零改动，每个 Agent 只改一行 import。协议边界收敛在 `tool_gateway.py` 一个文件里。
2. **持久 session 而不是每次调用起一个子进程**——server 子进程在首次调用时启动，之后所有调用复用同一个 session。`ResearchAgent` 的 `ThreadPoolExecutor` 并发 worker 共享这一个 session，MCP client 按 request id 多路复用并发请求。
3. **优雅降级**——`mcp` 包缺失、server 启动失败、协议层调用异常时，网关打 warning 并对本进程后续所有调用回退为直接函数调用（不反复重试 MCP）。工具内部的业务失败（如抓网页 403）不算 MCP 故障，会作为正常的 `{success: false}` 载荷经协议返回，与直接调用行为一致。
4. **搜索缓存留在 client 侧**——`ResearchAgent` 的 query 缓存查询发生在网关之上，缓存命中时根本不会发起 MCP 调用，保持热缓存性能不变。
5. **结果用 JSON 字符串传输**——server 端 `json.dumps`、网关端 `json.loads`，不依赖 MCP SDK 版本各异的结构化内容序列化行为。

## 边界说明（诚实起见）

- 这是**进程内自用的 MCP 化**：server 由网关自动拉起、只服务本 pipeline。还没有做成独立部署、供外部 IDE/Agent 连接的公共 MCP 服务（那需要 SSE/HTTP 传输和鉴权，属于后续方向）。
- `analyze` 阶段的规则抽取函数（financial/risk/valuation analyzer）没有 MCP 化：它们的输入是已在内存里的来源全文，走协议序列化几万字符纯属开销，没有跨进程复用价值。
