"""Tests for the retry of calls to the website search app: a 503 (the instance is still loading its index) or an
unreachable app is tried again, within the one request timeout; everything else fails as before."""
import os

import pytest
import requests

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402

URL = server.WEBSEARCH_URL + "/api/search"


def reply(status=200, headers=None, body=None):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = (body if body is not None else b'{"results": []}')
    return response


class Fake:
    """Stands in for requests.Session.post and for the retry's clock and sleep. A sleep moves the clock."""

    def __init__(self, monkeypatch, outcomes):
        self.outcomes = list(outcomes)
        self.timeouts, self.sleeps, self.now = [], [], 0.0
        monkeypatch.setattr(server.http, "post", self.post)
        monkeypatch.setattr(server, "_clock", lambda: self.now)
        monkeypatch.setattr(server, "_sleep", self.sleep)
        monkeypatch.setattr(server, "_jitter", lambda: 0.0)

    def post(self, url, headers=None, json=None, timeout=None):
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    @property
    def calls(self):
        return len(self.timeouts)


def call(**kw):
    return server.post_json_with_retry(URL, {"query": "q"}, "key", **kw)


def test_a_first_try_that_works_is_not_retried(monkeypatch):
    fake = Fake(monkeypatch, [reply()])
    assert call() == {"results": []}
    assert fake.calls == 1 and fake.sleeps == []


def test_a_503_is_retried_after_the_wait_it_asks_for(monkeypatch):
    fake = Fake(monkeypatch, [reply(503, {"Retry-After": "2"}), reply()])
    assert call() == {"results": []}
    assert fake.calls == 2 and fake.sleeps == [2.0]


def test_a_503_without_retry_after_waits_the_default(monkeypatch):
    fake = Fake(monkeypatch, [reply(503), reply()])
    call()
    assert fake.sleeps == [server.RETRY_DEFAULT_WAIT]


def test_an_unusable_retry_after_waits_the_default(monkeypatch):
    fake = Fake(monkeypatch, [reply(503, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), reply(503, {"Retry-After": "-5"}), reply()])
    call()
    assert fake.sleeps == [server.RETRY_DEFAULT_WAIT, server.RETRY_DEFAULT_WAIT]


def test_a_long_retry_after_is_capped(monkeypatch):
    fake = Fake(monkeypatch, [reply(503, {"Retry-After": "120"}), reply()])
    call()
    assert fake.sleeps == [server.RETRY_MAX_WAIT]


def test_the_jitter_is_added_to_the_wait(monkeypatch):
    fake = Fake(monkeypatch, [reply(503, {"Retry-After": "2"}), reply()])
    monkeypatch.setattr(server, "_jitter", lambda: 0.25)
    call()
    assert fake.sleeps == [2.25]


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 502, 504])
def test_other_statuses_are_not_retried(monkeypatch, status):
    fake = Fake(monkeypatch, [reply(status)])
    with pytest.raises(requests.HTTPError) as caught:
        call()
    assert caught.value.response.status_code == status
    assert fake.calls == 1 and fake.sleeps == []


def test_a_connection_error_is_retried(monkeypatch):
    fake = Fake(monkeypatch, [requests.ConnectionError("reset"), reply()])
    assert call() == {"results": []}
    assert fake.calls == 2 and fake.sleeps == [server.RETRY_DEFAULT_WAIT]


def test_an_error_that_is_not_a_network_error_is_not_retried(monkeypatch):
    fake = Fake(monkeypatch, [ValueError("bad")])
    with pytest.raises(ValueError):
        call()
    assert fake.calls == 1


def test_it_gives_up_after_the_attempts_and_raises_the_last_503(monkeypatch):
    fake = Fake(monkeypatch, [reply(503, {"Retry-After": "1"})])
    with pytest.raises(requests.HTTPError) as caught:
        call(attempts=4)
    assert caught.value.response.status_code == 503
    assert fake.calls == 4 and len(fake.sleeps) == 3


def test_attempts_of_one_means_no_retry(monkeypatch):
    fake = Fake(monkeypatch, [reply(503)])
    with pytest.raises(requests.HTTPError):
        call(attempts=1)
    assert fake.calls == 1 and fake.sleeps == []


def test_all_tries_together_stay_within_the_request_timeout(monkeypatch):
    # 8 s waits (the cap) against a 30 s timeout with 4 s needed to try again: tries at 0, 8, 16 and 24 would leave
    # -2 s at the fourth wait, so the fourth try is not made and the third failure is raised.
    monkeypatch.setattr(server, "TIMEOUT", 30)
    fake = Fake(monkeypatch, [reply(503, {"Retry-After": "8"})])
    with pytest.raises(requests.HTTPError):
        call(attempts=10)
    assert fake.calls == 4 and fake.sleeps == [8.0, 8.0, 8.0]
    assert fake.now <= 30


