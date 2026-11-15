"""Query variants are sent together after one ontology resolution."""
import json as jsonlib
import os

import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402


def resolved(*concepts):
    return {"status": "resolved", "ontology_version": "test", "matched_concepts": list(concepts),
            "concept_matches": [{"concept_id": c, "name": c.title(), "confidence": 1.0} for c in concepts],
            "domains": ["sujeeth.org"]}


def reply(body):
    response = requests.Response()
    response.status_code = 200
    response._content = jsonlib.dumps(body).encode()
    return response


def wire(monkeypatch, website, wiki=None):
    sent = []

    def post(url, headers=None, json=None, timeout=None):
        sent.append((url, dict(json)))
        return reply(website if url.startswith(server.WEBSEARCH_URL) else {"results": wiki or []})

    monkeypatch.setattr(server.http, "post", post)
    monkeypatch.setattr(server, "get_json", lambda *args, **kwargs: {"results": wiki or []})
    return sent


def test_no_variants_keeps_single_search_response(monkeypatch):
    hit = {"title": "Resume", "url": "https://sujeeth.org/resume", "snippet": "Advice",
           "source": "website", "domain": "sujeeth.org", "chunk_id": "resume-0"}
    wire(monkeypatch, {"status": "completed", "resolution": resolved("resume_resource"), "results": [hit]})
    monkeypatch.setattr(server, "resolve_ontology_plan", lambda query: {
        "resolution": resolved("resume_resource"), "queries": [query], "error": None})
    result = server.search_sujeeth_foundation.sync("resume")
    assert result["queries"] == ["resume"]
    assert result["results"][0]["url"] == hit["url"]
    assert "for_query" not in result["results"][0]


def test_same_topic_variants_make_one_request_to_each_search_service(monkeypatch):
    hit = {"title": "Research", "url": "https://sujeeth.org/research", "snippet": "Explore roles",
           "source": "website", "domain": "sujeeth.org", "chunk_id": "research-0"}
    sent = wire(monkeypatch, {"status": "completed", "resolution": resolved("explore"), "results": [hit]})
    plan = {"resolution": resolved("explore"),
            "queries": ["research companies", "research employers"], "error": None}
    resolutions = []
    monkeypatch.setattr(server, "resolve_ontology_plan", lambda query: resolutions.append(query) or plan)

    result = server.search_sujeeth_foundation.sync(
        "how do I research companies?", more_queries=["research employers"])

    website = [body for url, body in sent if url.startswith(server.WEBSEARCH_URL)]
    wiki = [body for url, body in sent if url.startswith(server.WIKISEARCH_URL)]
    assert resolutions == ["how do I research companies?"]
    assert len(website) == len(wiki) == 1
    assert website[0]["queries"] == wiki[0]["queries"] == [
        "how do I research companies?", "research companies", "research employers"]
    assert website[0]["concepts"] == wiki[0]["concepts"] == ["explore"]
    assert result["count"] == 1 and "for_query" not in result["results"][0]


def test_query_variants_are_capped_after_deduplication(monkeypatch):
    hit = {"title": "X", "url": "https://sujeeth.org/x", "snippet": "X",
           "source": "website", "domain": "sujeeth.org", "chunk_id": "x-0"}
    sent = wire(monkeypatch, {"status": "completed", "resolution": resolved("explore"), "results": [hit]})
    monkeypatch.setattr(server, "MAX_QUERIES", 2)
    monkeypatch.setattr(server, "resolve_ontology_plan", lambda query: {
        "resolution": resolved("explore"), "queries": [query, "generated variant"], "error": None})
    result = server.search_sujeeth_foundation.sync("research employers", more_queries=["RESEARCH EMPLOYERS", "third variant"])
    assert result["queries"] == ["research employers", "generated variant"]
    assert len([1 for url, _ in sent if url.endswith("/api/search")]) == 2


def test_a_true_no_match_keeps_the_exact_refusal_without_searching(monkeypatch):
    sent = wire(monkeypatch, {"status": "completed", "results": []})
    monkeypatch.setattr(server, "resolve_ontology_plan", lambda query: {
        "resolution": {"status": "no_match", "matched_concepts": [], "domains": [], "concept_matches": []},
        "queries": [query], "error": None})
    result = server.search_sujeeth_foundation.sync("directions to a bar", more_queries=["gin fizz"])
    assert result["out_of_scope"] is True and result["message"] == server.OUT_OF_SCOPE_MESSAGE
    assert sent == []


def test_agent_guidance_keeps_separate_topics_as_separate_tool_calls():
    assert "For separate topics" in server.SEARCH_DESCRIPTION
    assert "one search request" in server.SEARCH_DESCRIPTION
    assert "separate parts" not in server.SERVER_INSTRUCTIONS

