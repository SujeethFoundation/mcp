"""Repeat searches are answered from memory; failed or partial ones are not kept."""
import os

import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
from test_single_search_call import HIT, RESOLVED, wire  # noqa: E402


def search(query="what is career circles"):
    return server.search_sujeeth_foundation.sync(query, full_documents=False)


def test_a_repeat_search_is_answered_from_memory(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 300)
    first = search()
    second = search()
    assert len(calls) == 1
    assert second["results"] == first["results"]


def test_case_and_spacing_share_one_entry(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 300)
    search("What is  Career Circles")
    search("what is career circles")
    assert len(calls) == 1


def test_a_cached_answer_is_a_copy(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 300)
    search()["results"].clear()
    assert search()["results"]


def test_an_expired_entry_searches_again(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 300)
    search()
    for key, (_, value) in list(server._search_cache.items()):
        server._search_cache[key] = (0, value)
    search()
    assert len(calls) == 2


def test_a_partial_answer_is_not_kept(monkeypatch):
    calls = wire(monkeypatch, requests.ConnectionError("down"))
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 300)
    assert search()["status"] == "partial"
    search()
    assert len(calls) > 1 and not server._search_cache


def test_zero_seconds_turns_the_cache_off(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 0)
    search()
    search()
    assert len(calls) == 2


def test_excerpt_instructions_tell_the_agent_not_to_read_needlessly():
    assert "Never read a page just to confirm" in server.EXCERPT_INSTRUCTIONS
    assert "Never read a page just to confirm" in server.WIKI_DOCUMENT_INSTRUCTIONS
