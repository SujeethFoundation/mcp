"""The shared plan table: a plan another instance stored is used instead of calling the ontology model."""
import os
import time

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
from plan_store import PlanStore  # noqa: E402

resolve_plan = server.resolve_ontology_plan   # the real one: conftest replaces it on the module for other tests


class FakeTable:
    def __init__(self):
        self.rows, self.fail = {}, False

    def get_entity(self, partition, row):
        if self.fail:
            raise RuntimeError("down")
        if (partition, row) not in self.rows:
            class ResourceNotFoundError(Exception):
                pass
            raise ResourceNotFoundError()
        return dict(self.rows[(partition, row)])

    def upsert_entity(self, entity, mode=None):
        if self.fail:
            raise RuntimeError("down")
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)


def store(table, ttl=600):
    return PlanStore("x", "t", ttl, table=table)


KEY = ("how do i network", "v1", "model", ())


def test_a_plan_put_by_one_store_is_read_by_another():
    table = FakeTable()
    store(table).put(KEY, {"queries": ["a", "b"], "resolution": {"matched_concepts": ["c"]}}, wait=True)
    got = store(table).get(KEY)
    assert got["queries"] == ["a", "b"] and got["resolution"]["matched_concepts"] == ["c"]


def test_a_different_key_or_an_old_row_misses():
    table = FakeTable()
    s = store(table)
    s.put(KEY, {"queries": []}, wait=True)
    assert s.get(("other",) + KEY[1:]) is None
    for row in table.rows.values():
        row["ts"] = int(time.time()) - 5000
    assert s.get(KEY) is None


def test_a_failing_table_is_skipped_not_raised():
    table = FakeTable()
    table.fail = True
    s = store(table)
    assert s.get(KEY) is None and s.get(KEY) is None          # the second read is skipped during the cooldown
    assert s.stats["errors"] == 1 and s.stats["skipped"] == 1
    s.put(KEY, {"queries": []}, wait=True)                    # dropped while down


def test_off_without_a_connection_or_ttl():
    assert not PlanStore("", "t", 600).enabled and not PlanStore("x", "t", 0).enabled


def test_resolve_uses_a_stored_plan_without_calling_the_model(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(server, "plan_store", store(table))
    server.clear_ontology_plan_cache()
    catalog = {"classes": [], "ontology_version": "v1"}
    monkeypatch.setattr(server, "local_ontology_plan", lambda *a, **k: None)
    key = server._ontology_plan_cache_key("how do i network", catalog, {})
    monkeypatch.setattr(server, "clean_retrieval_context", lambda ctx, cat: {})
    server.plan_store.put(key, {"queries": ["x"], "resolution": {"resolved_by": "model"}}, wait=True)

    def boom(*a, **k):
        raise AssertionError("the model was called")
    monkeypatch.setattr(server, "call_ontology_model", boom)
    plan = resolve_plan("how do i network", catalog)
    assert plan["resolution"]["resolved_by"] == "model" and "x" in plan["queries"]
