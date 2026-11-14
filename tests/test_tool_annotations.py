"""Every tool in tools/list carries readOnlyHint, destructiveHint and openWorldHint."""
import asyncio
import importlib


def _tools(monkeypatch):
    monkeypatch.setenv("MCP_ADMIN_TOOLS", "true")
    import mcp_server
    server = importlib.reload(mcp_server)
    return server, asyncio.run(server.mcp.list_tools())


def test_server_name(monkeypatch):
    server, _ = _tools(monkeypatch)
    assert server.mcp.name == "Sujeeth Foundation Graph"


def test_every_tool_has_all_three_hints(monkeypatch):
    _, tools = _tools(monkeypatch)
    assert {t.name for t in tools} >= {
        "search_sujeeth_foundation", "get_documents", "search_website", "search_wiki",
        "check_services", "resolve_ontology", "get_ontology_context",
    }
    for t in tools:
        dumped = t.annotations.model_dump(by_alias=True, exclude_none=True)
        assert dumped["readOnlyHint"] is True, t.name
        assert dumped["destructiveHint"] is False, t.name
        assert isinstance(dumped["openWorldHint"], bool), t.name


def test_open_world_split(monkeypatch):
    _, tools = _tools(monkeypatch)
    open_world = {t.name: t.annotations.openWorldHint for t in tools}
    assert open_world["search_wiki"] is False
    assert open_world["search_website"] is True
    assert open_world["get_documents"] is True
    assert open_world["search_sujeeth_foundation"] is True
