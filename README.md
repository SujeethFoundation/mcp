# Sujeeth Foundation MCP Server

A local Model Context Protocol (MCP) server that lets AI assistants search and read Sujeeth Foundation knowledge. It sits in front of three Azure Function Apps and adds no search logic of its own beyond combining results and flagging weak ones:

| Backend | Repo | What it provides |
|---|---|---|
| Web Search | `websearch` | Ontology-gated hybrid search and page text for approved Foundation and partner websites |
| Wiki Search | `wikisearch` | Keyword search and page text for the GitHub wiki |
| Ontology | `ontology` | Concepts, relationships, and which domains are approved |

The server runs locally over Streamable HTTP at `http://127.0.0.1:8000/mcp`. It is not hosted anywhere yet (see Status).

## Tools

| Tool | Use it to |
|---|---|
| `search_sujeeth_foundation` | The main tool. Resolves the question through the ontology, searches website and wiki, de-duplicates by URL, and reports whether the answer needs clarification or a service failed |
| `get_document` | Read more of a result. Pass the `url` or `chunk_id` exactly as returned. Foundation sites and the wiki return page text; third-party sites return a short excerpt only |
| `search_website` | Search ontology-approved websites only |
| `search_wiki` | Search the GitHub wiki only |
| `resolve_ontology` | See which concepts and domains a question maps to |
| `get_ontology_context` | Read terminology and relationships for a concept |
| `check_services` | Health check of all three backends |

### What the results tell the agent

- `needs_clarification` and `suggested_question`: set when no concept matched, no domain was approved, nothing useful came back, or two weaker concept matches are nearly tied. Explicit matches (both "Sujeeth Foundation" and "donation" named in the question) are not treated as ambiguity.
- `service_degraded` and `service_errors`: set when a backend failed. The agent is told results may be incomplete, and not to treat missing results as proof that nothing exists.
- `agent_instructions`: every result tells the agent to cite URLs, never to search outside ontology-approved domains, and never to quote program statistics (counts, rates, placement or engagement figures) from these results. Current numbers come from sujeeth.org.
- Results carry `source`, `url`, `snippet`, `score`, `domain`, `concepts`, and `chunk_id`. Scores come from different engines for website and wiki results and cannot be compared.

### What is never returned

- Wiki pages about recorded circle sessions (recording links, meeting summaries). They are filtered here and in `wikisearch`.
- Full text from third-party sites. `get_document` returns only a short excerpt for them, with a note to link to the source.

## Setup (Windows PowerShell)

Requirements: Python 3.10 or newer.

```powershell
git clone https://github.com/SujeethFoundation/mcp.git
cd mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If `pip.exe` fails with "Access is denied", run it through Python as shown (`python -m pip`).

## Configure

The server reads **environment variables** (or a `local.settings.json` file with a `Values` object, which is gitignored). It does **not** read a `.env` file, even though `.env.example` lists the names. Set the keys in the same PowerShell window you start the server from:

```powershell
foreach ($n in "WEBSEARCH_FUNCTION_KEY","WIKISEARCH_FUNCTION_KEY","ONTOLOGY_FUNCTION_KEY") {
    $s = Read-Host $n -AsSecureString
    Set-Item "Env:$n" ([System.Net.NetworkCredential]::new("", $s).Password)
}
```

| Variable | Default | Purpose |
|---|---|---|
| `WEBSEARCH_FUNCTION_KEY`, `WIKISEARCH_FUNCTION_KEY`, `ONTOLOGY_FUNCTION_KEY` | none | Function (host) keys for each app. A missing key makes calls to that app fail with HTTP 401 |
| `WEBSEARCH_URL`, `WIKISEARCH_URL`, `ONTOLOGY_URL` | the deployed apps | Base URLs, no trailing path |
| `MCP_REQUEST_TIMEOUT` | `30` | Seconds to wait for a backend |
| `MCP_HOST`, `MCP_PORT` | `127.0.0.1`, `8000` | Where the MCP server listens |
| `WIKI_EXCLUDE_TITLE_PATTERN` | circle-session pattern | Wiki pages to drop from results |
| `LOCAL_SETTINGS_PATH` | `local.settings.json` | Optional settings file |

Never commit keys. Rotate any key that has been pasted into chat, a URL, or a commit.

## Run

```powershell
python .\mcp_server.py
```

Leave the window open. You should see the three backend URLs and `MCP endpoint: http://127.0.0.1:8000/mcp`. Check that the port is listening:

```powershell
Test-NetConnection 127.0.0.1 -Port 8000
```

## Connect an inspector or client

Add a Streamable HTTP server pointing at `http://127.0.0.1:8000/mcp` in an MCP inspector (for example MCPJam or the official MCP Inspector, run with `npx @mcpjam/inspector@latest` or `npx @modelcontextprotocol/inspector`). Keep the server window and the inspector window both running.

## Test checklist

Run these in order. Search results depend on what has been crawled and indexed.

| # | Tool | Input | Expect |
|---|---|---|---|
| 1 | `check_services` | none | `all_healthy: true` |
| 2 | `resolve_ontology` | `resume tips for students` | matched concepts and approved domains |
| 3 | `search_sujeeth_foundation` | `how can i donate to the sujeeth foundation?` | `status: completed`, results with URLs, the FAQ wiki page with the donation link, `needs_clarification: false` |
| 4 | `search_sujeeth_foundation` | `quantum cryptography` | `needs_clarification: true` and a suggested question |
| 5 | `get_document` | a `url` from test 3 | page text, or an excerpt with `excerpt_only: true` for third-party sites |
| 6 | `get_ontology_context` | concept `leadership` | relationships |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Service returned HTTP 401` | A Function key is missing or wrong. Restart the server with the right key |
| `Service returned HTTP 502` from website search | The backend raised an error. Call its `/api/search` directly to see the message (a 429 inside means embedding quota) |
| `needs_clarification` on a clear question | The ontology matched no concept. Add aliases for the topic in the ontology repo |
| `get_document` says not found | The page is not indexed yet, or the `url` or `chunk_id` was not copied exactly from a result |
| Nothing listening on port 8000 | The server is not running, or `MCP_PORT` was changed |

## Status and security

- **Local only.** It listens on `127.0.0.1`. ChatGPT, Claude, and Copilot Studio cannot reach it until it is hosted behind HTTPS with authentication, which is not built yet.
- The MCP endpoint itself has no authentication. Do not expose it to a network as it is.
- Function keys stay on the server and are never returned in tool output. Errors return only an HTTP status.