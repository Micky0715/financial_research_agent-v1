"""Rule-based detection and structuring of valuation-related text signals."""
from utils.text_utils import split_sentences

_VALUATION_CATEGORIES = {
    "PE": ["PE", "市盈率"],
    "PB": ["PB", "市净率"],
    "PS": ["PS", "市销率"],
    "DCF": ["DCF", "现金流折现", "折现现金流"],
    "comparable_company": ["可比公司", "同业对比", "同行业对比"],
    "cash_flow": ["自由现金流", "经营性现金流", "现金流"],
}


def detect_valuation_signals(text: str) -> dict:
    """Extract sentences mentioning each valuation method/category.

    Returns {category: [sentences...]} - purely structural extraction, no
    computed valuation numbers are invented.
    """
    result = {cat: [] for cat in _VALUATION_CATEGORIES}
    if not text:
        return result

    for sentence in split_sentences(text):
        for cat, keywords in _VALUATION_CATEGORIES.items():
            if any(kw in sentence for kw in keywords):
                result[cat].append(sentence)

    return result
