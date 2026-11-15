"""Repeat searches are answered from memory; failed or partial ones are not kept."""
import os
import json

import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
from test_single_search_call import HIT, RESOLVED, wire  # noqa: E402


def search(query="what is career circles"):
    return server.search_sujeeth_foundation.sync(query)


def test_a_repeat_search_is_answered_from_memory(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 300)
    first = search()
    second = search()
    assert len(calls) == 1
    assert second["results"] == first["results"]


def test_cache_hit_still_emits_streaming_timings(capsys, monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 300)
    search()
    search()
    records = [line.partition(" ")[2] for line in capsys.readouterr().err.splitlines()
               if line.startswith("search_timings_ms ")]
    assert len(records) == 2
    timing = json.loads(records[-1])
    assert timing["cache_hit"] is True
    assert timing["ontology_resolution_ms"] >= 0
    assert "tool_response_ready_ms" in timing


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


def test_slimmed_results_drop_fields_the_model_does_not_use(monkeypatch):
    monkeypatch.setattr(server, "SLIM_RESULTS", True)
    rows = [{"title": "T", "url": "u", "snippet": "s", "source": "website", "score": 0.1, "domain": None,
             "concepts": ["a"], "chunk_id": "c", "best_bet": False, "spotlight": True}]
    out = server.slim_results(rows)
    assert out == [{"title": "T", "url": "u", "snippet": "s", "source": "website", "spotlight": True}]
    monkeypatch.setattr(server, "SLIM_RESULTS", False)
    kept = [{"score": 0.1, "concepts": ["a"], "chunk_id": "c", "best_bet": False}]
    assert server.slim_results(kept) == [{"score": 0.1, "concepts": ["a"], "chunk_id": "c", "best_bet": False}]


def test_reply_format_decides_what_the_tool_wrapper_returns(monkeypatch):
    import asyncio

    @server.in_thread
    def tool():
        return {"answer": 1}

    monkeypatch.setattr(server, "REPLY_FORMAT", "text")
    assert asyncio.run(tool()) == {"answer": 1}
    monkeypatch.setattr(server, "REPLY_FORMAT", "structured")
    out = asyncio.run(tool())
    assert out.content == [] and out.structuredContent == {"answer": 1}
    assert tool.sync() == {"answer": 1}  # the warm-up reads the plain result

