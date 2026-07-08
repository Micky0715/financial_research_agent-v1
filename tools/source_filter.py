"""Lightweight pre-fetch ranking of search-result candidates.

Runs after domain/relevance pre-filtering and before BrowserAgent spends a
network round-trip on each candidate: cheaply scores each candidate using
only title/snippet/url (no fetch), so the read-limited/early-stopping browse
loop spends its budget on the most promising candidates first instead of
whatever order the search engine happened to return.
"""
import re

from tools.domain_rules import authority_tier, is_blacklisted, is_whitelisted

_FINANCIAL_SIGNAL_KEYWORDS = ["年报", "公告", "财务", "营收", "净利润", "估值", "风险", "研报"]
_LOW_QUALITY_HINTS = ["广告", "推广", "加群", "点击进入", "扫码关注"]

_TIER_SCORE = {1: 5.0, 2: 3.0, 0: 0.0}


def _text_hit_score(text: str, terms: list[str], per_hit: float, cap: float) -> float:
    if not text or not terms:
        return 0.0
    hits = sum(1 for t in terms if t and t in text)
    return min(cap, hits * per_hit)


def rank_search_results(results: list[dict], subject: str, aliases: list[str] | None = None) -> list[dict]:
    """Score and sort candidate search hits by (authority tier, subject/keyword hits).

    Blacklisted domains are dropped outright (BrowserAgent's own pre-filter
    already does this too, but ranking should never resurrect one). Returns a
    new list of candidate dicts, each with a `rank_score` key attached, sorted
    descending by score - highest-value candidates first.
    """
    aliases = [a for a in (aliases or []) if a]
    ranked: list[dict] = []

    for item in results:
        url = item.get("url", "")
        if is_blacklisted(url):
            continue

        title = item.get("title", "")
        snippet = item.get("snippet", "")
        haystack = f"{title} {snippet} {url}"

        score = _TIER_SCORE.get(authority_tier(url, subject), 0.0)
        if score == 0.0 and is_whitelisted(url):
            score = 2.0  # whitelisted but not tier1/tier2 (shouldn't normally happen, kept as a floor)

        score += _text_hit_score(haystack, [subject], per_hit=2.0, cap=4.0)
        score += _text_hit_score(haystack, aliases, per_hit=1.0, cap=2.0)
        score += _text_hit_score(f"{title} {snippet}", _FINANCIAL_SIGNAL_KEYWORDS, per_hit=0.5, cap=2.5)

        if any(hint in haystack for hint in _LOW_QUALITY_HINTS):
            score -= 3.0

        entry = dict(item)
        entry["rank_score"] = round(score, 3)
        ranked.append(entry)

    ranked.sort(key=lambda e: e["rank_score"], reverse=True)
    return ranked
