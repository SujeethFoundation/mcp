"""A website search that is still loading its index must not hold the whole answer for the full request timeout."""
import os

import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
from tests.test_retry import Fake, call, reply  # noqa: E402


def loading():
    return reply(503, {"Retry-After": "3"})


def test_a_search_app_that_keeps_answering_503_is_given_up_on_within_the_search_budget(monkeypatch):
    fake = Fake(monkeypatch, [loading()])
    try:
        call(timeout=12)
    except requests.HTTPError:
        pass
    assert fake.now <= 12 and fake.calls >= 2           # it retried, and stopped inside the budget
    assert max(fake.timeouts) <= 12


def test_without_a_budget_the_request_timeout_still_applies(monkeypatch):
    fake = Fake(monkeypatch, [loading()])
    monkeypatch.setattr(server, "RETRY_ATTEMPTS", 50)
    try:
        call()
    except requests.HTTPError:
        pass
    assert 12 < fake.now <= server.TIMEOUT


def test_a_budget_longer_than_the_request_timeout_is_capped_by_it(monkeypatch):
    fake = Fake(monkeypatch, [requests.Timeout("slow")])
    try:
        call(timeout=10_000)
    except requests.Timeout:
        pass
    assert all(t <= server.TIMEOUT for t in fake.timeouts)


def test_the_default_search_budget_is_well_under_the_request_timeout():
    assert 1 <= server.WEBSEARCH_SEARCH_TIMEOUT < server.TIMEOUT


def test_only_the_website_search_gets_the_short_budget(monkeypatch):
    from tests.test_single_search_call import RESOLVED, HIT, wire
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]}, wiki=[])
    plan = {"resolution": {**RESOLVED, "matched_concepts": ["explore"]}, "queries": ["a", "b", "c"], "error": None}
    monkeypatch.setattr(server, "resolve_ontology_plan", lambda query: plan)
    seen = []

    def record(url, payload, key="", attempts=None, timeout=None):
        seen.append((url, timeout))
        return {"results": []}

    monkeypatch.setattr(server, "post_json_with_retry", record)
    server.search_sujeeth_foundation.sync("how can I research a company?")
    web = [t for url, t in seen if url.startswith(server.WEBSEARCH_URL)]
    wiki = [t for url, t in seen if url.startswith(server.WIKISEARCH_URL)]
    assert web == [server.WEBSEARCH_SEARCH_TIMEOUT] and wiki == [None]
