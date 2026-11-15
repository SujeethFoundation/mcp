"""The resolver reuses validated plans for a short time without confusing query text."""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
resolve_plan = server.resolve_ontology_plan


def catalog(version="test-1"):
    return {
        "ontology_version": version,
        "classes": [{"id": "explore", "name": "Explore"}],
        "predicates": [],
        "relationships": [],
        "domains": [{"domain": "sujeeth.org", "search_enabled": True,
                     "associated_concepts": ["explore"]}],
    }


def test_normalized_repeat_reuses_plan_and_preserves_current_question(monkeypatch):
    server.clear_ontology_plan_cache()
    monkeypatch.setattr(server, "ONTOLOGY_PLAN_CACHE_SECONDS", 300)
    calls = []

    def resolve(question, _catalog):
        calls.append(question)
        return {"matched_concepts": ["explore"], "related_concepts": [],
                "queries": [question, "research employers"],
                "needs_clarification": False, "suggested_question": None}

    monkeypatch.setattr(server, "call_ontology_model", resolve)
    first = resolve_plan("how do I research companies?", catalog())
    second = resolve_plan("  HOW DO I RESEARCH COMPANIES?  ", catalog())

    assert calls == ["how do I research companies?"]
    assert second["queries"][0] == "HOW DO I RESEARCH COMPANIES?"
    assert second["resolution"] == first["resolution"]


def test_plan_cache_is_separated_by_ontology_version(monkeypatch):
    server.clear_ontology_plan_cache()
    monkeypatch.setattr(server, "ONTOLOGY_PLAN_CACHE_SECONDS", 300)
    calls = []

    def resolve(question, _catalog):
        calls.append(question)
        return {"matched_concepts": ["explore"], "related_concepts": [],
                "queries": [question], "needs_clarification": False, "suggested_question": None}

    monkeypatch.setattr(server, "call_ontology_model", resolve)
    resolve_plan("research a company", catalog("test-1"))
    resolve_plan("research a company", catalog("test-2"))
    assert calls == ["research a company", "research a company"]


def test_disabling_plan_cache_calls_model_each_time(monkeypatch):
    server.clear_ontology_plan_cache()
    monkeypatch.setattr(server, "ONTOLOGY_PLAN_CACHE_SECONDS", 0)
    calls = []

    def resolve(question, _catalog):
        calls.append(question)
        return {"matched_concepts": ["explore"], "related_concepts": [],
                "queries": [question], "needs_clarification": False, "suggested_question": None}

    monkeypatch.setattr(server, "call_ontology_model", resolve)
    resolve_plan("research a company", catalog())
    resolve_plan("research a company", catalog())
    assert calls == ["research a company", "research a company"]


def test_simultaneous_identical_questions_share_one_model_call(monkeypatch):
    server.clear_ontology_plan_cache()
    monkeypatch.setattr(server, "ONTOLOGY_PLAN_CACHE_SECONDS", 0)
    started, release = threading.Event(), threading.Event()
    calls = []

    def resolve(question, _catalog):
        calls.append(question)
        started.set()
        assert release.wait(2)
        return {"matched_concepts": ["explore"], "related_concepts": [],
                "queries": [question], "needs_clarification": False, "suggested_question": None}

    monkeypatch.setattr(server, "call_ontology_model", resolve)
    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(resolve_plan, "research a company", catalog())
        assert started.wait(1)
        waiting = [pool.submit(resolve_plan, "research a company", catalog()) for _ in range(3)]
        release.set()
        results = [first.result()] + [future.result() for future in waiting]

    assert calls == ["research a company"]
    assert all(result["resolution"]["matched_concepts"] == ["explore"] for result in results)

def test_compact_catalog_omits_vocabulary_that_the_local_matcher_already_used():
    compact = server.compact_ontology_catalog({
        "classes": [{"id": "explore", "aliases": ["research companies", "employers"],
                     "synonyms": ["Employers", "job roles"],
                     "query_phrases": ["research companies", "compare employers"]}],
        "relationships": [],
        "domains": [],
    })
    concept = compact["classes"][0]
    assert set(concept) == {"id"}


