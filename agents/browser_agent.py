"""Browser agent: fetches page/PDF content, scores sources, keeps the top-K."""
from typing import Any

from config import config
from schemas.request import ResearchRequest
from schemas.source import Source
from schemas.task import Task
from tools.domain_rules import get_domain, is_blacklisted
from tools.quality_scorer import score_source
from tools.source_filter import rank_search_results
from tools.tool_gateway import read_pdf, read_webpage  # same signatures; route via MCP when enabled
from utils.logger import logger
from utils.text_utils import build_relevance_profile, truncate

from .base_agent import BaseAgent

_MAX_CONTENT_CHARS = 8000
_MIN_TOPIC_RELEVANCE = 0.05
_RELAXED_MIN_USABLE = 5


class NoUsableSourcesError(RuntimeError):
    """Raised when zero sources survive the whole browse pipeline (including
    the relaxed-relevance fallback).

    Carries `partial_result` so BaseAgent.run can still surface browser_metrics
    diagnostics into the trace even though the task is ultimately marked failed
    (see agents/base_agent.py's use of getattr(exc, "partial_result", None)).
    `non_retryable` tells BaseAgent not to burn retries re-fetching the exact
    same URL batch when the problem was relevance, not a transient fetch error.
    """

    def __init__(self, message: str, browser_metrics: dict[str, Any], non_retryable: bool = False):
        super().__init__(message)
        self.partial_result = {"sources": [], "all_scored_count": 0, "browser_metrics": browser_metrics}
        self.non_retryable = non_retryable


