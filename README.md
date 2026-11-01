# Sujeeth Foundation MCP Server — Local Test Package

This package runs the Sujeeth Foundation MCP server locally and connects it to:

- the deployed Azure Wiki Search Function App;
- LangSearch Web Search, using ontology-matched website domains;
- the local Foundation ontology JSON file.

## 1. Requirements

- Python 3.10 or newer (Python 3.11 or 3.12 is a good choice).
- A LangSearch API key.
- A newly rotated Azure Function key for the Wiki Search endpoint.

The MCP server uses the official Python MCP SDK's `FastMCP` interface.

## 2. Configure secrets

Copy `.env.example` to `.env` and fill in the two API keys:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and replace the placeholder values. Do not commit `.env` to GitHub. Both keys were previously shared in chat/URLs, so rotate them before using them for testing.

`WIKI_SEARCH_URL` should be the endpoint only, without `?q=...` or `&code=...`. The server adds `q`, `limit`, and `code` as request parameters.

## 3. Create a virtual environment and install packages

### macOS / Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

### Windows PowerShell

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt
```

## 4. Run the ontology tests

```bash
pytest -q
```

These tests run locally and do not call Azure or LangSearch.

## 5. Start the MCP server

```bash
python mcp_server.py
```

The server listens at `http://127.0.0.1:8000/mcp` by default. Leave the terminal running.

To change the local host or port, edit `MCP_HOST` and `MCP_PORT` in `.env` before starting the server.

## 6. Connect an MCP client

For a local MCP Inspector, start it in another terminal using the MCP SDK tooling or the MCP Inspector package, then connect using the Streamable HTTP transport URL:

```text
http://127.0.0.1:8000/mcp
```

The ontology models a **Partnership as hosting a collection of Circles**; each Circle belongs to exactly one Partnership. The Partner institution is related to the Partnership, rather than directly hosting Circles in this model.

Both `search_sujeeth_wiki` and `search_sujeeth_web` are capped at **5 results per call**. A caller may request fewer, but requests above 5 are clamped to 5; LangSearch results are also sliced locally as a safeguard.

The MCP server exposes these tools:

- `search_sujeeth_wiki`
- `match_ontology_websites`
- `search_sujeeth_web`
- `get_ontology_summary`
- `find_knowledge_by_concept`
- `get_ontology_class`
- `get_ontology_relationships`
- `get_ontology_rules`
- `get_program_structure`
- `get_dataverse_mappings`
- `get_ontology_schema_gaps`
- `get_preferred_terminology`

## 7. Suggested smoke tests

Use these tool inputs in your MCP client:

1. `get_ontology_summary` — should return ontology version and counts. This does not need API keys.
2. `get_ontology_class` with `name="Partnership"` — should return its definition and mappings.
3. `match_ontology_websites` with `query="Week 4 networking strategies"` — should return matching concepts and domains. This does not need API keys.
4. `search_sujeeth_wiki` with `query="Career Circle"` — calls the deployed Azure Function and requires a valid `WIKI_SEARCH_FUNCTION_KEY`.
5. `search_sujeeth_web` with `query="Week 4 networking strategies"` — calls LangSearch and requires a valid `LANGSEARCH_API_KEY`.

## 8. Troubleshooting

- **HTTP 401 from Wiki Search:** check that `WIKI_SEARCH_FUNCTION_KEY` is a current key for the `wikisearch` Function App and that the endpoint is correct.
- **LangSearch authentication error:** check that `LANGSEARCH_API_KEY` is valid and active.
- **No website domains matched:** the web search still runs unrestricted by domain; inspect `match_ontology_websites` and extend `website_sources` in `ontology/ontology.json` if needed.
- **MCP connection error:** confirm the server process is running and the client URL is exactly `http://127.0.0.1:8000/mcp`.
- **Import error for `mcp`:** activate the virtual environment and run `pip install -r requirements-dev.txt`.

## Security

Never put API keys in Python files, JSON files, screenshots, or Git commits. Keep `.env` local and rotate any key that has been exposed.


## Preview live search results (agent-facing tool output)

After configuring `.env`, run:

```powershell
python preview_search.py "Career Circle"
```

This calls the same search functions registered as MCP tools and prints their
structured JSON responses. This is the content an MCP-connected agent receives
from the tool calls.

Run one service at a time:

```powershell
python preview_search.py "Career Circle" --wiki-only
python preview_search.py "networking strategies" --web-only
```

These are live integration checks and require the corresponding API credentials
and network access. They are separate from `pytest -q`, which tests local
ontology behavior without calling external services.
