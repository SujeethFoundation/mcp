"""A question the ontology can read with confidence skips the model call; anything uncertain still goes to the model."""
import os

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402

resolve_plan = server.resolve_ontology_plan   # captured before the shared fixture swaps in a stub

EXPAND = {"enabled": True, "effect": "expand"}


def catalog():
    return {
        "ontology_version": "local-test",
        "classes": [
            {"id": "resume_resource", "name": "Resume Resource", "aliases": ["resume writing", "resume tips"],
             "query_phrases": ["tailor my resume to a job"], "definition": "How to build a strong resume."},
            {"id": "interviewing", "name": "Interviewing", "aliases": ["job interview", "mock interview"],
             "query_phrases": ["prepare for an interview", "interview preparation", "interview questions for new graduates"],
             "definition": "Preparing for and performing well in job interviews."},
            {"id": "networking", "name": "Networking", "aliases": ["informational interview", "coffee chat"],
             "query_phrases": ["how do I network for a job", "meet people in my target industry"]},
            {"id": "validate", "name": "Validate", "aliases": ["informational interview"]},
            {"id": "career_circle_program", "name": "Career Circle Program", "aliases": ["Career Circles", "syllabus"]},
            {"id": "circle", "name": "Circle", "aliases": ["working out loud circle"]},
            {"id": "nervous", "name": "Nervous", "mood_meter": {"quadrant": "red"}},
            {"id": "spotlight", "name": "Spotlight"},
            {"id": "referral", "name": "Referral"},
            {"id": "outreach", "name": "Outreach"},
            {"id": "alumni", "name": "Alumni"},
        ],
        "predicates": [],
        "relationships": [
            {"subject": "spotlight", "predicate": "supports", "object": "networking", "search_behavior": EXPAND},
            {"subject": "networking", "predicate": "supports", "object": "referral", "search_behavior": EXPAND},
            {"subject": "networking", "predicate": "supports", "object": "outreach", "search_behavior": EXPAND},
            {"subject": "networking", "predicate": "supports", "object": "alumni",
             "search_behavior": {"enabled": False, "effect": "expand"}},
        ],
        "ambiguous_terms": [{"term": "circle", "candidates": ["circle", "career_circle_program"]},
                            {"term": "regret / failure", "candidates": ["interviewing", "networking"]}],
        "domains": [{"domain": "sujeeth.org", "search_enabled": True,
                     "associated_concepts": ["resume_resource", "interviewing", "networking", "spotlight",
                                             "career_circle_program", "referral", "outreach"]}],
    }


class Model:
    """Stands in for the Luna call and records when it is used."""
    def __init__(self, monkeypatch, matched=("interviewing",)):
        self.calls = []
        self.matched = list(matched)
        monkeypatch.setattr(server, "call_ontology_model", self)
        monkeypatch.setattr(server, "ONTOLOGY_PLAN_CACHE_SECONDS", 0)
        monkeypatch.setattr(server, "LOCAL_RESOLVE", True)
        server.clear_ontology_plan_cache()

    def __call__(self, question, *_rest):
        self.calls.append(question)
        return {"matched_concepts": self.matched, "related_concepts": [], "feelings": [], "intent": None,
                "topic_queries": ["model variant one", "model variant two", "model variant three"],
                "feeling_query": None, "needs_clarification": False, "suggested_question": None}


def test_unambiguous_question_skips_the_model(monkeypatch):
    model = Model(monkeypatch)
    plan = resolve_plan("How do I prepare for an interview?", catalog())
    assert model.calls == []
    resolution = plan["resolution"]
    assert resolution["status"] == "resolved" and resolution["resolved_by"] == "ontology"
    assert resolution["matched_concepts"][0] == "interviewing"
    assert resolution["domains"] == ["sujeeth.org"]
    assert resolution["concept_matches"][0] == {"concept_id": "interviewing", "source": "ontology_local"}
    assert not resolution["needs_clarification"] and resolution["feelings"] == []


def test_variants_come_from_the_ontology_with_the_question_first(monkeypatch):
    Model(monkeypatch)
    queries = resolve_plan("How do I prepare for an interview?", catalog())["queries"]
    assert queries[0] == "How do I prepare for an interview?"
    assert len(queries) == 3
    phrases = {"interview preparation", "interview questions for new graduates", "job interview", "mock interview"}
    assert set(queries[1:]) <= phrases                    # the concept's own query phrases and aliases
    assert all("performing well" not in q for q in queries)   # the definition is not used


def test_variants_favor_phrases_that_share_the_questions_own_intent():
    concept = catalog()["classes"][1]
    ranked = server.local_topic_queries("mock interview mistakes graduates avoid", "mock interview", concept)
    assert ranked[0] == "interview questions for new graduates"   # shares "graduates"; the others share nothing
    assert "mock interview" not in ranked                         # the matched phrase adds nothing


