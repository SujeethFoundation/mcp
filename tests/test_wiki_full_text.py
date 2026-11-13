"""Wiki results carry their whole page up front; website results stay excerpts."""
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


def test_wiki_result_carries_the_whole_page_and_website_stays_an_excerpt(monkeypatch):
    seen = wire(monkeypatch)
    monkeypatch.setattr(server, "WIKI_FULL_TEXT", True)
    result = server.search_sujeeth_foundation.sync("volunteer", full_documents=False)
    items = by_source(result)
    assert items["wiki"]["document"]["text"] == PAGE
    assert items["wiki"]["document"]["truncated"] is False
    assert "document" not in items["website"]
    assert seen[0]["full_text"] == "true" and seen[0]["max_chars"] == server.WIKI_FULL_TEXT_CHARS
    assert result["full_documents"]["from_search_text"] == 1
    assert "whole page in their document field" in result["agent_instructions"]
    assert "_full_text" not in items["wiki"]


def test_a_truncated_wiki_page_says_so(monkeypatch):
    wire(monkeypatch, truncated=True)
    monkeypatch.setattr(server, "WIKI_FULL_TEXT", True)
    result = server.search_sujeeth_foundation.sync("volunteer", full_documents=False)
    assert by_source(result)["wiki"]["document"]["truncated"] is True


def test_turning_it_off_returns_wiki_excerpts_again(monkeypatch):
    seen = wire(monkeypatch)
    monkeypatch.setattr(server, "WIKI_FULL_TEXT", False)
    result = server.search_sujeeth_foundation.sync("volunteer", full_documents=False)
    assert "document" not in by_source(result)["wiki"]
    assert seen[0]["full_text"] == "false"
    assert result["full_documents"] is None


def test_the_response_stays_inside_the_character_budget(monkeypatch):
    wire(monkeypatch)
    monkeypatch.setattr(server, "WIKI_FULL_TEXT", True)
    monkeypatch.setattr(server, "DOCUMENT_CHAR_BUDGET", 100)
    result = server.search_sujeeth_foundation.sync("volunteer", full_documents=False)
    assert "document" not in by_source(result)["wiki"]
    assert result["full_documents"]["skipped_for_length"] == 1


def test_search_wiki_tool_returns_the_page_by_default(monkeypatch):
    seen = wire(monkeypatch)
    monkeypatch.setattr(server, "WIKI_FULL_TEXT", True)
    result = server.search_wiki.sync("volunteer")
    assert result["results"][0]["document"]["text"] == PAGE
    assert seen[0]["full_text"] == "true"
