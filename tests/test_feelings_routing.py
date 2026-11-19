"""Emotion routing uses the ontology's deterministic Feelings API, not generic expansion."""
import os

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402

resolve_plan = server.resolve_ontology_plan


def catalog():
    return {
        "ontology_version": "feelings-test",
        "classes": [
            {"id": "interviewing", "name": "Interviewing"},
            {"id": "nervous", "name": "Nervous", "mood_meter": {"quadrant": "red"}},
            {"id": "emotional_regulation", "name": "Emotional Regulation"},
            {"id": "permission_to_feel", "name": "Permission to Feel"},
        ],
        "predicates": [{"id": "illustrates", "search_behavior": {"enabled": True, "effect": "expand"}}],
        "relationships": [
            {"subject": "emotional_regulation", "predicate": "illustrates", "object": "nervous",
             "retrieval": {"search_expansion": "feelings_api_only", "general_search_expand": False,
                           "intent_tags": ["regulation"]}},
            {"subject": "permission_to_feel", "predicate": "illustrates", "object": "nervous",
             "retrieval": {"search_expansion": "feelings_api_only", "general_search_expand": False,
                           "intent_tags": ["recognition"]}},
        ],
        "domains": [
            {"domain": "feelings.example", "search_enabled": True,
             "associated_concepts": ["nervous", "emotional_regulation", "permission_to_feel"]},
            {"domain": "career.example", "search_enabled": True, "associated_concepts": ["interviewing"]},
        ],
    }


def model_result(matched, feelings=None, intent=None, related=None):
    return {"matched_concepts": matched, "related_concepts": related or [], "feelings": feelings or [],
            "intent": intent, "queries": ["help"], "needs_clarification": False,
            "suggested_question": None}


def test_career_only_plan_never_calls_feelings_api(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "post_json", lambda *args, **kwargs: calls.append(args) or {})

    plan = server.add_feelings_frameworks(
        server.validate_ontology_plan(model_result(["interviewing"]), "interview help", catalog()), catalog())

    assert calls == []
    assert plan["resolution"]["matched_concepts"] == ["interviewing"]
    assert "feelings_route" not in plan["resolution"]


def test_feeling_only_plan_calls_api_and_uses_its_ranked_frameworks(monkeypatch):
    seen = []
    monkeypatch.setattr(server, "post_json", lambda url, payload, key: seen.append((url, payload, key)) or {
        "frameworks": [{"framework_id": "emotional_regulation", "framework_name": "Emotional Regulation",
                        "matched_feelings": ["nervous"], "intent_match": True, "ranking_score": 1.81}],
    })

    plan = server.add_feelings_frameworks(
        server.validate_ontology_plan(model_result(["nervous"], ["nervous"], "regulation"), "I am nervous", catalog()),
        catalog(),
    )

    assert seen[0][0].endswith("/api/ontology/feelings/resolve")
    assert seen[0][1] == {"feelings": ["nervous"], "intent": "regulation", "max_results": 3}
    assert plan["resolution"]["matched_concepts"] == ["nervous", "emotional_regulation"]
    assert plan["resolution"]["concept_matches"][-1] == {"concept_id": "emotional_regulation", "source": "feelings_api"}
    assert plan["resolution"]["feelings_route"]["status"] == "resolved"


def test_mixed_question_merges_ranked_framework_with_career_concepts(monkeypatch):
    monkeypatch.setattr(server, "post_json", lambda *args, **kwargs: {
        "frameworks": [{"framework_id": "emotional_regulation", "ranking_score": 1.81}],
    })
    plan = server.add_feelings_frameworks(
        server.validate_ontology_plan(
            model_result(["interviewing", "nervous"], ["nervous"], "regulation"),
            "I am nervous about my interview", catalog(),
        ),
        catalog(),
    )

    assert plan["resolution"]["matched_concepts"] == ["interviewing", "nervous", "emotional_regulation"]
    assert plan["resolution"]["domains"] == ["feelings.example", "career.example"]


def test_feelings_only_edges_cannot_be_used_by_generic_related_expansion():
    plan = server.validate_ontology_plan(
        model_result(["nervous"], ["nervous"], "regulation", related=["emotional_regulation"]),
        "I am nervous", catalog(),
    )

    assert plan["resolution"]["matched_concepts"] == ["nervous"]


def test_resolver_calls_feelings_api_once_and_caches_the_enriched_plan(monkeypatch):
    server.clear_ontology_plan_cache()
    monkeypatch.setattr(server, "ONTOLOGY_PLAN_CACHE_SECONDS", 300)
    monkeypatch.setattr(server, "ontology_model_context", lambda query, data: (data, None))
    monkeypatch.setattr(server, "call_ontology_model", lambda query, data: model_result(
        ["nervous"], ["nervous"], "regulation"))
    calls = []
    monkeypatch.setattr(server, "post_json", lambda *args, **kwargs: calls.append(args) or {
        "frameworks": [{"framework_id": "emotional_regulation", "ranking_score": 1.81}],
    })

    route_catalog = {**catalog(), "ontology_version": "feelings-route-cache"}
    first = resolve_plan("I am nervous", route_catalog)
    second = resolve_plan("  I AM NERVOUS ", route_catalog)

    assert len(calls) == 1
    assert first["resolution"]["matched_concepts"] == second["resolution"]["matched_concepts"]


def test_unavailable_feelings_api_returns_a_clear_degraded_route(monkeypatch):
    monkeypatch.setattr(server, "post_json", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    plan = server.add_feelings_frameworks(
        server.validate_ontology_plan(model_result(["nervous"], ["nervous"], "regulation"), "I am nervous", catalog()),
        catalog(),
    )

    assert plan["resolution"]["matched_concepts"] == ["nervous"]
    assert plan["resolution"]["feelings_route"]["degraded"] is True
