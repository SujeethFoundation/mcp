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
    monkeypatch.setattr(server, "ontology_catalog", lambda: {
        "ontology_version": "test",
        "classes": [
            {"id": "volunteer", "name": "Volunteer", "aliases": ["volunteer"]},
            {"id": "spotlight", "name": "Spotlight", "aliases": ["spotlight"]},
            {"id": "interviewing", "name": "Interviewing", "query_phrases": ["prepare for an interview"]},
            {"id": "explore", "name": "Explore", "query_phrases": ["research roles and companies", "research companies", "compare employers", "learn what a job role is really like"]},
            {"id": "career_development", "name": "Career Development", "aliases": ["career planning"]},
            {"id": "resume_resource", "name": "Resume Resource", "aliases": ["resume review"]},
            {"id": "week_3", "name": "Week 3"},
        ],
        "predicates": [{"id": "illustrates", "search_behavior": {"enabled": True, "effect": "expand"}}],
        "relationships": [{"subject": "spotlight", "predicate": "illustrates", "object": "volunteer"},
                          {"subject": "spotlight", "predicate": "illustrates", "object": "interviewing"}],
        "domains": [{"domain": "sujeeth.org", "search_enabled": True,
                     "associated_concepts": ["volunteer", "spotlight", "interviewing", "explore", "career_development", "resume_resource", "week_3"]}],
    })
    return calls


def search(query="how do I volunteer"):
    return server.search_sujeeth_foundation.sync(query, full_documents=False)


