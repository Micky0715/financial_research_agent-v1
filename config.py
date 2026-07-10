"""Central configuration loaded from environment variables (.env)."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)


def _get_int(name: str, default: int) -> int:
    """Read an int env var, falling back to a default on missing/invalid values."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    """Read a float env var, falling back to a default on missing/invalid values."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    """Read a bool env var (accepts true/false/1/0/yes/no, case-insensitive)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


class Config:
    """Process-wide settings. Read once at import time."""

    # LLM
    MODEL_NAME: str = os.getenv("MODEL_NAME", "gpt-4o-mini")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_API_BASE: str = os.getenv("OPENAI_API_BASE", "")
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    OLLAMA_API_BASE: str = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")

    # Search / retrieval
    SEARCH_MAX_RESULTS: int = _get_int("SEARCH_MAX_RESULTS", 8)
    TOP_K_SOURCES: int = _get_int("TOP_K_SOURCES", 5)

    # ResearchAgent concurrency/fallback tuning: run the first-round core
    # queries in parallel, cap how long any single query can block the batch,
    # and only fire the (site:-scoped) fallback queries when the first round
    # genuinely came up short - see agents/research_agent.py.
    SEARCH_MAX_WORKERS: int = _get_int("SEARCH_MAX_WORKERS", 5)
    SEARCH_QUERY_TIMEOUT: int = _get_int("SEARCH_QUERY_TIMEOUT", 15)
    SEARCH_TARGET_UNIQUE_RESULTS: int = _get_int("SEARCH_TARGET_UNIQUE_RESULTS", 40)
    SEARCH_MIN_UNIQUE_RESULTS: int = _get_int("SEARCH_MIN_UNIQUE_RESULTS", 20)

    # Search result cache (utils/cache_utils.py): keyed by exact query string,
    # persisted to outputs/cache/search_cache.json so repeat runs/debugging
    # don't re-hit ddgs for queries already seen within the TTL window.
    ENABLE_SEARCH_CACHE: bool = _get_bool("ENABLE_SEARCH_CACHE", True)
    SEARCH_CACHE_TTL_HOURS: int = _get_int("SEARCH_CACHE_TTL_HOURS", 24)

    # MCP tool routing (tools/tool_gateway.py): when true, web_search /
    # read_webpage / read_pdf calls go through a Model Context Protocol
    # client session to mcp_server/tools_server.py instead of direct function
    # calls. Gateway degrades to direct calls if MCP is unavailable.
    USE_MCP_TOOLS: bool = _get_bool("USE_MCP_TOOLS", True)
    MCP_TOOL_TIMEOUT: int = _get_int("MCP_TOOL_TIMEOUT", 90)

    # Semantic ranking layer (tools/semantic_scorer.py + tools/source_filter.py):
    # local bge-small-zh-v1.5 embedding similarity fused with the keyword rule
    # score as (1-w)*rule + w*semantic. Rules are kept for explainability;
    # semantic layer degrades to rules-only when the model is unavailable.
    ENABLE_SEMANTIC_RANKING: bool = _get_bool("ENABLE_SEMANTIC_RANKING", True)
    SEMANTIC_WEIGHT: float = _get_float("SEMANTIC_WEIGHT", 0.35)

    # Browser performance tuning: cap how many pre-filtered candidates get
    # fetched, and stop early once enough good sources are already in hand -
    # fetching all 60+ candidates when only top_k=5 are ever used is wasted
    # network time (see eval/bad_cases.md).
    MAX_BROWSE_CANDIDATES: int = _get_int("MAX_BROWSE_CANDIDATES", 20)
    EARLY_STOP_RELEVANT_MULTIPLIER: int = _get_int("EARLY_STOP_RELEVANT_MULTIPLIER", 2)
    MIN_RELEVANT_SCORE: float = _get_float("MIN_RELEVANT_SCORE", 0.5)

    # Analyze/report LLM input compression: bound how much raw source content
    # gets injected into prompts instead of dumping full page text.
    MAX_ANALYSIS_CHARS_PER_SOURCE: int = _get_int("MAX_ANALYSIS_CHARS_PER_SOURCE", 1800)
    MAX_REPORT_EXCERPT_CHARS_PER_SOURCE: int = _get_int("MAX_REPORT_EXCERPT_CHARS_PER_SOURCE", 500)

    # Output
    OUTPUT_FORMAT: str = os.getenv("OUTPUT_FORMAT", "markdown")

    # Networking
    REQUEST_TIMEOUT: int = _get_int("REQUEST_TIMEOUT", 10)
    USER_AGENT: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Paths
    BASE_DIR: Path = BASE_DIR
    OUTPUT_DIR: Path = BASE_DIR / "outputs"
    REPORTS_DIR: Path = OUTPUT_DIR / "reports"
    TRACES_DIR: Path = OUTPUT_DIR / "traces"
    SOURCES_DIR: Path = OUTPUT_DIR / "sources"
    EVALUATIONS_DIR: Path = OUTPUT_DIR / "evaluations"
    CACHE_DIR: Path = OUTPUT_DIR / "cache"
    SEARCH_CACHE_PATH: Path = CACHE_DIR / "search_cache.json"
    PROMPTS_DIR: Path = BASE_DIR / "prompts"

    # Evaluation tooling (scripts/*.py): aggregated eval CSV/MD reports and
    # direct-LLM baseline reports, kept separate from a single run's own
    # reports/traces/sources/evaluations.
    EVAL_DIR: Path = OUTPUT_DIR / "eval"
    BASELINE_REPORTS_DIR: Path = OUTPUT_DIR / "baseline_reports"

    @classmethod
    def ensure_output_dirs(cls) -> None:
        """Create all output directories if they do not already exist."""
        for d in (
            cls.REPORTS_DIR,
            cls.TRACES_DIR,
            cls.SOURCES_DIR,
            cls.EVALUATIONS_DIR,
            cls.CACHE_DIR,
            cls.EVAL_DIR,
            cls.BASELINE_REPORTS_DIR,
        ):
            d.mkdir(parents=True, exist_ok=True)


config = Config()
config.ensure_output_dirs()
