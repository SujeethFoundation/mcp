import mcp_server as server


class Reply:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


GOOD = {"ontology_version": "9", "classes": [{"id": "a"}], "relationships": []}


def serve(monkeypatch, answers, calls):
    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers))
        return answers[url]

    monkeypatch.setattr(server.requests, "get", fake_get)


def test_the_slim_catalog_from_the_ontology_app_is_read_first_with_the_key(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "ONTOLOGY_CATALOG_ENABLED", True)
    monkeypatch.setattr(server, "ONTOLOGY_KEY", "k")
    serve(monkeypatch, {server.ONTOLOGY_CATALOG_URL: Reply(GOOD)}, calls)
    assert server.fetch_ontology_json(5) == GOOD
    assert calls == [(server.ONTOLOGY_CATALOG_URL, {"x-functions-key": "k"})]


def test_the_raw_file_is_the_fallback_when_the_app_does_not_answer(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "ONTOLOGY_CATALOG_ENABLED", True)
    serve(monkeypatch, {server.ONTOLOGY_CATALOG_URL: Reply({}, 401), server.MCP_ONTOLOGY_JSON_URL: Reply(GOOD)}, calls)
    assert server.fetch_ontology_json(5) == GOOD
    assert [url for url, _ in calls] == [server.ONTOLOGY_CATALOG_URL, server.MCP_ONTOLOGY_JSON_URL]


def test_an_answer_that_is_not_an_ontology_is_never_used(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "ONTOLOGY_CATALOG_ENABLED", True)
    serve(monkeypatch, {server.ONTOLOGY_CATALOG_URL: Reply({"error": "x"}), server.MCP_ONTOLOGY_JSON_URL: Reply([])}, calls)
    try:
        server.fetch_ontology_json(5)
    except Exception:
        return
    raise AssertionError("a non-ontology answer was accepted")


def test_an_explicit_ontology_url_is_the_only_source(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "ONTOLOGY_CATALOG_ENABLED", False)
    serve(monkeypatch, {server.MCP_ONTOLOGY_JSON_URL: Reply(GOOD)}, calls)
    assert server.fetch_ontology_json(5) == GOOD
    assert [url for url, _ in calls] == [server.MCP_ONTOLOGY_JSON_URL]
