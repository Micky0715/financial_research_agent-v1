"""Rule-based extraction of financial-statement style sentences from sources."""
from schemas.source import Source
from utils.text_utils import split_sentences

_FINANCIAL_KEYWORDS = [
    "营收", "收入", "净利润", "毛利率", "现金流", "资产负债率", "同比", "环比", "亿元", "%",
]

_GROWTH_HINTS = ["增长", "提升", "上涨", "扩大", "增加", "同比增"]
_PRESSURE_HINTS = ["下降", "下滑", "亏损", "承压", "收窄", "减少", "同比降"]


def extract_financial_sentences(content: str) -> list[str]:
    """Return sentences from content that mention financial-statement keywords."""
    if not content:
        return []
    sentences = split_sentences(content)
    return [s for s in sentences if any(kw in s for kw in _FINANCIAL_KEYWORDS)]


def summarize_financial_points(sources: list[Source]) -> dict:
    """Aggregate financial sentences across sources into growth/pressure signals.

    Each retained point is tagged with its source_id so the report can cite it.
    """
    key_points = []
    growth_signals = []
    pressure_signals = []
    seen_sentences: set[str] = set()

    for src in sources:
        sentences = extract_financial_sentences(src.content)
        for sentence in sentences[:6]:
            if sentence in seen_sentences:
                continue
            seen_sentences.add(sentence)
            entry = {"text": sentence, "source_id": src.source_id}
            key_points.append(entry)
            if any(h in sentence for h in _GROWTH_HINTS):
                growth_signals.append(entry)
            if any(h in sentence for h in _PRESSURE_HINTS):
                pressure_signals.append(entry)

    return {
        "key_financial_points": key_points[:20],
        "possible_growth_signals": growth_signals[:10],
        "possible_pressure_signals": pressure_signals[:10],
    }
