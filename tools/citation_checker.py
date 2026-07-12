"""Citation validity checking (v3 stage D).

从报告文本中抽取 [sX] 形式的引用，校验每个引用的 source_id 是否真实存在于本次
运行的来源列表里——LLM 有时会引用不存在的编号（幻觉引用），这类引用在数字级
grounding 里不能算数。
"""
import re
from typing import Any

CITATION_RE = re.compile(r"\[((?:source_)?s\d+)\]")


def extract_citations(text: str) -> list[str]:
    """报告文本 -> 归一化的 source_id 列表（source_s3 -> s3，保留出现顺序含重复）。"""
    out = []
    for m in CITATION_RE.finditer(text or ""):
        sid = m.group(1)
        out.append(sid[len("source_"):] if sid.startswith("source_") else sid)
    return out


def check_citations(text: str, valid_source_ids: list[str]) -> dict[str, Any]:
    """Returns {citations, valid_citations, invalid_citations, invalid_citation_count}."""
    valid_set = set(valid_source_ids)
    citations = extract_citations(text)
    invalid = sorted({c for c in citations if c not in valid_set})
    return {
        "citations": citations,
        "valid_citations": sorted({c for c in citations if c in valid_set}),
        "invalid_citations": invalid,
        "invalid_citation_count": len(invalid),
    }
