"""Text cleaning and lightweight NLP helpers (regex-based, no heavy deps)."""
import re
from typing import Any

_WHITESPACE_RE = re.compile(r"[ \t　]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？.!?\n])")
_NUMBER_RE = re.compile(r"\d")
_PERCENT_RE = re.compile(r"\d+(\.\d+)?\s*%")

# Ordered by nothing in particular - normalize_topic always strips the
# *longest* matching suffix first so e.g. "投资分析" is removed as one
# unit instead of leaving a dangling "投资" behind.
_TOPIC_NOISE_SUFFIXES = [
    "投资价值分析", "投资分析", "深度研究报告", "深度分析", "研究报告",
    "行业研究", "行业分析", "分析报告", "价值分析", "研报", "分析", "研究", "报告",
]

# Shared keyword list used to pick out the financially-informative sentences
# from a source's raw content when building compressed LLM prompt context
# (AnalyzeAgent/ReportAgent) - avoids sending full page text through the LLM.
FINANCIAL_CONTEXT_KEYWORDS = [
    "营收", "收入", "净利润", "毛利率", "现金流", "同比", "环比", "亿元", "%",
    "估值", "PE", "PB", "市盈率", "市净率", "风险", "竞争", "市场份额",
    "增长", "下滑", "价格战", "政策", "出口", "海外", "行业",
]

# Company name -> known aliases (ticker/English name/nickname). Used so
# QualityScorer doesn't require the exact normalized subject string to appear
# verbatim - a source that only says "CATL" or "300750" is still clearly
# about 宁德时代. Extend as needed; a subject with no entry here just falls
# back to matching on the subject string alone.
COMPANY_ALIASES: dict[str, list[str]] = {
    "宁德时代": ["宁德时代", "CATL", "300750"],
    "比亚迪": ["比亚迪", "BYD", "002594"],
    "贵州茅台": ["贵州茅台", "茅台", "600519", "茅台酒"],
    "中芯国际": ["中芯国际", "SMIC", "688981", "0981.HK", "晶圆代工", "半导体制造"],
    "隆基绿能": ["隆基绿能", "隆基", "601012", "光伏组件", "硅片"],
}

# Industry topic -> synonym/related terms. Industry-research topics can't
# reasonably require the exact subject phrase to appear (a 光伏 article might
# only ever say "组件"/"硅片"), so QualityScorer matches against this set
# instead when report_type == "industry_research".
INDUSTRY_SYNONYMS: dict[str, list[str]] = {
    "AI机器人": ["AI机器人", "人工智能机器人", "智能机器人", "人形机器人", "机器人行业", "具身智能"],
    "半导体国产替代": ["半导体", "芯片", "集成电路", "国产替代", "半导体设备", "半导体材料"],
    "光伏": ["光伏", "光伏行业", "太阳能", "组件", "硅片", "电池片", "逆变器"],
    "低空经济": ["低空经济", "eVTOL", "无人机", "通航", "低空飞行", "低空产业"],
    "新能源汽车": ["新能源汽车", "新能源车", "电动车", "动力电池", "智能汽车", "汽车产业链"],
}


def _dedup_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def clean_text(text: str) -> str:
    """Collapse repeated whitespace/blank lines and strip leading/trailing space."""
    if not text:
        return ""
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def split_sentences(text: str) -> list[str]:
    """Split Chinese/English mixed text into sentence-like chunks."""
    if not text:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def contains_any(text: str, keywords: list[str]) -> bool:
    """Return True if any keyword appears in text."""
    return any(kw in text for kw in keywords)


def count_keyword_hits(text: str, keywords: list[str]) -> int:
    """Count total occurrences of any keyword in text (sum across all keywords)."""
    return sum(text.count(kw) for kw in keywords)


def has_number_or_percent(sentence: str) -> bool:
    """True if the sentence contains a digit (covers percentages and 亿元 amounts)."""
    return bool(_NUMBER_RE.search(sentence))


def truncate(text: str, max_chars: int) -> str:
    """Truncate text to max_chars, appending an ellipsis marker if cut."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def normalize_topic(topic: str) -> str:
    """Strip report-type noise suffixes to extract the core entity/subject.

    e.g. "宁德时代投资分析" -> "宁德时代", "新能源汽车行业研究报告" -> "新能源汽车行业".
    Search queries should use this instead of the raw topic: the raw phrasing
    the user typed is meant for humans (report titles), not search engines -
    padding every query with "投资分析"/"研究报告" narrows results for no benefit
    and often drives hit counts to zero. Falls back to the original topic if
    stripping would leave nothing.
    """
    cleaned = clean_text(topic)
    while True:
        matches = [s for s in _TOPIC_NOISE_SUFFIXES if cleaned.endswith(s) and len(cleaned) > len(s)]
        if not matches:
            break
        cleaned = cleaned[: -len(max(matches, key=len))].strip()
    return cleaned or topic


def extract_relevant_excerpt(
    content: str,
    keywords: list[str] = FINANCIAL_CONTEXT_KEYWORDS,
    max_chars: int = 1800,
    min_chars: int = 200,
) -> str:
    """Pull out keyword-bearing sentences instead of truncating raw content blindly.

    Keeps only sentences that mention a financial-context keyword (numbers,
    percentages, risk/valuation terms, ...), which is what an LLM doing
    financial analysis actually needs - not the surrounding article prose,
    ads, or navigation boilerplate. If too little survives that filter
    (`min_chars`), pads with the raw content head so short/atypical sources
    still contribute something. Result is capped at `max_chars`.
    """
    if not content:
        return ""
    sentences = split_sentences(content)
    hits = [s for s in sentences if any(kw in s for kw in keywords)]
    text = "".join(hits)
    if len(text) < min_chars:
        text = (text + " " + content[:min_chars]).strip()
    return truncate(text, max_chars)


def build_relevance_profile(topic: str, report_type: str = "company_research") -> dict[str, Any]:
    """Expand a raw topic into subject/intent/aliases/industry_terms/relevance_terms.

    normalize_topic() alone only strips noise suffixes - it doesn't know that
    "中芯国际投资风险分析" is *about* SMIC/688981, or that a "光伏" industry
    report will mostly talk about "组件"/"硅片" rather than the word "光伏"
    itself. This builds the richer profile QualityScorer needs to stop
    treating those as irrelevant.

    Matching against COMPANY_ALIASES/INDUSTRY_SYNONYMS uses substring
    containment (not exact dict-key equality) in both directions, because
    normalize_topic's generic suffix-stripping often leaves extra words
    attached (e.g. "贵州茅台财务与估值" for "贵州茅台财务与估值分析") that
    would fail an exact-match lookup but still clearly contain the subject.
    """
    subject = normalize_topic(topic)
    cleaned = clean_text(topic)
    intent = cleaned[len(subject):].strip() if cleaned.startswith(subject) else ""

    aliases: list[str] = []
    for company, alias_list in COMPANY_ALIASES.items():
        if company in subject or subject in company or any(a and a in subject for a in alias_list):
            aliases = list(alias_list)
            break

    industry_terms: list[str] = []
    for key, terms in INDUSTRY_SYNONYMS.items():
        if key in subject or subject in key or any(t and t in subject for t in terms):
            industry_terms = list(terms)
            break

    relevance_terms = _dedup_keep_order([subject] + aliases + industry_terms)

    return {
        "subject": subject,
        "intent": intent,
        "aliases": aliases,
        "industry_terms": industry_terms,
        "relevance_terms": relevance_terms,
        "report_type": report_type,
    }
