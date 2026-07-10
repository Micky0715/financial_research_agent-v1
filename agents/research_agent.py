"""Research agent: builds a small set of high-signal queries, runs them
concurrently (cache-first), and falls back to authoritative site: queries
only if the first round comes up short.

Design note: `requirements` (report sections like "财务分析"/"估值分析") used to
be turned 1:1 into extra search queries, which ballooned the query count to
9 with heavy semantic overlap ("{topic} 财务分析" vs "{topic} 财务 分析 风险
研报" barely differ) and did nothing but slow ResearchAgent down. Sections
are what the report needs to *cover*, not what search needs to *ask* -
5 well-chosen core queries already cover those information dimensions.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Optional

from config import config
from schemas.request import ResearchRequest
from schemas.task import Task
from tools.tool_gateway import web_search  # same signature; routes via MCP when enabled
from utils.cache_utils import get_cached_search_result, set_cached_search_result
from utils.logger import logger
from utils.text_utils import normalize_topic

from .base_agent import BaseAgent

_CORE_COMPANY_QUERY_TEMPLATES = [
    "{subject} 财务分析 研报",
    "{subject} 年报 营收 净利润 毛利率",
    "{subject} 投资价值 估值 风险",
    "{subject} 公告 财务数据",
    "{subject} 行业地位 竞争格局",
]

# No explicit spec was given for industry_research query wording - mirrored
# from the company list's structure (financial/valuation/risk/authoritative-
# data/competitive-position dimensions) rather than left as a per-requirement
# expansion.
_CORE_INDUSTRY_QUERY_TEMPLATES = [
    "{subject} 行业研报 市场规模",
    "{subject} 产业链 竞争格局",
    "{subject} 政策 趋势 风险",
    "{subject} 龙头企业 财务数据",
    "{subject} 投资机会 风险",
]

_FALLBACK_COMPANY_QUERY_TEMPLATES = [
    "{subject} site:cninfo.com.cn",
    "{subject} 年报 site:cninfo.com.cn",
    "{subject} site:szse.cn",
    "{subject} site:eastmoney.com",
    "{subject} 财务分析 site:10jqka.com.cn",
]

_FALLBACK_INDUSTRY_QUERY_TEMPLATES = [
    "{subject} 行业研报",
    "{subject} 市场规模 政策 风险",
    "{subject} 产业链 竞争格局",
    "{subject} site:gov.cn",
    "{subject} site:cls.cn",
]

_MAX_FIRST_ROUND_QUERIES = 6

# Small seed map of subject -> one high-signal alias (e.g. stock ticker) to
# add as a 6th first-round query. Extend as needed; a subject with no entry
# here simply gets the 5 core queries and nothing more.
_SUBJECT_ALIAS_MAP: dict[str, str] = {
    "比亚迪": "002594",
    "宁德时代": "300750",
}


def _dedup_preserve_order(items: list[str]) -> list[str]:
    """Drop duplicates while keeping first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _search_one_query(query: str, max_results: int) -> dict[str, Any]:
    """Run in a worker thread: cache-first, else live web_search(). Never raises.

    Always returns a dict with query/success/hit_count/duration/from_cache/
    error/hits so the caller can build both the candidate list and the
    per-query trace entries uniformly regardless of cache hit/miss/failure.
    """
    start = time.perf_counter()
    cached = get_cached_search_result(query)
    if cached is not None:
        return {
            "query": query,
            "success": True,
            "hit_count": len(cached),
            "duration": round(time.perf_counter() - start, 4),
            "from_cache": True,
            "error": None,
            "hits": cached,
        }

    try:
        hits = web_search(query, max_results=max_results)
        set_cached_search_result(query, hits)
        return {
            "query": query,
            "success": True,
            "hit_count": len(hits),
            "duration": round(time.perf_counter() - start, 4),
            "from_cache": False,
            "error": None,
            "hits": hits,
        }
    except Exception as exc:  # noqa: BLE001 - one query must never break the batch
        return {
            "query": query,
            "success": False,
            "hit_count": 0,
            "duration": round(time.perf_counter() - start, 4),
            "from_cache": False,
            "error": str(exc),
            "hits": [],
        }


