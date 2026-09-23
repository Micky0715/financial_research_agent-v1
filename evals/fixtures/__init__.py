"""Record/replay fixtures: deterministic tool behaviour for offline tests.

The default CI path must never depend on live search, live web pages, live
AkShare or a live LLM. Everything under `evals/` and `tests/harness/` runs
against these fixtures, which is what makes the suite reproducible on a laptop
with no API key.

Two things live here:

- `FixtureLibrary` - canned tool responses keyed by call fingerprint, with
  scripted failures (timeout, 403, malformed PDF, provider outage) so the
  recovery paths are exercised rather than asserted about.
- `install_fixture_tools()` - swaps the registry handlers for fixture-backed
  ones, leaving every other harness policy (validation, retry, circuits,
  budget, dedup, tracing) fully live. The tests therefore exercise the real
  executor, not a mock of it.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

FIXTURE_DIR = Path(__file__).resolve().parent
DEFAULT_LIBRARY = FIXTURE_DIR / "tool_responses.json"


class FixtureError(RuntimeError):
    """Scripted failure. Its message drives error classification, so the tests
    exercise the real `classify_exception` rather than a stubbed class."""


class FixtureLibrary:
    """Canned tool responses plus scripted failure scenarios."""

    def __init__(self, data: Optional[dict[str, Any]] = None) -> None:
        self.data: dict[str, Any] = data or {}
        #: name -> list of outcomes to yield on successive calls. An outcome is
        #: either {"raise": "<message>"} or {"return": <payload>}.
        self.scripts: dict[str, list[dict[str, Any]]] = {}
        self.call_log: list[tuple[str, dict[str, Any]]] = []

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path = DEFAULT_LIBRARY) -> "FixtureLibrary":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls(json.loads(p.read_text(encoding="utf-8")))

    def save(self, path: Path = DEFAULT_LIBRARY) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    # ------------------------------------------------------------------ #
    def script(self, tool_name: str, outcomes: list[dict[str, Any]]) -> "FixtureLibrary":
        """Queue scripted outcomes for a tool (consumed in order, then falls
        back to the canned data)."""
        self.scripts[tool_name] = list(outcomes)
        return self

    def fail_once(self, tool_name: str, message: str) -> "FixtureLibrary":
        return self.script(tool_name, [{"raise": message}])

    def _next_scripted(self, tool_name: str) -> Optional[dict[str, Any]]:
        queue = self.scripts.get(tool_name)
        if not queue:
            return None
        return queue.pop(0)

    # ------------------------------------------------------------------ #
    def search(self, query: str, max_results: int = 8) -> list[dict[str, Any]]:
        self.call_log.append(("web_search", {"query": query, "max_results": max_results}))
        if (scripted := self._next_scripted("web_search")) is not None:
            if "raise" in scripted:
                raise FixtureError(scripted["raise"])
            return scripted["return"]
        hits = self.data.get("web_search", {}).get("default", [])
        for key, value in self.data.get("web_search", {}).items():
            if key != "default" and key in query:
                hits = value
                break
        return hits[:max_results]

    def webpage(self, url: str, max_chars: int = 8000) -> dict[str, Any]:
        self.call_log.append(("read_webpage", {"url": url, "max_chars": max_chars}))
        if (scripted := self._next_scripted("read_webpage")) is not None:
            if "raise" in scripted:
                raise FixtureError(scripted["raise"])
            return scripted["return"]
        pages = self.data.get("read_webpage", {})
        page = pages.get(url)
        if page is None:
            return {"success": False, "url": url, "error": "404 not found in fixture library",
                    "content": "", "title": ""}
        return {**page, "url": url, "content": (page.get("content") or "")[:max_chars]}

    def pdf(self, url: str) -> dict[str, Any]:
        self.call_log.append(("read_pdf", {"url": url}))
        if (scripted := self._next_scripted("read_pdf")) is not None:
            if "raise" in scripted:
                raise FixtureError(scripted["raise"])
            return scripted["return"]
        docs = self.data.get("read_pdf", {})
        doc = docs.get(url)
        if doc is None:
            return {"success": False, "url": url, "error": "pdf parse failed: no text layer",
                    "full_text": "", "page_count": 0}
        return {**doc, "url": url}

    def snapshot(self, name_or_code: str) -> dict[str, Any]:
        self.call_log.append(("fetch_financial_snapshot", {"name_or_code": name_or_code}))
        if (scripted := self._next_scripted("fetch_financial_snapshot")) is not None:
            if "raise" in scripted:
                raise FixtureError(scripted["raise"])
            return scripted["return"]
        table = self.data.get("fetch_financial_snapshot", {})
        return table.get(name_or_code, table.get("default", {
            "success": False, "error": f"no fixture snapshot for {name_or_code}"}))

    def calls_to(self, tool_name: str) -> list[dict[str, Any]]:
        return [args for name, args in self.call_log if name == tool_name]


def install_fixture_tools(registry: Any, library: FixtureLibrary) -> Any:
    """Point the registry's handlers at `library`, keeping every spec intact.

    Specs are unchanged, so argument validation, output-schema checks, retry
    counts, timeouts and cost hints all behave exactly as in production.
    """
    from src.tools.registry import (
        EXPORT_REPORT_SPEC,
        FINANCIAL_SNAPSHOT_SPEC,
        READ_PDF_SPEC,
        READ_WEBPAGE_SPEC,
        SEARCH_SPEC,
        _handler_export_report_file,
        _project_page,
        _project_search,
        _project_snapshot,
    )

    registry.replace(SEARCH_SPEC, library.search, projector=_project_search)
    registry.replace(READ_WEBPAGE_SPEC, library.webpage, projector=_project_page)
    registry.replace(READ_PDF_SPEC, library.pdf, projector=_project_page)
    registry.replace(FINANCIAL_SNAPSHOT_SPEC, library.snapshot, projector=_project_snapshot)
    registry.replace(EXPORT_REPORT_SPEC, _handler_export_report_file)
    return registry


def build_default_library(subject: str = "示例公司",
                          report_type: str = "company_research") -> FixtureLibrary:
    """A small, self-consistent corpus with four sources whose text carries
    numbers the grounding graders can verify.

    `subject` is templated into every title and body. This matters more than it
    looks: with a fixed "示例公司" corpus, a scenario task about 贵州茅台 fails
    entity validation because no source mentions the subject, so the run ends
    `insufficient` and the scenario's actual condition (a timeout, an AkShare
    outage) is never exercised. Templating the subject makes each matrix cell
    test the failure mode it was written for.
    """
    if report_type == "macro_research":
        return _macro_corpus(subject)
    return _corpus(subject)


def _macro_corpus(subject: str) -> "FixtureLibrary":
    """A macro-policy corpus whose claims match the macro report path.

    Reusing company annual-report fixtures for macro tasks caused policy
    extraction to treat revenue and valuation prose as monetary policy.  That
    made the offline benchmark internally inconsistent and created unsupported
    numbers unrelated to the harness change under test.
    """
    policy = (
        f"{subject}：中国人民银行在货币政策执行报告中提出保持流动性合理充裕，"
        "强化利率政策执行，支持实体经济稳定增长。"
    ) * 4
    prices = (
        f"{subject}：国家统计局公布，2024年12月居民消费价格指数同比上涨0.1%，"
        "工业生产者出厂价格指数同比下降2.3%。"
    ) * 4
    liquidity = (
        f"{subject}：2024年12月广义货币M2同比增长7.3%，社会融资规模存量保持增长。"
    ) * 4
    risks = (
        f"{subject}面临外需波动、房地产调整和全球利率高位运行等风险，"
        "宏观指标存在发布时间与统计口径差异。"
    ) * 4
    hits = [
        {"title": f"{subject}货币政策执行报告", "url": "https://www.pbc.gov.cn/fixture/policy.html",
         "snippet": f"{subject}保持流动性合理充裕，支持实体经济。"},
        {"title": f"{subject}价格数据发布", "url": "https://www.stats.gov.cn/fixture/prices.html",
         "snippet": f"{subject}CPI与PPI最新数据。"},
        {"title": f"{subject}金融统计数据", "url": "https://www.pbc.gov.cn/fixture/liquidity.html",
         "snippet": f"{subject}M2与社会融资规模数据。"},
        {"title": f"{subject}宏观风险展望", "url": "https://www.gov.cn/fixture/outlook.html",
         "snippet": f"{subject}宏观风险与政策展望。"},
    ]
    return FixtureLibrary({
        "web_search": {"default": hits},
        "read_webpage": {
            hits[0]["url"]: {"success": True, "title": hits[0]["title"], "content": policy},
            hits[1]["url"]: {"success": True, "title": hits[1]["title"], "content": prices},
            hits[2]["url"]: {"success": True, "title": hits[2]["title"], "content": liquidity},
            hits[3]["url"]: {"success": True, "title": hits[3]["title"], "content": risks},
        },
        "read_pdf": {},
        "fetch_financial_snapshot": {},
    })


def offline_macro_snapshot(*, fail: bool = False) -> dict[str, Any]:
    """Stable structured macro fixture; never reads the live AkShare cache."""
    if fail:
        return {
            "provider": "offline-fixture", "query_type": "macro_snapshot",
            "success": False, "degraded": True, "error": "scripted AkShare outage",
            "fetched_at": "2025-01-15T00:00:00", "source_type": "structured_macro_data",
            "snapshot": None,
        }

    def point(key: str, name: str, value: float, unit: str, period: str,
              source: str, endpoint: str, previous: float) -> dict[str, Any]:
        return {
            "indicator_key": key, "indicator_name": name, "value": value, "unit": unit,
            "period": period, "frequency": "monthly", "source_name": source,
            "provider": "offline-fixture", "endpoint": endpoint,
            "original_platform": "record/replay fixture", "no_direct_source_url": True,
            "fetched_at": "2025-01-15T00:00:00", "previous_value": previous,
        }

    indicators = {
        "cpi_yoy": point("cpi_yoy", "CPI同比", 0.1, "%", "2024-12", "国家统计局",
                         "fixture_macro_cpi", 0.2),
        "ppi_yoy": point("ppi_yoy", "PPI同比", -2.3, "%", "2024-12", "国家统计局",
                         "fixture_macro_ppi", -2.5),
        "pmi_manufacturing": point("pmi_manufacturing", "制造业PMI", 50.1, "点", "2024-12",
                                   "国家统计局", "fixture_macro_pmi", 50.3),
        "m2_yoy": point("m2_yoy", "M2同比", 7.3, "%", "2024-12", "中国人民银行",
                        "fixture_macro_m2", 7.1),
        "cn_bond_10y": point("cn_bond_10y", "中国10年期国债收益率", 1.68, "%", "2025-01-14",
                             "中债", "fixture_cn_bond", 1.70),
        "us_bond_10y": point("us_bond_10y", "美国10年期国债收益率", 4.78, "%", "2025-01-14",
                             "美国财政部", "fixture_us_bond", 4.76),
        "us_fed_rate": point("us_fed_rate", "美联储联邦基金目标利率", 4.5, "%", "2024-12-31",
                             "美联储", "fixture_fed_rate", 4.75),
    }
    snapshot = {
        "indicators": indicators, "series": {}, "requested": list(indicators),
        "missing_indicators": ["social_financing", "urban_unemployment", "usd_index"],
        "errors": [], "fetched_at": "2025-01-15T00:00:00", "degraded": True,
        "metadata": {"fixture": True, "indicator_count": len(indicators)},
    }
    return {
        "provider": "offline-fixture", "query_type": "macro_snapshot", "success": True,
        "degraded": True, "error": None, "fetched_at": "2025-01-15T00:00:00",
        "source_type": "structured_macro_data", "snapshot": snapshot,
    }


@contextmanager
def offline_runtime(*, macro_failure: bool = False) -> Iterator[None]:
    """Block network escape hatches and fixture direct macro collection.

    The registry already replays the four Agent-facing tools.  Several legacy
    analysis helpers call Requests/AkShare directly, so an offline eval also
    needs a process-local guard around those calls.  Everything is restored at
    the end of the trial.
    """
    import requests
    import urllib.request
    from tools import macro_data_collector

    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise requests.exceptions.ConnectionError("outbound network disabled in offline eval")

    patches = [
        (requests.adapters.HTTPAdapter, "send", requests.adapters.HTTPAdapter.send),
        (requests.sessions.Session, "request", requests.sessions.Session.request),
        (requests, "get", requests.get),
        (requests, "post", requests.post),
        (urllib.request, "urlopen", urllib.request.urlopen),
        (macro_data_collector, "fetch_macro_snapshot", macro_data_collector.fetch_macro_snapshot),
    ]
    requests.adapters.HTTPAdapter.send = blocked
    requests.sessions.Session.request = blocked
    requests.get = blocked
    requests.post = blocked
    urllib.request.urlopen = blocked
    macro_data_collector.fetch_macro_snapshot = lambda *a, **k: offline_macro_snapshot(
        fail=macro_failure)
    try:
        yield
    finally:
        for owner, name, previous in reversed(patches):
            setattr(owner, name, previous)


@contextmanager
def install_gateway_fixtures(library: FixtureLibrary) -> Iterator[None]:
    """Replay the four tools at the **gateway transport** seam.

    `install_fixture_tools` patches the ToolExecutor *registry*, which only
    takes effect while a harness run is bound. The legacy ablation arm binds no
    harness, so `tools/tool_gateway.py` falls straight through to `_raw_*`,
    which goes out over MCP to a child process and onto the real internet -
    `offline_runtime` cannot stop that, because the network call happens in a
    subprocess where this process's `requests` patches do not exist.

    Measured before this existed: a single legacy browse step took 25.4s and
    the log showed live `CallToolRequest` traffic. That makes the legacy arm
    both non-deterministic and unfair to compare against an offline harness arm.

    Patching `_raw_*` is the correct seam: it is the lowest point both the
    harness path and the legacy path share.
    """
    from tools import tool_gateway as gw

    originals = {
        "_raw_web_search": gw._raw_web_search,
        "_raw_read_webpage": gw._raw_read_webpage,
        "_raw_read_pdf": gw._raw_read_pdf,
        "_raw_fetch_financial_snapshot": gw._raw_fetch_financial_snapshot,
    }
    gw._raw_web_search = lambda query, max_results=8: library.search(query, max_results)
    gw._raw_read_webpage = lambda url, max_chars=8000: library.webpage(url, max_chars)
    gw._raw_read_pdf = lambda url: library.pdf(url)
    gw._raw_fetch_financial_snapshot = lambda name_or_code: library.snapshot(name_or_code)
    try:
        yield
    finally:
        for name, fn in originals.items():
            setattr(gw, name, fn)


def _corpus(subject: str) -> "FixtureLibrary":
    annual_body = (
        f"{subject}2024年实现营业收入1200.5亿元，同比增长18.3%；"
        "归属于母公司股东的净利润97.2亿元，同比增长12.6%。"
        "毛利率22.4%，加权平均净资产收益率14.8%。"
        "经营活动产生的现金流量净额150.3亿元。"
    ) * 3
    research_body = (
        f"报告认为{subject}毛利率提升至22.4%，净利润率为8.1%，"
        "当前市盈率18.5倍，市净率2.3倍，估值处于历史中枢下方。"
    ) * 3
    industry_body = (
        f"2024年{subject}所在行业前五大企业合计市场份额CR5为61.2%，"
        "行业整体规模约8600亿元，同比增长9.4%。"
    ) * 3
    risk_body = (
        f"{subject}的主要风险包括原材料价格波动、海外需求放缓、"
        "汇率波动对出口业务的影响，以及行业产能过剩风险。"
    ) * 3

    return FixtureLibrary({
        "web_search": {
            "default": [
                {"title": f"{subject}2024年年度报告摘要",
                 "url": "https://www.cninfo.com.cn/fixture/annual.html",
                 "snippet": f"{subject}2024年营业收入1200.5亿元，同比增长18.3%。"},
                {"title": f"{subject}深度研究：盈利能力持续改善",
                 "url": "https://finance.eastmoney.com/fixture/report.html",
                 "snippet": f"{subject}毛利率提升至22.4%，净利润率8.1%。"},
                {"title": f"{subject}行业竞争格局分析",
                 "url": "https://www.cls.cn/fixture/industry.html",
                 "snippet": f"{subject}所在行业CR5为61.2%，集中度持续提升。"},
                {"title": f"{subject}风险提示",
                 "url": "https://www.10jqka.com.cn/fixture/risk.html",
                 "snippet": f"{subject}面临原材料价格波动与海外需求放缓风险。"},
                {"title": "无关页面", "url": "https://example.invalid/unrelated.html",
                 "snippet": "与本主题无关的内容。"},
            ],
        },
        "read_webpage": {
            "https://www.cninfo.com.cn/fixture/annual.html": {
                "success": True, "title": f"{subject}2024年年度报告摘要", "content": annual_body},
            "https://finance.eastmoney.com/fixture/report.html": {
                "success": True, "title": f"{subject}深度研究：盈利能力持续改善",
                "content": research_body},
            "https://www.cls.cn/fixture/industry.html": {
                "success": True, "title": f"{subject}行业竞争格局分析", "content": industry_body},
            "https://www.10jqka.com.cn/fixture/risk.html": {
                "success": True, "title": f"{subject}风险提示", "content": risk_body},
        },
        "read_pdf": {
            "https://www.cninfo.com.cn/fixture/annual.pdf": {
                "success": True, "page_count": 42,
                "full_text": f"{subject}2024年年度报告全文。营业收入1200.5亿元。" * 20},
        },
        "fetch_financial_snapshot": {
            # The production wrapper accepts either a code or a normalized
            # entity name.  Keep both lookup forms in the replay world so a
            # normal company task does not look spuriously degraded merely
            # because the fixture only recognised one hard-coded ticker.
            "600519": {"success": True, "symbol": "600519", "name": subject,
                       "revenue": 120050000000.0, "net_profit": 9720000000.0,
                       "gross_margin": 22.4, "roe": 14.8, "pe": 18.5, "pb": 2.3},
            subject: {"success": True, "symbol": "600519", "name": subject,
                      "revenue": 120050000000.0, "net_profit": 9720000000.0,
                      "gross_margin": 22.4, "roe": 14.8, "pe": 18.5, "pb": 2.3},
            "default": {"success": True, "symbol": "600519", "name": subject,
                        "revenue": 120050000000.0, "net_profit": 9720000000.0,
                        "gross_margin": 22.4, "roe": 14.8, "pe": 18.5, "pb": 2.3},
        },
    })
