"""The wiki is searched for the combined tool only when the ontology approves sujeeth.org for the question."""
import os

import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
from test_single_search_call import HIT, wire  # noqa: E402

WIKI_HIT = {"title": "Partner FAQ", "url": "https://github.com/SujeethFoundation/sujeethfoundation.github.io/wiki/FAQ-for-Partners",
            "snippet": "Partner FAQ", "source": "wiki", "chunk_id": "FAQ-for-Partners.md"}


def resolved(domains):
    return {"status": "resolved", "matched_concepts": ["resume_resource"], "domains": domains}


def search():
    return server.search_sujeeth_foundation.sync("resume tips", full_documents=False)


def urls(result):
    return [r["url"] for r in result["results"]]


def test_the_wiki_is_left_out_when_sujeeth_org_is_not_approved(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["tealhq.com", "www.themuse.com"]),
                       "results": [{**HIT, "url": "https://www.tealhq.com/post/resume", "domain": "tealhq.com"}]},
         wiki=[WIKI_HIT])
    assert WIKI_HIT["url"] not in urls(search())


def test_the_wiki_is_kept_when_sujeeth_org_is_approved(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["sujeeth.org", "tealhq.com"]), "results": [HIT]},
         wiki=[WIKI_HIT])
    assert WIKI_HIT["url"] in urls(search())


def test_a_subdomain_of_sujeeth_org_counts(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["topics.sujeeth.org"]), "results": [HIT]},
         wiki=[WIKI_HIT])
    assert WIKI_HIT["url"] in urls(search())


def test_the_wiki_is_the_fallback_when_the_search_app_is_down(monkeypatch):
    wire(monkeypatch, requests.ConnectionError("down"), wiki=[WIKI_HIT])
    assert WIKI_HIT["url"] in urls(search())


def test_a_wiki_error_is_ignored_when_the_wiki_was_not_wanted(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["tealhq.com"]),
                       "results": [{**HIT, "url": "https://www.tealhq.com/post/resume", "domain": "tealhq.com"}]},
         wiki=requests.ConnectionError("wiki down"))
    assert search()["status"] == "completed"


def test_the_gate_can_be_turned_off(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["tealhq.com"]), "results": [HIT]}, wiki=[WIKI_HIT])
    monkeypatch.setattr(server, "WIKI_NEEDS_FOUNDATION_DOMAIN", False)
    assert WIKI_HIT["url"] in urls(search())