class ResearchAgent(BaseAgent):
    """Runs a small core query set concurrently, with a site:-scoped fallback round."""

    name = "research_agent"
    description = "生成核心搜索query，并发调用搜索工具，来源不足时触发权威站点fallback"
    tools = ["web_search"]

    def _build_core_queries(self, subject: str, report_type: str) -> list[str]:
        """5 core queries covering financial/valuation/risk/official-data/competitive-position,
        plus (optionally) 1 alias query - capped at _MAX_FIRST_ROUND_QUERIES.
        """
        templates = (
            _CORE_INDUSTRY_QUERY_TEMPLATES if report_type == "industry_research" else _CORE_COMPANY_QUERY_TEMPLATES
        )
        queries = [t.format(subject=subject) for t in templates]

        alias = _SUBJECT_ALIAS_MAP.get(subject)
        if alias and report_type != "industry_research":
            queries.append(f"{alias} 年报 财务分析")

        return _dedup_preserve_order(queries)[:_MAX_FIRST_ROUND_QUERIES]

    def _build_fallback_queries(self, subject: str, report_type: str) -> list[str]:
        """Up to 5 site:-scoped queries targeting authoritative sources only."""
        templates = (
            _FALLBACK_INDUSTRY_QUERY_TEMPLATES if report_type == "industry_research" else _FALLBACK_COMPANY_QUERY_TEMPLATES
        )
        return _dedup_preserve_order([t.format(subject=subject) for t in templates])[:5]

    def _run_search_batch(
        self, queries: list[str], max_results: int, target_unique: Optional[int] = None
    ) -> dict[str, Any]:
        """Run `queries` concurrently (ThreadPoolExecutor), cache-first, URL-deduped.

        Each query gets up to config.SEARCH_QUERY_TIMEOUT seconds; a slow/failed
        query never blocks the others or the overall result. If `target_unique`
        is set and enough unique candidates have already been collected, stops
        harvesting further results early (remaining in-flight threads are asked
        to cancel, though already-running ddgs calls can't be interrupted).
        """
        query_results: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        early_stopped = False

        if not queries:
            return {"query_results": query_results, "candidates": candidates, "early_stopped": early_stopped}

        executor = ThreadPoolExecutor(max_workers=min(config.SEARCH_MAX_WORKERS, len(queries)))
        try:
            future_to_query = {executor.submit(_search_one_query, q, max_results): q for q in queries}
            for future, query in future_to_query.items():
                try:
                    result = future.result(timeout=config.SEARCH_QUERY_TIMEOUT)
                except FutureTimeoutError:
                    result = {
                        "query": query,
                        "success": False,
                        "hit_count": 0,
                        "duration": float(config.SEARCH_QUERY_TIMEOUT),
                        "from_cache": False,
                        "error": "timeout",
                        "hits": [],
                    }
                except Exception as exc:  # noqa: BLE001 - a single query must never break the batch
                    result = {
                        "query": query,
                        "success": False,
                        "hit_count": 0,
                        "duration": 0.0,
                        "from_cache": False,
                        "error": str(exc),
                        "hits": [],
                    }

                hits = result.pop("hits", [])
                query_results.append(result)
                for hit in hits:
                    url = hit.get("url", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    candidates.append(hit)

                if target_unique and len(candidates) >= target_unique:
                    early_stopped = True
                    break
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        return {"query_results": query_results, "candidates": candidates, "early_stopped": early_stopped}

    def execute(self, task: Task, context: dict[str, Any]) -> dict[str, Any]:
        """Run the core query round concurrently, then fallback only if results are thin."""
        request: ResearchRequest = context["request"]
        max_results = task.parameters.get("max_results", config.SEARCH_MAX_RESULTS)
        subject = normalize_topic(request.topic)

        research_start = time.perf_counter()

        first_round_queries = self._build_core_queries(subject, request.report_type)
        logger.info(f"Research queries: {len(first_round_queries)}")
        logger.info(f"Search workers: {config.SEARCH_MAX_WORKERS}")

        first_batch = self._run_search_batch(
            first_round_queries, max_results, target_unique=config.SEARCH_TARGET_UNIQUE_RESULTS
        )
        query_results = list(first_batch["query_results"])
        candidates = list(first_batch["candidates"])
        early_stopped = first_batch["early_stopped"]

        fallback_queries: list[str] = []
        fallback_result_count = 0
        fallback_reason: Optional[str] = None
        fallback_triggered = len(candidates) < config.SEARCH_MIN_UNIQUE_RESULTS

        if fallback_triggered:
            fallback_reason = "unique_result_count_below_threshold"
            fallback_queries = self._build_fallback_queries(subject, request.report_type)
            logger.info(
                f"Fallback triggered: true ({len(candidates)} < {config.SEARCH_MIN_UNIQUE_RESULTS})"
            )
            fallback_target = max(config.SEARCH_TARGET_UNIQUE_RESULTS - len(candidates), 1)
            fallback_batch = self._run_search_batch(fallback_queries, max_results, target_unique=fallback_target)
            query_results.extend(fallback_batch["query_results"])

            seen_urls = {c.get("url", "") for c in candidates}
            before = len(candidates)
            for hit in fallback_batch["candidates"]:
                url = hit.get("url", "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                candidates.append(hit)
            fallback_result_count = len(candidates) - before
            early_stopped = early_stopped or fallback_batch["early_stopped"]
        else:
            logger.info("Fallback triggered: false")

        final_queries = _dedup_preserve_order(first_round_queries + fallback_queries)
        raw_result_count = sum(r["hit_count"] for r in query_results)
        cache_hit_count = sum(1 for r in query_results if r["from_cache"])
        cache_miss_count = sum(1 for r in query_results if not r["from_cache"])
        research_duration = round(time.perf_counter() - research_start, 4)

        logger.info(f"Cache hits: {cache_hit_count}, misses: {cache_miss_count}")
        logger.info(f"Raw results: {raw_result_count}, unique: {len(candidates)}")
        logger.info(f"Research duration: {research_duration} seconds")

        research_metrics = {
            "query_count": len(first_round_queries),
            "first_round_queries": first_round_queries,
            "fallback_triggered": fallback_triggered,
            "fallback_reason": fallback_reason,
            "fallback_queries": fallback_queries,
            "fallback_result_count": fallback_result_count,
            "final_queries": final_queries,
            "concurrent_workers": config.SEARCH_MAX_WORKERS,
            "query_timeout": config.SEARCH_QUERY_TIMEOUT,
            "target_unique_results": config.SEARCH_TARGET_UNIQUE_RESULTS,
            "min_unique_results_for_no_fallback": config.SEARCH_MIN_UNIQUE_RESULTS,
            "cache_hit_count": cache_hit_count,
            "cache_miss_count": cache_miss_count,
            "raw_result_count": raw_result_count,
            "unique_result_count": len(candidates),
            "early_stopped": early_stopped,
            "research_duration": research_duration,
            "query_results": query_results,
        }

        return {"queries": final_queries, "candidates": candidates, "research_metrics": research_metrics}
