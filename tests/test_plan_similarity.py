"""Reusing the plan of a similar earlier question, and measuring how often that would have been right."""
import os

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import pytest  # noqa: E402

import mcp_server as server  # noqa: E402
from plan_similarity import Embedder, SimilarPlans, cosine, prefix_unit, reusable  # noqa: E402

resolve_plan = server.resolve_ontology_plan   # the real one: conftest replaces it on the module for other tests


def good_plan(concepts=("network",)):
    return {"resolution": {"resolved_by": "model", "matched_concepts": list(concepts), "domains": ["sujeeth.org"]},
            "queries": ["a", "b"], "error": None}


def test_prefix_unit_makes_unit_vectors_and_cosine_is_one_for_the_same():
    v = prefix_unit([3.0, 4.0] + [0.0] * 10)
    assert abs(cosine(v, v) - 1.0) < 1e-9


def test_nearest_only_looks_in_its_own_scope():
    store = SimilarPlans()
    a = prefix_unit([1.0, 0.0, 0.0])
    store.add(("v1", "m", ()), a, good_plan())
    assert store.nearest(("v2", "m", ()), a) == (0.0, None)
    sim, plan = store.nearest(("v1", "m", ()), a)
    assert sim > 0.999 and plan is not None


def test_reusable_refuses_out_of_scope_clarifying_and_degraded_plans():
    assert reusable(good_plan())
    assert not reusable({"resolution": {"resolved_by": "model", "matched_concepts": []}})
    assert not reusable({"resolution": {"resolved_by": "ontology", "matched_concepts": ["x"]}})
    assert not reusable({"resolution": {"resolved_by": "model", "matched_concepts": ["x"], "needs_clarification": True}})
    assert not reusable({"resolution": {"resolved_by": "model", "matched_concepts": ["x"], "feelings_route": {"degraded": True}}})


def test_the_embedder_is_off_without_settings_and_survives_failures():
    assert Embedder("", "", "").embed("x") is None

    class Boom:
        def post(self, *a, **k):
            raise RuntimeError("down")
    e = Embedder("https://e", "k", "dep", http=Boom())
    assert e.embed("x") is None and e.embed("x") is None      # the second is skipped during the cooldown


class FakeEmbedder:
    enabled = True

    def __init__(self, vectors):
        self.vectors, self.calls = vectors, []

    def embed(self, text):
        self.calls.append(text)
        return self.vectors[text]


def setup_model(monkeypatch, calls):
    catalog = {"classes": [], "ontology_version": "v1"}
    monkeypatch.setattr(server, "local_ontology_plan", lambda *a, **k: None)
    monkeypatch.setattr(server, "clean_retrieval_context", lambda ctx, cat: {})
    monkeypatch.setattr(server, "ontology_model_context", lambda *a, **k: ({}, set()))
    monkeypatch.setattr(server, "validate_ontology_plan", lambda result, query, *a, **k: {
        "resolution": {"matched_concepts": ["network"], "domains": ["sujeeth.org"]}, "queries": [query, "x"], "error": None})
    monkeypatch.setattr(server, "add_feelings_frameworks", lambda plan, cat: plan)
    monkeypatch.setattr(server, "call_ontology_model", lambda query, ctx, *a: calls.append(query) or {})
    monkeypatch.setattr(server, "similar_plans", SimilarPlans())
    monkeypatch.setattr(server, "ONTOLOGY_PLAN_CACHE_SECONDS", 0)
    monkeypatch.setattr(server.plan_store, "_ttl", 0)
    server.clear_ontology_plan_cache()
    return catalog


def test_shadow_mode_measures_but_still_calls_the_model(monkeypatch, capsys):
    calls = []
    catalog = setup_model(monkeypatch, calls)
    near = prefix_unit([1.0, 0.1, 0.0])
    monkeypatch.setattr(server, "plan_embedder", FakeEmbedder({"how do i network": prefix_unit([1.0, 0.0, 0.0]),
                                                               "how to network": near}))
    monkeypatch.setattr(server, "PLAN_REUSE_SIMILARITY", 0.0)
    resolve_plan("how do i network", catalog)
    resolve_plan("how to network", catalog)
    assert calls == ["how do i network", "how to network"]
    assert "plan_similarity sim=0.99" in capsys.readouterr().err


