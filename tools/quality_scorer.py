"""Heuristic source quality scoring: relevance, financial density, authority, etc."""
from tools.domain_rules import is_whitelisted
from utils.text_utils import count_keyword_hits, split_sentences

_FINANCIAL_KEYWORDS = [
    "营收", "收入", "净利润", "毛利率", "现金流", "资产负债率", "同比", "环比",
    "亿元", "万元", "增长率", "利润率", "市盈率", "市净率", "估值", "PE", "PB",
]

# Broader financial/industry-report signal words used by the new relevance
# scoring (see _topic_relevance) - deliberately overlaps _FINANCIAL_KEYWORDS
# but also covers industry-report vocabulary (市场规模/产业链/竞争格局/政策)
# that a company-specific keyword list wouldn't.
_FINANCIAL_SIGNAL_KEYWORDS = [
    "研报", "年报", "公告", "财务", "营收", "收入", "净利润", "毛利率", "估值",
    "风险", "行业", "市场规模", "产业链", "竞争格局", "政策", "增长", "同比", "亿元", "%",
]

_AUTHORITY_HIGH = [
    "交易所", "上交所", "深交所", "证监会", "年报", "公告", "招股说明书", "官网",
    "证券时报", "东方财富", "同花顺", "财联社", "证券研究报告", "研报", "国家统计局",
    "发改委", "工信部", "中国证券业协会",
]
_AUTHORITY_MID = ["新浪财经", "腾讯财经", "网易财经", "第一财经", "界面新闻", "澎湃新闻"]

_LOW_QUALITY_HINTS = ["广告", "推广", "加群", "点击进入", "扫码关注"]

_NEAR_ZERO_CAP = 0.05


def _topic_relevance(
    content: str,
    title: str,
    snippet: str,
    url: str,
    subject: str,
    aliases: list[str],
    industry_terms: list[str],
    relevance_terms: list[str],
    report_type: str,
) -> tuple[float, dict]:
    """Score topic relevance 0-1 against subject/aliases/industry_terms, not just
    a literal exact-subject-substring count.

    The old version split the raw topic string on whitespace and counted hits
    of the *whole* (often multi-word, suffix-laden) phrase - for CJK topics
    with no whitespace that degenerates to counting one giant compound phrase
    that real articles never repeat verbatim, so genuinely on-topic sources
    scored ~zero and got dropped (see eval/bad_cases.md). This version checks
    subject / alias / industry-synonym membership independently, so "CATL"
    or "硅片" can carry a source even when the exact subject string doesn't
    appear.
    """
    haystack = f"{title}\n{snippet}\n{content}"
    if not haystack.strip():
        return 0.0, {
            "matched_terms": [],
            "matched_aliases": [],
            "matched_industry_terms": [],
            "relevance_reason": "empty_content",
        }

    matched_terms = [t for t in relevance_terms if t and t in haystack]
    matched_aliases = [a for a in aliases if a and a in haystack]
    matched_industry_terms = [i for i in industry_terms if i and i in haystack]

    score = 0.0
    reasons: list[str] = []

    if subject and subject in haystack:
        score = max(score, 0.6)
        reasons.append("subject_match")

    if matched_aliases:
        score = max(score, 0.5)
        reasons.append("alias_match")

    if report_type == "industry_research" and matched_industry_terms:
        score = max(score, 0.45)
        reasons.append("industry_term_match")

    financial_hits = count_keyword_hits(haystack, _FINANCIAL_SIGNAL_KEYWORDS)
    if financial_hits:
        score += min(0.25, financial_hits * 0.03)
        reasons.append(f"financial_keyword_hits={financial_hits}")

    if is_whitelisted(url) and matched_terms:
        score = max(score, 0.55)
        reasons.append("authority_domain_with_term_match")

    if not matched_terms and financial_hits < 2:
        # Only allow a near-zero score when NOTHING on-topic matched at all
        # (no subject/alias/industry-term hit) AND financial vocabulary is
        # sparse too - a source with real financial substance shouldn't be
        # nuked to zero just because it never repeats the exact subject name.
        score = min(score, _NEAR_ZERO_CAP)
        reasons.append("no_relevance_signal")

    score = round(min(1.0, score), 3)
    details = {
        "matched_terms": matched_terms,
        "matched_aliases": matched_aliases,
        "matched_industry_terms": matched_industry_terms,
        "relevance_reason": "; ".join(reasons) if reasons else "no_match",
    }
    return score, details


