import json
import types

import mcp_server


def test_the_timing_line_says_how_long_a_resolution_waited_for_a_slot(monkeypatch, capsys):
    monkeypatch.setattr(mcp_server, "ONTOLOGY_MODEL_ENDPOINT", "https://example.test")
    monkeypatch.setattr(mcp_server, "ONTOLOGY_MODEL_API_KEY", "k")
    body = {"output_text": json.dumps({"matched_concepts": []}), "status": "completed",
            "usage": {"input_tokens": 10, "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 3}}}
    response = types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: body, status_code=200)
    monkeypatch.setattr(mcp_server.http, "post", lambda *a, **k: response)
    mcp_server.call_ontology_model("how do i volunteer", {"classes": [], "relationships": []})
    line = next(l for l in capsys.readouterr().err.splitlines() if l.startswith("ontology_model_timing_ms"))
    fields = json.loads(line.split(" ", 1)[1])
    assert fields["queue_wait_ms"] >= 0 and fields["attempts"] == 1 and fields["status"] == "completed"
    assert fields["reasoning_tokens"] == 3 and fields["waiting_behind"] == 0
    assert mcp_server._ontology_model_slots.acquire(blocking=False)      # the slot was given back
    mcp_server._ontology_model_slots.release()