def test_compact_catalog_sends_predicate_behavior_once_without_changing_illustrates_direction():
    behavior = {"enabled": True, "matched_endpoint": "object", "retrieve_endpoint": "subject",
                "traversal_direction": "object_to_subject", "purpose": "Illustrate the matched concept.",
                "interpretation_guardrail": "The subject still illustrates the object."}
    catalog_data = {
        "classes": [{"id": "spotlight", "name": "Spotlight"}, {"id": "explore", "name": "Explore"}],
        "predicates": [{"id": "illustrates", "search_behavior": behavior}],
        "relationships": [{"id": f"spotlight_illustrates_{i}", "subject": "spotlight",
                           "predicate": "illustrates", "object": "explore"} for i in range(20)],
        "domains": [],
    }
    compact = server.compact_ontology_catalog(catalog_data)
    old_edges = [{"subject": "spotlight", "predicate": "illustrates", "object": "explore",
                  "search_behavior": behavior} for _ in range(20)]

    assert len(compact["relationships"]) == 20
    assert all(set(edge) == {"subject", "predicate", "object"} for edge in compact["relationships"])
    assert compact["relationships"][0] == {"subject": "spotlight", "predicate": "illustrates", "object": "explore"}
    rule = compact["predicate_search_behavior"]["illustrates"]
    assert rule["matched_endpoint"] == "object" and rule["retrieve_endpoint"] == "subject"
    assert rule["traversal_direction"] == "object_to_subject"
    assert len(json.dumps(compact, separators=(",", ":"))) < len(json.dumps(old_edges, separators=(",", ":")))


def test_compact_catalog_keeps_relationship_specific_behavior_overrides():
    catalog_data = {
        "classes": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
        "predicates": [{"id": "supports", "search_behavior": {"enabled": True, "effect": "expand"}}],
        "relationships": [{"id": "a_supports_b", "subject": "a", "predicate": "supports", "object": "b",
                           "search_behavior": {"enabled": True, "effect": "none"}}],
        "domains": [],
    }
    compact = server.compact_ontology_catalog(catalog_data)
    assert compact["predicate_search_behavior"]["supports"] == {"enabled": True, "effect": "expand"}
    assert compact["relationships"][0]["search_behavior"] == {"enabled": True, "effect": "none"}


def test_model_context_sends_local_candidates_and_one_hop_relationships_only():
    data = {
        "ontology_version": "test",
        "classes": [
            {"id": "resume", "name": "Resume", "aliases": ["cv"]},
            {"id": "interviewing", "name": "Interviewing"},
            {"id": "networking", "name": "Networking"},
        ],
        "predicates": [{"id": "supports", "search_behavior": {"enabled": True, "effect": "expand"}}],
        "relationships": [{"subject": "resume", "predicate": "supports", "object": "networking"}],
        "domains": [{"domain": "sujeeth.org", "search_enabled": True, "associated_concepts": ["resume"]}],
    }
    context, allowed = server.ontology_model_context("help with my resume", data)

    assert allowed == {"resume", "networking"}
    assert context["packet_type"] == "candidates"
    assert {row["id"] for row in context["classes"]} == {"resume", "networking"}
    assert context["relationships"] == [{"subject": "resume", "predicate": "supports", "object": "networking"}]
    assert "search_domains" not in context


def test_unknown_wording_falls_back_to_the_full_ontology_packet():
    data = catalog()
    data["classes"].append({"id": "networking", "name": "Networking"})
    context, allowed = server.ontology_model_context("unrelated wording", data)

    assert allowed is None
    assert context["packet_type"] == "full_fallback"
    assert {row["id"] for row in context["classes"]} == {"explore", "networking"}
    assert context["relationships"] == []


def test_single_word_overlap_selects_a_longer_concept_name():
    data = catalog()
    data["classes"] = [{"id": "resume_resource", "name": "Resume Resource"}]

    context, allowed = server.ontology_model_context("help improve my resume", data)

    assert allowed == {"resume_resource"}
    assert context["packet_type"] == "candidates"


def test_candidate_packet_cannot_validate_an_unsupplied_concept():
    data = catalog()
    data["classes"].append({"id": "networking", "name": "Networking"})
    result = server.validate_ontology_plan(
        {"matched_concepts": ["networking"], "related_concepts": [], "queries": [],
         "needs_clarification": False, "suggested_question": None},
        "resume help", data, allowed_ids={"explore"},
    )

    assert result["resolution"]["status"] == "no_match"
