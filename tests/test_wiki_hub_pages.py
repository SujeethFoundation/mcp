"""A wiki overview page and the pages it links to are kept together, outside the per-source cap."""
import os

import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402

WIKI = "https://github.com/x/wiki/"
WEEKS = [f"{WIKI}Week-{n}" for n in range(1, 7)]


def reply(body):
    response = requests.Response()
    response.status_code = 200
    response._content = body
    return response


def wiki_item(name, **extra):
    url = f"{WIKI}{name}"
    return {"title": name.replace("-", " "), "url": url, "snippet": f"{name} agenda", "source": "wiki",
            "path": f"{name}.md", "chunk_id": f"{name}.md", "text": f"Full text of {name}.", "total_chars": 20, **extra}


def hub_response():
    """What the wiki app returns for 'weekly agendas': the overview, then the six weeks in link order."""
    return [wiki_item("Weekly-Content", hub_pages=WEEKS)] + [wiki_item(f"Week-{n}", hub_of=f"{WIKI}Weekly-Content") for n in range(1, 7)]


def wire(monkeypatch, wiki_items, website_count=1):
    def post(url, headers=None, json=None, timeout=None):
        sites = ", ".join(
            '{"title": "Alerts %d", "url": "https://sujeeth.org/alerts/%d", "snippet": "x", "source": "website", '
            '"domain": "sujeeth.org", "chunk_id": "w-%d"}' % (n, n, n) for n in range(website_count)
        )
        return reply(
            ('{"status": "completed", "resolution": {"status": "resolved", "matched_concepts": ["agenda", "week"], '
             '"domains": ["sujeeth.org"]}, "results": [%s]}' % sites).encode()
        )

    monkeypatch.setattr(server.http, "post", post)
    monkeypatch.setattr(server, "get_json", lambda url, key="", params=None: {"results": wiki_items})


def titles(result, source):
    return [r["title"] for r in result["results"] if r["source"] == source]


def test_the_overview_and_every_page_it_links_to_come_back_together(monkeypatch):
    wire(monkeypatch, hub_response())
    result = server.search_sujeeth_foundation.sync("weekly agendas")
    assert titles(result, "wiki") == ["Weekly Content"] + [f"Week {n}" for n in range(1, 7)]
    assert titles(result, "website") == ["Alerts 0"]
    # Page text is left for get_documents; helper fields do not leave the server.
    weeks = [r for r in result["results"] if r["title"].startswith("Week ")]
    assert all("document" not in r for r in weeks)
    assert not any(k.startswith("_") or k in ("hub_pages", "hub_of") for r in result["results"] for k in r)


def test_pages_without_an_overview_are_still_capped_per_source(monkeypatch):
    plain = [wiki_item(f"Page-{n}") for n in range(5)]
    wire(monkeypatch, plain)
    result = server.search_sujeeth_foundation.sync("volunteer")
    assert titles(result, "wiki") == ["Page 0", "Page 1"]


def test_the_overview_pages_sit_outside_the_result_limit_and_do_not_crowd_out_other_sources(monkeypatch):
    wire(monkeypatch, hub_response(), website_count=5)
    result = server.search_sujeeth_foundation.sync("weekly agendas", limit=2)
    assert len(titles(result, "wiki")) == 7 and len(titles(result, "website")) == 2


def test_the_page_count_is_capped_and_can_be_turned_off(monkeypatch):
    wire(monkeypatch, hub_response())
    monkeypatch.setattr(server, "WIKI_HUB_MAX", 4)
    result = server.search_sujeeth_foundation.sync("weekly agendas")
    assert titles(result, "wiki") == ["Weekly Content", "Week 1", "Week 2", "Week 3"]
    monkeypatch.setattr(server, "WIKI_HUB_MAX", 0)
    result = server.search_sujeeth_foundation.sync("weekly agendas")
    assert titles(result, "wiki") == ["Weekly Content", "Week 1"]  # the old behaviour: the per-source cap


def test_an_overview_page_listed_twice_appears_once(monkeypatch):
    items = hub_response()
    items.append(dict(items[1]))
    wire(monkeypatch, items)
    result = server.search_sujeeth_foundation.sync("weekly agendas")
    assert titles(result, "wiki").count("Week 1") == 1