def test_a_search_makes_one_request_to_the_search_app(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    search()
    assert len([url for url, _ in calls if url.startswith(server.WEBSEARCH_URL) and url.endswith("/api/search")]) == 1
    assert not any(url.endswith("/api/domain-resolution") for url, _ in calls)


def test_the_request_carries_only_concepts_selected_by_mcp(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    search()
    body = calls[0][1]
    assert body["concepts"] == ["volunteer"] and body["domains"] == ["sujeeth.org"]
    assert body["query"] == "how do I volunteer" and body["limit"] == server.CANDIDATE_POOL


def test_website_only_tool_uses_mcp_derived_filters(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "results": [HIT]})
    result = server.search_website.sync("how can I research a company?")
    search_calls = [body for url, body in calls if url.endswith("/api/search")]
    assert result["status"] == "completed"
    assert len(search_calls) == 1
    assert search_calls[0]["concepts"] == ["explore"]
    assert search_calls[0]["domains"] == ["sujeeth.org"]


def test_the_candidate_pool_is_what_the_search_app_will_return():
    assert server.CANDIDATE_POOL == 20


def test_the_ontology_block_is_the_search_apps_resolution(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    out = search()
    assert out["ontology"]["matched_concepts"] == ["volunteer"]
    assert out["ontology"]["domains"] == ["sujeeth.org"] and out["ontology"]["ontology_version"] == "test"
    assert out["needs_clarification"] is False and out["status"] == "completed"


NO_MATCH = {"status": "no_ontology_concept_match", "concepts": [], "domains": [], "count": 0, "results": [],
            "resolution": {"status": "no_match", "matched_concepts": [], "domains": [], "concept_matches": []}}
WIKI_HIT = {"title": "Donor FAQ", "url": "https://github.com/x/wiki/FAQ-for-Donors", "snippet": "Giving", "source": "wiki",
            "path": "FAQ.md", "chunk_id": "FAQ.md"}
REFUSAL = server.OUT_OF_SCOPE_MESSAGE


def test_a_question_with_no_concept_is_refused_not_clarified(monkeypatch):
    wire(monkeypatch, NO_MATCH)
    out = search("what is the weather")
    assert out["out_of_scope"] is True and out["status"] == "out_of_scope" and out["message"] == REFUSAL
    assert out["needs_clarification"] is False and out["suggested_question"] is None
    assert out["results"] == [] and out["count"] == 0
    assert REFUSAL in out["agent_instructions"] and "do not ask which area" in out["agent_instructions"]
    assert out["message"].startswith("That's outside what I can help with.") and "interview prep" in out["message"] and "mentor" in out["message"]


def test_an_azure_model_failure_is_not_treated_as_out_of_scope(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "results": [HIT]})
    monkeypatch.setattr(server, "resolve_ontology_plan", lambda query: (_ for _ in ()).throw(RuntimeError("model offline")))
    out = search("how can I research a company?")
    assert out["out_of_scope"] is False and out["service_degraded"] is True
    assert out["service_errors"][0]["service"] == "ontology"
    assert calls == []


def test_career_exploration_is_resolved_before_the_first_search(monkeypatch):
    wire(monkeypatch, {"status": "completed", "results": [HIT]})
    recovered = {"status": "completed", "resolution": {
        **RESOLVED, "matched_concepts": ["explore", "career_development"],
        "concept_matches": [{"concept_id": "explore", "confidence": 1.0, "source": "explicit"},
                            {"concept_id": "career_development", "confidence": 1.0, "source": "explicit"}],
    }, "results": [HIT]}
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append((url, json))
        return reply(recovered)

    monkeypatch.setattr(server.http, "post", post)
    monkeypatch.setattr(server, "get_json", lambda *args, **kwargs: {"results": []})
    for question in (
        "how can I research roles and companies?",
        "how do I research companies?",
        "how can I research a company?",
        "how do I compare employers?",
        "how do I learn what a job role is really like?",
    ):
        before = len([body for url, body in calls if url.startswith(server.WEBSEARCH_URL)])
        out = search(question)
        assert out["out_of_scope"] is False and out["status"] in {"completed", "partial"}
        assert out["ontology"]["matched_concepts"] == ["explore"]
        sent = [body for url, body in calls if url.startswith(server.WEBSEARCH_URL)]
        assert len(sent) == before + 1
        assert sent[-1]["concepts"] == ["explore"]
        assert sent[-1]["domains"] == ["sujeeth.org"]


def test_mcp_sends_every_model_generated_query_with_the_same_approved_filters(monkeypatch):
    calls = wire(monkeypatch, {"status": "completed", "results": [HIT]})
    plan = {"resolution": {"status": "resolved", "ontology_version": "test",
                            "matched_concepts": ["explore"], "domains": ["sujeeth.org"],
                            "concept_matches": [{"concept_id": "explore", "source": "azure_model"}]},
            "queries": ["how can I research a company?", "research roles and companies", "compare employers"],
            "error": None}
    monkeypatch.setattr(server, "resolve_ontology_plan", lambda query: plan)
    wiki_calls = []
    post_json = server.post_json_with_retry
    def record_wiki(url, payload, key="", attempts=None):
        if url.startswith(server.WIKISEARCH_URL):
            wiki_calls.append((url, dict(payload)))
            return {"results": []}
        return post_json(url, payload, key, attempts)
    monkeypatch.setattr(server, "post_json_with_retry", record_wiki)
    resolutions = []
    monkeypatch.setattr(server, "resolve_ontology_plan", lambda query: resolutions.append(query) or plan)
    result = search("how can I research a company?")
    sent = [body for url, body in calls if url.endswith("/api/search")]
    assert len(sent) == 1 and sent[0]["queries"] == plan["queries"]
    assert sent[0]["concepts"] == ["explore"] and sent[0]["domains"] == ["sujeeth.org"]
    assert len(wiki_calls) == 1 and wiki_calls[0][1]["queries"] == plan["queries"]
    assert wiki_calls[0][1]["concepts"] == ["explore"] and wiki_calls[0][1]["domains"] == ["sujeeth.org"]
    assert resolutions == ["how can I research a company?"]
    assert result["ontology"]["matched_concepts"] == ["explore"]


def test_model_resolution_accepts_related_ontology_concepts_and_generates_queries():
    catalog = {"ontology_version": "test",
               "classes": [{"id": "career_development", "name": "Career Development"},
                           {"id": "career_story", "name": "Career Story"}],
               "predicates": [{"id": "illustrates", "search_behavior": {"enabled": True, "effect": "expand",
                             "matched_endpoint": "object", "retrieve_endpoint": "subject"}}],
               "relationships": [{"subject": "career_story", "predicate": "illustrates", "object": "career_development"}],
               "domains": [{"domain": "sujeeth.org", "search_enabled": True,
                            "associated_concepts": ["career_development", "career_story"]}]}
    plan = server.validate_ontology_plan({"matched_concepts": ["career_development", "not_in_ontology"],
                                          "related_concepts": ["career_story", "not_in_ontology"],
                                          "queries": ["research companies", "compare employers"]},
                                         "how can I research a company?", catalog)
    assert plan["resolution"]["matched_concepts"] == ["career_development", "career_story"]
    assert plan["resolution"]["domains"] == ["sujeeth.org"]
    assert plan["queries"] == ["how can I research a company?", "research companies", "compare employers"]


def test_illustrates_matches_the_object_and_retrieves_its_subject_only():
    catalog = {"classes": [{"id": "career_story", "name": "Career Story"}, {"id": "career_development", "name": "Career Development"}],
               "predicates": [{"id": "illustrates", "search_behavior": {"enabled": True, "effect": "expand"}}],
               "relationships": [{"subject": "career_story", "predicate": "illustrates", "object": "career_development"}],
               "domains": [{"domain": "sujeeth.org", "search_enabled": True,
                            "associated_concepts": ["career_story", "career_development"]}]}
    object_match = server.validate_ontology_plan({"matched_concepts": ["career_development"],
                                                  "related_concepts": ["career_story"], "queries": []}, "role", catalog)
    subject_match = server.validate_ontology_plan({"matched_concepts": ["career_story"],
                                                   "related_concepts": ["career_development"], "queries": []}, "story", catalog)
    assert object_match["resolution"]["matched_concepts"] == ["career_development", "career_story"]
    assert subject_match["resolution"]["matched_concepts"] == ["career_story"]


def test_invalid_or_empty_model_matches_remain_a_no_match():
    catalog = {"classes": [{"id": "donation"}], "domains": []}
    plan = server.validate_ontology_plan({"matched_concepts": ["invented"], "related_concepts": [], "queries": []},
                                         "directions to a bar", catalog)
    assert plan["resolution"]["status"] == "no_match"
    assert plan["resolution"]["matched_concepts"] == [] and plan["queries"] == []


def test_ontology_resolution_calls_the_configured_azure_gpt6_luna_endpoint(monkeypatch):
    requests_seen = []
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"output_text": '{"matched_concepts":[],"related_concepts":[],"queries":[]}'}
    def post(url, **kwargs):
        requests_seen.append((url, kwargs))
        return Response()
    monkeypatch.setattr(server, "ONTOLOGY_MODEL_ENDPOINT", "https://models.example")
    monkeypatch.setattr(server, "ONTOLOGY_MODEL_API_KEY", "test-secret")
    monkeypatch.setattr(server, "ONTOLOGY_MODEL_NAME", "gpt-6-luna")
    monkeypatch.setattr(server.requests, "post", post)
    server.call_ontology_model("research a company", {"classes": [{"id": "explore"}], "relationships": []})
    url, request = requests_seen[0]
    assert url == "https://models.example/openai/v1/responses"
    assert request["headers"]["api-key"] == "test-secret"
    assert request["json"]["model"] == "gpt-6-luna" and request["json"]["store"] is False
    assert '"id": "explore"' in request["json"]["input"]


