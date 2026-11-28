"""MCP tells the search app which domains hold hand-added pages, so the search app's search path never asks the ontology."""
import os

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
from test_single_search_call import HIT, RESOLVED, wire  # noqa: E402

CATALOG = {"domains": [{"domain": "app.candid.org", "manual_only": True}, {"domain": "sujeeth.org", "manual_only": False},
                       {"domain": "portal.sujeeth.org", "manual_only": True}]}


def test_only_selected_manual_domains_are_sent():
    assert server.best_bet_domains(["sujeeth.org", "App.Candid.org"], CATALOG) == ["app.candid.org"]
    assert server.best_bet_domains(["sujeeth.org"], CATALOG) == []


def test_a_catalog_that_does_not_say_sends_nothing():
    assert server.best_bet_domains(["app.candid.org"], {"domains": [{"domain": "app.candid.org"}]}) is None
    assert server.best_bet_domains(["app.candid.org"], None) is None


def test_the_search_request_carries_them(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    monkeypatch.setattr(server, "SEARCH_CACHE_SECONDS", 0)
    monkeypatch.setattr(server, "best_bet_domains", lambda domains, catalog=None, **k: ["app.candid.org"])
    server.search_sujeeth_foundation.sync("what is career circles")
    sent = [body for url, body in calls if url.endswith("/api/search")]
    assert sent and sent[0]["best_bet_domains"] == ["app.candid.org"]


def test_the_domains_read_from_the_ontology_app_keep_manual_only(monkeypatch):
    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"domains": [{"domain": "app.candid.org", "manual_only": True, "associated_concepts": []},
                                {"domain": "sujeeth.org", "associated_concepts": ["x"]}]}

    monkeypatch.setattr(server.requests, "get", lambda *a, **k: Reply())
    rows = server.fetch_search_domains(1)
    assert [r["manual_only"] for r in rows] == [True, False]
