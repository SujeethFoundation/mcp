"""One request to the Search App does the ontology resolution and the search."""
import json as jsonlib
import os

import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402

RESOLVED = {"status": "resolved", "ontology_version": "1.10.0", "matched_concepts": ["volunteer", "spotlight"],
            "concept_matches": [{"concept_id": "volunteer", "confidence": 1.2, "source": "query_match"},
                                {"concept_id": "spotlight", "confidence": None, "source": "relationship"}],
            "domains": ["sujeeth.org"]}
HIT = {"title": "Volunteer", "url": "https://sujeeth.org/volunteer", "snippet": "Become a mentor.",
       "source": "website", "domain": "sujeeth.org", "chunk_id": "v-0"}


def reply(body, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = jsonlib.dumps(body).encode()
    return response


def wire(monkeypatch, website, wiki=None):
    """website: the Search App's answer (a dict) or an exception; wiki: a list of wiki results or an exception."""
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append((url, json))
        if isinstance(website, Exception):
            raise website
        return reply(website)

    def get_json(url, key="", params=None):
        if isinstance(wiki, Exception):
            raise wiki
        return {"results": wiki or []}

    monkeypatch.setattr(server.http, "post", post)
    monkeypatch.setattr(server, "get_json", get_json)
    monkeypatch.setattr(server, "WIKI_FULL_TEXT", False)
    monkeypatch.setattr(server, "_sleep", lambda seconds: None)      # a failing Search App is retried: do not wait for it
    return calls


def search(query="how do I volunteer"):
    return server.search_sujeeth_foundation.sync(query, full_documents=False)


def test_a_search_makes_one_request_to_the_search_app(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    search()
    assert len(calls) == 1 and calls[0][0].endswith("/api/search")
    assert not any(url.endswith("/api/domain-resolution") for url, _ in calls)


def test_the_request_names_no_concepts_so_none_count_as_explicit(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    search()
    body = calls[0][1]
    assert "concepts" not in body and "domains" not in body
    assert body["query"] == "how do I volunteer" and body["limit"] == server.CANDIDATE_POOL


def test_the_candidate_pool_is_what_the_search_app_will_return():
    assert server.CANDIDATE_POOL == 20


def test_the_ontology_block_is_the_search_apps_resolution(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    out = search()
    assert out["ontology"]["matched_concepts"] == ["volunteer", "spotlight"]
    assert out["ontology"]["domains"] == ["sujeeth.org"] and out["ontology"]["ontology_version"] == "1.10.0"
    assert out["needs_clarification"] is False and out["status"] == "completed"


NO_MATCH = {"status": "no_ontology_concept_match", "concepts": [], "domains": [], "count": 0, "results": [],
            "resolution": {"status": "no_match", "matched_concepts": [], "domains": [], "concept_matches": []}}
WIKI_HIT = {"title": "Donor FAQ", "url": "https://github.com/x/wiki/FAQ-for-Donors", "snippet": "Giving", "source": "wiki",
            "path": "FAQ.md", "chunk_id": "FAQ.md"}
REFUSAL = "I can only help with information about the Foundation or services from the Foundation."


def test_a_question_with_no_concept_is_refused_not_clarified(monkeypatch):
    wire(monkeypatch, NO_MATCH)
    out = search("what is the weather")
    assert out["out_of_scope"] is True and out["status"] == "out_of_scope" and out["message"] == REFUSAL
    assert out["needs_clarification"] is False and out["suggested_question"] is None
    assert out["results"] == [] and out["count"] == 0
    assert REFUSAL in out["agent_instructions"] and "do not ask which area" in out["agent_instructions"]


def test_a_refusal_returns_no_wiki_results_even_when_the_wiki_found_keyword_hits(monkeypatch):
    wire(monkeypatch, NO_MATCH, wiki=[WIKI_HIT])
    out = search("directions to a bar")
    assert out["out_of_scope"] is True and out["results"] == [] and out["best_bet_count"] == 0


def test_a_search_app_outage_is_not_a_refusal(monkeypatch):
    wire(monkeypatch, requests.ConnectionError("down"), wiki=[WIKI_HIT])
    out = search()
    assert out["out_of_scope"] is False and out["status"] == "partial" and "message" not in out
    assert [r["source"] for r in out["results"]] == ["wiki"]


def test_a_matched_question_is_never_marked_out_of_scope(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    out = search()
    assert out["out_of_scope"] is False and "message" not in out


def test_a_matched_question_with_no_results_says_so_and_does_not_ask(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": []})
    out = search()
    assert out["out_of_scope"] is False and out["needs_clarification"] is False and out["suggested_question"] is None
    assert out["reasons"] and "sujeeth.org" in out["agent_instructions"]
    assert "do not ask a clarifying question" in out["agent_instructions"]


def test_a_matched_question_with_no_approved_domain_does_not_ask(monkeypatch):
    resolution = dict(RESOLVED, domains=[])
    wire(monkeypatch, {"status": "no_approved_domains_for_concepts", "resolution": resolution, "results": []})
    out = search()
    assert out["needs_clarification"] is False and out["suggested_question"] is None
    assert "No approved search domain was resolved." in out["reasons"]


def test_one_clear_match_with_a_weaker_one_is_not_ambiguous(monkeypatch):
    monkeypatch.setattr(server, "_linked_pairs", set())
    resolution = {"status": "resolved", "matched_concepts": ["a", "b"], "domains": ["sujeeth.org"],
                  "concept_matches": [{"concept_id": "a", "name": "A", "confidence": 0.9},
                                      {"concept_id": "b", "name": "B", "confidence": 0.4}]}
    wire(monkeypatch, {"status": "completed", "resolution": resolution, "results": [HIT]})
    assert search("something")["needs_clarification"] is False


def test_the_server_instructions_tell_the_agent_how_to_refuse():
    assert REFUSAL in server.SERVER_INSTRUCTIONS and "out_of_scope" in server.SEARCH_DESCRIPTION


def test_a_search_app_failure_still_returns_the_wiki_answer(monkeypatch):
    wiki = [{"title": "FAQ", "url": "https://github.com/x/wiki/FAQ", "snippet": "How to volunteer", "source": "wiki",
             "path": "FAQ.md", "chunk_id": "FAQ.md"}]
    wire(monkeypatch, requests.ConnectionError("down"), wiki=wiki)
    out = search()
    assert [r["source"] for r in out["results"]] == ["wiki"]
    assert out["status"] == "partial" and out["service_degraded"] is True
    assert out["service_errors"][0]["service"] == "websearch"
    assert out["needs_clarification"] is False and out["suggested_question"] is None   # not the question's fault


def test_both_apps_down_says_so_and_does_not_ask_the_user_to_clarify(monkeypatch):
    wire(monkeypatch, requests.ConnectionError("down"), wiki=requests.ConnectionError("down"))
    out = search()
    assert out["results"] == [] and out["service_degraded"] is True and out["needs_clarification"] is False
    assert "do not treat missing results as proof" in out["agent_instructions"]
    assert {e["service"] for e in out["service_errors"]} == {"websearch", "wikisearch"}


def test_the_search_app_returning_no_resolution_block_still_works(monkeypatch):
    wire(monkeypatch, {"status": "completed", "concepts": ["volunteer"], "domains": ["sujeeth.org"], "results": [HIT]})
    out = search()
    assert out["ontology"]["matched_concepts"] == ["volunteer"] and out["needs_clarification"] is False


SPOTLIGHT = {"title": "Mentor story", "url": "https://www.sujeeth.org/spotlight/jane", "snippet": "Jane moved from analyst to VP.",
             "source": "website", "domain": "sujeeth.org", "chunk_id": "s-0", "spotlight": True}


def test_a_spotlight_comes_first_and_the_agent_is_told_to_offer_it_as_an_example(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT, SPOTLIGHT]})
    out = search("how do I prepare for an interview")
    assert out["spotlight_count"] == 1 and out["results"][0]["url"] == SPOTLIGHT["url"]
    assert "story of one volunteer's career path" in out["agent_instructions"]
    assert out["agent_instructions"].count("curated pages") == 1       # the priority sentence is said once


def test_without_a_spotlight_the_agent_is_not_told_about_one(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    assert "career path" not in search()["agent_instructions"]


def test_concepts_the_ontology_links_with_illustrates_are_not_a_close_call(monkeypatch):
    monkeypatch.setattr(server, "_linked_pairs", {frozenset(("resume_resource", "week_3"))})
    resolution = {"status": "resolved", "matched_concepts": ["resume_resource", "week_3"], "domains": ["sujeeth.org"],
                  "concept_matches": [{"concept_id": "resume_resource", "name": "Resume Resource", "confidence": 0.62},
                                      {"concept_id": "week_3", "name": "Week 3", "confidence": 0.60}]}
    wire(monkeypatch, {"status": "completed", "resolution": resolution, "results": [HIT]})
    assert search("review my resume")["needs_clarification"] is False


def test_unlinked_close_matches_still_ask(monkeypatch):
    monkeypatch.setattr(server, "_linked_pairs", set())
    resolution = {"status": "resolved", "matched_concepts": ["a", "b"], "domains": ["sujeeth.org"],
                  "concept_matches": [{"concept_id": "a", "name": "A", "confidence": 0.62},
                                      {"concept_id": "b", "name": "B", "confidence": 0.60}]}
    wire(monkeypatch, {"status": "completed", "resolution": resolution, "results": [HIT]})
    out = search("something")
    assert out["needs_clarification"] is True and out["out_of_scope"] is False
    assert out["suggested_question"] == "Are you asking about A or B?"
