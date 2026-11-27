"""The carried career context counts as active whether the caller took it from the last turn or from a summary."""
import json
import os

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import mcp_server as server  # noqa: E402

CATALOG = {
    "ontology_version": "ctx-test",
    "classes": [
        {"id": "networking", "name": "Networking"},
        {"id": "interviewing", "name": "Interviewing"},
        {"id": "anxiety", "name": "Anxiety", "mood_meter": {"quadrant": "red"}},
    ],
    "relationships": [],
}


def context(source):
    return server.clean_retrieval_context({"feeling": "anxiety", "career_context": "networking", "source": source}, CATALOG)


def test_the_career_context_is_a_concept_the_model_can_choose():
    for source in ("previous_turn", "summary", None):
        packet, allowed = server.ontology_model_context("is there an easy way to research a person?", CATALOG, context(source))
        assert {"anxiety", "networking"} <= {row["id"] for row in packet["classes"]}
        assert allowed is None or {"anxiety", "networking"} <= set(allowed)


def test_a_career_context_that_is_not_a_concept_is_not_added():
    ctx = server.clean_retrieval_context({"feeling": "anxiety", "career_context": "salary talk"}, CATALOG)
    packet, _ = server.ontology_model_context("research a person", CATALOG, ctx)
    assert "salary talk" not in json.dumps(packet)


def test_the_model_gets_the_same_context_whatever_the_source(monkeypatch):
    sent = []

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"output_text": json.dumps({"matched_concepts": ["networking"]}), "status": "completed"}

    monkeypatch.setattr(server, "ONTOLOGY_MODEL_ENDPOINT", "https://model.example")
    monkeypatch.setattr(server, "ONTOLOGY_MODEL_API_KEY", "k")
    monkeypatch.setattr(server.http, "post", lambda url, headers=None, json=None, timeout=None: sent.append(json) or Reply())
    monkeypatch.setattr(server, "_responses_output_text", lambda payload: payload["output_text"])
    for source in ("previous_turn", "summary", None):
        server.call_ontology_model("research a person", {"classes": []}, context(source))
    inputs = [json.loads(request["input"]) for request in sent]
    assert len(inputs) == 3
    assert all(item["conversation_context"] == {"feeling": "anxiety", "career_context": "networking"} for item in inputs)


def test_the_prompt_treats_a_summary_as_active_context():
    assert "immediately preceding" not in server.ONTOLOGY_RESOLUTION_PROMPT
    assert "summary" in server.ONTOLOGY_RESOLUTION_PROMPT
    assert "short follow-up" in server.ONTOLOGY_RESOLUTION_PROMPT
