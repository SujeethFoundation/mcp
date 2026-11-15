"""The resolver reuses validated plans for a short time without confusing query text."""
import os

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

def test_compact_catalog_removes_only_duplicate_vocabulary_terms():
    compact = server.compact_ontology_catalog({
        "classes": [{"id": "explore", "aliases": ["research companies", "employers"],
                     "synonyms": ["Employers", "job roles"],
                     "query_phrases": ["research companies", "compare employers"]}],
        "relationships": [],
        "domains": [],
    })
    concept = compact["classes"][0]
    vocabulary = [term for field in ("aliases", "synonyms", "query_phrases")
                  for term in concept.get(field, [])]
    assert vocabulary == ["research companies", "employers", "job roles", "compare employers"]
