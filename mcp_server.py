from __future__ import annotations

import asyncio
import functools
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

# A deployment package built in CI keeps its dependencies in .python_packages/lib/site-packages.
# The Azure Functions Python worker adds that folder to the path itself, but this server runs as a
# custom handler launched with plain `python`, so add it here before importing third-party packages.
_VENDORED = Path(__file__).resolve().parent / ".python_packages" / "lib" / "site-packages"
if _VENDORED.is_dir() and str(_VENDORED) not in sys.path:
    sys.path.insert(0, str(_VENDORED))

import requests  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

from rate_limit import RateLimitMiddleware, TrustedRanges  # noqa: E402


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

# Search diversity settings.
# SEARCH_RESULT_COUNT: ordinary results returned (best bets and spotlights come on top of these).
# SEARCH_CANDIDATE_POOL: how many website candidates to request before choosing, so less
#   dominant sources have a chance to appear.
# MAX_RESULTS_PER_DOMAIN: ceiling per source. Results are picked one per source first, then a
#   second per source, and so on, until SEARCH_RESULT_COUNT is reached.
RESULT_COUNT = int(setting("SEARCH_RESULT_COUNT", "10") or 10)
CANDIDATE_POOL = int(setting("SEARCH_CANDIDATE_POOL", "50") or 50)
MAX_PER_DOMAIN = int(setting("MAX_RESULTS_PER_DOMAIN", "2") or 2)
WIKI_POOL = MAX_PER_DOMAIN + 2  # the wiki counts as one source, so it needs only a few candidates
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
# Tool calls do blocking work (HTTP calls to the search apps), so they run on a pool of worker threads and the
# event loop stays free to accept requests. Without this, one instance answers one tool call at a time.
TOOL_THREADS = max(1, int(setting("MCP_TOOL_THREADS", "32") or 32))
RATE_LIMIT_GLOBAL = int(setting("MCP_RATE_LIMIT_GLOBAL", "600"))     # requests per minute in total, 0 = off

# Hosted AI assistants (Claude, ChatGPT, the Foundry agent) reach this server from a few shared addresses, so
# many visitors arrive as one client. Callers that are trusted as such count in a separate, much higher
# per-address bucket. Trust only changes the per-address limit; the global limit still counts everyone.
# See rate_limit.py for the full rules. "none" turns a list off.
RATE_LIMIT_TRUSTED_PER_IP = int(setting("MCP_RATE_LIMIT_TRUSTED_PER_IP", "1200"))  # per minute per trusted address, 0 = off
TRUSTED_RANGES = [] if setting("MCP_TRUSTED_RANGES", "160.79.104.0/21").strip().lower() == "none" else [
    x.strip() for x in setting("MCP_TRUSTED_RANGES", "160.79.104.0/21").split(",") if x.strip()
]  # default: Anthropic's published outbound range
TRUSTED_RANGE_URLS = [] if setting("MCP_TRUSTED_RANGE_URLS", "https://openai.com/chatgpt-connectors.json").strip().lower() == "none" else [
    x.strip() for x in setting("MCP_TRUSTED_RANGE_URLS", "https://openai.com/chatgpt-connectors.json").split(",") if x.strip()
]  # default: OpenAI's ChatGPT connectors manifest
TRUSTED_REFRESH_SECONDS = int(setting("MCP_TRUSTED_REFRESH_SECONDS", "86400"))
TRUSTED_KEY = setting("MCP_TRUSTED_KEY")                      # shared secret the Foundry project sends; empty = off
TRUSTED_KEY_HEADER = setting("MCP_TRUSTED_KEY_HEADER", "x-mcp-client-key")
TRUSTED_USER_AGENTS = setting("MCP_TRUSTED_USER_AGENTS")      # optional regex; a User-Agent can be faked

