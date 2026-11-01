from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import quote

# A deployment package built in CI keeps its dependencies in .python_packages/lib/site-packages.
# The Azure Functions Python worker adds that folder to the path itself, but this server runs as a
# custom handler launched with plain `python`, so add it here before importing third-party packages.
_VENDORED = Path(__file__).resolve().parent / ".python_packages" / "lib" / "site-packages"
if _VENDORED.is_dir() and str(_VENDORED) not in sys.path:
    sys.path.insert(0, str(_VENDORED))

import requests  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402


# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = Path(
    os.getenv("LOCAL_SETTINGS_PATH", str(BASE_DIR / "local.settings.json"))
)


def load_settings():
    if not SETTINGS_PATH.exists():
        return {}
    data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    return data.get("Values", {})


LOCAL = load_settings()


def setting(name, default=""):
    return os.getenv(name) or LOCAL.get(name) or default


WEBSEARCH_URL = setting(
    "WEBSEARCH_URL",
    "https://websearch-h9arcweeftfrd8e0.eastus-01.azurewebsites.net",
).rstrip("/")

WIKISEARCH_URL = setting(
    "WIKISEARCH_URL",
    "https://wikisearch-gmebdbewaec6axdp.eastus-01.azurewebsites.net",
).rstrip("/")

ONTOLOGY_URL = setting(
    "ONTOLOGY_URL",
    "https://ontology-ghgedphdh6eefych.eastus-01.azurewebsites.net",
).rstrip("/")

WEBSEARCH_KEY = setting("WEBSEARCH_FUNCTION_KEY")
WIKISEARCH_KEY = setting("WIKISEARCH_FUNCTION_KEY")
ONTOLOGY_KEY = setting("ONTOLOGY_FUNCTION_KEY")

TIMEOUT = int(setting("MCP_REQUEST_TIMEOUT", "30"))
# On Azure (App Service or Azure Functions) listen on all interfaces. Locally, listen on 127.0.0.1.
# As an Azure Functions custom handler the port must match "port" in host.json (8000 here).
HOSTED = bool(
    os.getenv("WEBSITE_SITE_NAME")
    or os.getenv("FUNCTIONS_CUSTOMHANDLER_PORT")
    or os.getenv("FUNCTIONS_EXTENSION_VERSION")
)
MCP_HOST = setting("MCP_HOST", "0.0.0.0" if HOSTED else "127.0.0.1")
MCP_PORT = int(setting(
    "MCP_PORT",
    os.getenv("FUNCTIONS_CUSTOMHANDLER_PORT") or os.getenv("PORT") or os.getenv("WEBSITES_PORT") or "8000",
))


def flag(name, default):
    return str(setting(name, default)).strip().lower() in {"1", "true", "yes", "on"}


# Public-service settings.
ADMIN_TOOLS = flag("MCP_ADMIN_TOOLS", "false")   # diagnostic and ontology tools stay hidden unless enabled
STATELESS = flag("MCP_STATELESS", "true")        # no in-memory sessions, safe behind a load balancer
RATE_LIMIT_PER_IP = int(setting("MCP_RATE_LIMIT_PER_IP", "120"))     # requests per minute per client IP, 0 = off
RATE_LIMIT_GLOBAL = int(setting("MCP_RATE_LIMIT_GLOBAL", "600"))     # requests per minute in total, 0 = off

# Wiki pages that hold recorded circle sessions (recording links, transcript summaries)
# are not served to AI clients. Override with WIKI_EXCLUDE_TITLE_PATTERN.
WIKI_EXCLUDE = re.compile(
    setting(
        "WIKI_EXCLUDE_TITLE_PATTERN",
        r"\b(spring|summer|fall|winter)\s+20\d{2}\b.*\bcircle\b",
    ),
    re.IGNORECASE,
)

http = requests.Session()
http.headers.update({"Accept": "application/json"})


def auth(key):
    return {"x-functions-key": key} if key else {}


