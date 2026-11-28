"""A website search paid for once is shared by questions with the same plan, across instances."""
import os

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
from plan_store import PlanStore  # noqa: E402
from test_plan_store import FakeTable  # noqa: E402
from test_single_search_call import HIT, RESOLVED, wire  # noqa: E402


def use_store(monkeypatch, table=None):
    table = table if table is not None else FakeTable()
    monkeypatch.setattr(server, "answer_store", PlanStore("x", "t", 600, table=table))
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 0)
    return table


def flush(store):
    if store._writer is not None:
        store._writer.shutdown(wait=True)
        store._writer = None


def test_the_key_follows_the_plan_not_the_wording():
    a = server._answer_key("how do i network", ["how do i network", "networking skills"], ["c1"], ["d"], {}, 5)
    b = server._answer_key("how to network", ["how to network", "networking skills"], ["c1"], ["d"], {}, 5)
    c = server._answer_key("how to network", ["how to network", "other topic"], ["c1"], ["d"], {}, 5)
    assert a == b and a != c


def test_a_plan_with_no_extra_queries_is_keyed_by_the_question():
    a = server._answer_key("what is x", ["what is x"], ["c1"], ["d"], {}, 5)
    b = server._answer_key("what is y", ["what is y"], ["c1"], ["d"], {}, 5)
    assert a != b


def test_a_second_instance_reads_the_answer_instead_of_searching(monkeypatch):
    table = use_store(monkeypatch)
    calls = wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    server.search_sujeeth_foundation.sync("what is career circles")
    flush(server.answer_store)
    assert len(calls) == 1 and table.rows
    use_store(monkeypatch, table)                      # another instance, same table
    again = server.search_sujeeth_foundation.sync("what is career circles")
    assert len(calls) == 1 and again["results"]


def test_nothing_is_stored_when_off_or_when_the_search_failed(monkeypatch):
    table = use_store(monkeypatch)
    monkeypatch.setattr(server, "answer_store", PlanStore("x", "t", 0, table=table))
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    server.search_sujeeth_foundation.sync("what is career circles")
    assert not table.rows
    table = use_store(monkeypatch)
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT], "search_mode": "keyword_only"})
    server.search_sujeeth_foundation.sync("what is career circles")
    flush(server.answer_store)
    assert not table.rows