@pytest.mark.parametrize("threshold,expect_calls", [(0.95, 1), (0.999, 2)])
def test_reuse_borrows_a_close_plan_and_keeps_the_new_wording_first(monkeypatch, threshold, expect_calls):
    calls = []
    catalog = setup_model(monkeypatch, calls)
    monkeypatch.setattr(server, "plan_embedder", FakeEmbedder({"how do i network": prefix_unit([1.0, 0.0, 0.0]),
                                                               "how to network": prefix_unit([1.0, 0.1, 0.0])}))
    monkeypatch.setattr(server, "PLAN_REUSE_SIMILARITY", threshold)
    resolve_plan("how do i network", catalog)
    plan = resolve_plan("how to network", catalog)
    assert len(calls) == expect_calls
    if expect_calls == 1:
        assert plan["resolution"]["resolved_by"] == "similar" and plan["queries"][0] == "how to network"


class FakeVectorTable:
    def __init__(self):
        self.rows = {}

    def upsert_entity(self, entity, mode=None):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def query_entities(self, query_filter):
        part = query_filter.split("'")[1]
        return [dict(v) for (p, _), v in self.rows.items() if p == part]


def test_a_vector_stored_by_one_instance_is_found_by_another():
    from plan_similarity import DIMS, SharedVectors
    table = FakeVectorTable()
    scope = ("v1", "m", ())
    near = prefix_unit([1.0, 0.1] + [0.0] * (DIMS - 2))
    SharedVectors("x", "t", 600, table=table).put(scope, prefix_unit([1.0, 0.0] + [0.0] * (DIMS - 2)), ("pk", "rk"), wait=True)
    reader = SharedVectors("x", "t", 600, table=table)
    similarity, ref = reader.nearest(scope, near)
    assert similarity > 0.99 and ref == ("pk", "rk")
    assert reader.nearest(("v2", "m", ()), near) == (0.0, None)          # another scope is another partition
    assert not SharedVectors("x", "t", 0).enabled


def test_a_failing_table_gives_no_rows_instead_of_an_error():
    from plan_similarity import SharedVectors

    class Down:
        def query_entities(self, query_filter):
            raise RuntimeError("down")
    reader = SharedVectors("x", "t", 600, table=Down())
    assert reader.rows(("v1", "m", ())) == [] and reader.stats["errors"] == 1


def test_reuse_finds_a_plan_another_instance_stored(monkeypatch):
    from plan_similarity import DIMS, SharedVectors
    from plan_store import PlanStore
    from test_plan_store import FakeTable as FakePlanTable
    calls = []
    catalog = setup_model(monkeypatch, calls)
    plans, vectors = FakePlanTable(), FakeVectorTable()
    base = prefix_unit([1.0, 0.0] + [0.0] * (DIMS - 2))
    close = prefix_unit([1.0, 0.1] + [0.0] * (DIMS - 2))
    monkeypatch.setattr(server, "plan_embedder", FakeEmbedder({"how do i network": base, "how to network": close}))
    monkeypatch.setattr(server, "PLAN_REUSE_SIMILARITY", 0.95)
    monkeypatch.setattr(server, "plan_store", PlanStore("x", "t", 600, table=plans))
    monkeypatch.setattr(server, "shared_vectors", SharedVectors("x", "t", 600, refresh=0, table=vectors))
    resolve_plan("how do i network", catalog)
    for _ in range(100):
        if vectors.rows and plans.rows:
            break
        import time
        time.sleep(0.02)
    monkeypatch.setattr(server, "similar_plans", SimilarPlans())          # a different instance: nothing in its memory
    server.clear_ontology_plan_cache()
    plan = resolve_plan("how to network", catalog)
    assert calls == ["how do i network"] and plan["resolution"]["resolved_by"] == "similar"
