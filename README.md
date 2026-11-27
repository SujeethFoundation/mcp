# Sujeeth Foundation MCP Server

A Model Context Protocol (MCP) server that lets AI assistants search and read Sujeeth Foundation knowledge. It is a public, customer-facing service that sits in front of three Azure Function Apps and adds little logic of its own beyond combining results and flagging weak ones:

| Backend | Repo | What it provides |
|---|---|---|
| Web Search | `websearch` | MCP-filtered hybrid search and page text for approved Foundation and partner websites |
| Wiki Search | `wikisearch` | Keyword search excerpts for the GitHub wiki; `get_documents` reads selected pages |
| Ontology | `ontology` | Concepts, relationships, and which domains are approved |

## Platform architecture

The Sujeeth Foundation helps college students network into their first job through Career Circles, a six-week mentorship program. Students, mentors and partners use two front ends, the static site at sujeeth.org and a Power Pages portal, both on one Dataverse environment. Azure Function apps add chat, knowledge search, payments, short links and texting. The Dataverse side (Base Camp, circles, book requests, surveys, credentials, the Career Ascent course) ships as Dataverse solutions and has no repo.

| Repo (README) | Role |
|---|---|
| [`sujeethfoundation.github.io`](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/README.md) | Website at sujeeth.org, wiki, Discussions, course player and Power Pages CSS |
| [`chat`](https://github.com/SujeethFoundation/chat/blob/main/README.md) | Chat API behind the site's assistant widget |
| [`mcp`](https://github.com/SujeethFoundation/mcp/blob/main/README.md) | MCP server (the "Sujeeth Foundation Graph") that AI assistants use to search Foundation knowledge |
| [`ontology`](https://github.com/SujeethFoundation/ontology/blob/main/README.md) | Concept graph and governance: concepts, approved domains, Feelings API |
| [`websearch`](https://github.com/SujeethFoundation/websearch/blob/main/README.md) | Crawler and hybrid search over ontology-approved websites |
| [`wikisearch`](https://github.com/SujeethFoundation/wikisearch/blob/main/README.md) | Keyword search over the GitHub wiki |
| [`payments`](https://github.com/SujeethFoundation/payments/blob/main/README.md) | PayPal checkout and webhook for online courses |
| [`shortner`](https://github.com/SujeethFoundation/shortner/blob/main/README.md) | Link shortener for go.sujeeth.org |
| [`acs-sms-bot`](https://github.com/SujeethFoundation/acs-sms-bot/blob/main/readme.md) | Texting through Azure Communication Services |

Architecture docs, in the website repo's `architecture/` folder: [platform architecture](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/architecture/platform-architecture.md) (repos, Azure apps, how they connect), [data model and portal](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/architecture/platform-data-model-and-portal.md) (Dataverse tables, solutions, Power Pages) and [business architecture](https://github.com/SujeethFoundation/sujeethfoundation.github.io/blob/main/architecture/business-architecture.md) (vision, assumptions, tests). This block is the same in every repo's README; the canonical copy is in the platform architecture doc.

**This repo:** called by the Foundation's chat agent (the `chat` repo) and by hosted AI assistants such as Claude and ChatGPT. It calls `ontology`, `websearch` and `wikisearch`. The MCP endpoint is `/mcp` on whatever host serves it: `http://127.0.0.1:8000/mcp` locally and `https://mcp.sujeeth.org/mcp` in production.

## Tools

**Public tools** (always available):

| Tool | Use it to |
|---|---|
| `search_sujeeth_foundation` | The main tool. Resolves the question and approved domains through the ontology, sends those filters to website search, searches the wiki, de-duplicates by URL, and reports whether the answer needs clarification or a service failed |
| `get_documents` | Read more of the results. Pass up to `MAX_BATCH_DOCUMENTS` (default 2) `urls` exactly as returned, in one call (they are read at the same time). `max_chars` defaults to `DOCUMENT_DEFAULT_CHARS` (12000) and is at most 20000. A page is read once and kept for 15 minutes whatever size is asked for. Foundation sites and the wiki return page text; third-party sites return a short excerpt only |
| `search_website` | Search ontology-approved websites only |
| `search_wiki` | Search the GitHub wiki only |

**MCP resolves before search.** `search_sujeeth_foundation` sends the question and compact ontology vocabulary and relationship graph to the configured Azure GPT-6 Luna deployment. The model identifies direct concepts, useful related concepts, and query variants. MCP accepts only concept IDs present in the ontology, allows related concepts only when an enabled ontology relationship supports retrieval, and derives searchable domains from those validated concepts. MCP sends the primary query, its variants, and shared approved filters in one request to Websearch and one to Wiki Search. A failed model call is a service failure, never an out-of-scope refusal. An actual no-concept match returns the exact refusal message without searching. If Websearch fails, Wiki Search results are still returned with `status: partial`. `retrieval.website_candidates` and `retrieval.wiki_candidates` count what each source found before the best results were chosen across sources; `count` is what the response returns.

**Feelings frameworks and the search filter.** The Feelings API ranks frameworks for a feeling and flags each with `intent_match` and a guardrail (for example "only foreground when an interview is mentioned"). All of them stay in `matched_concepts` and `feelings_route.frameworks`, so the agent sees them and their guardrails. Only the ones with `intent_match: true` join the concepts sent to Websearch and Wiki Search (`resolution.search_concepts`) and the domains derived from them. When the question has no other concept, all of them are used, so a feelings-only question still searches.

**Carried context.** A caller may pass `retrieval_context` (`feeling`, `career_context`, optional `source`). The model gets the feeling and career context as one active context and does not see `source`, so context taken from a summary counts the same as context from the last turn. When the career context is an ontology concept, it goes into the candidate packet, so a short follow-up such as "is there an easy way to research a person?" after networking resolves to networking. The model still ignores the context when the question is about something else.


**Local resolution skips the model for clear questions.** The model call is the slowest step before a search, and most of what it does is mechanical. When a question names exactly one concept in the ontology's own words, MCP builds the plan from the ontology and does not call the model: the concept, the concepts its enabled `expand`/`boost` relationships link (at most `MCP_LOCAL_RESOLVE_MAX_RELATED`), and up to two variants taken from that concept's query phrases and multi-word aliases, with the visitor's question always first. It takes about 2 ms. The model still decides every case that is not clear: nothing matched (the out-of-scope call), more than one concept or two concepts naming the same words, a feeling or a `retrieval_context` for the matched topic, a word the ontology lists as ambiguous (unless it is part of the matched phrase, like "Career Circle"), a leftover word that names another concept ("resume" in "resume and interview prep"), and questions over `MCP_LOCAL_RESOLVE_MAX_WORDS` words or with more than one sentence. A one-word alias counts only when it is five letters or more and not a common word. Ontology definitions are not used as variants. `/status` reports how many resolutions were `ontology`, how many were `model`, and why the rest were sent on (`ontology_resolution.skipped`), and each search's timings carry `ontology_resolved_by`. Set `MCP_LOCAL_RESOLVE=false` to send every question to the model, as before.

**Related searches.** The MCP model produces up to `SEARCH_MAX_QUERIES` (default 3) query variants for the same user question. MCP sends those variants together in one request to each search service. Same-topic variants can also be supplied with `more_queries`; separate topics should use separate tool calls so each gets its own ontology validation and filters.


All search tools return excerpts. They do not fetch page text automatically. The Chat Agent calls `get_documents` for selected result URLs when the excerpts do not answer the question. The tool descriptions and server instructions that agents read are built from one set of constants in `mcp_server.py`.


**Admin tools** (hidden unless `MCP_ADMIN_TOOLS=true`, which is meant for local testing): `check_services`, `resolve_ontology`, `get_ontology_context`. They expose backend health and the ontology's internal structure, so they are off in production.

**Tool annotations.** Every tool in `tools/list` carries `readOnlyHint: true` and `destructiveHint: false`, because none of them writes anything. `openWorldHint` is `true` for `search_sujeeth_foundation`, `search_website` and `get_documents` (results can come from approved third-party websites) and `false` for `search_wiki` and the admin tools (Foundation-owned data only). `tests/test_tool_annotations.py` checks all of this.

### What the results tell the agent

- `out_of_scope` and `message`: set when no ontology concept matched, which means the question is not about the Foundation. `status` is `out_of_scope`, `results` is empty (the wiki's keyword hits are dropped too), and the agent is told to reply with `message` as written ("That's outside what I can help with. I'm here to help you get ready for your first job: career guidance, interview prep, resume feedback, and how to build a network of professionals. If you want to mentor, give, or bring Career Circles to your school, ask me about that too.") and nothing from general knowledge. A backend outage is never a refusal.
- `needs_clarification` and `suggested_question`: set only when two weaker concept matches are nearly tied and the ontology does not link them, so they could conflict ("Are you asking about A or B?"). Explicit matches (for example both "Sujeeth Foundation" and "donation" named in the question) are not treated as ambiguity. A matched question with weak or empty results is not a reason to ask: `reasons` records it and the agent says nothing was found and points to sujeeth.org.
- `service_degraded` and `service_errors`: set when a backend failed. The agent is told results may be incomplete, to tell the user, and not to treat missing results as proof that nothing exists. An outage never turns into a question asking the customer to rephrase.
- `agent_instructions`: every result tells the agent to cite URLs, never to search outside ontology-approved domains, and to quote program statistics (counts, rates, placement or engagement figures) only from a result on sujeeth.org (subdomains such as topics.sujeeth.org count), with its url. Statistics from the wiki or third-party pages are never quoted; the agent points to sujeeth.org instead.
- Results carry `source`, `url`, `snippet`, `score`, `domain`, `concepts`, and `chunk_id`. Scores come from different engines for website and wiki results and cannot be compared.

### What is never returned

- Wiki pages about recorded circle sessions (recording links, meeting summaries). They are filtered here and in `wikisearch`.
- Full text from third-party sites. `get_documents` returns only a short excerpt for them, with a note to link to the source.
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
| `MCP_ONTOLOGY_URL` | not set | A raw ontology file URL. When set, it is the only source of the ontology for MCP's resolver and relationship checks. When not set, MCP reads the slim catalog from `ONTOLOGY_URL` (`/api/ontology/catalog`, sent with `ONTOLOGY_FUNCTION_KEY`) and falls back to the file on the `main` branch of `SujeethFoundation/ontology` if the app does not answer |
| `MCP_ONTOLOGY_CATALOG` | `true` | Set to `false` to skip the slim catalog and read the GitHub file |

The slim catalog does not carry the search domains. When the catalog has no `domains`, MCP also reads `/api/ontology/domains?search_enabled=true` (once per catalog refresh, five minutes) and keeps each domain with its associated concepts. If that read fails, MCP keeps the last known domains, or none, and asks again in about 30 seconds.
| `MCP_ONTOLOGY_MODEL_ENDPOINT` | empty (required) | Azure OpenAI / Foundry resource endpoint. Accepts a resource base URL, `/openai/v1` base, or full `/openai/v1/responses` URL; MCP normalizes it to the Responses endpoint |
| `MCP_ONTOLOGY_MODEL_API_KEY` | empty (required) | Secret key for the Azure model endpoint. Store it as a Function App setting or in ignored local settings; never commit it |
| `MCP_ONTOLOGY_MODEL_NAME` | `gpt-6-luna` | Deployed Azure model name used for ontology resolution and query generation |
| `MCP_ONTOLOGY_MODEL_TIMEOUT_SECONDS` | `25` | Maximum wait for one ontology-resolution request |
| `MCP_ONTOLOGY_REASONING_EFFORT` | `low` | Azure Responses API reasoning effort for ontology routing (`low`, `medium`, `high`, `xhigh`, or `max`); keep the value supported by the deployed model |
| `MCP_ONTOLOGY_MODEL_MAX_OUTPUT_TOKENS` | `400` | Output-token ceiling for the structured ontology plan |
| `MCP_ONTOLOGY_MODEL_MAX_CONCURRENCY` | `4` | Maximum simultaneous Luna calls per MCP instance; excess resolutions wait |
| `MCP_ONTOLOGY_MODEL_RETRY_ATTEMPTS` | `3` | Total attempts for a Luna HTTP 429, subject to the model timeout |
| `MCP_ONTOLOGY_MODEL_RETRY_MAX_WAIT_SECONDS` | `5` | Maximum wait before one Luna retry; MCP honors `retry-after-ms` and adds jitter |
| `MCP_ONTOLOGY_MODEL_CANDIDATE_LIMIT` | `8` | Maximum locally matched concepts sent to Luna before one-hop relationship expansion |
| `MCP_ONTOLOGY_MODEL_RELATIONSHIP_LIMIT` | `24` | Maximum one-hop relationships in a Luna candidate packet |
| `MCP_SUGGESTED_TOOLS_MAX` | `2` | Most tools offered with one answer (`suggested_tools`); `0` turns tool suggestions off |
| `MCP_ONTOLOGY_MODEL_DEFINITION_MAX_CHARS` | `240` | Maximum definition characters sent for a directly matched concept |
| `MCP_LOCAL_RESOLVE` | `true` | Build the plan from the ontology alone, with no model call, when a question names exactly one concept and nothing makes it uncertain. `false` sends every question to the model |
| `MCP_LOCAL_RESOLVE_MAX_WORDS` | `25` | Longest question (in words) the local path will take; longer ones go to the model |
| `MCP_LOCAL_RESOLVE_MAX_RELATED` | `2` | Most related concepts the local path adds through enabled `expand`/`boost` relationships (`0` adds none) |
| `MCP_ONTOLOGY_PLAN_CACHE_SECONDS` | `300` | How long each MCP instance reuses a validated ontology plan for a normalized query and ontology version; `0` disables it |
| `MCP_ONTOLOGY_PLAN_CACHE_MAX_ENTRIES` | `500` | Maximum cached ontology plans per MCP instance before older entries are evicted |
| `MCP_HOST` | `127.0.0.1` locally; `0.0.0.0` when running on Azure | Interface to listen on. It must not be `127.0.0.1` behind a custom domain, because requests for another host name are then rejected with HTTP 421 |
| `MCP_PORT` | `FUNCTIONS_CUSTOMHANDLER_PORT`, `PORT`, or `WEBSITES_PORT` if set, else `8000` | Port. On Azure Functions it must equal `port` in `host.json` (8000), so set it explicitly |
| `MCP_ADMIN_TOOLS` | `false` | Show the admin tools |
| `MCP_STATELESS` | `true` | No in-memory sessions, so any instance can answer any request |
| `MCP_RATE_LIMIT_PER_IP` | `120` | Requests per minute per client address for ordinary callers (0 turns it off) |
| `MCP_RATE_LIMIT_TRUSTED_PER_IP` | `1200` | Requests per minute per address for trusted callers: AI providers and our Foundry project (0 turns it off) |
| `MCP_TOOL_THREADS` | `32` | Worker threads that run tool calls. Each search waits on the search apps, so tools run off the event loop; this is how many can be in progress at once in one instance |
| `MCP_RATE_LIMIT_GLOBAL` | `600` | Requests per minute across all clients, trusted or not (0 turns it off) |
| `MCP_TRUSTED_RANGES` | `160.79.104.0/21` | Comma-separated CIDR ranges that count as trusted. Default is Anthropic's published outbound range. `none` clears it |
| `MCP_TRUSTED_RANGE_URLS` | OpenAI's ChatGPT connectors manifest (`https://openai.com/chatgpt-connectors.json`) | Comma-separated URLs of JSON manifests of ranges (`{"prefixes":[{"ipv4Prefix":"..."}]}`), read at start and refreshed daily. `none` turns it off |
| `MCP_TRUSTED_REFRESH_SECONDS` | `86400` | How often the manifests are read again. A failed read keeps the last good ranges |
| `MCP_TRUSTED_KEY` | empty (off) | Shared secret. A caller that sends it in the header below counts as trusted. Use it for the Foundry project |
| `MCP_TRUSTED_KEY_HEADER` | `x-mcp-client-key` | Name of the header that carries that secret |
| `MCP_TRUSTED_USER_AGENTS` | empty (off) | Optional regular expression. A matching User-Agent counts as trusted. It can be faked, so it only picks the bucket |
| `MCP_REQUEST_TIMEOUT` | `30` | Seconds to wait for a backend |
| `MCP_RETRY_ATTEMPTS` | `4` | Tries in all (the first included) for a call to the website search app that is refused with HTTP 503 (an instance still loading its index, when `LOCAL_INDEX_FAIL_FAST` is on there) or cannot be reached. `1` turns retries off. All tries together stay within `MCP_REQUEST_TIMEOUT` |
| `MCP_RETRY_DEFAULT_WAIT_SECONDS` | `3` | Wait before a retry when the 503 has no `Retry-After` header, and after a connection error |
| `MCP_RETRY_MAX_WAIT_SECONDS` | `8` | Longest single wait, whatever `Retry-After` says |
| `MCP_RETRY_MIN_ATTEMPT_SECONDS` | `4` | A retry is made only if at least this much of the timeout would be left after the wait |
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
- **Rate limits are per instance and in memory, and Flex Consumption adds instances under load.** The effective total is the limit times the number of running instances. Behind the Azure front end and the Functions host, the client address comes from the `X-Forwarded-For` header. Check that it looks right in your first test.
- **Hosted AI assistants share addresses, so they get their own bucket.** Claude and ChatGPT call this server from the provider's cloud, not from the user's device, and the Foundry agent calls from the project's data proxy. Every visitor behind one provider arrives from the same few addresses. A per-address limit sized for one person then throttles all of them at once. In a load test it did exactly that: the limit was reached, the Foundry run failed as `tool_user_error`, and nothing in our own logs said why. Callers that are trusted (see `rate_limit.py` and the settings above) count in a separate, higher per-address bucket. Trust changes only that limit. It does not authenticate anyone, and the global limit still counts every request.
- **Foundry does not publish a stable address for its tool calls**, so identify the Foundry project with a key. Set `MCP_TRUSTED_KEY` on this app, then in the Foundry project add the MCP tool with key-based authentication and the same value under the header name in `MCP_TRUSTED_KEY_HEADER`. Foundry keeps the secret in the project connection, so the agent never sees it. OpenAI says its own ranges are best effort and that addresses outside the manifest have appeared before, so watch for `rate_limited bucket=public` lines from OpenAI addresses and add them to `MCP_TRUSTED_RANGES`.
- **Tools run on worker threads.** FastMCP calls a plain `def` tool directly on the event loop, so before this change one instance answered one tool call at a time and `/healthz` stalled behind every search. In a load test of 10 searches a second, requests waited 25 seconds while the server's own timer showed under 1 second per search, and Application Insights showed the app scaling to 5 instances that together completed about 6 a second. Every tool is now wrapped with `in_thread`, and a test fails if one is added without it. Code that calls a tool function directly from a plain thread, as the warm-up does, must use `tool.sync(...)`; calling the wrapper returns a coroutine that never runs. This is also why the Flex Consumption per-instance HTTP concurrency setting (default 4 at 512 MB) only helps now: it lets the platform send an instance several requests at once, which the server can finally work on together.
- **Throttling is logged.** Each throttled address writes one line per minute: `rate_limited bucket=public|trusted|global ip=... limit_per_minute=...`. These lines contain client addresses. Search the log for them first when a tool call fails with no other explanation.
- **Each search can spend money.** Every search calls the websearch app, which embeds the question (see its README). Keep a budget alert on the embedding resource and on the Function Apps.
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
| 3 | `search_sujeeth_foundation` | `quantum cryptography` | `out_of_scope: true`, the refusal `message`, no results, `needs_clarification: false` |
| 4 | `get_documents` | `urls`: a url from test 2 | page text, or an excerpt with `excerpt_only: true` for third-party sites |
| 5 | `resolve_ontology` (admin) | `resume tips for students` | matched concepts and approved domains |
| 6 | `get_ontology_context` (admin) | concept `leadership` | relationships |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Service returned HTTP 401` | A Function key is missing or wrong. Fix the app setting and restart |
| `Service returned HTTP 502` from website search | The backend raised an error. Call its `/api/search` directly to see the message (a 429 inside means embedding quota) |
| `out_of_scope` on a question about the Foundation | The ontology matched no concept. Add aliases for the topic in the ontology repo |
| `needs_clarification` on a clear question | Two unlinked concepts scored within 0.10 of each other. Concepts joined by a predicate whose `search_behavior` effect is `expand` or `boost` (`illustrates`, `supports`, `is_part_of`, `uses` and so on) count as one answer; `suppress` links (`is_distinct_from`) do not. Add a relationship between them, or sharper aliases, in the ontology repo. The links reload every `LINKED_PAIRS_REFRESH_HOURS` (default 24) |
| `get_documents` reports an error for a url | The page is not indexed yet, or the `url` was not copied exactly from a result |
| HTTP 401 on every request | The Functions host requires a function key. Set `defaultAuthorizationLevel` to `anonymous` in `host.json` |
| HTTP 421 "Invalid Host header" | The server is bound to `127.0.0.1` behind a custom domain. Use `MCP_HOST=0.0.0.0` |
| Deployed app returns 502 or never becomes healthy | `MCP_PORT` does not match `port` in `host.json`, or the Python version in the workflow does not match the app's runtime |
| HTTP 429 | A rate limit was reached. Wait for the `Retry-After` interval or raise the limit |
| `Unknown tool: check_services` | Admin tools are off. Set `MCP_ADMIN_TOOLS=true` for local testing only |

## Suggested tools

Students can connect some tools themselves (for example YouSpot for researching people before a networking message, or the Foundation's own MCP server). The ontology describes them and MCP offers them when they fit; they never change what is searched.

- **In the ontology.** A tool is a class with `category: "tool"`, a `name`, a `definition`, and a `tool` object: `url` (required), `kind` (`app`, `mcp` or `connector`), `connect` (what the student does to start, in the words the agent should use), `fit_when`, and, for a tool with an MCP server, `mcp_url` and `available_in` (where students can add it, for example the Claude connector directory or ChatGPT). A relationship `tool` `supports` `skill` says what it helps with, with an optional `weight` (0 to 1, default 0.5) and `reason` (one line on why it helps).
- **In the plan.** The model returns `tool_intent`: `asked` (the person asked for a tool), `fits` (a practical how-to where a tool is a natural next step) or `none` (including feeling-only questions).
- **In the response.** Unless `tool_intent` is `none`, `suggested_tools` lists up to `MCP_SUGGESTED_TOOLS_MAX` tools that support a matched concept, best weight first. `agent_instructions` tells the agent to offer one only when it fits, as an option, and never as required or as something it ran. For `fits`, the agent answers first and mentions at most one at the end.