class BrowserAgent(BaseAgent):
    """Reads each candidate URL (web or PDF), scores it, and returns the top-K sources."""

    name = "browser_agent"
    description = "读取网页/PDF正文，做质量评分并筛选Top-K来源"
    tools = ["read_webpage", "read_pdf", "score_source"]

    @staticmethod
    def _is_pdf(url: str) -> bool:
        """Heuristic: treat URLs ending in .pdf (ignoring query string) as PDFs."""
        return url.lower().split("?")[0].endswith(".pdf")

    @staticmethod
    def _looks_relevant(candidate: dict, relevance_terms: list[str]) -> bool:
        """Cheap pre-fetch relevance check using only title+snippet (no network I/O).

        Checks against the full relevance profile (subject + aliases +
        industry synonyms), not just the bare subject string, so e.g. a
        candidate that only mentions "CATL" or "硅片" still passes. Used to
        skip obviously-irrelevant candidates before paying for a full fetch.
        Deliberately lenient: with no snippet text to judge we keep the
        candidate and let the post-fetch topic_relevance scorer decide.
        """
        text = f"{candidate.get('title', '')} {candidate.get('snippet', '')}".strip()
        if not text:
            return True
        return any(term and term in text for term in relevance_terms)

    def _pre_filter(self, candidates: list[dict], relevance_terms: list[str]) -> tuple[list[dict], dict[str, int]]:
        """Drop blacklisted domains and obviously-irrelevant candidates before fetching."""
        allowed = [c for c in candidates if not is_blacklisted(c.get("url", ""))]
        blacklisted_drop_count = len(candidates) - len(allowed)
        if blacklisted_drop_count:
            logger.warning(f"pre-filter dropped {blacklisted_drop_count} blacklisted-domain candidate(s)")

        relevant = [c for c in allowed if self._looks_relevant(c, relevance_terms)]
        relevance_drop_count = 0
        if relevant:
            relevance_drop_count = len(allowed) - len(relevant)
            if relevance_drop_count:
                logger.info(f"pre-filter relevance check dropped {relevance_drop_count} candidate(s)")
            filtered = relevant
        else:
            # Relevance check killed everything (e.g. entity phrasing doesn't
            # match snippet wording) - fall back to blacklist-only filtering
            # rather than starving the pipeline over a cheap heuristic's false
            # negative.
            filtered = allowed

        return filtered, {
            "blacklisted_domain_drop_count": blacklisted_drop_count,
            "relevance_drop_count": relevance_drop_count,
        }

    def _fetch_one(self, candidate: dict, profile: dict[str, Any], idx: int) -> Source | None:
        """Fetch and score a single candidate. Returns None if content is unusable.

        Any exception here is swallowed - one bad URL must not break the batch.
        """
        url = candidate.get("url", "")
        title = candidate.get("title", "")
        snippet = candidate.get("snippet", "")
        source_id = f"s{idx}"

        try:
            if self._is_pdf(url):
                parsed = read_pdf(url)
                if not parsed.get("success"):
                    logger.warning(f"skip pdf (parse failed): {url} - {parsed.get('error')}")
                    return None
                content = truncate(parsed["full_text"], _MAX_CONTENT_CHARS)
                source_type = "pdf"
                metadata = {"page_count": parsed.get("page_count", 0)}
            else:
                parsed = read_webpage(url, max_chars=_MAX_CONTENT_CHARS)
                if not parsed.get("success"):
                    logger.warning(f"skip webpage (fetch failed): {url} - {parsed.get('error')}")
                    return None
                content = parsed["content"]
                title = title or parsed.get("title", "")
                source_type = "web"
                metadata = {}

            if not content:
                return None

            scored = score_source(
                content=content,
                subject=profile["subject"],
                title=title,
                snippet=snippet,
                url=url,
                aliases=profile["aliases"],
                industry_terms=profile["industry_terms"],
                relevance_terms=profile["relevance_terms"],
                report_type=profile["report_type"],
            )
            from tools.source_tier import classify_source

            tier = classify_source(url, title=title, source_type=source_type)
            return Source(
                source_id=source_id,
                title=title or url,
                url=url,
                snippet=snippet,
                content=content,
                source_type=source_type,
                score=scored["score"],
                quality_details=scored["details"],
                metadata=metadata,
                authority_tier=tier["authority_tier"],
                authority_reason=tier["authority_reason"],
                domain=tier["domain"],
                is_official_source=tier["is_official_source"],
                is_financial_media=tier["is_financial_media"],
                is_user_generated_content=tier["is_user_generated_content"],
            )
        except Exception as exc:  # noqa: BLE001 - never let one source kill the batch
            logger.warning(f"browser_agent failed on {url}: {exc}")
            return None

    def execute(self, task: Task, context: dict[str, Any]) -> dict[str, Any]:
        """Fetch, score and rank all candidate sources gathered by ResearchAgent.

        Applies a read cap (MAX_BROWSE_CANDIDATES) and an early-stop rule (stop
        once max_sources * EARLY_STOP_RELEVANT_MULTIPLIER relevant sources are
        in hand) so this doesn't pay for 60+ fetches when only top_k=5 are
        ever used - see eval/bad_cases.md for the run that motivated this.
        """
        request: ResearchRequest = context["request"]
        candidates = context.get("search_results", {}).get("candidates", [])
        top_k = task.parameters.get("top_k", request.max_sources or config.TOP_K_SOURCES)

        # Build the full relevance profile (subject + company aliases +
        # industry synonyms) instead of matching on the bare normalized
        # subject string alone - see utils.text_utils.build_relevance_profile
        # and tools/quality_scorer.py for why: a source that only ever says
        # "CATL"/"硅片" is still clearly on-topic even without the literal
        # subject phrase.
        profile = build_relevance_profile(request.topic, request.report_type)
        pre_filtered, pre_filter_counts = self._pre_filter(candidates, profile["relevance_terms"])
        logger.info(f"Browser pre-filter: {len(candidates)} candidates -> {len(pre_filtered)} kept")

        ranked = rank_search_results(
            pre_filtered, profile["subject"], aliases=profile["aliases"], query_text=request.topic
        )

        # 域名分散守卫：语义层会把同一站点的多篇高相关内容一起推进窗口（实测
        # 贵州茅台一轮 top6 里挤进 4 个雪球帖，恰逢该站批量 403，20 个候选只
        # 抓成 3 个）。单域名限席让窗口对"单站点当天反爬"保持韧性。
        max_per_domain = 4
        domain_counts: dict[str, int] = {}
        diversified: list[dict] = []
        overflow: list[dict] = []
        for c in ranked:
            domain = get_domain(c.get("url", ""))
            if domain_counts.get(domain, 0) < max_per_domain:
                domain_counts[domain] = domain_counts.get(domain, 0) + 1
                diversified.append(c)
            else:
                overflow.append(c)
        ranked = diversified + overflow  # overflow 不丢弃，只是排到窗口后面

        max_browse_candidates = config.MAX_BROWSE_CANDIDATES
        capped_candidates = ranked[:max_browse_candidates]
        if len(ranked) > max_browse_candidates:
            logger.info(f"Browser read limit: only reading top {max_browse_candidates} candidates")

        early_stop_relevant_count = max(1, top_k * config.EARLY_STOP_RELEVANT_MULTIPLIER)
        min_relevant_score = config.MIN_RELEVANT_SCORE

        sources: list[Source] = []
        actual_browsed_count = 0
        early_stopped = False
        early_stop_reason = ""

        for idx, candidate in enumerate(capped_candidates, start=1):
            actual_browsed_count = idx
            src = self._fetch_one(candidate, profile, idx)
            if src is not None:
                sources.append(src)

            relevant_so_far = sum(1 for s in sources if s.score >= min_relevant_score)
            if relevant_so_far >= early_stop_relevant_count:
                early_stopped = True
                early_stop_reason = (
                    f"reached {relevant_so_far} sources with score>={min_relevant_score} "
                    f"after browsing {actual_browsed_count}/{len(capped_candidates)} candidates"
                )
                logger.info(f"Browser early stopped after {relevant_so_far} relevant sources")
                break

        # Drop sources with ~zero topic relevance outright: a high overall score
        # from length/readability/authority alone does not make an irrelevant
        # page usable grounding, and citing it would be worse than citing nothing.
        relevant_sources = [
            s for s in sources if s.quality_details.get("topic_relevance", 0) > _MIN_TOPIC_RELEVANCE
        ]
        dropped = len(sources) - len(relevant_sources)
        if dropped:
            logger.warning(f"dropped {dropped} source(s) with near-zero topic relevance")

        relaxed_relevance_used = False
        relaxed_selected_count = 0
        retry_skipped_reason = None

        if relevant_sources:
            relevant_sources.sort(key=lambda s: s.score, reverse=True)
            top_sources = relevant_sources[:top_k]
        elif len(sources) >= _RELAXED_MIN_USABLE:
            # Fetched plenty of usable content but the relevance scorer found
            # nothing it recognized as on-topic - more likely an overly-strict
            # scorer than 20 genuinely irrelevant pages. Fall back to the
            # highest-scoring usable sources rather than failing outright, and
            # mark them so downstream/trace consumers know grounding is weaker
            # than a normal relevance-passing selection.
            relaxed = sorted(sources, key=lambda s: s.score, reverse=True)[:top_k]
            for s in relaxed:
                s.metadata["relaxed_selected"] = True
            top_sources = relaxed
            relaxed_relevance_used = True
            relaxed_selected_count = len(relaxed)
            retry_skipped_reason = "fetched_ok_but_zero_relevant"
            logger.warning(
                f"relaxed relevance selection used: {len(sources)} usable sources but 0 passed "
                f"the topic_relevance threshold - selecting top {len(relaxed)} by overall score"
            )
        else:
            top_sources = []
            if sources:
                # Some content fetched but too little to relax into a real
                # selection - retrying the same URL batch won't change that.
                retry_skipped_reason = "fetched_ok_but_zero_relevant"

        # Diagnostic-only: flag when candidate/usable volume looks thin enough
        # that a fallback search round (reusing ResearchAgent's fallback query
        # mechanism) would help. Not wired up to actually trigger a second
        # search round here - that would mean BrowserAgent calling back into
        # ResearchAgent, a bigger architectural change than this fix warrants.
        browser_fallback_triggered = len(pre_filtered) < 5 or len(sources) < 3
        browser_fallback_reason = (
            "insufficient_candidates_or_usable_sources" if browser_fallback_triggered else None
        )

        top_ranked = ranked[:10]
        browser_metrics = {
            "candidates_total": len(candidates),
            "candidates_after_prefilter": len(pre_filtered),
            "max_browse_candidates": max_browse_candidates,
            "actual_browsed_count": actual_browsed_count,
            "usable_count": len(sources),
            "relevant_count": len(relevant_sources),
            "selected_count": len(top_sources),
            "early_stopped": early_stopped,
            "early_stop_reason": early_stop_reason,
            "blacklisted_domain_drop_count": pre_filter_counts["blacklisted_domain_drop_count"],
            "relevance_drop_count": pre_filter_counts["relevance_drop_count"],
            "relaxed_relevance_used": relaxed_relevance_used,
            "relaxed_selected_count": relaxed_selected_count,
            "retry_skipped_reason": retry_skipped_reason,
            "browser_fallback_triggered": browser_fallback_triggered,
            "browser_fallback_reason": browser_fallback_reason,
            "top_ranked_domains": [get_domain(c.get("url", "")) for c in top_ranked],
            "top_ranked_sources": [
                {
                    "url": c.get("url", ""),
                    "title": c.get("title", ""),
                    "rank_score": c.get("rank_score", 0.0),
                    "rule_score": c.get("rule_score"),
                    "semantic_score": c.get("semantic_score"),
                }
                for c in top_ranked
            ],
        }

        logger.info(
            f"browsed {actual_browsed_count} candidates -> {len(sources)} usable -> "
            f"{len(relevant_sources)} relevant -> top {len(top_sources)} kept"
            + (" (relaxed)" if relaxed_relevance_used else "")
        )

        if not top_sources:
            # Zero usable sources at all (every fetch failed) is likely a
            # transient network issue - worth one retry. Zero *relevant* with
            # usable content already in hand (relaxed selection still empty,
            # which only happens when usable_count < _RELAXED_MIN_USABLE) is a
            # deterministic outcome for this exact URL batch - retrying would
            # just repeat it, so mark non_retryable.
            raise NoUsableSourcesError(
                f"no usable sources found (candidates={len(candidates)}, "
                f"after_pre_filter={len(pre_filtered)}, browsed={actual_browsed_count}, "
                f"fetched_ok={len(sources)}, topic_relevant={len(relevant_sources)})",
                browser_metrics,
                non_retryable=bool(sources),
            )

        return {
            "all_scored_count": len(sources),
            "sources": [s.model_dump() for s in top_sources],
            "browser_metrics": browser_metrics,
        }
