"""What the agent reads: one set of instructions, consistent limits, whole pages, one read per page."""
import os
import re

import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402


def reply(body):
    response = requests.Response()
    response.status_code = 200
    response._content = body
    return response


RESOLUTION = '"resolution": {"status": "resolved", "matched_concepts": ["circle"], "domains": ["sujeeth.org"]}'


def wire(monkeypatch, website_domain="sujeeth.org", wiki=True):
    def post(url, headers=None, json=None, timeout=None):
        return reply(
            ('{"status": "completed", ' + RESOLUTION + ', "results": [{"title": "Report", "url": "https://www.sujeeth.org/report.pdf", '
             '"snippet": "Part of the report.", "source": "website", "domain": "%s", "chunk_id": "r-3"}]}'
             % website_domain).encode())

    monkeypatch.setattr(server.http, "post", post)
    monkeypatch.setattr(server, "get_json", lambda url, key="", params=None: {"results": []})


# -- instructions ---------------------------------------------------------------------------------------------------

def test_every_instruction_names_the_same_page_limit(monkeypatch):
    monkeypatch.setattr(server, "WIKI_FULL_TEXT", False)
    wire(monkeypatch)
    text = server.search_sujeeth_foundation.sync("report", full_documents=False)["agent_instructions"]
    assert f"at most {server.MAX_BATCH_DOCUMENTS} pages" in text
    assert f"at most {server.MAX_BATCH_DOCUMENTS}," in server.GET_DOCUMENTS_DESCRIPTION
    assert f"at most {server.MAX_BATCH_DOCUMENTS}" in server.SERVER_INSTRUCTIONS


def test_the_statistics_rule_reads_the_same_everywhere():
    for text in (server.DOCUMENT_INSTRUCTIONS, server.CITE_AND_STATS, server.SERVER_INSTRUCTIONS,
                 server.GET_DOCUMENTS_INSTRUCTIONS):
        assert server.STATS_RULE in text


def test_statistics_may_come_from_sujeeth_org_only():
    rule = server.STATS_RULE
    assert "only from a result whose domain is sujeeth.org" in rule
    assert "topics.sujeeth.org" in rule and "cite its url" in rule
    assert "Never quote them from the wiki, third-party pages" in rule
    assert "Never quote program statistics" not in rule  # the blanket ban is gone


def test_a_page_read_says_which_domain_it_came_from(monkeypatch):
    monkeypatch.setattr(server, "fetch_document",
                        lambda url, *a, **k: {"text": "body", "domain": "topics.sujeeth.org", "truncated": False})
    doc = server.get_documents.sync(["https://topics.sujeeth.org/x"])["documents"][0]
    assert doc["domain"] == "topics.sujeeth.org"


def test_nothing_the_agent_reads_names_the_removed_tool():
    texts = [server.SEARCH_DESCRIPTION, server.GET_DOCUMENTS_DESCRIPTION, server.SERVER_INSTRUCTIONS,
             server.EXCERPT_INSTRUCTIONS, server.FULL_DOCUMENT_INSTRUCTIONS, server.WIKI_DOCUMENT_INSTRUCTIONS,
             server.GET_DOCUMENTS_INSTRUCTIONS, server.TRUNCATED_INSTRUCTIONS]
    assert not any(re.search(r"get_document\b", t) for t in texts)


def test_a_truncated_page_can_always_be_read_further():
    """The instruction asks for max_chars the tool will really give, and says what to do when that is not enough."""
    assert str(server.DOCUMENT_MAX_CHARS) in server.TRUNCATED_INSTRUCTIONS
    assert server.DOCUMENT_DEFAULT_CHARS < server.DOCUMENT_MAX_CHARS
    assert "link to it" in server.TRUNCATED_INSTRUCTIONS


def test_the_server_tells_the_agent_where_to_start():
    assert "search_sujeeth_foundation" in server.SERVER_INSTRUCTIONS
    assert server.mcp._mcp_server.instructions == server.SERVER_INSTRUCTIONS


def test_tool_descriptions_are_the_agent_facing_text():
    tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
    assert tools["search_sujeeth_foundation"].description == server.SEARCH_DESCRIPTION
    assert tools["get_documents"].description == server.GET_DOCUMENTS_DESCRIPTION
    assert "Use this first" in tools["search_sujeeth_foundation"].description
    assert "Prefer search_sujeeth_foundation" in tools["search_wiki"].description


