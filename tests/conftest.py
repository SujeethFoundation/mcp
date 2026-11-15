import sys

import pytest


@pytest.fixture(autouse=True)
def _fresh_search_cache(monkeypatch):
    """Tests start with the search cache off and empty, so one search's answer is never served to another.
    tests/test_search_cache.py turns it on for itself."""
    server = sys.modules.get("mcp_server")
    if server is not None:
        monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 0)
        def model_plan(query, catalog=None):
            text = query.lower()
            if any(term in text for term in ("vodka", "gin fizz", "directions to a bar", "weather")):
                return {"resolution": {"status": "no_match", "ontology_version": "test", "matched_concepts": [],
                                       "domains": [], "concept_matches": []}, "queries": [query], "error": None}
            concept = ("resume_resource" if "resume" in text else "interviewing" if "interview" in text else
                       "agenda" if "agenda" in text or "week" in text else "explore" if any(t in text for t in
                       ("company", "companies", "employer", "role", "career research")) else "volunteer")
            return {"resolution": {"status": "resolved", "ontology_version": "test",
                                   "matched_concepts": [concept], "domains": ["sujeeth.org"],
                                   "concept_matches": [{"concept_id": concept, "source": "test_model"}]},
                    "queries": [query], "error": None}
        monkeypatch.setattr(server, "resolve_ontology_plan", model_plan)
        server.clear_search_cache()
    yield
