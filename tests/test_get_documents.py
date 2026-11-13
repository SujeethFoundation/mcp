"""get_documents is the only tool that reads pages. One url, several urls and a wiki url all go through it."""
import os
import re

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402


def read(urls, **kwargs):
    return server.get_documents.sync(urls, **kwargs)


def test_only_get_documents_reads_pages():
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert "get_documents" in names and "get_document" not in names


def test_one_url_reads_one_page(monkeypatch):
    monkeypatch.setattr(server, "fetch_document",
                        lambda url, *a, **k: {"title": "T", "text": "body", "total_chars": 4, "truncated": False})
    out = read(["https://www.sujeeth.org/circle"])
    assert out["count"] == 1
    assert out["documents"][0]["status"] == "ok" and out["documents"][0]["text"] == "body"


def test_instructions_point_at_get_documents(monkeypatch):
    monkeypatch.setattr(server, "fetch_document", lambda url, *a, **k: {"text": "body", "truncated": True})
    text = read(["https://www.sujeeth.org/circle"])["agent_instructions"]
    assert "get_documents" in text and not re.search(r"get_document\b", text)


def test_urls_over_the_batch_limit_are_reported_not_read(monkeypatch):
    monkeypatch.setattr(server, "MAX_BATCH_DOCUMENTS", 2)
    monkeypatch.setattr(server, "fetch_document", lambda url, *a, **k: {"text": "body"})
    urls = [f"https://www.sujeeth.org/p{i}" for i in range(4)]
    out = read(urls)
    assert out["count"] == 2 and out["not_read"] == urls[2:] and "Only the first 2" in out["note"]


def test_a_failed_page_does_not_fail_the_others(monkeypatch):
    def fetch(url, *a, **k):
        if url.endswith("bad"):
            raise RuntimeError("boom")
        return {"text": "body"}

    monkeypatch.setattr(server, "fetch_document", fetch)
    out = read(["https://www.sujeeth.org/ok", "https://www.sujeeth.org/bad"])
    assert [d["status"] for d in out["documents"]] == ["ok", "error"]


def test_a_wiki_url_goes_to_the_wiki_app(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "DOCUMENT_CACHE_SECONDS", 0)
    monkeypatch.setattr(server, "post_json", lambda url, payload, key="", timeout=None: calls.append(url) or {"text": "x"})
    read(["https://github.com/SujeethFoundation/sujeeth-wiki/wiki/FAQ"])
    assert calls and calls[0].startswith(server.WIKISEARCH_URL)


def test_no_url_is_an_error():
    assert "error" in read([])