def test_a_question_that_matches_nothing_goes_to_the_model(monkeypatch):
    model = Model(monkeypatch, matched=())
    plan = resolve_plan("What's a good vodka tonic recipe?", catalog())
    assert model.calls == ["What's a good vodka tonic recipe?"]
    assert plan["resolution"]["status"] == "no_match" and plan["resolution"]["resolved_by"] == "model"


def test_two_concepts_go_to_the_model(monkeypatch):
    model = Model(monkeypatch)
    resolve_plan("resume tips and a mock interview", catalog())
    assert len(model.calls) == 1


def test_two_concepts_naming_the_same_words_go_to_the_model(monkeypatch):
    model = Model(monkeypatch)
    resolve_plan("What is an informational interview?", catalog())
    assert len(model.calls) == 1


def test_a_phrase_inside_another_concepts_phrase_is_not_a_second_concept(monkeypatch):
    model = Model(monkeypatch)
    plan = resolve_plan("How do I get ready for a mock interview?", catalog())   # "interview" is inside "mock interview"
    assert model.calls == [] and plan["resolution"]["matched_concepts"][0] == "interviewing"


def test_a_leftover_word_that_names_another_concept_goes_to_the_model(monkeypatch):
    model = Model(monkeypatch)
    resolve_plan("Can you help with my resume and interview preparation?", catalog())   # "resume" is not a phrase here
    assert len(model.calls) == 1


def test_a_feeling_goes_to_the_model(monkeypatch):
    model = Model(monkeypatch)
    resolve_plan("I feel nervous", catalog())
    resolve_plan("Interviewing makes me nervous", catalog())
    assert len(model.calls) == 2


def test_a_retrieval_context_for_the_matched_topic_goes_to_the_model(monkeypatch):
    model = Model(monkeypatch, matched=("networking",))
    context = {"feeling": "nervous", "career_context": "networking", "source": "previous_turn"}
    resolve_plan("How do I network for a job?", catalog(), context)
    assert len(model.calls) == 1
    resolve_plan("How do I prepare for an interview?", catalog(), context)   # a different topic: the context is moot
    assert len(model.calls) == 1


def test_an_ambiguous_word_left_in_the_question_goes_to_the_model(monkeypatch):
    model = Model(monkeypatch)
    resolve_plan("Can I see the syllabus for my circle?", catalog())
    assert len(model.calls) == 1


def test_an_ambiguous_word_inside_the_matched_phrase_does_not_stop_it(monkeypatch):
    model = Model(monkeypatch)
    plan = resolve_plan("What is a Career Circle?", catalog())   # "Career Circle" names the program; "circle" alone would not
    assert model.calls == [] and plan["resolution"]["matched_concepts"] == ["career_circle_program"]


def test_long_and_multi_sentence_questions_go_to_the_model(monkeypatch):
    model = Model(monkeypatch)
    resolve_plan("How do I prepare for an interview? I have one on Friday.", catalog())
    resolve_plan("How do I prepare for an interview " + "really " * 30 + "well?", catalog())
    assert len(model.calls) == 2


def test_the_shortcut_can_be_switched_off(monkeypatch):
    model = Model(monkeypatch)
    monkeypatch.setattr(server, "LOCAL_RESOLVE", False)
    resolve_plan("How do I prepare for an interview?", catalog())
    assert len(model.calls) == 1


def test_related_concepts_follow_enabled_edges_and_are_capped(monkeypatch):
    Model(monkeypatch)
    monkeypatch.setattr(server, "LOCAL_RESOLVE_MAX_RELATED", 2)
    concepts = resolve_plan("How do I network for a job?", catalog())["resolution"]["matched_concepts"]
    assert concepts[0] == "networking" and len(concepts) == 3
    assert "alumni" not in concepts                      # its edge is disabled
    monkeypatch.setattr(server, "LOCAL_RESOLVE_MAX_RELATED", 0)
    assert resolve_plan("How do I network for a job?", catalog())["resolution"]["matched_concepts"] == ["networking"]


def test_counts_show_how_often_the_model_is_skipped(monkeypatch):
    Model(monkeypatch)
    before = server.resolution_counts()
    resolve_plan("How do I prepare for an interview?", catalog())
    resolve_plan("What's a good vodka tonic recipe?", catalog())
    after = server.resolution_counts()
    assert after["ontology"] == before["ontology"] + 1 and after["model"] == before["model"] + 1
    assert after["skipped"]["no_confident_match"] == before["skipped"].get("no_confident_match", 0) + 1
