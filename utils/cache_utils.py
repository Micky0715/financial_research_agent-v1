"""File-based cache for web_search results, keyed by exact query string.

Persisted to outputs/cache/search_cache.json so repeat runs (and iterative
debugging of the same topic) don't re-hit ddgs for a query already seen
within the TTL window. A module-level in-memory copy avoids re-reading the
file for every query within a single process run.
"""
import json
import threading
from datetime import datetime, timedelta
from typing import Any, Optional

from config import config
from utils.logger import logger

_lock = threading.Lock()
_cache_mem: Optional[dict[str, Any]] = None


def load_search_cache() -> dict[str, Any]:
    """Load the on-disk search cache into the in-process memory copy.

    Returns the (possibly empty) cache dict. A missing or corrupt cache file
    is never fatal - it just starts fresh, logging a warning.
    """
    global _cache_mem
    if _cache_mem is not None:
        return _cache_mem

    path = config.SEARCH_CACHE_PATH
    if not path.exists():
        _cache_mem = {}
        return _cache_mem

    try:
        with open(path, "r", encoding="utf-8") as f:
            _cache_mem = json.load(f)
        if not isinstance(_cache_mem, dict):
            raise ValueError("cache file did not contain a JSON object")
    except Exception as exc:  # noqa: BLE001 - a bad cache file must never break the run
        logger.warning(f"search cache load failed, starting fresh: {exc}")
        _cache_mem = {}
    return _cache_mem


def save_search_cache() -> None:
    """Persist the in-memory cache to disk. Never raises."""
    if _cache_mem is None:
        return
    path = config.SEARCH_CACHE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_cache_mem, f, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001 - caching is an optimization, not a requirement
        logger.warning(f"search cache save failed: {exc}")


def get_cached_search_result(query: str) -> Optional[list[dict]]:
    """Return cached results for `query` if present and not expired, else None.

    Returns None (cache miss) whenever ENABLE_SEARCH_CACHE is off, the query
    was never cached, the entry is malformed, or it has aged past
    SEARCH_CACHE_TTL_HOURS.
    """
    if not config.ENABLE_SEARCH_CACHE:
        return None

    with _lock:
        cache = load_search_cache()
        entry = cache.get(query)
        if not entry:
            return None
        try:
            created_at = datetime.fromisoformat(entry["created_at"])
            results = entry["results"]
        except (KeyError, TypeError, ValueError):
            return None
        if datetime.now() - created_at > timedelta(hours=config.SEARCH_CACHE_TTL_HOURS):
            return None
        return results


def set_cached_search_result(query: str, results: list[dict]) -> None:
    """Store `results` for `query` with the current timestamp and persist to disk."""
    if not config.ENABLE_SEARCH_CACHE:
        return
    with _lock:
        cache = load_search_cache()
        cache[query] = {"created_at": datetime.now().isoformat(), "results": results}
        save_search_cache()
