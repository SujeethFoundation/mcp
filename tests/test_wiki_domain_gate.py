"""The wiki is searched for every question about the Foundation; WIKI_NEEDS_FOUNDATION_DOMAIN=true limits it to those where the
ontology approved sujeeth.org."""
import os

import pytest
import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402
from test_single_search_call import HIT, wire  # noqa: E402

WIKI_HIT = {"title": "Partner FAQ", "url": "https://github.com/SujeethFoundation/sujeethfoundation.github.io/wiki/FAQ-for-Partners",
            "snippet": "Partner FAQ", "source": "wiki", "chunk_id": "FAQ-for-Partners.md"}


def resolved(domains):
    return {"status": "resolved", "matched_concepts": ["resume_resource"], "domains": domains}


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(server, "WIKI_NEEDS_FOUNDATION_DOMAIN", True)


def search():
    return server.search_sujeeth_foundation.sync("resume tips", full_documents=False)


def plan(monkeypatch, domains, concepts=("resume_resource",)):
    def resolved_plan(query):
        return {"resolution": {"status": "resolved" if concepts else "no_match", "ontology_version": "test",
                               "matched_concepts": list(concepts), "domains": list(domains),
                               "concept_matches": [{"concept_id": c, "source": "test_model"} for c in concepts]},
                "queries": [query] if concepts else [], "error": None}
    monkeypatch.setattr(server, "resolve_ontology_plan", resolved_plan)


def urls(result):
    return [r["url"] for r in result["results"]]


def test_the_wiki_is_left_out_when_sujeeth_org_is_not_approved(monkeypatch, gate):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["tealhq.com", "www.themuse.com"]),
                       "results": [{**HIT, "url": "https://www.tealhq.com/post/resume", "domain": "tealhq.com"}]},
         wiki=[WIKI_HIT])
    plan(monkeypatch, ["tealhq.com", "www.themuse.com"])
    assert WIKI_HIT["url"] not in urls(search())


def test_the_wiki_is_kept_when_sujeeth_org_is_approved(monkeypatch, gate):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["sujeeth.org", "tealhq.com"]), "results": [HIT]},
         wiki=[WIKI_HIT])
    plan(monkeypatch, ["sujeeth.org", "tealhq.com"])
    assert WIKI_HIT["url"] in urls(search())


def test_a_subdomain_of_sujeeth_org_counts(monkeypatch, gate):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["topics.sujeeth.org"]), "results": [HIT]},
         wiki=[WIKI_HIT])
    plan(monkeypatch, ["topics.sujeeth.org"])
    assert WIKI_HIT["url"] in urls(search())


def test_a_partner_only_scope_does_not_leak_wiki_results_when_websearch_is_down(monkeypatch, gate):
    wire(monkeypatch, requests.ConnectionError("down"), wiki=[WIKI_HIT])
    plan(monkeypatch, ["tealhq.com"])
    assert WIKI_HIT["url"] not in urls(search())


def test_a_wiki_error_is_ignored_when_the_wiki_was_not_wanted(monkeypatch, gate):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["tealhq.com"]),
                       "results": [{**HIT, "url": "https://www.tealhq.com/post/resume", "domain": "tealhq.com"}]},
         wiki=requests.ConnectionError("wiki down"))
    plan(monkeypatch, ["tealhq.com"])
    assert search()["status"] == "completed"


def test_the_wiki_is_searched_by_default_even_when_the_ontology_approved_other_domains(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["tealhq.com"]), "results": [HIT]}, wiki=[WIKI_HIT])
    plan(monkeypatch, ["tealhq.com"])
    assert server.WIKI_NEEDS_FOUNDATION_DOMAIN is False and WIKI_HIT["url"] in urls(search())


def test_a_concept_with_no_approved_domain_still_gets_the_wiki(monkeypatch):
    """"what does each week cover" resolved to the concept "week", which has no domain: the answer was empty."""
    wire(monkeypatch, {"status": "completed", "resolution": {"status": "resolved", "matched_concepts": ["week"], "domains": []},
                       "results": []}, wiki=[WIKI_HIT])
    plan(monkeypatch, [], concepts=("week",))
    result = search()
    assert WIKI_HIT["url"] in urls(result) and result["out_of_scope"] is False


def test_a_question_that_matches_no_concept_is_still_out_of_scope(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": {"status": "resolved", "matched_concepts": [], "domains": []},
                       "results": []}, wiki=[WIKI_HIT])
    plan(monkeypatch, [], concepts=())
    result = search()
    assert result["out_of_scope"] is True and result["results"] == []

