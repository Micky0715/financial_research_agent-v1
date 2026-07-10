"""Lightweight pre-fetch ranking of search-result candidates.

Runs after domain/relevance pre-filtering and before BrowserAgent spends a
network round-trip on each candidate: cheaply scores each candidate using
only title/snippet/url (no fetch), so the read-limited/early-stopping browse
loop spends its budget on the most promising candidates first instead of
whatever order the search engine happened to return.

v2 模块2：在规则打分之上叠加一层本地 embedding 语义相似度（加权融合，不替换
规则分）。每个候选的结果里同时保留 rule_score / semantic_score / rank_score
三个字段——规则分的可解释性不丢，语义分单独可见，最终排序用融合分。
语义模型不可用时自动退回纯规则排序（semantic_score=None，权重记 0）。
"""
import re

from config import config
from tools.domain_rules import authority_tier, is_blacklisted, is_whitelisted
from utils.logger import logger

_FINANCIAL_SIGNAL_KEYWORDS = ["年报", "公告", "财务", "营收", "净利润", "估值", "风险", "研报"]
_LOW_QUALITY_HINTS = ["广告", "推广", "加群", "点击进入", "扫码关注"]

_TIER_SCORE = {1: 5.0, 2: 3.0, 0: 0.0}

# Rule scores land in roughly [−3, 10.5]; normalize by this bound before
# fusing with the [0,1] semantic similarity so the configured weight means
# what it says.
_RULE_SCORE_SCALE = 10.0


def _text_hit_score(text: str, terms: list[str], per_hit: float, cap: float) -> float:
    if not text or not terms:
        return 0.0
    hits = sum(1 for t in terms if t and t in text)
    return min(cap, hits * per_hit)


def _rule_score(item: dict, subject: str, aliases: list[str]) -> float:
    """The original v1 keyword/authority rule score (unchanged logic)."""
    url = item.get("url", "")
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

    return score


def rank_search_results(
    results: list[dict],
    subject: str,
    aliases: list[str] | None = None,
    query_text: str | None = None,
) -> list[dict]:
    """Score and sort candidate search hits: rule score fused with semantic similarity.

    Blacklisted domains are dropped outright (BrowserAgent's own pre-filter
    already does this too, but ranking should never resurrect one). Returns a
    new list of candidate dicts sorted descending by `rank_score`, where each
    entry carries the full scoring breakdown:

    - rule_score:      raw v1 keyword/authority score (explainable, unchanged)
    - semantic_score:  embedding cosine similarity vs `query_text` in [0,1],
                       or None when the semantic layer is off/unavailable
    - rank_score:      (1-w)*normalized_rule + w*semantic, w = SEMANTIC_WEIGHT
                       (equals normalized rule score when semantic is off)

    `query_text` should be the user's full topic (e.g. "比亚迪投资价值分析") -
    it carries the research intent better than the bare subject; defaults to
    `subject` when not given.
    """
    aliases = [a for a in (aliases or []) if a]
    kept = [item for item in results if not is_blacklisted(item.get("url", ""))]

    rule_scores = [_rule_score(item, subject, aliases) for item in kept]

    semantic = None
    if config.ENABLE_SEMANTIC_RANKING and kept:
        from tools.semantic_scorer import semantic_scores

        texts = [f"{item.get('title', '')} {item.get('snippet', '')}".strip() for item in kept]
        semantic = semantic_scores(query_text or subject, texts)
        if semantic is None:
            logger.warning("semantic ranking unavailable for this batch - using rule scores only")

    weight = config.SEMANTIC_WEIGHT if semantic is not None else 0.0

    ranked: list[dict] = []
    for i, item in enumerate(kept):
        rule_norm = max(0.0, min(1.0, rule_scores[i] / _RULE_SCORE_SCALE))
        sem = semantic[i] if semantic is not None else None
        final = (1 - weight) * rule_norm + weight * (sem or 0.0)

        entry = dict(item)
        entry["rule_score"] = round(rule_scores[i], 3)
        entry["semantic_score"] = round(sem, 3) if sem is not None else None
        entry["rank_score"] = round(final, 4)
        ranked.append(entry)

    ranked.sort(key=lambda e: e["rank_score"], reverse=True)
    return ranked
