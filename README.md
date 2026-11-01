# Sujeeth Foundation MCP Server

A Model Context Protocol (MCP) server that lets AI assistants search and read Sujeeth Foundation knowledge. It is a public, customer-facing service that sits in front of three Azure Function Apps and adds little logic of its own beyond combining results and flagging weak ones:

| Backend | Repo | What it provides |
|---|---|---|
| Web Search | `websearch` | Ontology-gated hybrid search and page text for approved Foundation and partner websites |
| Wiki Search | `wikisearch` | Keyword search and page text for the GitHub wiki |
| Ontology | `ontology` | Concepts, relationships, and which domains are approved |

The MCP endpoint is `/mcp` on whatever host serves it: `http://127.0.0.1:8000/mcp` locally and `https://mcp.sujeeth.org/mcp` in production.

## Tools

**Public tools** (always available):

| Tool | Use it to |
|---|---|
| `search_sujeeth_foundation` | The main tool. Resolves the question through the ontology, searches website and wiki, de-duplicates by URL, and reports whether the answer needs clarification or a service failed |
| `get_document` | Read more of a result. Pass the `url` or `chunk_id` exactly as returned. Foundation sites and the wiki return page text; third-party sites return a short excerpt only |
| `search_website` | Search ontology-approved websites only |
| `search_wiki` | Search the GitHub wiki only |

**Admin tools** (hidden unless `MCP_ADMIN_TOOLS=true`, which is meant for local testing): `check_services`, `resolve_ontology`, `get_ontology_context`. They expose backend health and the ontology's internal structure, so they are off in production.

### What the results tell the agent

- `needs_clarification` and `suggested_question`: set when no concept matched, no domain was approved, nothing useful came back, or two weaker concept matches are nearly tied. Explicit matches (for example both "Sujeeth Foundation" and "donation" named in the question) are not treated as ambiguity.
- `service_degraded` and `service_errors`: set when a backend failed. The agent is told results may be incomplete, to tell the user, and not to treat missing results as proof that nothing exists. An outage never turns into a question asking the customer to rephrase.
- `agent_instructions`: every result tells the agent to cite URLs, never to search outside ontology-approved domains, and never to quote program statistics (counts, rates, placement or engagement figures) from these results. Current numbers come from sujeeth.org.
- Results carry `source`, `url`, `snippet`, `score`, `domain`, `concepts`, and `chunk_id`. Scores come from different engines for website and wiki results and cannot be compared.

### What is never returned

- Wiki pages about recorded circle sessions (recording links, meeting summaries). They are filtered here and in `wikisearch`.
- Full text from third-party sites. `get_document` returns only a short excerpt for them, with a note to link to the source.
- Function keys, internal URLs, or backend error details. Errors return only an HTTP status or a general message.

## Run locally (Windows PowerShell)

Requirements: Python 3.10 or newer.

```powershell
git clone https://github.com/SujeethFoundation/mcp.git
cd mcp
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If `pip.exe` fails with "Access is denied", run it through Python as shown (`python -m pip`).

The server reads **environment variables** (or a `local.settings.json` file with a `Values` object, which is gitignored). It does **not** read a `.env` file, even though `.env.example` lists the names. Set the keys in the same PowerShell window you start the server from:

```powershell
foreach ($n in "WEBSEARCH_FUNCTION_KEY","WIKISEARCH_FUNCTION_KEY","ONTOLOGY_FUNCTION_KEY") {
    $s = Read-Host $n -AsSecureString
    Set-Item "Env:$n" ([System.Net.NetworkCredential]::new("", $s).Password)
}
$env:MCP_ADMIN_TOOLS = "true"     # optional: show the three admin tools while testing
python .\mcp_server.py
```

Check that the port is listening with `Test-NetConnection 127.0.0.1 -Port 8000`. Connect an MCP inspector (for example `npx @mcpjam/inspector@latest` or `npx @modelcontextprotocol/inspector`) to `http://127.0.0.1:8000/mcp`.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `WEBSEARCH_FUNCTION_KEY`, `WIKISEARCH_FUNCTION_KEY`, `ONTOLOGY_FUNCTION_KEY` | none | Function (host) keys for each app. A missing key makes calls to that app fail with HTTP 401 |
| `WEBSEARCH_URL`, `WIKISEARCH_URL`, `ONTOLOGY_URL` | the deployed apps | Base URLs |
| `MCP_HOST` | `127.0.0.1` locally; `0.0.0.0` when running on Azure | Interface to listen on. It must not be `127.0.0.1` behind a custom domain, because requests for another host name are then rejected with HTTP 421 |
| `MCP_PORT` | `FUNCTIONS_CUSTOMHANDLER_PORT`, `PORT`, or `WEBSITES_PORT` if set, else `8000` | Port. On Azure Functions it must equal `port` in `host.json` (8000), so set it explicitly |
| `MCP_ADMIN_TOOLS` | `false` | Show the admin tools |
| `MCP_STATELESS` | `true` | No in-memory sessions, so any instance can answer any request |
| `MCP_RATE_LIMIT_PER_IP` | `120` | Requests per minute per client address (0 turns it off) |
| `MCP_RATE_LIMIT_GLOBAL` | `600` | Requests per minute across all clients (0 turns it off) |
| `MCP_REQUEST_TIMEOUT` | `30` | Seconds to wait for a backend |
| `WIKI_EXCLUDE_TITLE_PATTERN` | circle-session pattern | Wiki pages to drop from results |
| `LOCAL_SETTINGS_PATH` | `local.settings.json` | Optional settings file |