def test_each_try_is_given_only_the_time_that_is_left(monkeypatch):
    monkeypatch.setattr(server, "TIMEOUT", 30)
    fake = Fake(monkeypatch, [reply(503, {"Retry-After": "8"})])
    with pytest.raises(requests.HTTPError):
        call(attempts=10)
    assert fake.timeouts == [30, 22, 14, 6]
    assert all(t <= server.TIMEOUT for t in fake.timeouts)


def test_a_slow_first_failure_leaves_no_room_for_a_retry(monkeypatch):
    monkeypatch.setattr(server, "TIMEOUT", 30)
    fake = Fake(monkeypatch, [reply(503, {"Retry-After": "3"})])
    original_post = fake.post

    def slow_post(*a, **k):
        fake.now += 27          # the first try used up most of the time
        return original_post(*a, **k)

    monkeypatch.setattr(server.http, "post", slow_post)
    with pytest.raises(requests.HTTPError):
        call()
    assert fake.calls == 1 and fake.sleeps == []


def test_retries_are_counted_with_the_calls(monkeypatch):
    server._downstream.clear()
    Fake(monkeypatch, [reply(503, {"Retry-After": "1"}), reply(503, {"Retry-After": "1"}), reply()])
    call()
    stats = server.downstream_stats()["websearch"]
    assert stats["calls"] == 3 and stats["errors"] == 2 and stats["retries"] == 2


def test_a_service_that_was_never_retried_reports_zero_retries():
    server._downstream.clear()
    server._note_call(server.WIKISEARCH_URL + "/x", 0.1, False)
    assert server.downstream_stats()["wikisearch"]["retries"] == 0


# -- the callers ----------------------------------------------------------------------------------------------------

def test_search_website_recovers_from_a_loading_instance(monkeypatch):
    server._downstream.clear()
    Fake(monkeypatch, [reply(503, {"Retry-After": "1"}), reply(body=b'{"results": [{"title": "T"}]}')])
    assert server.search_website.sync("career circle", concepts=["circle"], domains=["sujeeth.org"]) == {"results": [{"title": "T"}]}


def test_search_website_reports_an_error_when_the_instance_never_loads(monkeypatch):
    Fake(monkeypatch, [reply(503, {"Retry-After": "1"})])
    assert server.search_website.sync("career circle", concepts=["circle"], domains=["sujeeth.org"]) == {"error": "Service returned HTTP 503."}


def test_the_website_document_read_is_retried(monkeypatch):
    fake = Fake(monkeypatch, [reply(503), reply(body=b'{"text": "page"}')])
    assert server._fetch_document(url="https://www.sujeeth.org/circle") == {"text": "page"}
    assert fake.calls == 2


def test_a_wiki_document_read_is_not_retried(monkeypatch):
    fake = Fake(monkeypatch, [reply(503)])
    with pytest.raises(requests.HTTPError):
        server._fetch_document(chunk_id="notes/week1.md", source="wiki")
    assert fake.calls == 1


# -- the combined search --------------------------------------------------------------------------------------------

def routed(monkeypatch, search_outcomes):
    """The combined search with the ontology resolving, the wiki answering and /api/search following search_outcomes."""
    fake = Fake(monkeypatch, search_outcomes)
    search_post = fake.post

    def post(url, headers=None, json=None, timeout=None):
        if url.endswith("/api/domain-resolution"):
            return reply(body=b'{"status": "resolved", "matched_concepts": ["circle"], "domains": ["sujeeth.org"]}')
        return search_post(url, headers=headers, json=json, timeout=timeout)

    monkeypatch.setattr(server.http, "post", post)
    monkeypatch.setattr(server, "get_json", lambda url, key="", params=None: {"results": []})
    return fake


WEBSITE_HIT = (b'{"status": "completed", "results": [{"title": "Career Circle", "url": "https://www.sujeeth.org/circle", '
               b'"snippet": "A six week program.", "source": "website", "domain": "sujeeth.org", "chunk_id": "c-0"}]}')


def test_the_combined_search_completes_when_the_first_try_hits_a_loading_instance(monkeypatch):
    fake = routed(monkeypatch, [reply(503, {"Retry-After": "1"}), reply(body=WEBSITE_HIT)])
    result = server.search_sujeeth_foundation.sync("career circle", full_documents=False)
    assert result["status"] == "completed" and not result["service_errors"]
    assert [r["url"] for r in result["results"]] == ["https://www.sujeeth.org/circle"]
    assert fake.calls == 2


def test_the_combined_search_is_partial_when_the_instance_never_loads(monkeypatch):
    routed(monkeypatch, [reply(503, {"Retry-After": "1"})])
    result = server.search_sujeeth_foundation.sync("career circle", full_documents=False)
    assert result["status"] == "partial"
    assert result["service_errors"] == [{"service": "websearch", "error": "Service returned HTTP 503."}]