# -- the response ---------------------------------------------------------------------------------------------------

def test_candidates_are_named_so_they_are_not_read_as_results(monkeypatch):
    monkeypatch.setattr(server, "WIKI_FULL_TEXT", False)
    wire(monkeypatch)
    result = server.search_sujeeth_foundation.sync("report", full_documents=False)
    assert result["count"] == len(result["results"])
    assert set(result["retrieval"]) >= {"website_candidates", "wiki_candidates"}
    assert "wiki_search" not in result and "website_search" not in result


def test_a_complete_page_drops_its_snippet_and_a_cut_page_keeps_it(monkeypatch):
    wire(monkeypatch)
    for truncated, expect_snippet in ((False, False), (True, True)):
        monkeypatch.setattr(server, "fetch_document",
                            lambda url, *a, _t=truncated, **k: {"text": "whole page", "truncated": _t})
        result = server.search_sujeeth_foundation.sync("report", full_documents=True)
        item = result["results"][0]
        assert item["document"]["text"] == "whole page"
        assert ("snippet" in item) is expect_snippet


def test_a_foundation_page_is_read_whole_by_url_not_as_a_passage(monkeypatch):
    wire(monkeypatch)
    calls = []
    monkeypatch.setattr(server, "fetch_document", lambda *a, **k: calls.append(a) or {"text": "page"})
    server.search_sujeeth_foundation.sync("report", full_documents=True)
    url, chunk_id = calls[0][0], calls[0][1]
    assert url == "https://www.sujeeth.org/report.pdf" and chunk_id is None


def test_a_third_party_page_is_still_read_around_the_matched_passage(monkeypatch):
    wire(monkeypatch, website_domain="example.com")
    calls = []
    monkeypatch.setattr(server, "fetch_document", lambda *a, **k: calls.append(a) or {"text": "excerpt", "excerpt_only": True})
    server.search_sujeeth_foundation.sync("report", full_documents=True)
    assert calls[0][1] == "r-3"


# -- the page cache -------------------------------------------------------------------------------------------------

PAGE = "x" * 7000


def counting_fetch(monkeypatch):
    server._document_cache.clear()
    monkeypatch.setattr(server, "DOCUMENT_CACHE_SECONDS", 900)
    calls = []

    def fetch(url=None, chunk_id=None, source=None, neighbors=1, max_chars=6000):
        calls.append(max_chars)
        return {"text": PAGE[:max_chars], "total_chars": len(PAGE), "truncated": len(PAGE) > max_chars}

    monkeypatch.setattr(server, "_fetch_document", fetch)
    return calls


def test_a_second_read_at_a_different_size_is_served_from_memory(monkeypatch):
    calls = counting_fetch(monkeypatch)
    small = server.fetch_document("https://www.sujeeth.org/report.pdf", None, None, 1, 6000)
    large = server.fetch_document("https://www.sujeeth.org/report.pdf", None, None, 1, 12000)
    assert len(calls) == 1 and calls[0] == server.DOCUMENT_MAX_CHARS
    assert len(small["text"]) == 6000 and small["truncated"] is True and small["_cached"] is False
    assert len(large["text"]) == 7000 and large["truncated"] is False and large["_cached"] is True


def test_the_default_read_returns_the_whole_annual_report_in_one_call(monkeypatch):
    counting_fetch(monkeypatch)
    out = server.get_documents.sync(["https://www.sujeeth.org/report.pdf"])
    doc = out["documents"][0]
    assert doc["truncated"] is False and len(doc["text"]) == 7000


def test_diagnostics_reach_the_agent_only_when_timings_are_on(monkeypatch):
    counting_fetch(monkeypatch)
    url = ["https://www.sujeeth.org/report.pdf"]
    monkeypatch.setattr(server, "TIMINGS", False)
    assert "ms" not in server.get_documents.sync(url)["documents"][0]
    monkeypatch.setattr(server, "TIMINGS", True)
    doc = server.get_documents.sync(url)["documents"][0]
    assert doc["cached"] is True and "ms" in doc
