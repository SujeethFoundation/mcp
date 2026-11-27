"""Tools the ontology says support a matched concept are offered when the question asks for one or one fits."""
import os

os.environ.setdefault("MCP_TRUSTED_RANGE_URLS", "none")

import pytest  # noqa: E402

import mcp_server as server  # noqa: E402


def tool(tool_id, name, url="https://example.org/" , **extra):
    return {"id": tool_id, "category": "tool", "name": name, "definition": f"{name} helps students.",
            "tool": {"url": url + tool_id, "kind": "app", "connect": f"Open {name}.", "fit_when": f"when using {name}", **extra}}


def supports(tool_id, concept, weight=None, reason=None):
    row = {"subject": tool_id, "predicate": "supports", "object": concept}
    if weight is not None:
        row["weight"] = weight
    if reason:
        row["reason"] = reason
    return row


CATALOG = {
    "ontology_version": "tools-test",
    "classes": [
        {"id": "networking", "name": "Networking", "category": "skill"},
        {"id": "resume_resource", "name": "Resume Resource", "category": "skill"},
        {"id": "career_development", "name": "Career Development", "category": "skill"},
        tool("youspot", "YouSpot"), tool("teal", "Teal"), tool("indeed", "Indeed"), tool("foundation_mcp", "Foundation MCP"),
        {"id": "no_link", "category": "tool", "name": "No Link", "tool": {"kind": "app"}},
    ],
    "relationships": [
        supports("youspot", "networking", 0.9, "Research the people behind posts before you write to them."),
        supports("teal", "resume_resource", 0.8), supports("teal", "networking", 0.3),
        supports("indeed", "career_development", 0.6), supports("foundation_mcp", "career_development", 0.5),
        supports("no_link", "networking", 1.0), supports("ghost_tool", "networking", 1.0),
    ],
    "domains": [],
}


def names(found):
    return [row["id"] for row in found]


def test_a_matched_concept_brings_its_tools_best_first():
    found = server.suggest_tools(CATALOG, ["networking"], "asked")
    assert names(found) == ["youspot", "teal"]
    assert found[0]["why"] == "Research the people behind posts before you write to them."
    assert found[0]["url"] == "https://example.org/youspot" and found[0]["connect"] == "Open YouSpot."
    assert found[1]["why"] == "Teal helps students."          # no reason on the edge: the definition


def test_an_mcp_address_and_where_it_is_available_come_through(monkeypatch):
    catalog = {**CATALOG, "classes": [tool("youspot", "YouSpot", mcp_url="https://example.org/mcp",
                                           available_in=["Claude connector directory", "ChatGPT", 7])]}
    found = server.suggest_tools(catalog, ["networking"], "asked")
    assert found[0]["mcp_url"] == "https://example.org/mcp"
    assert found[0]["available_in"] == ["Claude connector directory", "ChatGPT"]
    assert "mcp_url" not in server.suggest_tools(CATALOG, ["networking"], "asked")[0]


def test_only_tools_for_matched_concepts_are_offered():
    assert names(server.suggest_tools(CATALOG, ["resume_resource"], "fits")) == ["teal"]
    assert server.suggest_tools(CATALOG, ["unrelated"], "asked") == []


def test_no_tool_intent_means_no_tools():
    assert server.suggest_tools(CATALOG, ["networking"], "none") == []
    assert server.suggest_tools(CATALOG, [], "asked") == []


def test_a_tool_without_a_link_or_a_class_is_never_offered():
    assert "no_link" not in names(server.suggest_tools(CATALOG, ["networking"], "asked"))
    assert "ghost_tool" not in names(server.suggest_tools(CATALOG, ["networking"], "asked"))


def test_the_count_is_capped_and_can_be_turned_off(monkeypatch):
    assert len(server.suggest_tools(CATALOG, ["networking", "resume_resource", "career_development"], "asked")) == 2
    monkeypatch.setattr(server, "SUGGESTED_TOOLS_MAX", 1)
    assert names(server.suggest_tools(CATALOG, ["networking"], "asked")) == ["youspot"]
    monkeypatch.setattr(server, "SUGGESTED_TOOLS_MAX", 0)
    assert server.suggest_tools(CATALOG, ["networking"], "asked") == []


def test_a_tool_that_supports_several_matched_concepts_ties_ahead():
    found = server.suggest_tools(CATALOG, ["career_development", "networking", "resume_resource"], "fits")
    assert names(found)[:2] == ["youspot", "teal"]          # 0.9, then 0.8 with two matched concepts


def result(**extra):
    return {"matched_concepts": ["networking"], "related_concepts": [], "feelings": [], "intent": None,
            "topic_queries": ["a", "b", "c"], "feeling_query": None, "needs_clarification": False,
            "suggested_question": None, **extra}


def test_the_plan_carries_the_tool_intent_and_the_tools():
    plan = server.validate_ontology_plan(result(tool_intent="asked"), "tools for research", CATALOG)
    assert plan["resolution"]["tool_intent"] == "asked"
    assert names(plan["resolution"]["suggested_tools"]) == ["youspot", "teal"]


@pytest.mark.parametrize("value", [None, "always", 3])
def test_an_unknown_tool_intent_is_none(value):
    extra = {} if value is None else {"tool_intent": value}
    plan = server.validate_ontology_plan(result(**extra), "q", CATALOG)
    assert plan["resolution"]["tool_intent"] == "none" and plan["resolution"]["suggested_tools"] == []


def test_the_model_is_asked_for_a_tool_intent():
    assert "tool_intent" in server.ONTOLOGY_PLAN_SCHEMA["required"]
    assert server.ONTOLOGY_PLAN_SCHEMA["properties"]["tool_intent"]["enum"] == ["asked", "fits", "none"]
    assert "tool_intent" in server.ONTOLOGY_RESOLUTION_PROMPT


def quality(intent):
    plan = server.validate_ontology_plan(result(tool_intent=intent), "q", CATALOG)
    return server.assess_quality(plan["resolution"], [{"snippet": "text"}], [])


def test_the_response_lists_the_tools_and_tells_the_agent_how_to_offer_them():
    asked = quality("asked")
    assert names(asked["suggested_tools"]) == ["youspot", "teal"]
    assert "suggested_tools are optional" in asked["agent_instructions"] and "they asked for tools" in asked["agent_instructions"]
    assert "Never present a tool as required" in asked["agent_instructions"]
    fits = quality("fits")
    assert "answer first and mention at most one at the end" in fits["agent_instructions"]


def test_no_tools_means_no_field_and_no_instruction():
    none = quality("none")
    assert "suggested_tools" not in none and "suggested_tools are optional" not in none["agent_instructions"]
