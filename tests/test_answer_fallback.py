"""When the search app fails, answer from the stored search of the nearest earlier question."""
import os

import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
from plan_similarity import DIMS, SimilarPlans, prefix_unit  # noqa: E402
from plan_store import PlanStore  # noqa: E402
from test_plan_similarity import FakeEmbedder  # noqa: E402
from test_plan_store import FakeTable  # noqa: E402
from test_single_search_call import HIT, wire  # noqa: E402

RES = {"status": "resolved", "ontology_version": "test", "matched_concepts": ["explore"], "domains": ["sujeeth.org"],
       "resolved_by": "model", "concept_matches": [{"concept_id": "explore", "source": "azure_model"}]}
EARLIER = {"resolution": RES, "queries": ["how do i network", "networking skills"], "error": None}


def e(x, y):
    return prefix_unit([x, y] + [0.0] * (DIMS - 2))


def service_down(status=503):
    response = requests.Response()
    response.status_code = status
    response._content = b'{"stage": "index_loading"}'
    return requests.HTTPError(response=response)


def setup(monkeypatch, near_vector, mine, answer=True):
    calls = wire(monkeypatch, service_down())
    table = FakeTable()
    store = PlanStore("x", "t", 600, table=table)
    monkeypatch.setattr(server, "answer_store", store)
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 0)
    scope = ("how to network", "test", server.ONTOLOGY_MODEL_NAME, ())
    key = server._ontology_plan_cache_key("how to network", server.ontology_catalog())
    plans = SimilarPlans()
    plans.add((key[1], key[2], key[3]), near_vector, EARLIER)
    monkeypatch.setattr(server, "similar_plans", plans)
    monkeypatch.setattr(server, "plan_embedder", FakeEmbedder({"how to network": mine}))
    if answer:
        store.put(server._answer_key("how do i network", EARLIER["queries"], ["explore"], ["sujeeth.org"], RES,
                                     server.RESULT_COUNT), {"status": "completed", "results": [HIT]}, wait=True)
    plan = {"resolution": RES, "queries": ["how to network"], "error": None}
    monkeypatch.setattr(server, "resolve_ontology_plan", lambda query: plan)
    return calls


def test_a_failing_search_app_is_not_called_again_when_a_close_answer_is_stored(monkeypatch):
    calls = setup(monkeypatch, e(1.0, 0.0), e(1.0, 0.3))                # cosine about 0.96
    out = server.search_sujeeth_foundation.sync("how to network")
    assert len(calls) == 1 and out["results"] and out["retrieval"]["answer_from_similar"] > 0.9


def test_the_bar_is_lower_after_a_second_failure(monkeypatch):
    mine = e(1.0, 0.55)                                                    # cosine about 0.88: under 0.90, over 0.85
    calls = setup(monkeypatch, e(1.0, 0.0), mine)
    out = server.search_sujeeth_foundation.sync("how to network")
    assert len(calls) == 2 and out["retrieval"]["answer_from_similar"] < 0.9


def test_too_far_or_nothing_stored_means_the_usual_retries_and_failure(monkeypatch):
    calls = setup(monkeypatch, e(1.0, 0.0), e(1.0, 1.5))                  # cosine about 0.55
    out = server.search_sujeeth_foundation.sync("how to network")
    assert len(calls) > 2 and not out["results"] and "answer_from_similar" not in out["retrieval"]
    calls = setup(monkeypatch, e(1.0, 0.0), e(1.0, 0.3), answer=False)
    out = server.search_sujeeth_foundation.sync("how to network")
    assert len(calls) > 2 and not out["results"]


def test_one_try_of_the_search_app_call_is_capped_but_not_the_overall_budget(monkeypatch):
    seen = []

    def post(url, payload, key="", timeout=None):
        seen.append(round(timeout))
        raise requests.Timeout("slow")

    monkeypatch.setattr(server, "post_json", post)
    monkeypatch.setattr(server, "_sleep", lambda s: None)
    clock = iter(range(0, 1000, 8))                      # each look at the clock is 8 s later
    monkeypatch.setattr(server, "_clock", lambda: next(clock))
    try:
        server.post_json_with_retry("https://x/api/search", {"queries": ["a"]}, try_timeout=10)
    except requests.Timeout:
        pass
    assert seen and max(seen) == 10 and server.WEBSEARCH_TRY_TIMEOUT == 10