# Token savings. Searches return short excerpts by default; the agent reads a whole page with
# get_document only when it needs more. Set SEARCH_FULL_DOCUMENTS=true to attach full page
# text to search results again. SEARCH_VERBOSE_ONTOLOGY adds the per-concept match details
# and a second copy of the domain list to each response (about 600 tokens).
FULL_DOCUMENTS_DEFAULT = flag("SEARCH_FULL_DOCUMENTS", "false")
VERBOSE_ONTOLOGY = flag("SEARCH_VERBOSE_ONTOLOGY", "false")
# SEARCH_TIMINGS=true adds a timings_ms block to each combined search response. The timings are
# always written to the log, so you can read them in Application Insights without the flag.
TIMINGS = flag("SEARCH_TIMINGS", "false")
# Most pages one get_documents call will read (they are read in parallel).
MAX_BATCH_DOCUMENTS = max(1, int(setting("MAX_BATCH_DOCUMENTS", "3") or 3))
# Pages read through get_document and get_documents are kept in memory so a repeat read does not
# call the search apps again. DOCUMENT_CACHE_SECONDS=0 turns this off. A page changed on the
# site can show its old text for up to this long (a wiki page for up to this long plus the
# wiki app's own 15 minutes). Each entry is one page, at most about 12 to 24 KB.
DOCUMENT_CACHE_SECONDS = max(0, int(setting("DOCUMENT_CACHE_SECONDS", "900") or 0))
DOCUMENT_CACHE_MAX = max(10, int(setting("DOCUMENT_CACHE_MAX_ENTRIES", "300") or 300))

# Warm-up. Shortly after the server starts, and then every WARMUP_INTERVAL_SECONDS, a background
# thread runs the common questions below and reads the Foundation's own pages they find, so the
# page cache is filled before a user asks. It also keeps the search apps awake and rebuilds the
# wiki app's own cache (a 20 second job) in the background instead of during a user's request.
# WARMUP_QUERIES is separated by semicolons. WARMUP_INTERVAL_SECONDS=0 warms once at start only.
# Keep the interval shorter than DOCUMENT_CACHE_SECONDS, or pages expire between refreshes.
WARMUP_ENABLED = flag("WARMUP_ENABLED", "true")
WARMUP_QUERIES = [
    q.strip()
    for q in str(setting(
        "WARMUP_QUERIES",
        "Week 1 agenda; Week 2 agenda; Week 3 agenda; Week 4 agenda; Week 5 agenda; "
        "Week 6 agenda; how Career Circles work; become a mentor volunteer; mentor FAQ",
    )).replace("\n", ";").split(";")
    if q.strip()
]
WARMUP_PAGES_PER_QUERY = max(1, int(setting("WARMUP_PAGES_PER_QUERY", "2") or 2))
WARMUP_INTERVAL_SECONDS = max(0, int(setting("WARMUP_INTERVAL_SECONDS", "300") or 0))
WARMUP_DELAY_SECONDS = max(0, int(setting("WARMUP_DELAY_SECONDS", "10") or 0))

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
_adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=max(10, TOOL_THREADS * 2))
http.mount("https://", _adapter)
http.mount("http://", _adapter)

_tool_pool = ThreadPoolExecutor(max_workers=TOOL_THREADS, thread_name_prefix="mcp-tool")


