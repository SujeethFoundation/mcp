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


def test_identical_searches_at_the_same_time_make_one_call(monkeypatch):
    import threading
    import time as _time
    use_store(monkeypatch)
    started, release, calls = threading.Event(), threading.Event(), []

    def fetch():
        calls.append(1)
        started.set()
        release.wait(2)
        return {"results": [HIT]}

    out = []
    first = threading.Thread(target=lambda: out.append(server._one_flight(("k",), fetch)))
    first.start()
    started.wait(2)
    second = threading.Thread(target=lambda: out.append(server._one_flight(("k",), fetch)))
    second.start()
    _time.sleep(0.05)
    release.set()
    first.join(); second.join()
    assert len(calls) == 1 and sorted(joined for _, joined in out) == [False, True]
    assert out[0][0] is not out[1][0]                    # each caller gets its own copy


def test_a_failure_is_shared_too_and_the_next_caller_tries_again():
    import pytest
    def boom():
        raise RuntimeError("down")
    with pytest.raises(RuntimeError):
        server._one_flight(("k2",), boom)
    assert server._one_flight(("k2",), lambda: {"ok": 1}) == ({"ok": 1}, False)


def test_the_search_apps_stage_times_reach_the_timings_but_not_the_table(monkeypatch):
    table = use_store(monkeypatch)
    calls = []

    class Reply:
        status_code = 200
        headers = {"X-Stage-Ms": '{"rank":12,"embed":30}'}

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "completed", "resolution": RESOLVED, "results": [HIT]}

    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    monkeypatch.setattr(server.http, "post", lambda url, headers=None, json=None, timeout=None: calls.append(url) or Reply())
    monkeypatch.setattr(server, "TIMINGS", True)
    out = server.search_sujeeth_foundation.sync("what is career circles")
    flush(server.answer_store)
    assert out["timings_ms"]["websearch_stage_ms"] == {"rank": 12, "embed": 30}
    assert table.rows and all("_stage_ms" not in str(row) for row in table.rows.values())