def get_json(url, key="", params=None):
    response = http.get(
        url,
        headers=auth(key),
        params=params,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {"data": data}


def post_json(url, payload, key=""):
    response = http.post(
        url,
        headers={
            **auth(key),
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {"data": data}


def error_result(exc):
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else "unknown"
        return {"error": f"Service returned HTTP {status}."}
    if isinstance(exc, requests.Timeout):
        return {"error": "Service request timed out."}
    if isinstance(exc, requests.RequestException):
        return {"error": f"Could not reach service ({type(exc).__name__})."}
    return {"error": f"{type(exc).__name__}: {exc}"}


def normalize_results(payload, source):
    results = []
    for item in payload.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        results.append({
            "title": item.get("title") or item.get("name") or "Untitled",
            "url": item.get("url") or item.get("link"),
            "snippet": item.get("snippet") or item.get("text") or "",
            "source": item.get("source") or source,
            "score": item.get("score"),
            "domain": item.get("domain"),
            "concepts": item.get("concepts"),
            "chunk_id": item.get("chunk_id"),
        })
    return results


def drop_excluded_wiki(items):
    return [
        x for x in items
        if not WIKI_EXCLUDE.search(str(x.get("title") or ""))
        and not WIKI_EXCLUDE.search(str(x.get("url") or "").replace("-", " ").replace("%3A", " "))
    ]


def merge_sources(wiki_items, website_items):
    """Alternate wiki and website results, wiki first, so neither source crowds out the other."""
    merged = []
    for index in range(max(len(wiki_items), len(website_items))):
        if index < len(wiki_items):
            merged.append(wiki_items[index])
        if index < len(website_items):
            merged.append(website_items[index])
    return merged


def resolution_summary(data):
    return {
        "status": data.get("status"),
        "ontology_version": data.get("ontology_version"),
        "matched_concepts": data.get("matched_concepts", []),
        "concept_matches": data.get("concept_matches", []),
        "domains": data.get("domains", []),
        "message": data.get("message"),
    }


def assess_quality(resolution, results, errors):
    concepts = resolution.get("matched_concepts") or []
    matches = resolution.get("concept_matches") or []
    reasons = []

    if not concepts:
        reasons.append("No clear ontology concept match.")
    if not resolution.get("domains"):
        reasons.append("No approved search domain was resolved.")
    if not results:
        reasons.append("No usable search results were returned.")
    elif not any(str(x.get("snippet") or "").strip() for x in results):
        reasons.append("Results contain no useful excerpts.")

    # Matches at or above 1.0 are explicit term hits in the query (for example both
    # "Sujeeth Foundation" and "donation" were named), so they are not competing
    # interpretations. Only compare the weaker, fuzzy matches against each other.
    weak = []
    has_strong = False
    for match in matches:
        if not isinstance(match, dict):
            continue
        try:
            confidence = float(match.get("confidence"))
        except (TypeError, ValueError):
            continue
        if confidence >= 1.0:
            has_strong = True
        else:
            weak.append((confidence, match))
    weak.sort(key=lambda pair: pair[0], reverse=True)

    # If the question named something outright ("week 1"), partial matches such as "Week 2" and
    # "Week 3" are noise, not a competing reading. Only look for a tie when nothing was named.
    close_pair = None
    if not has_strong and len(weak) >= 2 and weak[0][0] - weak[1][0] <= 0.10:
        reasons.append("Leading ontology matches are very close.")
        close_pair = (weak[0][1], weak[1][1])

    # A service failure is not a reason to quiz the user about their topic.
    service_degraded = bool(errors)
    all_failed = service_degraded and not results
    needs_clarification = bool(reasons) and not all_failed
    question = None

    if needs_clarification:
        if not concepts:
            question = (
                "Which Foundation area are you asking about: the Career Circle "
                "Program, students, volunteers, books, or resources?"
            )
        elif close_pair:
            first = close_pair[0].get("name", "the first topic")
            second = close_pair[1].get("name", "the second topic")
            question = f"Are you asking about {first} or {second}?"
        else:
            question = (
                "Could you clarify whether you mean the Career Circle Program, "
                "a specific person, a book, or another Foundation resource?"
            )

    instructions = (
        "If needs_clarification is true, ask the suggested question before "
        "making a definitive claim. Use ontology context to explain "
        "available concepts. Never guess a domain or search outside "
        "ontology-approved domains. Otherwise answer from the results "
        "and cite their URLs. Never quote program statistics (counts, rates, "
        "placement or engagement figures) from these results; point the user "
        "to sujeeth.org for current numbers."
    )
    if service_degraded:
        instructions += (
            " One or more search services failed, so results may be incomplete. "
            "Tell the user this and suggest trying again; do not treat missing "
            "results as proof that nothing exists."
        )

    return {
        "needs_clarification": needs_clarification,
        "service_degraded": service_degraded,
        "reasons": reasons,
        "suggested_question": question,
        "agent_instructions": instructions,
    }


# ------------------------------------------------------------
# MCP server
# ------------------------------------------------------------

mcp = FastMCP(
    "Sujeeth Foundation Search",
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=STATELESS,
)


def admin_tool(fn):
    """Register a diagnostic or ontology tool only when MCP_ADMIN_TOOLS is on."""
    return mcp.tool()(fn) if ADMIN_TOOLS else fn


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    """Liveness probe for the hosting platform. Does not call any backend."""
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/", methods=["GET"])
async def root(request):
    """The Azure Functions host pings / to check that a custom handler is running."""
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok", "service": "Sujeeth Foundation MCP", "endpoint": "/mcp"})


class RateLimitMiddleware:
    """Per-client and global request limits that answer HTTP 429.

    Counts are in memory, so each running instance applies its own limits. Clients that
    connect through a shared provider (a hosted AI assistant, for example) arrive from the
    provider's addresses, which is why the global limit exists as well as the per-IP one.
    """

    def __init__(self, app, per_ip, global_limit, window=60.0):
        self.app = app
        self.per_ip = per_ip
        self.global_limit = global_limit
        self.window = window
        self.hits = {}
        self.all_hits = deque()

    @staticmethod
    def client_ip(scope):
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-for":
                parts = [p.strip() for p in value.decode("latin-1").split(",") if p.strip()]
                if parts:
                    # The proxy appends the address it saw, so the last entry is the one to trust.
                    last = parts[-1]
                    return last.split(":")[0] if last.count(":") == 1 else last
        client = scope.get("client")
        return client[0] if client else "unknown"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in ("/healthz", "/"):
            await self.app(scope, receive, send)
            return
        now = time.monotonic()
        while self.all_hits and now - self.all_hits[0] > self.window:
            self.all_hits.popleft()
        if len(self.hits) > 5000:
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] <= self.window}
        recent = self.hits.setdefault(self.client_ip(scope), deque())
        while recent and now - recent[0] > self.window:
            recent.popleft()
        if (self.per_ip and len(recent) >= self.per_ip) or (
            self.global_limit and len(self.all_hits) >= self.global_limit
        ):
            await send({"type": "http.response.start", "status": 429, "headers": [
                (b"content-type", b"application/json"), (b"retry-after", b"30")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"Too many requests. Please try again shortly."}'})
            return
        recent.append(now)
        self.all_hits.append(now)
        await self.app(scope, receive, send)


@admin_tool
def check_services() -> dict[str, Any]:
    """Check health of all three Azure Function Apps."""
    services = {
        "websearch": (f"{WEBSEARCH_URL}/api/health", WEBSEARCH_KEY),
        "wikisearch": (f"{WIKISEARCH_URL}/api/health", WIKISEARCH_KEY),
        "ontology": (f"{ONTOLOGY_URL}/api/health", ONTOLOGY_KEY),
    }

    output = {}
    for name, (url, key) in services.items():
        try:
            output[name] = get_json(url, key)
        except Exception as exc:
            output[name] = error_result(exc)

    output["all_healthy"] = all(
        name in output
        and "error" not in output[name]
        and output[name].get("status") in {"healthy", "ok"}
        for name in services
    )
    return output


@admin_tool
def resolve_ontology(
    query: str,
    concepts: list[str] | None = None,
    domains: list[str] | None = None,
    purpose: str = "search",
) -> dict[str, Any]:
    """Resolve a query to ontology concepts and approved domains."""
    query = (query or "").strip()
    if not query:
        return {"error": "Please provide a query."}

    try:
        result = post_json(
            f"{WEBSEARCH_URL}/api/domain-resolution",
            {
                "query": query,
                "concepts": concepts or [],
                "domains": domains or [],
                "purpose": purpose,
            },
            WEBSEARCH_KEY,
        )
        result["resolution_summary"] = resolution_summary(result)
        return result
    except Exception as exc:
        return error_result(exc)


@admin_tool
def get_ontology_context(
    concept: str | None = None,
    depth: int = 2,
) -> dict[str, Any]:
    """Get ontology terminology and relationship context for a concept."""
    depth = max(1, min(int(depth), 4))

    try:
        terminology = get_json(
            f"{ONTOLOGY_URL}/api/ontology/terminology",
            ONTOLOGY_KEY,
        )

        result = {"terminology": terminology}

        if concept and concept.strip():
            concept_id = quote(concept.strip().lower(), safe="")
            result["concept"] = concept_id
            result["relationships"] = get_json(
                f"{ONTOLOGY_URL}/api/ontology/concepts/{concept_id}/relationships",
                ONTOLOGY_KEY,
                {"depth": depth},
            )
        else:
            result["classes"] = get_json(
                f"{ONTOLOGY_URL}/api/ontology/classes",
                ONTOLOGY_KEY,
            )
            result["relationships_summary"] = get_json(
                f"{ONTOLOGY_URL}/api/ontology/relationships/summary",
                ONTOLOGY_KEY,
            )

        return result
    except Exception as exc:
        return error_result(exc)


@mcp.tool()
def search_website(
    query: str,
    concepts: list[str] | None = None,
    domains: list[str] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Search ontology-approved Sujeeth Foundation websites."""
    query = (query or "").strip()
    if not query:
        return {"error": "Please provide a query."}

    try:
        return post_json(
            f"{WEBSEARCH_URL}/api/search",
            {
                "query": query,
                "concepts": concepts or [],
                "domains": domains or [],
                "purpose": "search",
                "limit": max(1, min(int(limit), 5)),
            },
            WEBSEARCH_KEY,
        )
    except Exception as exc:
        return error_result(exc)


@mcp.tool()
def search_wiki(query: str, limit: int = 5) -> dict[str, Any]:
    """Search the Sujeeth Foundation GitHub wiki."""
    query = (query or "").strip()
    if not query:
        return {"error": "Please provide a query."}

    try:
        payload = get_json(
            f"{WIKISEARCH_URL}/api/search",
            WIKISEARCH_KEY,
            {"q": query, "limit": max(1, min(int(limit), 10))},
        )

        results = drop_excluded_wiki([
            item for item in normalize_results(payload, "wiki")
            if item.get("source") == "wiki"
        ])

        return {
            "query": payload.get("query", query),
            "count": len(results),
            "results": results,
        }
    except Exception as exc:
        return error_result(exc)


@mcp.tool()
def search_sujeeth_foundation(
    query: str,
    limit: int = 5,
) -> dict[str, Any]:
    """
    Search Website Search and Wiki Search together using ontology resolution.
    Returns a suggested clarification question when search quality is weak.
    """
    query = (query or "").strip()
    if not query:
        return {"error": "Please provide a query."}

    limit = max(1, min(int(limit), 5))
    errors = []

    # First resolve the query through the Search App's ontology resolver.
    try:
        resolution = post_json(
            f"{WEBSEARCH_URL}/api/domain-resolution",
            {"query": query, "purpose": "search"},
            WEBSEARCH_KEY,
        )
    except Exception as exc:
        return {
            "query": query,
            "status": "resolution_failed",
            "results": [],
            "service_errors": [
                {"service": "websearch_resolution", **error_result(exc)}
            ],
            "needs_clarification": False,
            "service_degraded": True,
            "suggested_question": None,
            "agent_instructions": (
                "The Foundation search service is temporarily unavailable. Tell the "
                "user, suggest trying again shortly, and do not guess an answer. "
                "Do not ask the user to clarify their question."
            ),
        }

    concepts = resolution.get("matched_concepts") or []
    domains = resolution.get("domains") or []

    # Only search websites when the ontology authorizes a concept and domain.
    if concepts and domains:
        try:
            website = post_json(
                f"{WEBSEARCH_URL}/api/search",
                {
                    "query": query,
                    "concepts": concepts,
                    "domains": domains,
                    "purpose": "search",
                    "limit": limit,
                },
                WEBSEARCH_KEY,
            )
        except Exception as exc:
            website = {"results": []}
            errors.append({"service": "websearch", **error_result(exc)})
    else:
        website = {"results": [], "status": resolution.get("status")}

    try:
        wiki = get_json(
            f"{WIKISEARCH_URL}/api/search",
            WIKISEARCH_KEY,
            {"q": query, "limit": limit},
        )
    except Exception as exc:
        wiki = {"results": []}
        errors.append({"service": "wikisearch", **error_result(exc)})

    wiki_items = drop_excluded_wiki([
        item for item in normalize_results(wiki, "wiki")
        if item.get("source") in {"wiki", None, ""}
    ])
    combined = merge_sources(wiki_items, normalize_results(website, "website"))

    # Remove duplicate URLs.
    seen = set()
    results = []
    for item in combined:
        identity = str(item.get("url") or item.get("title") or "").lower()
        if identity and identity not in seen:
            seen.add(identity)
            results.append(item)

    results = results[:limit]
    quality = assess_quality(resolution, results, errors)

    return {
        "query": query,
        "status": "partial" if errors else "completed",
        "ontology": resolution_summary(resolution),
        "website_search": {
            "status": website.get("status"),
            "count": len(website.get("results", []) or []),
            "domains": website.get("domains", domains),
            "search_mode": website.get("search_mode"),
        },
        "wiki_search": {
            "count": len(drop_excluded_wiki(normalize_results(wiki, "wiki"))),
        },
        "count": len(results),
        "results": results,
        "service_errors": errors,
        **quality,
    }


def looks_like_wiki(url, chunk_id, source):
    if source:
        return str(source).strip().lower() == "wiki"
    if url and "github.com" in str(url).lower() and "/wiki/" in str(url).lower():
        return True
    return bool(chunk_id and str(chunk_id).lower().endswith(".md"))


@mcp.tool()
def get_document(
    url: str | None = None,
    chunk_id: str | None = None,
    source: str | None = None,
    neighbors: int = 1,
    max_chars: int = 6000,
) -> dict[str, Any]:
    """
    Read more of a search result. Pass the url or chunk_id exactly as a search result
    returned it (and its source, "website" or "wiki", if known). Foundation sites and the
    wiki return the page text. Third-party sites return only a short excerpt around the
    matched passage, so link to the source instead of reproducing it.
    """
    if not (url or chunk_id):
        return {"error": "Provide a url or chunk_id from a search result."}
    max_chars = max(500, min(int(max_chars), 20000))

    try:
        if looks_like_wiki(url, chunk_id, source):
            result = post_json(
                f"{WIKISEARCH_URL}/api/document",
                {"url": url, "path": chunk_id, "max_chars": max_chars},
                WIKISEARCH_KEY,
            )
        else:
            result = post_json(
                f"{WEBSEARCH_URL}/api/document",
                {
                    "url": url,
                    "chunk_id": chunk_id,
                    "neighbors": max(0, min(int(neighbors), 3)),
                    "max_chars": max_chars,
                },
                WEBSEARCH_KEY,
            )
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 404:
            return {"error": "That document was not found. Use a url or chunk_id from a search result."}
        if status == 403:
            return {"error": "That document is not available through this server."}
        return error_result(exc)
    except Exception as exc:
        return error_result(exc)

    result["agent_instructions"] = (
        "Cite the url. Never quote program statistics (counts, rates, placement or "
        "engagement figures) from this text; point the user to sujeeth.org for current "
        "numbers. If excerpt_only is true, summarize briefly and link to the source."
    )
    return result


if __name__ == "__main__":
    import uvicorn

    print("Starting Sujeeth Foundation MCP Server")
    print(f"Website Search: {WEBSEARCH_URL}")
    print(f"Wiki Search: {WIKISEARCH_URL}")
    print(f"Ontology: {ONTOLOGY_URL}")
    print(f"Admin tools: {'on' if ADMIN_TOOLS else 'off'} | stateless: {STATELESS} | "
          f"rate limits per minute: per-IP {RATE_LIMIT_PER_IP}, global {RATE_LIMIT_GLOBAL}")
    print(f"MCP endpoint: http://{MCP_HOST}:{MCP_PORT}/mcp")

    app = mcp.streamable_http_app()
    if RATE_LIMIT_PER_IP or RATE_LIMIT_GLOBAL:
        app = RateLimitMiddleware(app, RATE_LIMIT_PER_IP, RATE_LIMIT_GLOBAL)
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")