def in_thread(fn):
    """Run a blocking tool function on the worker pool.

    FastMCP calls a plain `def` tool directly on the event loop, so a search that waits on the search apps
    stops the whole process from accepting or answering anything else (including /healthz) until it ends.
    The wrapper is async, so FastMCP awaits it, and keeps the name, docstring and signature FastMCP reads.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_tool_pool, functools.partial(fn, *args, **kwargs))

    return wrapper


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
            "best_bet": bool(item.get("best_bet")),
            "spotlight": bool(item.get("spotlight")),
            # Wiki Search returns the whole page as text beside the snippet. Keep it for
            # attach_full_documents and strip it before the response goes out.
            "_full_text": item.get("text") if item.get("snippet") and item.get("text") else None,
            "_truncated": bool(item.get("truncated")),
        })
    return results


def strip_internal(results):
    """Remove keys that only the server uses from results before they are returned."""
    for item in results:
        item.pop("_full_text", None)
        item.pop("_truncated", None)
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


def domain_key(item):
    """The source an item counts against. The wiki is one source; sujeeth.org subdomains are one."""
    if item.get("source") == "wiki":
        return "wiki"
    host = str(item.get("domain") or "").strip().lower()
    if not host:
        host = (urlparse(str(item.get("url") or "")).hostname or "").lower()
    host = host.removeprefix("www.")
    if host == "sujeeth.org" or host.endswith(".sujeeth.org"):
        return "sujeeth.org"
    return host or "unknown"


def select_diverse(candidates, count, max_per_domain):
    """Round-robin across sources. Round 1 takes each source's best result (sources ordered by
    where their best result ranks), round 2 takes each source's second, up to max_per_domain,
    stopping at count."""
    by_domain = {}
    for item in candidates:
        by_domain.setdefault(domain_key(item), []).append(item)
    picked = []
    for round_n in range(max(1, max_per_domain)):
        for docs in by_domain.values():
            if round_n < len(docs):
                picked.append(docs[round_n])
                if len(picked) >= count:
                    return picked
    return picked


def combine_with_best_bets(wiki_items, website_items, limit):
    """Best bets first (hand-added pages the website search flagged), then spotlights, then up
    to `limit` wiki and website results chosen across sources (see select_diverse). Best bets
    and spotlights sit outside the limit and the per-source cap. Each URL appears once."""
    best, spotlights, seen = [], [], set()
    for item in website_items:
        identity = str(item.get("url") or "").lower()
        if item.get("best_bet") and identity and identity not in seen:
            seen.add(identity)
            best.append(item)
    # Spotlight pages the website search reserved for career questions go right after best
    # bets, so wiki and Teal results cannot crowd them out of the limit.
    for item in website_items:
        identity = str(item.get("url") or "").lower()
        if item.get("spotlight") and not item.get("best_bet") and identity and identity not in seen:
            seen.add(identity)
            spotlights.append(item)
    normal = []
    prioritized = lambda x: x.get("best_bet") or x.get("spotlight")
    for item in merge_sources(wiki_items, [x for x in website_items if not prioritized(x)]):
        identity = str(item.get("url") or item.get("title") or "").lower()
        if identity and identity not in seen:
            seen.add(identity)
            normal.append(item)
    return best + spotlights + select_diverse(normal, limit, MAX_PER_DOMAIN), len(best), len(spotlights)


def resolution_summary(data):
    summary = {
        "status": data.get("status"),
        "ontology_version": data.get("ontology_version"),
        "matched_concepts": data.get("matched_concepts", []),
        "domains": data.get("domains", []),
        "message": data.get("message"),
    }
    if VERBOSE_ONTOLOGY:
        summary["concept_matches"] = data.get("concept_matches", [])
    return summary


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


DOCUMENT_INSTRUCTIONS = (
    "Cite the url. Never quote program statistics (counts, rates, placement or "
    "engagement figures) from this text; point the user to sujeeth.org for current "
    "numbers. If excerpt_only is true, summarize briefly and link to the source."
)

FULL_DOCUMENT_INSTRUCTIONS = (
    " Results with a document field carry the full page text for that result. Answer from "
    "the document, not the snippet, and cite the url. If a document has excerpt_only set to "
    "true, it is third-party content: summarize briefly and link to the source, do not "
    "reproduce it. If a document is truncated, say so and call get_document for more."
)

# Used when a search returns excerpts only. This sits beside the results, which is where the
# agent decides whether to answer or to read a page.
EXCERPT_INSTRUCTIONS = (
    " Results are excerpts and may stop mid-sentence. If they do not fully answer the question "
    "(steps, an agenda, a list, dates, exact wording, or an excerpt that looks cut off), call "
    "get_documents once, with the urls of every page you need, before answering, starting with "
    "a best_bet or spotlight. Read at most two pages per question, and read them in one call. "
    "Results whose domain is not sujeeth.org are third-party: summarize briefly and link "
    "instead of reading them. Do not answer program details from an excerpt that does not "
    "contain them."
)

GET_DOCUMENT_INSTRUCTIONS = DOCUMENT_INSTRUCTIONS + (
    " If truncated is true and the part you need is missing, call get_document again with a "
    "larger max_chars (up to 12000)."
)

PRIORITY_INSTRUCTIONS = (
    " Results flagged best_bet or spotlight are curated pages. Present them first, "
    "in the order returned. When one fits the question, include it with its link "
    "and one sentence on why it fits."
)

# Total characters of document text attached to one search response, so a broad question
# cannot flood the conversation.
DOCUMENT_CHAR_BUDGET = int(setting("FULL_DOCUMENT_CHAR_BUDGET", "60000") or 60000)


def attach_full_documents(results, max_documents, per_document_chars):
    """Attach full page text to best bets and the first ordinary results, one document per distinct url.

    Foundation pages and wiki pages come back whole. Third-party pages come back as the
    excerpt the document service allows, flagged excerpt_only. A failed fetch leaves the
    result as it was. Returns a small summary for the response.
    """
    # Best bets and spotlights are curated pages, so every one of them gets its document. They
    # do not count against max_documents, which limits only the ordinary results after them.
    targets, seen, ordinary = [], set(), 0
    for item in results:
        key = str(item.get("url") or "").strip().lower()
        if not key or key in seen:
            continue
        if item.get("best_bet") or item.get("spotlight"):
            seen.add(key)
            targets.append(item)
        elif ordinary < max_documents:
            seen.add(key)
            targets.append(item)
            ordinary += 1

    def load(item):
        # Text that came back with the search result needs no second request.
        if item.get("_full_text"):
            return {
                "text": item["_full_text"],
                "truncated": item.get("_truncated"),
                "excerpt_only": False,
                "from_search": True,
            }
        try:
            return fetch_document(
                item.get("url"), item.get("chunk_id"), item.get("source"), 3, per_document_chars
            )
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=max(1, min(4, len(targets) or 1))) as pool:
        fetched = list(pool.map(load, targets))

    summary = {
        "requested": len(targets), "returned": 0, "excerpt_only": 0, "failed": 0,
        "skipped_for_length": 0, "from_search_text": 0,
    }
    used = 0
    for item, doc in zip(targets, fetched):
        text = str((doc or {}).get("text") or "")
        if not text:
            summary["failed"] += 1
            continue
        if used + len(text) > DOCUMENT_CHAR_BUDGET:
            summary["skipped_for_length"] += 1
            continue
        used += len(text)
        excerpt = bool(doc.get("excerpt_only"))
        item["document"] = {
            "text": text,
            "excerpt_only": excerpt,
            "truncated": bool(doc.get("truncated")),
            "note": doc.get("note"),
        }
        summary["returned"] += 1
        summary["excerpt_only"] += 1 if excerpt else 0
        summary["from_search_text"] += 1 if doc.get("from_search") else 0
    return summary


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
    return mcp.tool()(in_thread(fn)) if ADMIN_TOOLS else fn


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
@in_thread
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


def run_wiki_search(query, full_documents, document_max_chars):
    """Wiki search for the combined tool. Returns (payload, error) and never raises."""
    try:
        params = {"q": query, "limit": WIKI_POOL}
        if full_documents:
            params.update({
                "full_text": "true",
                "max_chars": max(500, min(int(document_max_chars), 20000)),
            })
        else:
            params["full_text"] = "false"
        return get_json(f"{WIKISEARCH_URL}/api/search", WIKISEARCH_KEY, params), None
    except Exception as exc:
        return {"results": []}, exc


@mcp.tool()
@in_thread
def search_wiki(
    query: str,
    limit: int = 5,
    full_documents: bool = FULL_DOCUMENTS_DEFAULT,
    document_max_chars: int = 12000,
) -> dict[str, Any]:
    """
    Search the Sujeeth Foundation GitHub wiki. Returns short excerpts that can stop
    mid-sentence. To read pages, call get_documents once with their urls.
    Set full_documents to true to attach the full page text to every result (uses many tokens).
    """
    query = (query or "").strip()
    if not query:
        return {"error": "Please provide a query."}

    document_max_chars = max(500, min(int(document_max_chars), 20000))
    try:
        params = {"q": query, "limit": max(1, min(int(limit), 10))}
        if full_documents:
            params.update({"full_text": "true", "max_chars": document_max_chars})
        else:
            params["full_text"] = "false"
        payload = get_json(f"{WIKISEARCH_URL}/api/search", WIKISEARCH_KEY, params)

        results = drop_excluded_wiki([
            item for item in normalize_results(payload, "wiki")
            if item.get("source") == "wiki"
        ])
        response = {
            "query": payload.get("query", query),
            "count": len(results),
            "results": results,
        }
        if full_documents:
            response["full_documents"] = attach_full_documents(
                results, len(results) or 1, document_max_chars
            )
            if response["full_documents"]["returned"]:
                response["agent_instructions"] = DOCUMENT_INSTRUCTIONS.replace(
                    "If excerpt_only is true, summarize briefly and link to the source.",
                    "Answer from the document field, not the snippet.",
                )
        else:
            response["agent_instructions"] = DOCUMENT_INSTRUCTIONS.replace(
                "If excerpt_only is true, summarize briefly and link to the source.",
                EXCERPT_INSTRUCTIONS.strip(),
            )
        strip_internal(results)
        return response
    except Exception as exc:
        return error_result(exc)


@mcp.tool()
@in_thread
def search_sujeeth_foundation(
    query: str,
    limit: int | None = None,
    full_documents: bool = FULL_DOCUMENTS_DEFAULT,
    max_documents: int | None = None,
    document_max_chars: int = 12000,
) -> dict[str, Any]:
    """
    Search Website Search and Wiki Search together using ontology resolution.
    Results flagged best_bet or spotlight are curated pages; present them first.
    Results come from as many different sources as possible (at most MAX_RESULTS_PER_DOMAIN
    from any one). limit defaults to SEARCH_RESULT_COUNT.
    Results are short excerpts that can stop mid-sentence. To read pages, call
    get_documents once with their urls. Set full_documents to true to attach
    full page text instead: every best bet gets one, plus up to max_documents other results
    (default: all of them), each up to document_max_chars, within the response's total
    character budget (uses many tokens). Third-party pages are always excerpts.
    Returns a suggested clarification question when search quality is weak.
    """
    query = (query or "").strip()
    if not query:
        return {"error": "Please provide a query."}

    limit = max(1, min(int(limit or RESULT_COUNT), 20))
    errors = []

    # Timing: each lap records the milliseconds since the previous one.
    started = mark = time.perf_counter()
    timings = {}

    def lap(name):
        nonlocal mark
        now = time.perf_counter()
        timings[name] = round((now - mark) * 1000)
        mark = now

    # The wiki search does not depend on the ontology, so run it alongside the steps below.
    wiki_pool = ThreadPoolExecutor(max_workers=1)
    wiki_future = wiki_pool.submit(run_wiki_search, query, full_documents, document_max_chars)

    # First resolve the query through the Search App's ontology resolver.
    try:
        resolution = post_json(
            f"{WEBSEARCH_URL}/api/domain-resolution",
            {"query": query, "purpose": "search"},
            WEBSEARCH_KEY,
        )
    except Exception as exc:
        wiki_pool.shutdown(wait=False)
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

    lap("resolution_ms")
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
                    "limit": CANDIDATE_POOL,
                },
                WEBSEARCH_KEY,
            )
        except Exception as exc:
            website = {"results": []}
            errors.append({"service": "websearch", **error_result(exc)})
    else:
        website = {"results": [], "status": resolution.get("status")}

    lap("website_ms")
    wiki, wiki_error = wiki_future.result()
    wiki_pool.shutdown(wait=False)
    if wiki_error is not None:
        errors.append({"service": "wikisearch", **error_result(wiki_error)})
    lap("wiki_wait_ms")

    wiki_items = drop_excluded_wiki([
        item for item in normalize_results(wiki, "wiki")
        if item.get("source") in {"wiki", None, ""}
    ])
    results, best_bet_count, spotlight_count = combine_with_best_bets(
        wiki_items, normalize_results(website, "website"), limit
    )
    quality = assess_quality(resolution, results, errors)
    if best_bet_count or spotlight_count:
        quality["agent_instructions"] += PRIORITY_INSTRUCTIONS

    documents_summary = None
    if full_documents and results:
        documents_summary = attach_full_documents(
            results,
            max(1, min(int(max_documents or limit), 20)),
            max(500, min(int(document_max_chars), 20000)),
        )
        if documents_summary["returned"]:
            quality["agent_instructions"] = quality["agent_instructions"] + FULL_DOCUMENT_INSTRUCTIONS
    lap("documents_ms")
    if not (documents_summary and documents_summary["returned"]):
        quality["agent_instructions"] = quality["agent_instructions"].replace(
            "Otherwise answer from the results and cite their URLs.",
            "Otherwise answer from the results, reading pages as described below, and cite their URLs.",
        ) + EXCERPT_INSTRUCTIONS
    strip_internal(results)
    timings["total_ms"] = round((time.perf_counter() - started) * 1000)
    print(f"search_timings_ms {json.dumps(timings)}", file=sys.stderr, flush=True)

    return {
        "query": query,
        "status": "partial" if errors else "completed",
        "ontology": resolution_summary(resolution),
        "website_search": {
            "status": website.get("status"),
            "count": len(website.get("results", []) or []),
            **({"domains": website.get("domains", domains)} if VERBOSE_ONTOLOGY else {}),
            "search_mode": website.get("search_mode"),
        },
        "wiki_search": {
            "count": len(drop_excluded_wiki(normalize_results(wiki, "wiki"))),
        },
        "count": len(results),
        "best_bet_count": best_bet_count,
        "spotlight_count": spotlight_count,
        "full_documents": documents_summary,
        "results": results,
        "service_errors": errors,
        **({"timings_ms": timings} if TIMINGS else {}),
        **quality,
    }


def looks_like_wiki(url, chunk_id, source):
    if source:
        return str(source).strip().lower() == "wiki"
    if url and "github.com" in str(url).lower() and "/wiki/" in str(url).lower():
        return True
    return bool(chunk_id and str(chunk_id).lower().endswith(".md"))


def _fetch_document(url=None, chunk_id=None, source=None, neighbors=1, max_chars=6000):
    """Fetch one document from Wiki Search or Website Search. Raises on failure."""
    max_chars = max(500, min(int(max_chars), 20000))
    if looks_like_wiki(url, chunk_id, source):
        return post_json(
            f"{WIKISEARCH_URL}/api/document",
            {"url": url, "path": chunk_id, "max_chars": max_chars},
            WIKISEARCH_KEY,
        )
    return post_json(
        f"{WEBSEARCH_URL}/api/document",
        {
            "url": url,
            "chunk_id": chunk_id,
            "neighbors": max(0, min(int(neighbors), 3)),
            "max_chars": max_chars,
        },
        WEBSEARCH_KEY,
    )


_document_cache = {}  # (url, max_chars) -> (expires, result)
_document_cache_lock = threading.Lock()


def fetch_document(url=None, chunk_id=None, source=None, neighbors=1, max_chars=6000, refresh=False):
    """Fetch one document, reusing a recent copy of a whole-page read. Raises on failure.

    Only reads by url are kept. A read by chunk_id returns a passage with its neighbors, which
    depends on the arguments, so it always goes to the search apps. Failures are never kept.
    """
    if DOCUMENT_CACHE_SECONDS <= 0 or chunk_id or not url:
        return _fetch_document(url, chunk_id, source, neighbors, max_chars)
    key = (str(url).strip().lower().rstrip("/"), max(500, min(int(max_chars), 20000)))
    now = time.monotonic()
    with _document_cache_lock:
        hit = _document_cache.get(key)
    if hit and hit[0] > now and not refresh:
        return dict(hit[1])
    result = _fetch_document(url, chunk_id, source, neighbors, max_chars)
    if isinstance(result, dict) and result.get("text") and not result.get("error"):
        with _document_cache_lock:
            _document_cache[key] = (now + DOCUMENT_CACHE_SECONDS, dict(result))
            if len(_document_cache) > DOCUMENT_CACHE_MAX:
                for old in list(_document_cache)[: max(1, DOCUMENT_CACHE_MAX // 10)]:
                    _document_cache.pop(old, None)
    return result


@mcp.tool()
@in_thread
def get_document(
    url: str | None = None,
    chunk_id: str | None = None,
    source: str | None = None,
    neighbors: int = 1,
    max_chars: int = 6000,
) -> dict[str, Any]:
    """
    Read the page behind a search result. Search results are excerpts; call this when they do
    not fully answer the question. Pass the url (or chunk_id) exactly as the result returned it,
    and its source, "website" or "wiki". Foundation sites and the wiki return the page text
    (total_chars and truncated say whether more exists; raise max_chars up to 12000 for more).
    Third-party sites return only a short excerpt around the matched passage, so link to the
    source instead of reproducing it.
    """
    if not (url or chunk_id):
        return {"error": "Provide a url or chunk_id from a search result."}

    try:
        result = fetch_document(url, chunk_id, source, neighbors, max_chars)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status == 404:
            return {"error": "That document was not found. Use a url or chunk_id from a search result."}
        if status == 403:
            return {"error": "That document is not available through this server."}
        return error_result(exc)
    except Exception as exc:
        return error_result(exc)

    result["agent_instructions"] = GET_DOCUMENT_INSTRUCTIONS
    return result


def explain_fetch_error(exc):
    """A short, user-safe reason a page could not be read."""
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        if status == 404:
            return "That document was not found. Use a url from a search result."
        if status == 403:
            return "That document is not available through this server."
    return error_result(exc).get("error", "Could not read the document.")


@mcp.tool()
@in_thread
def get_documents(urls: list[str], max_chars: int = 6000) -> dict[str, Any]:
    """
    Read the pages behind several search results in one call, all at the same time. Call it
    once with every url you need (up to 3), exactly as the search results returned them,
    instead of calling get_document more than once. Foundation sites and the wiki return the
    page text; third-party sites return only a short excerpt, so link to the source instead
    of reproducing it. Each document reports total_chars and truncated.
    """
    seen, wanted = set(), []
    for raw in urls or []:
        url = str(raw or "").strip()
        key = url.lower().rstrip("/")
        if url and key not in seen:
            seen.add(key)
            wanted.append(url)
    if not wanted:
        return {"error": "Provide at least one url from a search result."}
    skipped = wanted[MAX_BATCH_DOCUMENTS:]
    wanted = wanted[:MAX_BATCH_DOCUMENTS]
    max_chars = max(500, min(int(max_chars), 12000))

    def read(url):
        began = time.perf_counter()
        try:
            doc = fetch_document(url, None, None, 1, max_chars)
            item = {"url": url, "status": "ok"}
            for key in ("title", "source", "text", "total_chars", "truncated", "excerpt_only", "note"):
                if doc.get(key) is not None:
                    item[key] = doc[key]
        except Exception as exc:
            item = {"url": url, "status": "error", "error": explain_fetch_error(exc)}
        item["ms"] = round((time.perf_counter() - began) * 1000)
        return item

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        documents = list(pool.map(read, wanted))
    total_ms = round((time.perf_counter() - started) * 1000)
    print(f"get_documents_ms {json.dumps({'total': total_ms, 'each': [d['ms'] for d in documents]})}",
          file=sys.stderr, flush=True)

    response = {
        "count": len(documents),
        "documents": documents,
        "agent_instructions": GET_DOCUMENT_INSTRUCTIONS.replace("call get_document again", "call get_documents again"),
    }
    if skipped:
        response["not_read"] = skipped
        response["note"] = f"Only the first {MAX_BATCH_DOCUMENTS} urls were read."
    return response


def is_foundation_page(item):
    """True for the Foundation's own pages and the wiki; third-party pages are never warmed."""
    domain = str(item.get("domain") or "").lower()
    return item.get("source") == "wiki" or domain == "sujeeth.org" or domain.endswith(".sujeeth.org")


