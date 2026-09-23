"""Tool registry: the single catalog of what the agent may call.

Honesty rule, restated from the audit: this project has **five** genuinely
agent-selectable tools, not forty-nine. `tools/*.py` contains ~45 deterministic
analysis functions (DCF, DuPont, concentration, ...) which are steps in a fixed
chain, not choices - they are registered under `ToolCategory.INTERNAL_STEP` so
their execution is still traced and budgeted, but `catalog()` excludes them by
default so no prompt or report can imply the agent is choosing among 49 tools.

Handlers wrap the existing `tools/tool_gateway.py` functions rather than
replacing them, so MCP routing and its degrade-to-direct behaviour are
preserved exactly.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Iterable, Optional

from src.runtime.errors import ToolNotFoundError
from src.tools.sanitize import scan_untrusted_content, wrap_untrusted
from src.tools.schemas import (
    AGENT_SELECTABLE,
    CostHint,
    ToolCategory,
    ToolRegistration,
    ToolSpec,
)


class ToolRegistry:
    """Name -> (spec, handler). Thread-safe for concurrent tool execution."""

    def __init__(self) -> None:
        self._entries: dict[str, ToolRegistration] = {}
        self._lock = threading.RLock()

    def register(self, spec: ToolSpec, handler: Callable[..., Any],
                 projector: Optional[Callable[[Any], tuple[Any, Any]]] = None) -> ToolSpec:
        with self._lock:
            if spec.name in self._entries:
                raise ValueError(f"tool {spec.name!r} is already registered")
            self._entries[spec.name] = ToolRegistration(spec=spec, handler=handler, projector=projector)
            return spec

    def replace(self, spec: ToolSpec, handler: Callable[..., Any],
                projector: Optional[Callable[[Any], tuple[Any, Any]]] = None) -> ToolSpec:
        """Register, overwriting any existing entry. Used by tests and fixtures."""
        with self._lock:
            self._entries[spec.name] = ToolRegistration(spec=spec, handler=handler, projector=projector)
            return spec

    def unregister(self, name: str) -> None:
        with self._lock:
            self._entries.pop(name, None)

    def get(self, name: str) -> ToolRegistration:
        with self._lock:
            entry = self._entries.get(name)
        if entry is None:
            raise ToolNotFoundError(
                f"unknown tool {name!r}; registered: {sorted(self._entries)}",
                details={"requested": name, "available": sorted(self._entries)},
            )
        return entry

    def spec(self, name: str) -> ToolSpec:
        return self.get(name).spec

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._entries

    def names(self, *, selectable_only: bool = False) -> list[str]:
        with self._lock:
            entries = list(self._entries.values())
        return sorted(
            e.spec.name for e in entries
            if not selectable_only or e.spec.category in AGENT_SELECTABLE
        )

    def catalog(self, *, selectable_only: bool = True,
                categories: Optional[Iterable[ToolCategory]] = None) -> list[dict[str, Any]]:
        """Compact catalog for prompts and router datasets."""
        wanted = set(categories) if categories else None
        with self._lock:
            entries = list(self._entries.values())
        out = []
        for entry in sorted(entries, key=lambda e: e.spec.name):
            spec = entry.spec
            if selectable_only and spec.category not in AGENT_SELECTABLE:
                continue
            if wanted and spec.category not in wanted:
                continue
            out.append(spec.to_catalog_entry())
        return out

    def stats(self) -> dict[str, int]:
        with self._lock:
            entries = list(self._entries.values())
        return {
            "total": len(entries),
            "agent_selectable": sum(1 for e in entries if e.spec.category in AGENT_SELECTABLE),
            "internal_steps": sum(1 for e in entries if e.spec.category == ToolCategory.INTERNAL_STEP),
        }


# --------------------------------------------------------------------------- #
# Result projectors - keep big payloads out of the agent's context
# --------------------------------------------------------------------------- #
def _project_search(raw: Any) -> tuple[Any, Any]:
    """Search returns a list of hits; keep title/url/snippet only, drop the rest."""
    hits = raw if isinstance(raw, list) else []
    compact = [
        {"title": (h.get("title") or "")[:200],
         "url": h.get("url", ""),
         "snippet": (h.get("snippet") or "")[:300]}
        for h in hits if isinstance(h, dict)
    ]
    return compact, raw


def _project_page(raw: Any) -> tuple[Any, Any]:
    """Web/PDF reads: the agent gets metadata plus a bounded excerpt; the full
    body goes to the artifact store and is retrieved by id when a grader or the
    report builder needs to verify a quote.

    This is also the choke point where *all* external content enters the system,
    so it is where untrusted content is scanned for injection attempts and the
    excerpt is wrapped in an explicit data envelope.
    """
    if not isinstance(raw, dict):
        return raw, raw
    body = raw.get("content") or raw.get("full_text") or ""
    verdict = scan_untrusted_content(body)
    compact = {
        "success": bool(raw.get("success")),
        "title": (raw.get("title") or "")[:200],
        "url": raw.get("url", ""),
        "content_chars": len(body),
        "excerpt": wrap_untrusted(body[:800], raw.get("url", "web")),
        "page_count": raw.get("page_count"),
        "error": raw.get("error"),
        "injection_suspected": verdict["suspected"],
        "injection_categories": verdict["categories"],
    }
    full = dict(raw)
    full["injection_scan"] = verdict
    return compact, full


def _project_snapshot(raw: Any) -> tuple[Any, Any]:
    """AkShare snapshot: already structured and small; pass through unchanged
    so number-grounding can match values exactly."""
    return raw, raw


# --------------------------------------------------------------------------- #
# Specs for the agent-selectable tools
# --------------------------------------------------------------------------- #
SEARCH_SPEC = ToolSpec(
    name="web_search",
    namespace="research",
    category=ToolCategory.SEARCH,
    description="按关键词检索公开网页，返回候选标题、URL 和摘要，不返回正文。",
    when_to_use=(
        "需要发现新的信息来源时的第一步：不知道有哪些页面讨论该主体/行业/指标。"
        "适合公司公告线索、行业规模口径、政策原文入口的定位。"
    ),
    when_not_to_use=(
        "已经拿到 URL 时不要再搜——直接用 read_webpage。"
        "上市公司财务数值不要靠搜索取数，用 fetch_financial_snapshot，"
        "搜索结果里的财务数字往往是二手转述且口径不明。"
        "同一 query 已经搜过时不要重复调用（结果 24h 内缓存，重复调用只是浪费预算）。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 300,
                      "description": "检索式。中文主题词 + 意图词效果最好，可用 site: 限定权威站点。"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "array",
        "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "url": {"type": "string"}, "snippet": {"type": "string"}}},
    },
    returns_summary_of="候选来源列表（title/url/snippet），无正文",
    cost_hint=CostHint.MODERATE,
    timeout_s=20.0,
    max_attempts=3,
)

READ_WEBPAGE_SPEC = ToolSpec(
    name="read_webpage",
    namespace="research",
    category=ToolCategory.WEB_READ,
    description="抓取并抽取一个网页的正文文本。",
    when_to_use="已有具体 URL、且需要正文内容做引用或事实核验时。",
    when_not_to_use=(
        "URL 以 .pdf 结尾时用 read_pdf。"
        "只需要知道页面存在/标题时不必抓正文。"
        "同一 URL 在本次运行中已抓取成功时不要重复抓（执行器会去重并记为冗余调用）。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "minLength": 4, "maxLength": 2000},
            "max_chars": {"type": "integer", "minimum": 500, "maximum": 50000, "default": 8000},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"success": {"type": "boolean"}, "content_chars": {"type": "integer"}},
        "required": ["success"],
    },
    returns_summary_of="success/title/url/正文字符数/前 800 字摘要；完整正文存 artifact",
    cost_hint=CostHint.MODERATE,
    timeout_s=25.0,
    max_attempts=2,
)

READ_PDF_SPEC = ToolSpec(
    name="read_pdf",
    namespace="research",
    category=ToolCategory.PDF_READ,
    description="下载并解析 PDF，抽取全文文本。",
    when_to_use="URL 指向 PDF（年报、券商研报、政策文件）且需要其中的文本或数字时。",
    when_not_to_use=(
        "普通 HTML 页面用 read_webpage。"
        "扫描件/图片型 PDF 抽不出文本，解析失败后不要反复重试——本工具不做 OCR。"
    ),
    input_schema={
        "type": "object",
        "properties": {"url": {"type": "string", "minLength": 4, "maxLength": 2000}},
        "required": ["url"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"success": {"type": "boolean"}, "page_count": {"type": ["integer", "null"]}},
        "required": ["success"],
    },
    returns_summary_of="success/页数/文本字符数/前 800 字摘要；完整文本存 artifact",
    cost_hint=CostHint.EXPENSIVE,
    timeout_s=60.0,
    max_attempts=2,
)

FINANCIAL_SNAPSHOT_SPEC = ToolSpec(
    name="fetch_financial_snapshot",
    namespace="market",
    category=ToolCategory.MARKET_DATA,
    description="通过 AkShare 获取 A 股上市公司的结构化财务快照（营收/净利/毛利率/ROE/PE/PB 等）。",
    when_to_use=(
        "研究对象是沪深北 A 股上市公司、且需要可溯源的结构化财务数值时。"
        "这是财务数字的首选来源：字段有 lineage，优于从网页正文里读到的转述数字。"
    ),
    when_not_to_use=(
        "研究对象是行业/宏观主题时（没有对应证券代码，会返回失败）。"
        "港股/美股/新三板/未上市公司不在覆盖范围内。"
        "需要历史多期三表时这个快照不够，走 analyze 阶段的三表抽取链路。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name_or_code": {"type": "string", "minLength": 2, "maxLength": 64,
                             "description": "公司简称或 6 位证券代码，例如 '贵州茅台' 或 '600519'"},
        },
        "required": ["name_or_code"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"success": {"type": "boolean"}},
        "required": ["success"],
    },
    returns_summary_of="结构化财务字段字典 + 字段级 lineage；失败时 success=False 且不编造数值",
    cost_hint=CostHint.EXPENSIVE,
    timeout_s=90.0,
    max_attempts=2,
)

EXPORT_REPORT_SPEC = ToolSpec(
    name="export_report_file",
    namespace="output",
    category=ToolCategory.FILE_EXPORT,
    description="把已生成的报告写入本地文件（Markdown/HTML）。",
    when_to_use="报告内容已定稿、需要落盘为最终产物时。",
    when_not_to_use="草稿阶段不要导出；同一 run 不要导出两次（非幂等写入会产生重复文件）。",
    input_schema={
        "type": "object",
        "properties": {
            "content": {"type": "string", "minLength": 1},
            "path": {"type": "string", "minLength": 1, "maxLength": 500},
            "overwrite": {"type": "boolean", "default": False},
        },
        "required": ["content", "path"],
        "additionalProperties": False,
    },
    output_schema={
        "type": "object",
        "properties": {"success": {"type": "boolean"}, "path": {"type": "string"}},
        "required": ["success"],
    },
    returns_summary_of="写入的文件路径与字节数",
    # The only registered tool with a side effect. It is a *local* write, and
    # it is here because the export step genuinely exists in this pipeline -
    # not to demonstrate the approval machinery on a fabricated write tool.
    side_effect_free=False,
    approval_required=False,
    idempotent=False,
    compensation_action="delete_exported_file",
    cost_hint=CostHint.FREE,
    timeout_s=10.0,
    max_attempts=1,
)


# --------------------------------------------------------------------------- #
# Handlers (thin wrappers over the existing gateway)
# --------------------------------------------------------------------------- #
def _handler_web_search(query: str, max_results: int = 8) -> Any:
    from tools.tool_gateway import _raw_web_search

    return _raw_web_search(query, max_results=max_results)


def _handler_read_webpage(url: str, max_chars: int = 8000) -> Any:
    from tools.tool_gateway import _raw_read_webpage

    result = _raw_read_webpage(url, max_chars=max_chars)
    if isinstance(result, dict):
        result.setdefault("url", url)
    return result


def _handler_read_pdf(url: str) -> Any:
    from tools.tool_gateway import _raw_read_pdf

    result = _raw_read_pdf(url)
    if isinstance(result, dict):
        result.setdefault("url", url)
    return result


def _handler_fetch_financial_snapshot(name_or_code: str) -> Any:
    from tools.tool_gateway import _raw_fetch_financial_snapshot

    return _raw_fetch_financial_snapshot(name_or_code)


def _handler_export_report_file(content: str, path: str, overwrite: bool = False) -> Any:
    from pathlib import Path

    target = Path(path)
    if target.exists() and not overwrite:
        return {"success": False, "path": str(target), "error": "file exists and overwrite=False"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"success": True, "path": str(target), "bytes": len(content.encode("utf-8"))}


# --------------------------------------------------------------------------- #
# Default registry
# --------------------------------------------------------------------------- #
def build_default_registry(*, include_export: bool = True) -> ToolRegistry:
    """The five agent-selectable tools, bound to the existing implementations.

    Handlers call the concrete tool modules directly rather than
    `tools.tool_gateway`, because the gateway is where the harness hook lives -
    routing back through it would recurse.
    """
    registry = ToolRegistry()
    registry.register(SEARCH_SPEC, _handler_web_search, projector=_project_search)
    registry.register(READ_WEBPAGE_SPEC, _handler_read_webpage, projector=_project_page)
    registry.register(READ_PDF_SPEC, _handler_read_pdf, projector=_project_page)
    registry.register(FINANCIAL_SNAPSHOT_SPEC, _handler_fetch_financial_snapshot,
                      projector=_project_snapshot)
    if include_export:
        registry.register(EXPORT_REPORT_SPEC, _handler_export_report_file)
    return registry


def register_internal_step(registry: ToolRegistry, name: str, description: str,
                           handler: Callable[..., Any], *, namespace: str = "analysis") -> ToolSpec:
    """Register a deterministic analysis function.

    These are traced and budgeted like tools but are excluded from `catalog()`:
    the agent does not choose them, the report_type does.
    """
    spec = ToolSpec(
        name=name, namespace=namespace, category=ToolCategory.INTERNAL_STEP,
        description=description,
        when_to_use="由 report_type 决定的确定性分析步骤，非 Agent 自主选择。",
        when_not_to_use="不参与工具选择决策，不出现在工具目录中。",
        input_schema={"type": "object"}, cost_hint=CostHint.FREE, timeout_s=60.0, max_attempts=1,
    )
    registry.register(spec, handler)
    return spec


#: Process-wide default, built lazily so importing this module has no side effects.
_default_registry: Optional[ToolRegistry] = None
_default_lock = threading.Lock()


def default_registry() -> ToolRegistry:
    global _default_registry
    with _default_lock:
        if _default_registry is None:
            _default_registry = build_default_registry()
        return _default_registry
