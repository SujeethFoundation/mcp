"""Several parts of a question in one call to search_sujeeth_foundation (more_queries)."""
import json as jsonlib
import os

import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402


def resolved(*concepts):
    return {"status": "resolved", "ontology_version": "1.12.2", "matched_concepts": list(concepts),
            "concept_matches": [{"concept_id": c, "name": c.title(), "confidence": 1.2} for c in concepts],
            "domains": ["sujeeth.org"]}


def hit(path, **extra):
    return {"title": path.title(), "url": f"https://sujeeth.org/{path}", "snippet": f"About {path}.",
            "source": "website", "domain": "sujeeth.org", "chunk_id": f"{path}-0", **extra}


RESUME = {"status": "completed", "resolution": resolved("resume_resource"), "results": [hit("resume"), hit("shared")]}
INTERVIEW = {"status": "completed", "resolution": resolved("interviewing"),
             "results": [hit("interview"), hit("shared"), hit("spotlight/ana", spotlight=True)]}
NOTHING = {"status": "no_authorized_domains", "resolution": {"status": "unresolved", "matched_concepts": [], "domains": []},
           "results": []}
CLOSE = {"status": "completed", "results": [hit("feedback")],
         "resolution": {"status": "resolved", "matched_concepts": ["a", "b"], "domains": ["sujeeth.org"],
                        "concept_matches": [{"concept_id": "a", "name": "Survey", "confidence": 0.8},
                                            {"concept_id": "b", "name": "Portal", "confidence": 0.78}]}}


def reply(body):
    response = requests.Response()
    response.status_code = 200
    response._content = jsonlib.dumps(body).encode()
    return response


def wire(monkeypatch, answers, wiki=None):
    """answers: {query: the Search App's answer or an exception}. Returns the queries the Search App was sent."""
    sent = []

    def post(url, headers=None, json=None, timeout=None):
        sent.append(json["query"])
        answer = answers[json["query"]]
        if isinstance(answer, Exception):
            raise answer
        return reply(answer)

    monkeypatch.setattr(server.http, "post", post)
    monkeypatch.setattr(server, "get_json", lambda url, key="", params=None: {"results": wiki or []})
    monkeypatch.setattr(server, "WIKI_FULL_TEXT", False)
    monkeypatch.setattr(server, "_sleep", lambda seconds: None)
    monkeypatch.setattr(server, "linked_concept_pairs", lambda: set())
    return sent


def search(query, more=None, **kwargs):
    return server.search_sujeeth_foundation.sync(query, full_documents=False, more_queries=more, **kwargs)


def urls(result):
    return [item["url"].rsplit("sujeeth.org/", 1)[1] for item in result["results"]]


def test_no_more_queries_is_the_single_search_unchanged(monkeypatch):
    wire(monkeypatch, {"resume": RESUME})
    single, empty = search("resume"), search("resume", [])
    assert single == empty == server.search_one("resume", full_documents=False)
    assert "queries" not in single and "for_query" not in single["results"][0]


def test_parts_are_searched_once_each_and_merged(monkeypatch):
    sent = wire(monkeypatch, {"resume": RESUME, "interview": INTERVIEW})
    result = search("resume", ["interview"])
    assert sorted(sent) == ["interview", "resume"]
    assert urls(result) == ["spotlight/ana", "resume", "interview", "shared"]   # curated first, then in turn, each page once
    assert result["count"] == 4 and result["spotlight_count"] == 1
    assert [item["for_query"] for item in result["results"]] == ["interview", "resume", "interview", "resume"]
    assert result["ontology"]["matched_concepts"] == ["resume_resource", "interviewing"]
    assert [(q["query"], q["status"]) for q in result["queries"]] == [("resume", "completed"), ("interview", "completed")]
    assert result["status"] == "completed" and not result["out_of_scope"] and not result["needs_clarification"]
    assert "combines 2 searches" in result["agent_instructions"]