def warm_cache_once():
    """Run the warm-up questions and read the Foundation pages they find. Never raises."""
    began = time.perf_counter()
    seen, pages, failures = set(), 0, 0
    for query in WARMUP_QUERIES:
        try:
            found = search_sujeeth_foundation(query, limit=4, full_documents=False)
            targets = []
            for item in found.get("results", []) or []:
                url = str(item.get("url") or "").strip()
                key = url.lower().rstrip("/")
                if not url or key in seen or not is_foundation_page(item):
                    continue
                seen.add(key)
                targets.append((url, item.get("source")))
                if len(targets) >= WARMUP_PAGES_PER_QUERY:
                    break
            if targets:
                with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                    list(pool.map(
                        lambda t: fetch_document(t[0], None, t[1], 1, 6000, refresh=True), targets))
                pages += len(targets)
        except Exception as exc:
            failures += 1
            print(f"warmup_failed query={query!r} error={type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
    print(f"warmup_done {json.dumps({'queries': len(WARMUP_QUERIES), 'pages': pages, 'failed_queries': failures, 'seconds': round(time.perf_counter() - began, 1)})}",
          file=sys.stderr, flush=True)


def warm_loop():
    time.sleep(WARMUP_DELAY_SECONDS)  # let the server start answering first
    while True:
        warm_cache_once()
        if WARMUP_INTERVAL_SECONDS <= 0:
            return
        time.sleep(WARMUP_INTERVAL_SECONDS)


def start_warmup():
    """Start the background warm-up. It does nothing when the page cache is off."""
    if not (WARMUP_ENABLED and WARMUP_QUERIES and DOCUMENT_CACHE_SECONDS > 0):
        return False
    if 0 < DOCUMENT_CACHE_SECONDS <= WARMUP_INTERVAL_SECONDS:
        print("warmup_note WARMUP_INTERVAL_SECONDS is not shorter than DOCUMENT_CACHE_SECONDS, "
              "so warmed pages will expire between refreshes.", file=sys.stderr, flush=True)
    threading.Thread(target=warm_loop, name="cache-warmup", daemon=True).start()
    return True


if __name__ == "__main__":
    import uvicorn

    print("Starting Sujeeth Foundation MCP Server")
    print(f"Website Search: {WEBSEARCH_URL}")
    print(f"Wiki Search: {WIKISEARCH_URL}")
    print(f"Ontology: {ONTOLOGY_URL}")
    print(f"Admin tools: {'on' if ADMIN_TOOLS else 'off'} | stateless: {STATELESS} | "
          f"rate limits per minute: per-IP {RATE_LIMIT_PER_IP}, trusted per-IP {RATE_LIMIT_TRUSTED_PER_IP}, "
          f"global {RATE_LIMIT_GLOBAL} | trusted key: {'on' if TRUSTED_KEY else 'off'} | tool threads: {TOOL_THREADS}")
    print(f"MCP endpoint: http://{MCP_HOST}:{MCP_PORT}/mcp")

    if start_warmup():
        print(f"Warm-up: {len(WARMUP_QUERIES)} questions, first run in {WARMUP_DELAY_SECONDS}s, "
              f"then every {WARMUP_INTERVAL_SECONDS}s" if WARMUP_INTERVAL_SECONDS else
              f"Warm-up: {len(WARMUP_QUERIES)} questions at start only")

    app = mcp.streamable_http_app()
    if RATE_LIMIT_PER_IP or RATE_LIMIT_GLOBAL or RATE_LIMIT_TRUSTED_PER_IP:
        trusted = TrustedRanges(TRUSTED_RANGES, TRUSTED_RANGE_URLS, TRUSTED_REFRESH_SECONDS)
        trusted.start()  # reads the provider manifests in the background
        app = RateLimitMiddleware(
            app, RATE_LIMIT_PER_IP, RATE_LIMIT_GLOBAL,
            trusted=trusted, trusted_per_ip=RATE_LIMIT_TRUSTED_PER_IP,
            trusted_key=TRUSTED_KEY, key_header=TRUSTED_KEY_HEADER,
            trusted_agents=TRUSTED_USER_AGENTS or None,
        )
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")