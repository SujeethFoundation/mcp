"""The slim catalog has no domains, so MCP asks the ontology app for them; without them no search domain resolves and
the website search never runs."""
import os

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402


class Reply:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


SLIM = {"ontology_version": "9", "classes": [{"id": "resume_resource", "name": "Resume Resource"}], "relationships": []}
DOMAIN_ROWS = {"domains": [
    {"domain": "tealhq.com", "search_enabled": True, "crawl_enabled": True, "crawl_policy": {"x": 1},
     "associated_concepts": ["resume_resource", "interviewing"]},
    {"domain": "sujeeth.org", "search_enabled": True, "associated_concepts": ["resume_resource"]},
]}


def serve(monkeypatch, answers, calls):
    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers))
        answer = answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(server.requests, "get", fake_get)
    monkeypatch.setattr(server, "ONTOLOGY_CATALOG_ENABLED", True)
    monkeypatch.setattr(server, "ONTOLOGY_KEY", "k")
    monkeypatch.setattr(server, "_ontology_catalog", None)
    monkeypatch.setattr(server, "_ontology_catalog_loaded_at", None)


DOMAINS_URL = f"{server.ONTOLOGY_URL}/api/ontology/domains?search_enabled=true"


def plan_domains(catalog, concept="resume_resource"):
    result = {"matched_concepts": [concept], "related_concepts": [], "feelings": [], "intent": None, "topic_queries": [],
              "feeling_query": None, "needs_clarification": False, "suggested_question": None}
    return server.validate_ontology_plan(result, "q", catalog)["resolution"]["domains"]


def test_a_catalog_without_domains_gets_them_from_the_ontology_app(monkeypatch):
    calls = []
    serve(monkeypatch, {server.ONTOLOGY_CATALOG_URL: Reply(SLIM), DOMAINS_URL: Reply(DOMAIN_ROWS)}, calls)
    catalog = server.ontology_catalog()
    assert [url for url, _ in calls] == [server.ONTOLOGY_CATALOG_URL, DOMAINS_URL]
    assert calls[1][1] == {"x-functions-key": "k"}
    assert plan_domains(catalog) == ["tealhq.com", "sujeeth.org"]
    assert catalog["domains"][0] == {"domain": "tealhq.com", "search_enabled": True,
                                     "associated_concepts": ["resume_resource", "interviewing"]}   # crawl policy not kept


def test_a_catalog_that_has_domains_is_not_asked_again(monkeypatch):
    calls = []
    raw = {**SLIM, "domains": [{"domain": "sujeeth.org", "search_enabled": True, "associated_concepts": ["resume_resource"]}]}
    serve(monkeypatch, {server.ONTOLOGY_CATALOG_URL: Reply(raw)}, calls)
    assert plan_domains(server.ontology_catalog()) == ["sujeeth.org"]
    assert [url for url, _ in calls] == [server.ONTOLOGY_CATALOG_URL]


def test_domains_are_fetched_once_per_catalog_refresh_not_per_question(monkeypatch):
    calls = []
    serve(monkeypatch, {server.ONTOLOGY_CATALOG_URL: Reply(SLIM), DOMAINS_URL: Reply(DOMAIN_ROWS)}, calls)
    for _ in range(5):
        server.ontology_catalog()
    assert len(calls) == 2


def test_when_the_domains_route_fails_the_catalog_still_works_and_is_retried_soon(monkeypatch):
    calls = []
    serve(monkeypatch, {server.ONTOLOGY_CATALOG_URL: Reply(SLIM), DOMAINS_URL: Reply({}, 503)}, calls)
    catalog = server.ontology_catalog()
    assert catalog["classes"] == SLIM["classes"] and catalog["domains"] == []
    age = server.time.monotonic() - server._ontology_catalog_loaded_at
    assert age >= server.ONTOLOGY_CATALOG_REFRESH_SECONDS - server.ONTOLOGY_DOMAINS_RETRY_SECONDS - 1   # due again in about 30 s


def test_a_failed_refresh_keeps_the_last_known_domains(monkeypatch):
    calls = []
    answers = {server.ONTOLOGY_CATALOG_URL: Reply(SLIM), DOMAINS_URL: Reply(DOMAIN_ROWS)}
    serve(monkeypatch, answers, calls)
    first = server.ontology_catalog()
    answers[DOMAINS_URL] = Reply({}, 503)
    monkeypatch.setattr(server, "_ontology_catalog_loaded_at", -1e9)   # the refresh is due
    second = server.ontology_catalog()
    assert second is not first and second["domains"] == first["domains"]


def test_an_answer_that_is_not_a_list_of_domains_is_never_used(monkeypatch):
    calls = []
    serve(monkeypatch, {server.ONTOLOGY_CATALOG_URL: Reply(SLIM), DOMAINS_URL: Reply({"error": "x"})}, calls)
    assert server.ontology_catalog()["domains"] == []
