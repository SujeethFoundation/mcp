"""The combined search returns excerpts; full pages stay an explicit follow-up."""
import os

import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402

PAGE = "Q: Who can volunteer? A: Professionals with 10 years of experience. " * 40


def reply(body):
    response = requests.Response()
    response.status_code = 200
    response._content = body
    return response


def wire(monkeypatch, wiki_text=PAGE, truncated=False, wiki_hits=1):
    """The combined search with one wiki hit (snippet and text) and one website hit. Returns the wiki params seen."""
    seen = []

    def post(url, headers=None, json=None, timeout=None):
        return reply(
            b'{"status": "completed", "resolution": {"status": "resolved", "matched_concepts": ["volunteer"], '
            b'"domains": ["sujeeth.org"]}, "results": [{"title": "Volunteer", "url": "https://sujeeth.org/volunteer", '
            b'"snippet": "Our volunteer mission.", "source": "website", "domain": "sujeeth.org", "chunk_id": "w-0"}]}'
        )

    def get_json(url, key="", params=None):
        seen.append(dict(params or {}))
        items = []
        for n in range(wiki_hits):
            item = {"title": f"Wiki page {n}", "url": f"https://github.com/x/wiki/Page-{n}",
                    "snippet": "Q: Who can volunteer?", "source": "wiki", "path": f"Page-{n}.md",
                    "chunk_id": f"Page-{n}.md", "score": 7.6 - n}
            if params and params.get("full_text") == "true":
                item.update({"text": wiki_text, "total_chars": len(wiki_text), "truncated": truncated})
            items.append(item)
        return {"results": items}

    monkeypatch.setattr(server.http, "post", post)
    monkeypatch.setattr(server, "get_json", get_json)
    return seen


def by_source(result):
    return {r["source"]: r for r in result["results"]}


def test_combined_search_never_reads_full_text_automatically(monkeypatch):
    seen = wire(monkeypatch)
    result = server.search_sujeeth_foundation.sync("volunteer")
    items = by_source(result)
    assert "document" not in items["wiki"]
    assert "document" not in items["website"]
    assert seen[0]["full_text"] == "false"
    assert result["full_documents"] is None
    assert "_full_text" not in items["wiki"]


def test_combined_search_keeps_wiki_as_excerpt(monkeypatch):
    wire(monkeypatch, truncated=True)
    result = server.search_sujeeth_foundation.sync("volunteer")
    assert "document" not in by_source(result)["wiki"]


def test_wiki_search_returns_excerpts_and_leaves_page_reads_to_get_documents(monkeypatch):
    seen = wire(monkeypatch)
    result = server.search_wiki.sync("volunteer")
    assert "document" not in by_source(result)["wiki"]
    assert seen[0]["full_text"] == "false"


def test_combined_search_does_not_attach_text_that_exceeds_the_budget(monkeypatch):
    wire(monkeypatch)
    result = server.search_sujeeth_foundation.sync("volunteer")
    assert "document" not in by_source(result)["wiki"]
    assert result["full_documents"] is None


def test_search_wiki_tool_never_returns_full_page_text(monkeypatch):
    seen = wire(monkeypatch)
    result = server.search_wiki.sync("volunteer")
    assert "document" not in result["results"][0]
    assert seen[0]["full_text"] == "false"