def test_repeats_and_blanks_are_dropped_and_the_number_is_capped(monkeypatch):
    sent = wire(monkeypatch, {"resume": RESUME, "interview": INTERVIEW})
    monkeypatch.setattr(server, "MAX_QUERIES", 2)
    search("resume", ["  Resume ", "", None, "interview", "a third part"])
    assert sorted(sent) == ["interview", "resume"]


def test_limit_caps_the_merged_list_but_not_curated_pages(monkeypatch):
    wire(monkeypatch, {"resume": RESUME, "interview": INTERVIEW})
    assert urls(search("resume", ["interview"], limit=2)) == ["spotlight/ana", "resume", "interview"]


def test_one_part_out_of_scope_does_not_refuse_the_rest(monkeypatch):
    wire(monkeypatch, {"resume": RESUME, "vodka tonic": NOTHING})
    result = search("resume", ["vodka tonic"])
    assert not result["out_of_scope"] and urls(result) == ["resume", "shared"]
    assert result["queries"][1]["status"] == "out_of_scope"
    assert "outside what you can help with" in result["agent_instructions"]


def test_every_part_out_of_scope_is_the_usual_refusal(monkeypatch):
    wire(monkeypatch, {"vodka tonic": NOTHING, "gin fizz": NOTHING})
    result = search("vodka tonic", ["gin fizz"])
    assert result["out_of_scope"] and result["results"] == [] and result["message"] == server.OUT_OF_SCOPE_MESSAGE
    assert len(result["queries"]) == 2


def test_clarification_is_asked_only_when_every_part_would_ask(monkeypatch):
    wire(monkeypatch, {"resume": RESUME, "feedback": CLOSE, "feedback form": CLOSE})
    def model_plan(query):
        concept = "resume_resource" if "resume" in query else "feedback_form"
        clarify = "feedback" in query
        return {"resolution": {"status": "resolved", "ontology_version": "test",
                               "matched_concepts": [concept], "domains": ["sujeeth.org"],
                               "concept_matches": [{"concept_id": concept, "source": "test_model"}],
                               "needs_clarification": clarify,
                               "suggested_question": "Do you mean a feedback form or general feedback?" if clarify else None},
                "queries": [query], "error": None}
    monkeypatch.setattr(server, "resolve_ontology_plan", model_plan)
    mixed = search("resume", ["feedback"])
    assert not mixed["needs_clarification"] and mixed["suggested_question"] is None
    assert mixed["queries"][1]["needs_clarification"] and mixed["queries"][1]["suggested_question"]
    both = search("feedback", ["feedback form"])
    assert both["needs_clarification"] and both["suggested_question"] == "Do you mean a feedback form or general feedback?"


def test_a_failed_search_app_on_one_part_is_partial_not_lost(monkeypatch):
    wire(monkeypatch, {"resume": RESUME, "interview": requests.ConnectionError("down")})
    result = search("resume", ["interview"])
    assert result["status"] == "partial" and result["service_degraded"]
    assert "resume" in urls(result)
    assert "down" not in jsonlib.dumps(result)   # backend error text never reaches the client


def test_attached_pages_across_parts_stay_within_the_budget(monkeypatch):
    monkeypatch.setattr(server, "DOCUMENT_CHAR_BUDGET", 5000)
    page = lambda n, size, **extra: {"url": f"https://w/{n}", "document": {"text": "x" * size, "truncated": False}, **extra}
    kept = server._fit_documents([page(1, 3000), page(2, 3000), page(3, 3000, snippet="s"), page(4, 3000), {"url": "https://w/5"}])
    assert [len((item.get("document") or {}).get("text", "")) for item in kept] == [3000, 2000, 0, 0]
    assert kept[1]["document"]["truncated"] and [item["url"][-1] for item in kept] == ["1", "2", "3", "5"]


def test_the_agent_is_told_about_more_queries():
    assert "more_queries" in server.SEARCH_DESCRIPTION and "more_queries" in server.SERVER_INSTRUCTIONS
