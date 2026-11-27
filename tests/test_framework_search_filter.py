"""A feelings framework the API did not tie to the question's intent guides the agent; it does not filter or rank the search."""
import os

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
from tests.test_feelings_routing import catalog  # noqa: E402


def frameworks(*rows):
    return {"frameworks": [{"framework_id": fid, "framework_name": fid, "matched_feelings": ["nervous"],
                            "intent_match": intent, "ranking_score": 0.9, "guardrail": "g"} for fid, intent in rows]}


def enrich(monkeypatch, direct, reply):
    monkeypatch.setattr(server, "post_json", lambda *args, **kwargs: reply)
    plan = {"resolution": {"matched_concepts": list(direct), "feelings": ["nervous"], "intent": None, "domains": [],
                           "concept_matches": []}}
    return server.add_feelings_frameworks(plan, catalog())["resolution"]


def test_frameworks_without_an_intent_match_do_not_filter_the_search(monkeypatch):
    resolution = enrich(monkeypatch, ["interviewing"],
                        frameworks(("permission_to_feel", False), ("emotional_regulation", False)))
    assert resolution["matched_concepts"] == ["interviewing", "permission_to_feel", "emotional_regulation"]
    assert resolution["search_concepts"] == ["interviewing"]
    assert resolution["domains"] == ["career.example"]
    assert [row["framework_id"] for row in resolution["feelings_route"]["frameworks"]] == [
        "permission_to_feel", "emotional_regulation"]   # the agent still sees them and their guardrails
    assert server.search_concepts(resolution) == ["interviewing"]


def test_a_framework_with_an_intent_match_still_joins_the_search(monkeypatch):
    resolution = enrich(monkeypatch, ["interviewing"],
                        frameworks(("emotional_regulation", True), ("permission_to_feel", False)))
    assert resolution["search_concepts"] == ["interviewing", "emotional_regulation"]
    assert sorted(resolution["domains"]) == ["career.example", "feelings.example"]


def test_a_feelings_only_question_keeps_its_frameworks_to_search_on(monkeypatch):
    resolution = enrich(monkeypatch, [], frameworks(("permission_to_feel", False)))
    assert resolution["search_concepts"] == ["permission_to_feel"]
    assert resolution["domains"] == ["feelings.example"]


def test_search_concepts_falls_back_to_matched_concepts_when_not_set():
    assert server.search_concepts({"matched_concepts": ["a", "b"]}) == ["a", "b"]
    assert server.search_concepts({}) == []


def test_the_scope_decision_still_counts_the_frameworks(monkeypatch):
    resolution = enrich(monkeypatch, [], frameworks(("permission_to_feel", False)))
    reasons = server.assess_quality(resolution, [], [])
    assert not reasons.get("out_of_scope")
