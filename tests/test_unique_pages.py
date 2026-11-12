"""The combined answer holds each page once, whether it came back as www.sujeeth.org or sujeeth.org."""
import os

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402


def item(url, **extra):
    return {"url": url, "title": url, **extra}


def test_one_key_for_a_page_however_it_was_reached():
    keys = {server.page_identity(item(u)) for u in ("https://www.sujeeth.org/circle", "https://sujeeth.org/circle/", "http://SUJEETH.org/Circle#week-4")}
    assert keys == {"sujeeth.org/circle"}
    assert server.page_identity(item("https://sujeeth.org/circles")) != "sujeeth.org/circle"
    assert server.page_identity({"url": "", "title": " Some Page "}) == "some page" and server.page_identity({}) == ""
    assert server.page_identity(item("https://x.org/a?b=1")) == "x.org/a?b=1"


def test_a_best_bet_and_the_same_page_in_the_ordinary_results_appear_once_even_as_www():
    website = [item("https://www.sujeeth.org/circle", best_bet=True, source="website", domain="sujeeth.org"),
               item("https://sujeeth.org/circle/", source="website", domain="sujeeth.org"),
               item("https://sujeeth.org/volunteer", source="website", domain="sujeeth.org")]
    combined, best, spotlights = server.combine_with_best_bets([], website, 5)
    assert [r["url"] for r in combined] == ["https://www.sujeeth.org/circle", "https://sujeeth.org/volunteer"] and best == 1 and spotlights == 0


def test_a_spotlight_and_the_same_page_in_the_ordinary_results_appear_once():
    website = [item("https://sujeeth.org/spotlight/x/", spotlight=True, source="website", domain="sujeeth.org"),
               item("https://www.sujeeth.org/spotlight/x", source="website", domain="sujeeth.org"),
               item("https://tealhq.com/post/a", source="website", domain="tealhq.com")]
    combined, _, spotlights = server.combine_with_best_bets([], website, 5)
    assert [r["url"] for r in combined] == ["https://sujeeth.org/spotlight/x/", "https://tealhq.com/post/a"] and spotlights == 1


def test_wiki_and_website_results_for_the_same_page_appear_once():
    wiki = [item("https://github.com/o/r/wiki/Home", source="wiki")]
    website = [item("https://github.com/o/r/wiki/home/", source="website", domain="github.com"), item("https://sujeeth.org/a", source="website", domain="sujeeth.org")]
    combined, _, _ = server.combine_with_best_bets(wiki, website, 5)
    assert len(combined) == 2 and combined[0]["source"] == "wiki"


def test_pages_without_an_address_are_told_apart_by_title():
    combined, _, _ = server.combine_with_best_bets([], [{"title": "A", "source": "website"}, {"title": "a", "source": "website"}, {"title": "B", "source": "website"}], 5)
    assert [r["title"] for r in combined] == ["A", "B"]
