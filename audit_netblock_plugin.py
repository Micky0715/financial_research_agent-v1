"""Pytest plugin: hard-block outbound HTTP for the whole session.

Used once, by the final-verification audit, to answer a single question:
does any test in this suite actually depend on the network?
"""
import pytest


@pytest.fixture(autouse=True)
def _audit_block_network(monkeypatch):
    import requests

    def blocked(*args, **kwargs):
        raise requests.exceptions.ConnectionError("network blocked by audit plugin")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", blocked, raising=False)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked, raising=False)
    monkeypatch.setattr(requests, "get", blocked, raising=False)
    monkeypatch.setattr(requests, "post", blocked, raising=False)
    import urllib.request as u
    monkeypatch.setattr(u, "urlopen", blocked, raising=False)
