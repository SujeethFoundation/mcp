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


def urls(result):
    return [r["url"] for r in result["results"]]


def test_the_wiki_is_left_out_when_sujeeth_org_is_not_approved(monkeypatch, gate):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["tealhq.com", "www.themuse.com"]),
                       "results": [{**HIT, "url": "https://www.tealhq.com/post/resume", "domain": "tealhq.com"}]},
         wiki=[WIKI_HIT])
    assert WIKI_HIT["url"] not in urls(search())


def test_the_wiki_is_kept_when_sujeeth_org_is_approved(monkeypatch, gate):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["sujeeth.org", "tealhq.com"]), "results": [HIT]},
         wiki=[WIKI_HIT])
    assert WIKI_HIT["url"] in urls(search())


def test_a_subdomain_of_sujeeth_org_counts(monkeypatch, gate):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["topics.sujeeth.org"]), "results": [HIT]},
         wiki=[WIKI_HIT])
    assert WIKI_HIT["url"] in urls(search())


def test_the_wiki_is_the_fallback_when_the_search_app_is_down(monkeypatch, gate):
    wire(monkeypatch, requests.ConnectionError("down"), wiki=[WIKI_HIT])
    assert WIKI_HIT["url"] in urls(search())


def test_a_wiki_error_is_ignored_when_the_wiki_was_not_wanted(monkeypatch, gate):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["tealhq.com"]),
                       "results": [{**HIT, "url": "https://www.tealhq.com/post/resume", "domain": "tealhq.com"}]},
         wiki=requests.ConnectionError("wiki down"))
    assert search()["status"] == "completed"


def test_the_wiki_is_searched_by_default_even_when_the_ontology_approved_other_domains(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": resolved(["tealhq.com"]), "results": [HIT]}, wiki=[WIKI_HIT])
    assert server.WIKI_NEEDS_FOUNDATION_DOMAIN is False and WIKI_HIT["url"] in urls(search())


def test_a_concept_with_no_approved_domain_still_gets_the_wiki(monkeypatch):
    """"what does each week cover" resolved to the concept "week", which has no domain: the answer was empty."""
    wire(monkeypatch, {"status": "completed", "resolution": {"status": "resolved", "matched_concepts": ["week"], "domains": []},
                       "results": []}, wiki=[WIKI_HIT])
    result = search()
    assert WIKI_HIT["url"] in urls(result) and result["out_of_scope"] is False


def test_a_question_that_matches_no_concept_is_still_out_of_scope(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": {"status": "resolved", "matched_concepts": [], "domains": []},
                       "results": []}, wiki=[WIKI_HIT])
    result = search()
    assert result["out_of_scope"] is True and result["results"] == []