Never commit keys. Rotate any key that has been pasted into chat, a URL, or a commit.

## Deploy to Azure Functions (Flex Consumption) at mcp.sujeeth.org

The server runs as a **self-hosted MCP server on Azure Functions**: the Functions host starts `python mcp_server.py` as a custom handler and forwards every HTTP request to it, so the code is the same FastMCP server you run locally. This hosting option is in **public preview** and requires a Flex Consumption app, a stateless server, and the streamable HTTP transport, which this server already uses.

**Files that matter**

- `host.json` turns the Functions app into a custom handler. The profile `mcp-custom-handler` forwards all paths (so `/mcp` works at the root) and sets `defaultAuthorizationLevel` to `anonymous`, because the service is public. Without that setting, the host requires a function key on every request. `port` must match `MCP_PORT`.
- `.funcignore` keeps local files out of a `func` publish. The GitHub workflow has its own exclusions.
- `.github/workflows/deploy.yml` builds with the dependencies vendored into `.python_packages`, smoke-tests the server, and deploys. The server adds that folder to its import path itself.

**1. Create the Function App**

Portal → Create → Function App → **Flex Consumption**, runtime **Python** (use the same version as `PYTHON_VERSION` in the workflow, currently 3.13), in the same region as the other apps. Then:

- Settings → **Scale and concurrency**: set the **`http`** always-ready count (2 for high availability), the instance memory size (512 MB is enough to try; the server does no heavy work), and a maximum instance count that caps cost.
- If you want zone redundancy like your other apps, turn it on when creating the app. It requires at least 2 always-ready instances.
- Do **not** turn on built-in authentication. It sends visitors to a Microsoft sign-in page, which customers do not have.

**2. App settings** (Settings → Environment variables)

| Setting | Value |
|---|---|
| `WEBSEARCH_FUNCTION_KEY`, `WIKISEARCH_FUNCTION_KEY`, `ONTOLOGY_FUNCTION_KEY` | the three keys |
| `MCP_PORT` | `8000` |

Leave `MCP_ADMIN_TOOLS` unset.

**3. Deploy**

Add the secrets and variable listed at the top of the workflow file and push to `main`. Alternatively, open the Function App → Deployment Center, connect this repository, and let Azure generate the sign-in secrets as it did for your other apps.

**4. Custom domain (CNAME, not forwarding)**

Domain forwarding redirects to the long `azurewebsites.net` address, and POST requests do not survive redirects reliably. Use a custom domain:

1. In the DNS for `sujeeth.org`, add a CNAME record: host `mcp`, value `<your-app>.azurewebsites.net`. Custom domains on Flex Consumption support CNAME records only, which is all a subdomain needs.
2. Add a TXT record: host `asuid.mcp`, value = the app's Custom Domain Verification ID (Function App → Custom domains).
3. Function App → Custom domains → Add custom domain → `mcp.sujeeth.org`.
4. Add a TLS certificate. Free App Service Managed Certificates became generally available on Flex Consumption in September 2026. If the portal does not offer one for your app, upload your own certificate.
5. Turn on HTTPS only.

