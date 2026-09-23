"""Shared fixtures for harness tests.

Everything here is offline and deterministic. Semantic ranking is disabled
because loading the bge embedding model costs ~40s on a cold start and adds
nothing to what these tests assert.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config import config  # noqa: E402
from evals.fixtures import build_default_library, install_fixture_tools  # noqa: E402
from src.observability.trace_store import ArtifactStore, InMemoryTraceStore  # noqa: E402
from src.runtime.budget import BudgetLimits, BudgetTracker  # noqa: E402
from src.runtime.policies import RetryPolicy  # noqa: E402
from src.runtime.run_context import force_unbind  # noqa: E402
from src.runtime.state import RunState  # noqa: E402
from src.tools.executor import ToolExecutor  # noqa: E402
from src.tools.registry import build_default_registry  # noqa: E402


@pytest.fixture(autouse=True)
def _fast_offline_config(monkeypatch):
    """Force the deterministic, network-free code paths for every harness test."""
    monkeypatch.setattr(config, "ENABLE_SEMANTIC_RANKING", False, raising=False)
    monkeypatch.setattr(config, "USE_MCP_TOOLS", False, raising=False)
    monkeypatch.setattr(config, "ENABLE_SEARCH_CACHE", False, raising=False)
    yield
    force_unbind()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Hard-block outbound HTTP for the whole harness suite.

    The registry's fixture handlers cover the four gateway tools, but the
    analyze stage's deep chains (`tools/financial_statement_extractor.py`,
    `peer_comparison.py`, `shareholder_structure.py`, `macro_data_collector.py`)
    call AkShare *directly*, bypassing the gateway. Without this guard those
    reach the live internet: the first integration run took 191s and its
    duration depended on whether Sina/EastMoney answered - a test suite that is
    slow and non-deterministic for reasons unrelated to what it asserts.

    Blocking at the socket-adapter level makes every such call fail fast. The
    pipeline's documented degradation paths then handle it, which is exactly
    what these tests should be exercising.
    """
    import requests

    def _blocked(*args, **kwargs):
        raise requests.exceptions.ConnectionError(
            "outbound network disabled in the harness test suite (tests/harness/conftest.py)")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _blocked, raising=False)
    monkeypatch.setattr(requests.sessions.Session, "request", _blocked, raising=False)
    monkeypatch.setattr(requests, "get", _blocked, raising=False)
    monkeypatch.setattr(requests, "post", _blocked, raising=False)
    try:
        import urllib.request as _urllib

        monkeypatch.setattr(_urllib, "urlopen", _blocked, raising=False)
    except ImportError:  # pragma: no cover
        pass


@pytest.fixture
def library():
    return build_default_library()


@pytest.fixture
def registry(library):
    return install_fixture_tools(build_default_registry(), library)


@pytest.fixture
def trace():
    return InMemoryTraceStore(run_id="test-run")


@pytest.fixture
def artifacts(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def run_state():
    return RunState(topic="示例公司投资价值分析", report_type="company_research")


@pytest.fixture
def executor(run_state, trace, registry, artifacts):
    """A fully live executor: only the tool handlers are fixtures.

    Retry backoff is zeroed and jitter disabled so tests are fast and
    deterministic while still exercising the real retry decision logic.
    """
    ex = ToolExecutor(
        run_state=run_state, trace=trace, registry=registry,
        budget=BudgetTracker(BudgetLimits(max_duration_s=600)), artifacts=artifacts,
        retry_policy=RetryPolicy(jitter=False, base_delay_s=0.0, max_delay_s=0.0),
        sleep=lambda _s: None,
    )
    yield ex
    ex.close()


@pytest.fixture
def stub_llm(monkeypatch):
    """Deterministic LLM stub returning a grounded, citation-bearing report."""
    from agents.base_agent import BaseAgent

    calls: list[dict] = []

    def _fake(prompt: str, system: str = "", temperature: float = 0.3) -> str:
        calls.append({"prompt": prompt[:200], "system": system[:100]})
        return (
            "## 公司概况\n示例公司为制造业企业 [s1]。\n"
            "## 财务分析\n2024年营业收入1200.5亿元 [s1]，毛利率22.4% [s1]。\n"
            "## 风险提示\n原材料价格波动 [s2]。\n"
            "## 投资观点\n维持中性判断 [s2]。\n"
        )

    monkeypatch.setattr(BaseAgent, "call_llm", staticmethod(_fake))
    return calls
