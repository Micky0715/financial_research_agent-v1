"""Local file ingestion as report sources (v3 stage G).

支持 PDF / TXT / Markdown / CSV / Excel。读取结果转换为标准 Source：
- source_id: s901 起（与网页来源的 s1..s2x 区分且兼容 [sX] 引用格式）
- source_type: "local_file"，authority_tier: "local_user_file"
- metadata: filename / page_count / sheet_names / extracted_tables /
  extracted_financials（尽量抽取年份-营收/净利润，抽不到就空，不编造）

任何单个文件读取失败只跳过该文件并记录 warning，不影响主流程。
"""
from pathlib import Path
from typing import Any, Optional

from schemas.source import Source
from utils.logger import logger

_LOCAL_ID_BASE = 900
_MAX_CONTENT_CHARS = 12000


def _read_pdf_file(path: Path) -> dict[str, Any]:
    import fitz  # pymupdf, existing dependency

    with fitz.open(str(path)) as doc:
        pages = [page.get_text() for page in doc]
    return {"content": "\n".join(pages), "metadata": {"page_count": len(pages)}}


def _read_text_file(path: Path) -> dict[str, Any]:
    return {"content": path.read_text(encoding="utf-8", errors="ignore"), "metadata": {}}


def _frame_to_text(df, name: str = "") -> str:
    head = df.head(50)
    label = f"[{name}] " if name else ""
    return f"{label}columns: {list(df.columns)}\n{head.to_string()}"


def _read_csv_file(path: Path) -> dict[str, Any]:
    import pandas as pd

    df = pd.read_csv(path)
    return {
        "content": _frame_to_text(df),
        "metadata": {"extracted_tables": [{"rows": len(df), "columns": list(map(str, df.columns))}]},
    }


def _read_excel_file(path: Path) -> dict[str, Any]:
    import pandas as pd

    sheets = pd.read_excel(path, sheet_name=None)
    parts, tables = [], []
    for name, df in list(sheets.items())[:5]:
        parts.append(_frame_to_text(df, name))
        tables.append({"sheet_name": name, "rows": len(df), "columns": list(map(str, df.columns))})
    return {"content": "\n\n".join(parts), "metadata": {"sheet_names": list(sheets.keys()), "extracted_tables": tables}}


_READERS = {
    ".pdf": _read_pdf_file,
    ".txt": _read_text_file,
    ".md": _read_text_file,
    ".csv": _read_csv_file,
    ".xlsx": _read_excel_file,
    ".xls": _read_excel_file,
}


def _try_extract_financials(content: str, subject: str) -> dict[str, Any]:
    """尽力从本地文件文本中抽取年度财务序列（复用图表抽取器的守卫逻辑）。失败返回空。"""
    try:
        from tools.chart_renderer import extract_metric_series

        probe = Source(source_id="local_probe", content=content)
        out = {}
        for metric in ("营收", "净利润"):
            series = extract_metric_series([probe], metric, subject)
            if series:
                out[metric] = series
        return out
    except Exception:  # noqa: BLE001 - extraction is best-effort
        return {}


def read_local_file(path: str, source_id: str, subject: str = "") -> Optional[Source]:
    """单个本地文件 -> Source。不支持的类型/读取失败返回 None（调用方跳过）。"""
    p = Path(path)
    if not p.exists():
        logger.warning(f"local file not found, skipped: {path}")
        return None
    reader = _READERS.get(p.suffix.lower())
    if reader is None:
        logger.warning(f"unsupported local file type ({p.suffix}), skipped: {path}")
        return None
    try:
        parsed = reader(p)
    except Exception as exc:  # noqa: BLE001 - one bad file must not break the batch
        logger.warning(f"local file read failed, skipped: {path}: {exc}")
        return None

    content = (parsed["content"] or "")[:_MAX_CONTENT_CHARS]
    if not content.strip():
        logger.warning(f"local file has no extractable text, skipped: {path}")
        return None

    metadata = {"filename": p.name, **parsed["metadata"]}
    financials = _try_extract_financials(content, subject)
    if financials:
        metadata["extracted_financials"] = financials

    return Source(
        source_id=source_id,
        title=f"本地文件：{p.name}",
        url=f"file:///{p.resolve().as_posix()}",
        snippet=content[:150],
        content=content,
        source_type="local_file",
        score=1.0,  # 用户提供的资料默认保留，不参与相关性竞争
        quality_details={"topic_relevance": 1.0, "note": "user-provided local file"},
        metadata=metadata,
        authority_tier="local_user_file",
        authority_reason="用户本地上传文件，可信度由使用者自行判断",
        domain="",
        is_official_source=False,
        is_financial_media=False,
        is_user_generated_content=False,
    )


def build_local_sources(paths: list[str], subject: str = "") -> list[Source]:
    """一组本地文件路径 -> Source 列表（source_id 从 s901 递增）。"""
    sources = []
    for i, path in enumerate(paths or []):
        src = read_local_file(path, f"s{_LOCAL_ID_BASE + i + 1}", subject)
        if src is not None:
            sources.append(src)
    if paths:
        logger.info(f"local files: {len(sources)}/{len(paths)} readable, added as sources")
    return sources
