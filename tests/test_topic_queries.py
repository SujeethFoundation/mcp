"""MCP_TOPIC_QUERIES: how many topic wordings go to the search apps (default 1, the model's first rewrite)."""
import os

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
from test_single_search_call import HIT, wire  # noqa: E402

PLAN = {"resolution": {"status": "resolved", "ontology_version": "test", "matched_concepts": ["explore"],
                       "domains": ["sujeeth.org"], "concept_matches": [{"concept_id": "explore", "source": "azure_model"}],
                       "query_plan": {"topic_queries": ["review my resume and suggest improvements please",
                                                        "improve resume structure", "tailor resume keywords"],
                                      "feeling_query": None}},
        "queries": ["review my resume and suggest improvements please", "improve resume structure",
                    "tailor resume keywords"], "error": None}
QUESTION = "review my resume and suggest improvements please"


def test_one_topic_query_is_the_models_first_rewrite():
    assert server._queries_to_search(QUESTION, PLAN["queries"], PLAN) == ["improve resume structure"]


def test_the_feeling_query_is_added_when_the_plan_has_one():
    plan = {**PLAN, "resolution": {**PLAN["resolution"], "query_plan": {
        **PLAN["resolution"]["query_plan"], "feeling_query": "managing nervousness about resumes"}}}
    assert server._queries_to_search(QUESTION, PLAN["queries"], plan) == [
        "improve resume structure", "managing nervousness about resumes"]


def test_no_rewrite_means_the_question_itself():
    plan = {"resolution": {"query_plan": {"topic_queries": [QUESTION]}}}
    assert server._queries_to_search(QUESTION, [QUESTION, "agent wording"], plan) == [QUESTION]


def test_more_or_zero_keep_the_others(monkeypatch):
    monkeypatch.setattr(server, "TOPIC_QUERIES", 2)
    assert server._queries_to_search(QUESTION, PLAN["queries"], PLAN) == ["improve resume structure", "tailor resume keywords"]
    monkeypatch.setattr(server, "TOPIC_QUERIES", 0)
    assert server._queries_to_search(QUESTION, PLAN["queries"], PLAN) == PLAN["queries"]


def test_the_search_app_gets_one_query(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "results": [HIT]})
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 0)
    monkeypatch.setattr(server, "resolve_ontology_plan", lambda query: PLAN)
    out = server.search_sujeeth_foundation.sync(QUESTION)
    sent = [body for url, body in calls if url.endswith("/api/search")]
    assert len(sent) == 1 and sent[0]["query"] == "improve resume structure" and sent[0]["queries"] == ["improve resume structure"]
    assert out["query"] == QUESTION and out["results"]
