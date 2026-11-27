import time

import mcp_server as server


class Reply:
    def __init__(self, data):
        self.data = data

    def json(self):
        return self.data

    def raise_for_status(self):
        pass


ONTOLOGY = {
    "predicates": [
        {"id": "illustrates", "search_behavior": {"effect": "expand", "weight": 0}},
        {"id": "supports", "search_behavior": {"effect": "boost", "weight": 0.25}},
        {"id": "is_distinct_from", "search_behavior": {"effect": "suppress", "weight": 0.5}},
        {"id": "precedes"},
        {"id": "informs"},
    ],
    "relationships": [
        {"subject": "resume_resource", "predicate": "illustrates", "object": "week_3"},
        {"subject": "networking", "predicate": "supports", "object": "referral"},
        {"subject": "interview_feedback", "predicate": "is_distinct_from", "object": "feedback_survey"},
        {"subject": "week_1", "predicate": "precedes", "object": "week_2"},
        {"subject": "a", "predicate": "informs", "object": "b"},
        {"subject": "c", "predicate": "informs", "object": "d", "search_behavior": {"effect": "boost", "weight": 0.25}},
        {"subject": "e", "predicate": "supports", "object": "f", "search_behavior": {"enabled": False}},
        {"subject": "g", "predicate": "illustrates", "object": "h", "search_behavior": {"effect": "suppress"}},
    ],
}


def reset(monkeypatch):
    monkeypatch.setattr(server, "_linked_pairs", None)
    monkeypatch.setattr(server, "_linked_pairs_loaded_at", None)


def test_expand_and_boost_links_are_one_answer_and_suppress_links_are_not():
    pairs = server.pairs_from_ontology(ONTOLOGY)
    assert frozenset(("resume_resource", "week_3")) in pairs
    assert frozenset(("networking", "referral")) in pairs
    assert frozenset(("interview_feedback", "feedback_survey")) not in pairs
    assert frozenset(("week_1", "week_2")) not in pairs


def test_a_predicate_with_no_effect_is_not_linked_but_a_relationship_can_set_its_own():
    pairs = server.pairs_from_ontology(ONTOLOGY)
    assert frozenset(("a", "b")) not in pairs
    assert frozenset(("c", "d")) in pairs


def test_a_relationship_can_opt_out_or_change_its_effect():
    pairs = server.pairs_from_ontology(ONTOLOGY)
    assert frozenset(("e", "f")) not in pairs
    assert frozenset(("g", "h")) not in pairs


def test_an_older_ontology_without_effects_still_links_illustrates_only():
    old = {"predicates": [{"id": "illustrates"}, {"id": "supports"}], "relationships": [
        {"subject": "x", "predicate": "illustrates", "object": "y"},
        {"subject": "p", "predicate": "supports", "object": "q"}]}
    assert server.pairs_from_ontology(old) == {frozenset(("x", "y"))}


def test_the_pairs_are_reloaded_after_the_refresh_interval(monkeypatch):
    reset(monkeypatch)
    calls = []
    versions = [{"classes": [], "relationships": [{"subject": "x", "predicate": "illustrates", "object": "y"}]},
                {"classes": [], "relationships": [{"subject": "m", "predicate": "illustrates", "object": "n"}]}]

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return Reply(versions[len(calls) - 1])

    monkeypatch.setattr(server.requests, "get", fake_get)
    assert server.linked_concept_pairs() == {frozenset(("x", "y"))}
    assert server.linked_concept_pairs() == {frozenset(("x", "y"))} and len(calls) == 1
    monkeypatch.setattr(server, "_linked_pairs_loaded_at", time.monotonic() - server.LINKED_PAIRS_REFRESH_SECONDS - 1)
    assert server.linked_concept_pairs() == {frozenset(("m", "n"))} and len(calls) == 2


def test_a_failed_refresh_keeps_the_last_good_pairs_and_retries(monkeypatch):
    reset(monkeypatch)
    good = {frozenset(("x", "y"))}
    monkeypatch.setattr(server, "_linked_pairs", good)
    monkeypatch.setattr(server, "_linked_pairs_loaded_at", time.monotonic() - server.LINKED_PAIRS_REFRESH_SECONDS - 1)

    def boom(url, headers=None, timeout=None):
        raise RuntimeError("down")

    monkeypatch.setattr(server.requests, "get", boom)
    assert server.linked_concept_pairs() == good
    assert server._linked_pairs is good  # still stale, so the next question tries again


def test_a_first_load_failure_is_not_cached(monkeypatch):
    reset(monkeypatch)
    monkeypatch.setattr(server.requests, "get", lambda url, headers=None, timeout=None: (_ for _ in ()).throw(RuntimeError("down")))
    assert server.linked_concept_pairs() == set()
    assert server._linked_pairs is None
