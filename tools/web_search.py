"""Web search tool backed by duckduckgo-search, with retry and dedup."""
from tenacity import retry, stop_after_attempt, wait_fixed

from utils.logger import logger


@retry(stop=stop_after_attempt(2), wait=wait_fixed(1), reraise=False)
def _ddgs_search(query: str, max_results: int) -> list[dict]:
    """Run a single DuckDuckGo text search call. Raises on transient failure."""
    from ddgs import DDGS  # `duckduckgo-search` was renamed to `ddgs`; old pkg is unmaintained/rate-limited

    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=max_results, region="cn-zh"))


def web_search(query: str, max_results: int = 8) -> list[dict]:
    """Search the web for `query` and return up to max_results candidate hits.

    Each hit is a dict with keys: title, url, snippet. Never raises - on failure
    it logs the error and returns an empty list so the caller can continue with
    other queries.
    """
    if not query or not query.strip():
        return []

    try:
        raw_results = _ddgs_search(query.strip(), max_results)
    except Exception as exc:  # noqa: BLE001 - search must never crash the pipeline
        logger.warning(f"web_search failed for query='{query}': {exc}")
        return []

    seen_urls: set[str] = set()
    results: list[dict] = []
    for item in raw_results or []:
        url = (item.get("href") or item.get("url") or "").strip()
        title = (item.get("title") or "").strip()
        if not url or not title or url in seen_urls:
            continue
        seen_urls.add(url)
        results.append(
            {
                "title": title,
                "url": url,
                "snippet": (item.get("body") or item.get("snippet") or "").strip(),
            }
        )
    return results