def _financial_density(content: str) -> float:
    """Score density of financial-domain keywords in the content."""
    if not content:
        return 0.0
    hits = count_keyword_hits(content, _FINANCIAL_KEYWORDS)
    sentences = max(len(split_sentences(content)), 1)
    ratio = hits / sentences
    return min(1.0, ratio * 2)


def _content_length_score(content: str) -> float:
    """Score based on content length: too short is low-signal, cap the benefit."""
    length = len(content or "")
    if length < 200:
        return length / 200 * 0.4
    if length >= 3000:
        return 1.0
    return 0.4 + (length - 200) / (3000 - 200) * 0.6


def _readability_score(content: str) -> float:
    """Penalize obvious ad/spam boilerplate; reward normal sentence structure."""
    if not content:
        return 0.0
    penalty = 0.15 * count_keyword_hits(content, _LOW_QUALITY_HINTS)
    sentences = split_sentences(content)
    avg_len = sum(len(s) for s in sentences) / max(len(sentences), 1)
    base = 0.8 if 10 <= avg_len <= 120 else 0.5
    return max(0.0, min(1.0, base - penalty))


def _authority_hint(url: str, title: str) -> float:
    """Score source authority based on domain/title hints."""
    if is_whitelisted(url):
        return 1.0
    text = f"{url} {title}".lower()
    for kw in _AUTHORITY_HIGH:
        if kw.lower() in text:
            return 1.0
    for kw in _AUTHORITY_MID:
        if kw.lower() in text:
            return 0.6
    return 0.3


def score_source(
    content: str,
    subject: str = "",
    title: str = "",
    snippet: str = "",
    url: str = "",
    aliases: list[str] | None = None,
    industry_terms: list[str] | None = None,
    relevance_terms: list[str] | None = None,
    report_type: str = "company_research",
    topic: str = "",
) -> dict:
    """Score a candidate source 0-1 across several heuristics and return an aggregate.

    `subject` (preferred) is the normalized entity/topic. `topic` is kept as a
    backward-compatible alias for callers that haven't been updated to pass
    the richer relevance profile (see utils.text_utils.build_relevance_profile) -
    when only `topic` is given, relevance_terms falls back to just [subject].

    Returns {"score": float, "details": {dim: value, ..., matched_terms: [...],
    matched_aliases: [...], matched_industry_terms: [...], relevance_reason: str}}.
    """
    subject = subject or topic
    aliases = aliases or []
    industry_terms = industry_terms or []
    relevance_terms = relevance_terms if relevance_terms is not None else [subject] if subject else []

    full_text = content or snippet or ""

    topic_relevance, relevance_details = _topic_relevance(
        full_text, title, snippet, url, subject, aliases, industry_terms, relevance_terms, report_type
    )

    details = {
        "topic_relevance": topic_relevance,
        "financial_density": round(_financial_density(full_text), 3),
        "content_length": round(_content_length_score(full_text), 3),
        "source_readability": round(_readability_score(full_text), 3),
        "authority_hint": round(_authority_hint(url, title), 3),
        **relevance_details,
    }

    weights = {
        "topic_relevance": 0.30,
        "financial_density": 0.25,
        "content_length": 0.15,
        "source_readability": 0.10,
        "authority_hint": 0.20,
    }
    score = sum(details[k] * w for k, w in weights.items())
    return {"score": round(min(1.0, score), 3), "details": details}