def test_ontology_model_endpoint_accepts_resource_base_and_full_responses_url():
    assert server.ontology_model_responses_url("https://resource.example") == "https://resource.example/openai/v1/responses"
    assert server.ontology_model_responses_url("https://resource.example/openai/v1/") == "https://resource.example/openai/v1/responses"
    assert server.ontology_model_responses_url("https://resource.example/openai/v1/responses") == "https://resource.example/openai/v1/responses"


def test_exact_refusal_scenario_stays_out_of_scope_without_a_recovery_retry(monkeypatch):
    calls = wire(monkeypatch, NO_MATCH)
    out = search("directions to a bar")
    assert out["out_of_scope"] is True and out["message"] == REFUSAL
    assert len(calls) == 0  # MCP classifies out-of-scope before calling Websearch.


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
    assert out["out_of_scope"] is False and "No usable search results were returned." in out["reasons"]


def test_one_clear_match_with_a_weaker_one_is_not_ambiguous(monkeypatch):
    monkeypatch.setattr(server, "_linked_pairs", set())
    resolution = {"status": "resolved", "matched_concepts": ["a", "b"], "domains": ["sujeeth.org"],
                  "concept_matches": [{"concept_id": "a", "name": "A", "confidence": 0.9},
                                      {"concept_id": "b", "name": "B", "confidence": 0.4}]}
    wire(monkeypatch, {"status": "completed", "resolution": resolution, "results": [HIT]})
    assert search("something")["needs_clarification"] is False


def test_the_server_instructions_tell_the_agent_how_to_refuse():
    assert "out_of_scope" in server.SEARCH_DESCRIPTION and "as written" in server.SERVER_INSTRUCTIONS


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
    assert "volunteer" in out["ontology"]["matched_concepts"] and out["needs_clarification"] is False


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


def test_mcp_resolution_is_authoritative_over_search_app_resolution(monkeypatch):
    wire(monkeypatch, {"status": "completed", "resolution": RESOLVED, "results": [HIT]})
    out = search("something")
    assert out["out_of_scope"] is False and out["needs_clarification"] is False