**5. Verify**

```powershell
Invoke-RestMethod https://mcp.sujeeth.org/healthz        # expect: status ok

$h = @{ "Accept" = "application/json, text/event-stream"; "Content-Type" = "application/json" }
$body = '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
Invoke-WebRequest -Method POST -Uri "https://mcp.sujeeth.org/mcp" -Headers $h -Body $body | Select-Object StatusCode, Content
```

Expect HTTP 200 and the four public tools. A 401 means the host is still requiring a function key, so check `defaultAuthorizationLevel` in `host.json`. A 421 means `MCP_HOST` is `127.0.0.1`.

Local testing of the Functions setup needs Azure Functions Core Tools (`func start`). Running `python mcp_server.py` directly still works for everyday development.

## Operating notes

- **There is no client login, by design.** The service is public. The protections are: admin tools hidden, rate limits, answers limited to ontology-approved domains, excerpt-only output for third-party sites, and the circle-session filter. Backend keys never leave the server.
- **Rate limits are per instance and in memory, and Flex Consumption adds instances under load.** The effective total is the limit times the number of running instances. Clients that connect through a shared provider (a hosted AI assistant, for example) arrive from the provider's addresses, so many customers can share one address. Keep the global limit as the main protection and raise the per-IP limit if legitimate users get HTTP 429. Behind the Azure front end and the Functions host, the client address comes from the `X-Forwarded-For` header. Check that it looks right in your first test.
- **Each search can spend money.** Every search calls the websearch app, which uses Azure AI Search and the embedding deployment. Keep a budget alert on those resources.
- **Scaling.** Flex Consumption adds instances as requests arrive, up to the maximum you set. Stateless mode is what makes that safe. The always-ready instances keep the first request from waiting on a cold start.
- **Preview status.** Hosting your own SDK-based MCP server on Azure Functions is in public preview. Check the Azure documentation before relying on it for something with a service-level agreement.
- **Browser-based clients.** The server does not add CORS headers. Clients that connect server to server (hosted AI assistants and developer tools) are unaffected.
- **Logs.** The server's own log records request paths and status codes. Search text travels in the request body, which is not logged.

## Test checklist

Local tests that use admin tools need `MCP_ADMIN_TOOLS=true`. Search results depend on what has been crawled and indexed.

| # | Tool | Input | Expect |
|---|---|---|---|
| 1 | `check_services` (admin) | none | `all_healthy: true` |
| 2 | `search_sujeeth_foundation` | `how can i donate to the sujeeth foundation?` | `status: completed`, results with URLs, the FAQ wiki page with the donation link, `needs_clarification: false` |
| 3 | `search_sujeeth_foundation` | `quantum cryptography` | `needs_clarification: true` and a suggested question |
| 4 | `get_document` | a `url` from test 2 | page text, or an excerpt with `excerpt_only: true` for third-party sites |
| 5 | `resolve_ontology` (admin) | `resume tips for students` | matched concepts and approved domains |
| 6 | `get_ontology_context` (admin) | concept `leadership` | relationships |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Service returned HTTP 401` | A Function key is missing or wrong. Fix the app setting and restart |
| `Service returned HTTP 502` from website search | The backend raised an error. Call its `/api/search` directly to see the message (a 429 inside means embedding quota) |
| `needs_clarification` on a clear question | The ontology matched no concept. Add aliases for the topic in the ontology repo |
| `get_document` says not found | The page is not indexed yet, or the `url` or `chunk_id` was not copied exactly from a result |
| HTTP 401 on every request | The Functions host requires a function key. Set `defaultAuthorizationLevel` to `anonymous` in `host.json` |
| HTTP 421 "Invalid Host header" | The server is bound to `127.0.0.1` behind a custom domain. Use `MCP_HOST=0.0.0.0` |
| Deployed app returns 502 or never becomes healthy | `MCP_PORT` does not match `port` in `host.json`, or the Python version in the workflow does not match the app's runtime |
| HTTP 429 | A rate limit was reached. Wait for the `Retry-After` interval or raise the limit |
| `Unknown tool: check_services` | Admin tools are off. Set `MCP_ADMIN_TOOLS=true` for local testing only |