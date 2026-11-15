from __future__ import annotations

import asyncio
import copy
import functools
import hashlib
import json
import os
import random
import re
import socket
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
from mcp.types import CallToolResult, ToolAnnotations  # noqa: E402

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
ONTOLOGY_MODEL_ENDPOINT = setting("MCP_ONTOLOGY_MODEL_ENDPOINT").rstrip("/")
ONTOLOGY_MODEL_API_KEY = setting("MCP_ONTOLOGY_MODEL_API_KEY")
ONTOLOGY_MODEL_NAME = setting("MCP_ONTOLOGY_MODEL_NAME", "gpt-6-luna")
ONTOLOGY_MODEL_TIMEOUT = max(5, int(setting("MCP_ONTOLOGY_MODEL_TIMEOUT_SECONDS", "25") or 25))
ONTOLOGY_REASONING_EFFORT = str(setting("MCP_ONTOLOGY_REASONING_EFFORT", "low") or "low").strip().lower()
if ONTOLOGY_REASONING_EFFORT not in {"low", "medium", "high", "xhigh", "max"}:
    ONTOLOGY_REASONING_EFFORT = "low"

TIMEOUT = int(setting("MCP_REQUEST_TIMEOUT", "30"))

# Retries of calls to the website search app. A search instance that is still loading its index refuses a request at
# once with HTTP 503 and a Retry-After header (LOCAL_INDEX_FAIL_FAST on that app), so the request can be tried again
# in a moment instead of waiting on a busy instance. Connection errors are retried too. Every attempt together stays
# within MCP_REQUEST_TIMEOUT, so a caller never waits longer than it did without retries.
RETRY_ATTEMPTS = max(1, int(setting("MCP_RETRY_ATTEMPTS", "4") or 4))                 # tries in all, the first included; 1 = no retry
RETRY_DEFAULT_WAIT = float(setting("MCP_RETRY_DEFAULT_WAIT_SECONDS", "3") or 3)       # wait when the 503 has no Retry-After
RETRY_MAX_WAIT = float(setting("MCP_RETRY_MAX_WAIT_SECONDS", "8") or 8)               # longest single wait, whatever Retry-After says
RETRY_MIN_ATTEMPT = float(setting("MCP_RETRY_MIN_ATTEMPT_SECONDS", "4") or 4)         # a retry is made only if this long is left after the wait

# Search diversity settings.
# SEARCH_RESULT_COUNT: ordinary results returned (best bets and spotlights come on top of these).
# SEARCH_CANDIDATE_POOL: how many website candidates to request before choosing, so less
#   dominant sources have a chance to appear.
# MAX_RESULTS_PER_DOMAIN: ceiling per source. Results are picked one per source first, then a
#   second per source, and so on, until SEARCH_RESULT_COUNT is reached.
RESULT_COUNT = int(setting("SEARCH_RESULT_COUNT", "10") or 10)
CANDIDATE_POOL = int(setting("SEARCH_CANDIDATE_POOL", "20") or 20)   # the Search App returns at most SEARCH_MAX_LIMIT (20)
MAX_PER_DOMAIN = int(setting("MAX_RESULTS_PER_DOMAIN", "2") or 2)
MAX_QUERIES = max(1, min(int(setting("SEARCH_MAX_QUERIES", "3") or 3), 5))   # searches one tool call may combine (query + more_queries)
WIKI_POOL = MAX_PER_DOMAIN + 2  # the wiki counts as one source, so it needs only a few candidates
# When the best wiki result is an overview that links to the pages that answer the question ("weekly agendas" -> Weekly Content,
# which links Week 1 to Week 6), the wiki app returns those pages with it (hub_pages / hub_of). They are kept together, outside the
# per-source cap and the result count, up to this many pages (the overview included): capping them at MAX_RESULTS_PER_DOMAIN left
# the agent with the overview alone, which says less about each week than the week's own page. 0 turns this off.
WIKI_HUB_MAX = max(0, int(setting("WIKI_HUB_MAX_RESULTS", "8") or 0))
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

# Search tools return excerpts. The chat agent reads full pages separately with get_documents only
# when it needs more. SEARCH_VERBOSE_ONTOLOGY adds per-concept match details and a second domain
# list to each response (about 600 tokens).
FULL_DOCUMENTS_DEFAULT = False
VERBOSE_ONTOLOGY = flag("SEARCH_VERBOSE_ONTOLOGY", "false")
# SEARCH_TIMINGS=true adds a timings_ms block to each combined search response. The timings are
# always written to the log, so you can read them in Application Insights without the flag.
TIMINGS = flag("SEARCH_TIMINGS", "false")
# Most pages one get_documents call will read (they are read in parallel).
# The agent is told to read this many pages per question, so the tool and the instructions agree.
MAX_BATCH_DOCUMENTS = max(1, int(setting("MAX_BATCH_DOCUMENTS", "2") or 2))
# The most text one page read returns (the search apps' own limit). A read with no max_chars
# gets DOCUMENT_DEFAULT_CHARS, enough for nearly every Foundation page in one read.
DOCUMENT_MAX_CHARS = 20000
DOCUMENT_DEFAULT_CHARS = max(500, min(int(setting("DOCUMENT_DEFAULT_CHARS", "12000") or 12000), DOCUMENT_MAX_CHARS))
# Pages read through get_documents are kept in memory so a repeat read does not
# call the search apps again. DOCUMENT_CACHE_SECONDS=0 turns this off. A page changed on the
# site can show its old text for up to this long (a wiki page for up to this long plus the
# wiki app's own 15 minutes). Each entry is one page, at most about 12 to 24 KB.
DOCUMENT_CACHE_SECONDS = max(0, int(setting("DOCUMENT_CACHE_SECONDS", "900") or 0))
DOCUMENT_CACHE_MAX = max(10, int(setting("DOCUMENT_CACHE_MAX_ENTRIES", "300") or 300))

# Finished search answers are kept in memory by query, so a repeat of a common question ("what is Career Circles")
# skips the search apps. SEARCH_CACHE_SECONDS=0 turns this off. Only complete answers are kept (never a partial one
# after a service error). Each instance keeps its own copy, and a changed page shows up within this time.
SEARCH_CACHE_SECONDS = max(0, int(setting("SEARCH_CACHE_SECONDS", "300") or 0))
SEARCH_CACHE_MAX = max(10, int(setting("SEARCH_CACHE_MAX_ENTRIES", "200") or 200))

# The wiki is the Foundation's own content and is searched for every question the ontology recognizes as being about the
# Foundation, whatever domains it approved: a concept with no approved domain ("week") used to leave the wiki out and the
# answer empty. The wiki search runs alongside the website search either way, so this costs no time. A question that matches
# no concept at all is still out of scope and returns nothing. WIKI_NEEDS_FOUNDATION_DOMAIN=true brings back the gate: the
# wiki is used only when the ontology approved sujeeth.org (it kept a partner FAQ out of a resume question that resolved to
# tealhq.com, at the cost of empty answers for concepts without a domain).
WIKI_NEEDS_FOUNDATION_DOMAIN = flag("WIKI_NEEDS_FOUNDATION_DOMAIN", "false")

# What the model reads. Every search result is read by the model on the next step, so extra bytes are extra seconds.
# SLIM_RESULTS drops the per-result fields the model does not use (score, concept list, chunk id, empty flags).
# REPLY_FORMAT is how the reply travels. "structured" (default) sends only the structuredContent copy, with no text block;
# it measured about half a second faster than "text", which sends one JSON text block. "both" is FastMCP's default, which sends the same JSON twice and the
# agent reads both. The MCP spec says a structured reply should also carry a text copy for older clients, so check
# the agent still answers properly before leaving "structured" on. It is separate from SLIM_RESULTS so each can be
# switched back alone.
SLIM_RESULTS = flag("SLIM_RESULTS", "true")
REPLY_FORMAT = str(setting("REPLY_FORMAT", "structured") or "structured").strip().lower()
if REPLY_FORMAT not in {"text", "structured", "both"}:
    REPLY_FORMAT = "structured"

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
        "Week 6 agenda; how Career Circles work; become a mentor volunteer; mentor FAQ; "
        "Career Circle six-week mentorship program; Career Circles six-week mentorship program",
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
_tool_state = {"in_flight": 0, "peak_in_flight": 0}   # tool calls running or waiting for a thread, and the most seen at once
_tool_lock = threading.Lock()


_PROCESS_STARTED = time.time()


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _rss_mb():
    """Resident memory of this process in MB (Linux only; None elsewhere)."""
    try:
        return round(int((_read("/proc/self/statm") or "").split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1048576, 1)
    except Exception:
        return None


def _cgroup():
    """The container's CPU limit (cores), memory limit (MB) and total time its CPU was throttled, from cgroup v2 or v1.
    A value the platform does not expose is None, so an unknown limit never reads as 'no limit'."""
    out = {"cpu_limit_cores": None, "mem_limit_mb": None, "cpu_throttled_seconds": None}
    try:
        quota = _read("/sys/fs/cgroup/cpu.max")                       # v2: "max 100000" or "200000 100000"
        if quota:
            part = quota.split()
            if len(part) == 2 and part[0] != "max":
                out["cpu_limit_cores"] = round(int(part[0]) / int(part[1]), 2)
        else:                                                         # v1
            q, p = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"), _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
            if q and p and int(q) > 0:
                out["cpu_limit_cores"] = round(int(q) / int(p), 2)
        mem = _read("/sys/fs/cgroup/memory.max") or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if mem and mem.isdigit() and int(mem) < (1 << 50):
            out["mem_limit_mb"] = round(int(mem) / 1048576)
        stat = _read("/sys/fs/cgroup/cpu.stat") or _read("/sys/fs/cgroup/cpu/cpu.stat")
        if stat:
            vals = dict(line.split()[:2] for line in stat.splitlines() if len(line.split()) >= 2)
            if "throttled_usec" in vals:
                out["cpu_throttled_seconds"] = round(int(vals["throttled_usec"]) / 1e6, 2)
            elif "throttled_time" in vals:
                out["cpu_throttled_seconds"] = round(int(vals["throttled_time"]) / 1e9, 2)
    except Exception:
        pass
    return out


def process_info():
    """Who is answering, how long it has run, and how much memory and CPU it uses, so a restart shows as a new pid or a
    drop in uptime and a busy instance shows as CPU time climbing faster than the clock."""
    return {
        "pid": os.getpid(),
        "instance": os.getenv("WEBSITE_INSTANCE_ID") or socket.gethostname(),
        "started_at": datetime.fromtimestamp(_PROCESS_STARTED, timezone.utc).isoformat(),
        "uptime_seconds": int(time.time() - _PROCESS_STARTED),
        "rss_mb": _rss_mb(),
        "cpu_seconds": round(time.process_time(), 2),   # CPU time used by every thread of this process since it started
        "cpu_count": os.cpu_count(),
        **_cgroup(),
    }


def in_thread(fn):
    """Run a blocking tool function on the worker pool.

    FastMCP calls a plain `def` tool directly on the event loop, so a search that waits on the search apps
    stops the whole process from accepting or answering anything else (including /healthz) until it ends.
    The wrapper is async, so FastMCP awaits it, and keeps the name, docstring and signature FastMCP reads.
    Code that is not running on the event loop and wants the result directly (the warm-up thread) must call
    the plain function as `tool.sync(...)`. Calling the wrapper itself returns a coroutine that never runs.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        with _tool_lock:
            _tool_state["in_flight"] += 1
            _tool_state["peak_in_flight"] = max(_tool_state["peak_in_flight"], _tool_state["in_flight"])
        try:
            result = await loop.run_in_executor(_tool_pool, functools.partial(fn, *args, **kwargs))
            if REPLY_FORMAT == "structured" and isinstance(result, dict):
                return CallToolResult(content=[], structuredContent=result)  # no text block, only the structured copy
            return result
        finally:
            with _tool_lock:
                _tool_state["in_flight"] -= 1

    wrapper.sync = fn
    return wrapper


def auth(key):
    return {"x-functions-key": key} if key else {}


def _get_json_raw(url, key="", params=None):
    response = http.get(
        url,
        headers=auth(key),
        params=params,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {"data": data}


def _post_json_raw(url, payload, key="", timeout=None):
    response = http.post(
        url,
        headers={
            **auth(key),
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=TIMEOUT if timeout is None else timeout,
    )
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {"data": data}


_downstream = {}   # service -> {"calls", "errors", "recent": [(finished at, seconds)]}
_downstream_lock = threading.Lock()


def _service_of(url):
    for name, base in (("websearch", WEBSEARCH_URL), ("wikisearch", WIKISEARCH_URL), ("ontology", ONTOLOGY_URL)):
        if base and str(url).startswith(base):
            return name
    return "other"


def _note_call(url, seconds, failed):
    with _downstream_lock:
        entry = _downstream.setdefault(_service_of(url), {"calls": 0, "errors": 0, "retries": 0, "recent": deque(maxlen=3000)})
        entry["calls"] += 1
        entry["errors"] += 1 if failed else 0
        entry["recent"].append((time.time(), seconds))


def _note_retry(url):
    with _downstream_lock:
        entry = _downstream.setdefault(_service_of(url), {"calls": 0, "errors": 0, "retries": 0, "recent": deque(maxlen=3000)})
        entry["retries"] += 1


def downstream_stats():
    """Per backend app: calls, errors, and over the last minute the call count, median and p95 time, and the seconds this app
    spent waiting on it (divide by 60 for the average number of calls in progress)."""
    now, out = time.time(), {}
    with _downstream_lock:
        snapshot = {k: (v["calls"], v["errors"], v.get("retries", 0), list(v["recent"])) for k, v in _downstream.items()}
    for name, (calls, errors, retries, recent) in snapshot.items():
        last = sorted(d for t, d in recent if now - t <= 60)
        pick = lambda q: round(1000 * last[min(len(last) - 1, int(q * len(last)))]) if last else None
        out[name] = {"calls": calls, "errors": errors, "retries": retries, "last_minute_calls": len(last), "p50_ms": pick(0.5),
                     "p95_ms": pick(0.95), "waited_seconds_last_minute": round(sum(last), 1)}
    return out


def get_json(url, key="", params=None):
    began, failed = time.perf_counter(), True
    try:
        data = _get_json_raw(url, key, params)
        failed = False
        return data
    finally:
        _note_call(url, time.perf_counter() - began, failed)


def post_json(url, payload, key="", timeout=None):
    began, failed = time.perf_counter(), True
    try:
        data = _post_json_raw(url, payload, key, timeout)
        failed = False
        return data
    finally:
        _note_call(url, time.perf_counter() - began, failed)


# -- retries ------------------------------------------------------------------------------------------------------
_clock = time.monotonic   # the two clocks the retry uses; tests replace them
_sleep = time.sleep


def _jitter():
    """A little random wait on top of each retry, so callers refused together do not all come back together."""
    return random.uniform(0, 0.5)


def _retry_after_seconds(response):
    """The seconds a 503 asks us to wait (Retry-After as a number), or the default when it is missing or unusable."""
    raw = response.headers.get("Retry-After") if response is not None else None
    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        return RETRY_DEFAULT_WAIT
    return seconds if seconds >= 0 else RETRY_DEFAULT_WAIT


def _retry_wait(exc):
    """Seconds to wait before trying again, or None when this failure is not worth another try.

    Retried: HTTP 503 (the search app is still loading its index) and connection errors or timeouts. Not retried: any
    other HTTP status, which a second try would not change (a 502 is a real failure in the search itself)."""
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        if response is not None and response.status_code == 503:
            return min(_retry_after_seconds(response), RETRY_MAX_WAIT)
        return None
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return min(RETRY_DEFAULT_WAIT, RETRY_MAX_WAIT)
    return None


def _why(exc):
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


def post_json_with_retry(url, payload, key="", attempts=None):
    """post_json, tried again when the search app is still loading or briefly unreachable.

    All tries together take at most MCP_REQUEST_TIMEOUT seconds: each try is given only the time that is left, and a
    retry is made only if at least MCP_RETRY_MIN_ATTEMPT_SECONDS would remain after the wait. The last failure is
    raised as before, so callers handle it exactly as they did without retries. Every try is counted in the downstream
    numbers, and each retry also in "retries"."""
    attempts = RETRY_ATTEMPTS if attempts is None else max(1, int(attempts))
    deadline = _clock() + TIMEOUT
    attempt = 0
    while True:
        attempt += 1
        try:
            return post_json(url, payload, key, timeout=max(1.0, min(float(TIMEOUT), deadline - _clock())))
        except Exception as exc:
            wait = _retry_wait(exc) if attempt < attempts else None
            if wait is None:
                raise
            wait += _jitter()
            if deadline - _clock() - wait < RETRY_MIN_ATTEMPT:
                raise
            _note_retry(url)
            print(f"retry service={_service_of(url)} attempt={attempt} reason={_why(exc)} wait_s={wait:.1f}",
                  file=sys.stderr, flush=True)
            _sleep(wait)


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
            # Wiki Search marks an overview page with the addresses of the pages it links to that match (hub_pages).
            "_hub_pages": item.get("hub_pages") if isinstance(item.get("hub_pages"), list) else None,
        })
    return results


def slim_results(results):
    """Drop the per-result fields the model does not use. Run after everything that needs them (best-bet ordering,
    full-document reads)."""
    if not SLIM_RESULTS:
        return results
    for item in results:
        for key in ("score", "concepts", "chunk_id"):
            item.pop(key, None)
        for key in ("best_bet", "spotlight", "domain"):
            if not item.get(key):
                item.pop(key, None)
    return results


def strip_internal(results):
    """Remove keys that only the server uses from results before they are returned."""
    for item in results:
        item.pop("_full_text", None)
        item.pop("_truncated", None)
        item.pop("_hub_pages", None)
    return results


def drop_excluded_wiki(items):
    return [
        x for x in items
        if not WIKI_EXCLUDE.search(str(x.get("title") or ""))
        and not WIKI_EXCLUDE.search(str(x.get("url") or "").replace("-", " ").replace("%3A", " "))
    ]


def page_identity(item):
    """One key for a page however it was reached: no scheme, no leading www., no fragment, no trailing slash, lower case.
    https://www.sujeeth.org/circle/ and http://sujeeth.org/circle#top are the same page. Falls back to the title."""
    url = str(item.get("url") or "").strip().split("#", 1)[0]
    url = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", url)
    host, slash, rest = url.partition("/")
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return (host + slash + rest).rstrip("/").lower() or str(item.get("title") or "").strip().lower()


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


def wiki_hub_pages(wiki_items):
    """(kept, linked): the best-ranked wiki overview and the pages it links to that the wiki search returned with it, in the
    overview's link order, at most WIKI_HUB_MAX of them; and every page the overview links to, kept or not, so a page cut by
    the maximum does not come back through the per-source selection. Both are empty when no wiki result is an overview."""
    if not WIKI_HUB_MAX:
        return [], []
    hub = next((x for x in wiki_items if x.get("_hub_pages")), None)
    if hub is None:
        return [], []
    by_url = {x.get("url"): x for x in wiki_items}
    members = [hub] + [by_url[u] for u in hub["_hub_pages"] if u in by_url and by_url[u] is not hub]
    return members[:WIKI_HUB_MAX], members


def combine_with_best_bets(wiki_items, website_items, limit):
    """Best bets first (hand-added pages the website search flagged), then spotlights, then a wiki overview
    with the pages it links to (see wiki_hub_pages), then up to `limit` wiki and website results chosen
    across sources (see select_diverse). Best bets, spotlights and the overview's pages sit outside the
    limit and the per-source cap. Each URL appears once."""
    best, spotlights, seen = [], [], set()
    for item in website_items:
        identity = page_identity(item) if item.get("url") else ""
        if item.get("best_bet") and identity and identity not in seen:
            seen.add(identity)
            best.append(item)
    # Spotlight pages the website search reserved for career questions go right after best
    # bets, so wiki and Teal results cannot crowd them out of the limit.
    for item in website_items:
        identity = page_identity(item) if item.get("url") else ""
        if item.get("spotlight") and not item.get("best_bet") and identity and identity not in seen:
            seen.add(identity)
            spotlights.append(item)
    hub_pages = []
    kept, linked = wiki_hub_pages(wiki_items)
    for item in kept:
        identity = page_identity(item)
        if identity and identity not in seen:
            seen.add(identity)
            hub_pages.append(item)
    seen.update(page_identity(item) for item in linked if item.get("url"))
    normal = []
    prioritized = lambda x: x.get("best_bet") or x.get("spotlight")
    for item in merge_sources(wiki_items, [x for x in website_items if not prioritized(x)]):
        identity = page_identity(item)
        if identity and identity not in seen:
            seen.add(identity)
            normal.append(item)
    return best + spotlights + hub_pages + select_diverse(normal, limit, MAX_PER_DOMAIN), len(best), len(spotlights)


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


# Concepts the ontology links so that one is part of the other's answer (for example Resume Resource illustrates
# Week 3, or networking supports referral) are one answer, not two competing readings. Each predicate in the public
# ontology file says what it does to a search: "expand" and "boost" predicates link concepts that answer together;
# "suppress" predicates (is_distinct_from, precedes) keep concepts apart, so a tie between them is still asked about.
# The pairs are loaded from the ontology file and refreshed every LINKED_PAIRS_REFRESH_HOURS (default 24), so a running
# instance follows an ontology change without a restart. If the file cannot be loaded the set stays as it was (empty on
# the first try) and the close-match question is asked as before.
MCP_ONTOLOGY_JSON_URL = setting(
    "MCP_ONTOLOGY_URL", "https://raw.githubusercontent.com/SujeethFoundation/ontology/main/ontology/ontology.json"
)
LINKED_PAIRS_REFRESH_SECONDS = max(60.0, float(setting("LINKED_PAIRS_REFRESH_HOURS", "24") or 24) * 3600)
LINKING_EFFECTS = {"expand", "boost"}
_linked_pairs: set | None = None
_linked_pairs_loaded_at: float | None = None


def pairs_from_ontology(data: dict) -> set:
    """Concept pairs linked by a predicate whose search_behavior effect is expand or boost. A relationship can carry its
    own search_behavior to override its predicate, or {"enabled": false} to opt out. A predicate with no search_behavior
    (an older ontology file) counts only if it is "illustrates", which is what this check used before predicates had
    effects."""
    predicates = data.get("predicates") or []
    if isinstance(predicates, dict):
        predicates = [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in predicates.items()]
    behaviors = {str(p["id"]).lower(): p.get("search_behavior") for p in predicates if isinstance(p, dict) and p.get("id")}
    pairs = set()
    for rel in data.get("relationships", []):
        name = str(rel.get("predicate", "")).lower()
        spec = rel["search_behavior"] if "search_behavior" in rel else behaviors.get(name)
        if isinstance(spec, dict):
            linked = spec.get("enabled") is not False and str(spec.get("effect") or "").lower() in LINKING_EFFECTS
            if spec.get("enabled") is not False and not spec.get("effect"):
                linked = name == "illustrates"
        else:
            linked = name == "illustrates" and spec is None and "search_behavior" not in rel
        if linked and rel.get("subject") and rel.get("object"):
            pairs.add(frozenset((rel["subject"], rel["object"])))
    return pairs


def linked_concept_pairs():
    global _linked_pairs, _linked_pairs_loaded_at
    now = time.monotonic()
    stale = _linked_pairs_loaded_at is not None and now - _linked_pairs_loaded_at > LINKED_PAIRS_REFRESH_SECONDS
    if _linked_pairs is None or stale:
        try:
            data = requests.get(MCP_ONTOLOGY_JSON_URL, timeout=min(5, TIMEOUT)).json()
            pairs = pairs_from_ontology(data)
        except Exception:
            # Do not cache a failure: keep the last good set (empty on the first try) and try again next question.
            return _linked_pairs or set()
        _linked_pairs, _linked_pairs_loaded_at = pairs, now
    return _linked_pairs


# What the customer is told when nothing in the ontology matches the question.
OUT_OF_SCOPE_MESSAGE = (
    "That's outside what I can help with. I'm here to help you get ready for your first job: career guidance, "
    "interview prep, resume feedback, and how to build a network of professionals. If you want to mentor, give, "
    "or bring Career Circles to your school, ask me about that too."
)

OUT_OF_SCOPE_INSTRUCTIONS = (
    "This question is not about the Foundation: no ontology concept matched it, so nothing was searched. "
    f'Reply with this message, in these words: "{OUT_OF_SCOPE_MESSAGE}" Add nothing to it. Do not '
    "answer the question from general knowledge, do not search again for it, and do not ask which area they mean."
)

_ontology_catalog: dict | None = None
_ontology_catalog_loaded_at: float | None = None
ONTOLOGY_CATALOG_REFRESH_SECONDS = 300
ONTOLOGY_PLAN_CACHE_SECONDS = max(0, int(setting("MCP_ONTOLOGY_PLAN_CACHE_SECONDS", "300") or 0))
ONTOLOGY_PLAN_CACHE_MAX = max(10, int(setting("MCP_ONTOLOGY_PLAN_CACHE_MAX_ENTRIES", "500") or 500))
_ontology_plan_cache = {}  # (query, ontology version, model) -> (expires, validated plan)
_ontology_plan_cache_lock = threading.Lock()
MAX_RESOLVED_QUERIES = max(1, min(int(setting("SEARCH_MAX_QUERIES", "3") or 3), 5))
ONTOLOGY_RESOLUTION_PROMPT = f"""Route the user's question for the Sujeeth Foundation using the complete supplied ontology. Match against concept names, definitions, aliases, query phrases, and relevant relationships; do not infer Foundation topics from general knowledge. Relationship edges are subject-predicate-object triples; `predicate_search_behavior` gives the default retrieval rule, with any edge-level `search_behavior` overriding it.
Return direct concept IDs for the user's topic. Add related IDs only when an ontology relationship supports retrieval. For `illustrates`, a matched object may retrieve its subject as an example; never reverse that direction. Follow other relationships only as their declared search behavior allows. Return only IDs in the ontology.
Preserve the user's intent. Return at most {MAX_RESOLVED_QUERIES} short search queries covering the ask. Ask one clarification only when two materially different in-scope interpretations remain. An empty direct matched_concepts list means out of scope. A lack of search results does not mean out of scope."""

ONTOLOGY_PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "matched_concepts": {"type": "array", "items": {"type": "string"}},
        "related_concepts": {"type": "array", "items": {"type": "string"}},
        "queries": {"type": "array", "items": {"type": "string"}},
        "needs_clarification": {"type": "boolean"},
        "suggested_question": {"type": ["string", "null"]},
    },
    "required": ["matched_concepts", "related_concepts", "queries", "needs_clarification", "suggested_question"],
}


def ontology_catalog() -> dict | None:
    """Load the public ontology catalog used by the AI resolver and domain authorization."""
    global _ontology_catalog, _ontology_catalog_loaded_at
    now = time.monotonic()
    if _ontology_catalog is not None and _ontology_catalog_loaded_at is not None and now - _ontology_catalog_loaded_at < ONTOLOGY_CATALOG_REFRESH_SECONDS:
        return _ontology_catalog
    try:
        response = requests.get(MCP_ONTOLOGY_JSON_URL, timeout=min(5, TIMEOUT))
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("classes"), list):
            _ontology_catalog, _ontology_catalog_loaded_at = data, now
    except Exception:
        pass  # A failed catalog read cannot turn an unknown question into an in-scope one.
    return _ontology_catalog


def compact_ontology_catalog(catalog):
    """Keep the full searchable vocabulary and relevant edges while avoiding unrelated ontology metadata."""
    classes = []
    for item in catalog.get("classes", []):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        keep = {key: item[key] for key in ("id", "name", "category", "definition", "aliases", "synonyms",
                                            "alternative_terms", "alternate_names", "query_phrases") if key in item}
        seen_terms = set()
        for key in ("aliases", "synonyms", "alternative_terms", "alternate_names", "query_phrases"):
            terms = keep.get(key)
            if not isinstance(terms, list):
                continue
            unique = []
            for term in terms:
                normalized = " ".join(term.casefold().split()) if isinstance(term, str) else None
                if normalized is None or normalized not in seen_terms:
                    unique.append(term)
                    if normalized is not None:
                        seen_terms.add(normalized)
            keep[key] = unique
        classes.append(keep)
    predicates = catalog.get("predicates", [])
    if isinstance(predicates, dict):
        predicates = [{"id": key, **(value if isinstance(value, dict) else {})} for key, value in predicates.items()]
    predicate_map = {str(p.get("id", "")).lower(): p for p in predicates if isinstance(p, dict)}
    edges = []
    predicate_search_behavior = {}
    for rel in catalog.get("relationships", []):
        if not isinstance(rel, dict):
            continue
        pred = str(rel.get("predicate", "")).lower()
        p = predicate_map.get(pred, {})
        behavior = rel.get("search_behavior", p.get("search_behavior", {}))
        if behavior or p.get("expands_search"):
            predicate_rule = p.get("search_behavior") or ({"effect": "expand"} if p.get("expands_search") else None)
            if predicate_rule is not None:
                predicate_search_behavior[pred] = predicate_rule
            edge = {"subject": rel.get("subject"), "predicate": rel.get("predicate"),
                    "object": rel.get("object")}
            relationship_rule = rel.get("search_behavior")
            if relationship_rule and relationship_rule != predicate_rule:
                edge["search_behavior"] = relationship_rule
            edges.append(edge)
    domains = [{"domain": row.get("domain"), "associated_concepts": row.get("associated_concepts") or []}
               for row in catalog.get("domains", []) if isinstance(row, dict) and row.get("search_enabled") and row.get("domain")]
    return {"ontology_version": catalog.get("ontology_version"), "classes": classes,
            "relationships": edges, "predicate_search_behavior": predicate_search_behavior,
            "search_domains": domains}


def _responses_output_text(payload):
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    for item in payload.get("output", []):
        for part in item.get("content", []) if isinstance(item, dict) else []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                return part["text"]
    raise ValueError("Azure model response contained no text output")


def ontology_model_responses_url(endpoint):
    """Accept a resource URL, a `/openai/v1` base, or the complete Responses endpoint URL."""
    endpoint = str(endpoint or "").strip().rstrip("/")
    if endpoint.endswith("/openai/v1/responses"):
        return endpoint
    if endpoint.endswith("/openai/v1"):
        return endpoint + "/responses"
    return endpoint + "/openai/v1/responses"

def clear_ontology_plan_cache():
    """Clear cached validated ontology plans, primarily for tests and local diagnostics."""
    with _ontology_plan_cache_lock:
        _ontology_plan_cache.clear()


def _ontology_plan_cache_key(query, catalog):
    return (" ".join((query or "").casefold().split()), catalog.get("ontology_version"), ONTOLOGY_MODEL_NAME)


def _ontology_plan_cache_get(key):
    if ONTOLOGY_PLAN_CACHE_SECONDS <= 0:
        return None
    now = time.monotonic()
    with _ontology_plan_cache_lock:
        entry = _ontology_plan_cache.get(key)
        if entry and entry[0] > now:
            return copy.deepcopy(entry[1])
        if entry:
            _ontology_plan_cache.pop(key, None)
    return None


def _ontology_plan_cache_put(key, plan):
    if ONTOLOGY_PLAN_CACHE_SECONDS <= 0:
        return
    with _ontology_plan_cache_lock:
        _ontology_plan_cache[key] = (time.monotonic() + ONTOLOGY_PLAN_CACHE_SECONDS, copy.deepcopy(plan))
        if len(_ontology_plan_cache) > ONTOLOGY_PLAN_CACHE_MAX:
            for old in list(_ontology_plan_cache)[:max(1, ONTOLOGY_PLAN_CACHE_MAX // 10)]:
                _ontology_plan_cache.pop(old, None)


def call_ontology_model(query, catalog):
    """Call the configured Azure GPT-6 Luna deployment with the searchable ontology catalog."""
    if not ONTOLOGY_MODEL_ENDPOINT or not ONTOLOGY_MODEL_API_KEY:
        raise RuntimeError("Azure ontology model is not configured")
    ontology = compact_ontology_catalog(catalog)
    cache_key_source = f"{ONTOLOGY_MODEL_NAME}:{catalog.get('ontology_version', '')}:ontology-plan-v1"
    prompt_cache_key = hashlib.sha256(cache_key_source.encode("utf-8")).hexdigest()
    model_started = time.perf_counter()
    response = http.post(
        ontology_model_responses_url(ONTOLOGY_MODEL_ENDPOINT),
        headers={"api-key": ONTOLOGY_MODEL_API_KEY, "Content-Type": "application/json"},
        json={"model": ONTOLOGY_MODEL_NAME, "instructions": ONTOLOGY_RESOLUTION_PROMPT,
              "input": json.dumps({"ontology": ontology, "question": query}, ensure_ascii=False,
                                  separators=(",", ":")),
              "prompt_cache_key": prompt_cache_key,
              "reasoning": {"effort": ONTOLOGY_REASONING_EFFORT},
              "max_output_tokens": 1200, "store": False,
              "text": {"format": {"type": "json_schema", "name": "ontology_search_plan", "strict": True,
                                   "schema": ONTOLOGY_PLAN_SCHEMA}}},
        timeout=ONTOLOGY_MODEL_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()
    usage = payload.get("usage") if isinstance(payload, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    print(f"ontology_model_timing_ms {json.dumps({'duration_ms': round((time.perf_counter() - model_started) * 1000), 'input_tokens': usage.get('input_tokens', usage.get('prompt_tokens')), 'output_tokens': usage.get('output_tokens', usage.get('completion_tokens')), 'cached_input_tokens': input_details.get('cached_tokens') if isinstance(input_details, dict) else None})}",
          file=sys.stderr, flush=True)
    return json.loads(_responses_output_text(payload))


def validate_ontology_plan(result, query, catalog):
    """Validate model-selected concepts, relationship expansions, domains, and query variants."""
    valid_ids = {str(row["id"]) for row in catalog["classes"] if isinstance(row, dict) and row.get("id")}
    matched = list(dict.fromkeys(x for x in result.get("matched_concepts", []) if isinstance(x, str) and x in valid_ids))
    related = list(dict.fromkeys(x for x in result.get("related_concepts", []) if isinstance(x, str) and x in valid_ids))
    # A model may identify a concept as related only when an enabled ontology edge supports retrieval.
    predicates = catalog.get("predicates", [])
    if isinstance(predicates, dict):
        predicates = [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in predicates.items()]
    behavior_map = {str(p.get("id", "")).lower(): p.get("search_behavior", {}) for p in predicates if isinstance(p, dict)}
    allowed_related = set()
    for rel in catalog.get("relationships", []):
        if not isinstance(rel, dict):
            continue
        predicate = str(rel.get("predicate", "")).lower()
        spec = rel.get("search_behavior", behavior_map.get(predicate, {}))
        if not isinstance(spec, dict) or spec.get("enabled") is False:
            continue
        effect = str(spec.get("effect") or ("expand" if predicate == "illustrates" else "")).lower()
        if effect not in LINKING_EFFECTS:
            continue
        subject, obj = rel.get("subject"), rel.get("object")
        if predicate == "illustrates":
            if obj in matched and subject:
                allowed_related.add(str(subject))
        elif subject in matched and obj:
            allowed_related.add(str(obj))
        elif obj in matched and subject:
            allowed_related.add(str(subject))
    related = [x for x in related if x in allowed_related and x not in matched]
    concepts = matched + related
    domains = list(dict.fromkeys(
        row["domain"] for row in catalog.get("domains", [])
        if isinstance(row, dict) and row.get("search_enabled") and row.get("domain")
        and set(concepts).intersection(row.get("associated_concepts") or [])
    ))
    queries = []
    for candidate in result.get("queries", []):
        if not isinstance(candidate, str):
            continue
        candidate = " ".join(candidate.split())
        if candidate and len(candidate) <= 300 and candidate.casefold() not in {q.casefold() for q in queries}:
            queries.append(candidate)
        if len(queries) >= MAX_RESOLVED_QUERIES:
            break
    if matched and query.strip() and query.casefold() not in {q.casefold() for q in queries}:
        queries.insert(0, query.strip())
        queries = queries[:MAX_RESOLVED_QUERIES]
    suggested = result.get("suggested_question")
    suggested = " ".join(suggested.split()) if isinstance(suggested, str) else None
    needs_clarification = bool(result.get("needs_clarification") and matched and suggested and len(suggested) <= 300)
    return {"resolution": {"status": "resolved" if matched else "no_match",
                           "ontology_version": catalog.get("ontology_version"),
                           "matched_concepts": concepts, "domains": domains,
                           "needs_clarification": needs_clarification,
                           "suggested_question": suggested if needs_clarification else None,
                           "concept_matches": ([{"concept_id": x, "source": "azure_model"} for x in matched] +
                                               [{"concept_id": x, "source": "ontology_relationship"} for x in related])},
            "queries": queries, "error": None}


def resolve_ontology_plan(query, catalog=None):
    """Ask Azure GPT-6 Luna to resolve the query against ontology concepts and suggest retrieval queries."""
    catalog = catalog if catalog is not None else ontology_catalog()
    if not isinstance(catalog, dict) or not isinstance(catalog.get("classes"), list):
        raise RuntimeError("Ontology catalog is unavailable")
    key = _ontology_plan_cache_key(query, catalog)
    cached = _ontology_plan_cache_get(key)
    if cached is not None:
        normalized = key[0]
        extras = [item for item in cached.get("queries", [])
                  if " ".join(str(item).casefold().split()) != normalized]
        cached["queries"] = _clean_queries(query, extras)
        return cached
    plan = validate_ontology_plan(call_ontology_model(query, catalog), query, catalog)
    _ontology_plan_cache_put(key, plan)
    return plan

NO_RESULTS_INSTRUCTIONS = (
    " The Foundation's pages returned nothing that answers this. Say so plainly and point the person to sujeeth.org. "
    "Do not answer from general knowledge and do not ask a clarifying question."
)


def assess_quality(resolution, results, errors):
    """Decide whether the question is out of scope, ambiguous, or fine.

    Out of scope: no ontology concept matched, so the question is not about the Foundation. The answer is a refusal
    with no results. Ambiguous: two weaker concept matches are nearly tied and the ontology does not link them, so
    they could conflict. Only then is the person asked a question. Nothing else is a reason to ask: a question
    that matched a concept but found little is answered with what there is, or with a plain "nothing found".
    """
    concepts = resolution.get("matched_concepts") or []
    matches = resolution.get("concept_matches") or []
    reasons = []

    # When the Search App could not be reached nothing is known about the topic, so it is not a refusal either.
    out_of_scope = resolution.get("status") != "unavailable" and not concepts
    if out_of_scope:
        reasons.append("No ontology concept matched: the question is not about the Foundation.")
    elif resolution.get("status") != "unavailable" and not resolution.get("domains"):
        reasons.append("No approved search domain was resolved.")
    if not out_of_scope:
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
        pair_ids = frozenset((weak[0][1].get("concept_id"), weak[1][1].get("concept_id")))
        if pair_ids not in linked_concept_pairs():
            reasons.append("Leading ontology matches are very close.")
            close_pair = (weak[0][1], weak[1][1])

    # A service failure is not a reason to quiz the user about their topic.
    service_degraded = bool(errors)
    all_failed = service_degraded and not results
    needs_clarification = (bool(close_pair) or bool(resolution.get("needs_clarification"))) and not all_failed and not out_of_scope
    question = resolution.get("suggested_question") if resolution.get("needs_clarification") else None

    if needs_clarification and not question:
        first = close_pair[0].get("name", "the first topic")
        second = close_pair[1].get("name", "the second topic")
        question = f"Are you asking about {first} or {second}?"

    if out_of_scope:
        return {
            "out_of_scope": True,
            "message": OUT_OF_SCOPE_MESSAGE,
            "needs_clarification": False,
            "service_degraded": service_degraded,
            "reasons": reasons,
            "suggested_question": None,
            "agent_instructions": OUT_OF_SCOPE_INSTRUCTIONS,
        }

    instructions = (
        "If needs_clarification is true, ask the suggested question before "
        "making a definitive claim. Use ontology context to explain "
        "available concepts. Never guess a domain or search outside "
        "ontology-approved domains. Otherwise answer from the results, reading "
        "pages as described below, and cite their URLs. " + STATS_RULE
    )
    if not results and not service_degraded:
        instructions += NO_RESULTS_INSTRUCTIONS
    instructions += (
        " When the matched concepts include both a skill and the program week that illustrates it "
        "(for example Resume Resource and Week 3), do not ask which one the user meant. Lead with the "
        "guidance, then use the week's agenda as the worked example."
    )
    if service_degraded:
        instructions += (
            " One or more search services failed, so results may be incomplete. "
            "Tell the user this and suggest trying again; do not treat missing "
            "results as proof that nothing exists."
        )

    return {
        "out_of_scope": False,
        "needs_clarification": needs_clarification,
        "service_degraded": service_degraded,
        "reasons": reasons,
        "suggested_question": question,
        "agent_instructions": instructions,
    }


# The rule about program statistics, in one place so every response says it the same way.
STATS_RULE = (
    "Quote program statistics (counts, rates, placement or engagement figures) only from a result "
    "whose domain is sujeeth.org (a subdomain such as topics.sujeeth.org counts), and cite its url. "
    "Never quote them from the wiki, third-party pages or your own totals; for those, point the "
    "user to sujeeth.org for current numbers."
)

DOCUMENT_INSTRUCTIONS = "Cite the url. " + STATS_RULE + (
    " If excerpt_only is true, summarize briefly and link to the source."
)

# What to do when a page is cut off. Both the search responses and get_documents say the same thing.
TRUNCATED_INSTRUCTIONS = (
    f"If a document is truncated and the part you need is missing, call get_documents again with "
    f"that url and max_chars {DOCUMENT_MAX_CHARS}. If it is still truncated, say the page is longer "
    f"than what you read and link to it."
)

FULL_DOCUMENT_INSTRUCTIONS = (
    " Results with a document field carry the page text for that result. Answer from the "
    "document, not the snippet, and cite the url. If a document has excerpt_only set to true, "
    "it is third-party content: summarize briefly and link to the source, do not reproduce it. "
    + TRUNCATED_INSTRUCTIONS
)

# Used when a search returns excerpts only. This sits beside the results, which is where the
# agent decides whether to answer or to read a page.
READ_PAGES_INSTRUCTIONS = (
    f"call get_documents once, with the urls of every page you need, before answering, starting "
    f"with a best_bet or spotlight. Read at most {MAX_BATCH_DOCUMENTS} pages per question, and read "
    f"them in one call. Results whose domain is not sujeeth.org are third-party: summarize briefly "
    f"and link instead of reading them. Do not answer program details from an excerpt that does not "
    f"contain them."
)

# Each get_documents call is another model round trip, which is the slowest part of a turn. So the agent answers
# from what it has whenever it can, and reads a page only when the answer needs text the results lack.
FEWER_READS_INSTRUCTIONS = (
    " Answer from the results you have when they cover the question: overviews, what something is, who it is for, "
    "and where to go next are all fine from excerpts, with the link. Read a page only when the answer needs exact "
    "text the results lack (steps, an agenda, a list, dates, wording) or an excerpt is cut off where the answer "
    "should be. Never read a page just to confirm what an excerpt already says."
)

EXCERPT_INSTRUCTIONS = FEWER_READS_INSTRUCTIONS + (
    " Results are excerpts and may stop mid-sentence. When you do need more, "
    + READ_PAGES_INSTRUCTIONS
)

# Used when the wiki results carry their whole page and the other results are excerpts.
WIKI_DOCUMENT_INSTRUCTIONS = (
    " Wiki results are excerpts. Answer from them when they cover the question; call get_documents "
    "only when the excerpt does not contain what you need."
    + FEWER_READS_INSTRUCTIONS + " When you do need more, " + READ_PAGES_INSTRUCTIONS
)

GET_DOCUMENTS_INSTRUCTIONS = DOCUMENT_INSTRUCTIONS + " " + TRUNCATED_INSTRUCTIONS

PRIORITY_INSTRUCTIONS = (
    " Results flagged best_bet or spotlight are curated pages. Present them first, "
    "in the order returned. When one fits the question, include it with its link "
    "and one sentence on why it fits."
)

# A spotlight is a story about one volunteer. It is the Foundation's own proof of what mentoring leads to, so the
# agent offers it as an example rather than burying it among the other links.
SPOTLIGHT_INSTRUCTIONS = (
    " A spotlight is the story of one volunteer's career path. When one fits the question, offer it as a "
    "real example: say in one sentence what that person did, and give the link."
)


CITE_AND_STATS = "Cite the url. " + STATS_RULE

# What the agent reads when it lists the tools. They are written for the agent: what each tool is
# for, when to use it, and what to do with what comes back. Implementation detail stays in code comments.
SEARCH_DESCRIPTION = (
    "Search the Sujeeth Foundation website and GitHub wiki together. Use this first for the visitor's "
    "question. MCP uses Azure GPT-6 Luna to resolve the wording against the full ontology, follow "
    "enabled concept relationships, and generate related search queries. It validates the selected "
    "concept IDs and approved website domains, then sends the primary query and its related variants together "
    "in one request to each search service. "
    "Do not classify a question as out of scope yourself; rely on this tool's out_of_scope result. "
    + "Each result has a title, url, source (website or wiki) and matched "
    "concepts; results flagged best_bet or spotlight are curated pages. "
    + "Every result is an excerpt that can stop mid-sentence; call get_documents separately only when needed. The response "
    "includes agent_instructions and, only when two readings of the question could conflict, a "
    "suggested_question to ask. If the response has out_of_scope true, the question is not about the Foundation: "
    "relay its message and do not answer from other sources. "
    + (f"You may pass same-topic wording variants in more_queries (up to {MAX_QUERIES} queries in one search request). " if MAX_QUERIES > 1 else "")
    + "For separate topics, call this tool separately. Do not request page text here; use get_documents only when the excerpts are insufficient."
)

WEBSITE_SEARCH_DESCRIPTION = (
    "Search only ontology-approved Foundation websites (not the wiki). MCP resolves the query, "
    "generates related queries, and supplies validated concepts and domains. Prefer "
    "search_sujeeth_foundation unless website-only results are needed. Results are excerpts; "
    "get_documents reads full pages."
)

WIKI_SEARCH_DESCRIPTION = (
    "Search only the Foundation's GitHub wiki: mentor, student, donor and partner FAQs, the weekly "
    "agendas for weeks 1 to 6, and essays on the program's ideas. Prefer search_sujeeth_foundation "
    "unless you want wiki pages alone. "
    + "Results are excerpts; get_documents reads full pages."
)

GET_DOCUMENTS_DESCRIPTION = (
    "Read the full text of pages that search results pointed to. Call it only when the results do not "
    "answer the question (steps, an agenda, a list, dates, exact wording, or a result that stops "
    f"mid-sentence). Pass every url you need in one call, at most {MAX_BATCH_DOCUMENTS}, exactly as "
    "the search results returned them. Foundation pages and the wiki return the page text; "
    "third-party pages return only a short excerpt, so link to the source instead of reproducing it. "
    "Each document has status, title, text and truncated (true means more of the page exists). If it is "
    f"truncated and the part you need is missing, call again with that url and max_chars up to "
    f"{DOCUMENT_MAX_CHARS}."
)

SERVER_INSTRUCTIONS = (
    "Knowledge about the Sujeeth Foundation: its Career Circles student mentorship program, mentoring, "
    "volunteering, donating and partnering. Start with search_sujeeth_foundation; it searches the "
    "website and the wiki together"
    + (", and can send same-topic query variants together in one search request. " if MAX_QUERIES > 1 else ". ")
    + "Results are excerpts. "
    + f"If the excerpts do not answer the question, call get_documents once with the urls you need "
    f"(at most {MAX_BATCH_DOCUMENTS}). Cite the url for every claim. " + STATS_RULE
    + " If a search comes back out_of_scope, reply with its message as written and nothing from "
    "general knowledge. Ask a clarifying question only when the response has needs_clarification true."
)

# Total characters of document text attached to one search response, so a broad question
# cannot flood the conversation.
DOCUMENT_CHAR_BUDGET = int(setting("FULL_DOCUMENT_CHAR_BUDGET", "60000") or 60000)


def fold_snippet(item):
    """Once a result carries its whole page, drop the snippet: it only repeats part of the page.

    A truncated document keeps its snippet, because the best passage may lie past the cut.
    """
    doc = item.get("document") or {}
    if doc.get("text") and not doc.get("truncated"):
        item.pop("snippet", None)


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
        key = page_identity(item) if item.get("url") else ""
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
            if is_foundation_page(item) and item.get("url"):
                # A Foundation page is read whole, by url. That read is cached, so a later
                # get_documents of the same page costs nothing. (A read by chunk_id returns only
                # the matched passage and its neighbours, not the page.)
                return fetch_document(item.get("url"), None, item.get("source"), 1, per_document_chars)
            # Third-party pages come back as an excerpt around the matched passage.
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
        fold_snippet(item)
        summary["returned"] += 1
        summary["excerpt_only"] += 1 if excerpt else 0
        summary["from_search_text"] += 1 if doc.get("from_search") else 0
    return summary


def attach_wiki_documents(results):
    """Attach the page text the wiki search already returned to the wiki results that were kept.

    No second request is made. Only wiki results are touched, so website results stay excerpts.
    Returns the same summary shape as attach_full_documents.
    """
    summary = {
        "requested": 0, "returned": 0, "excerpt_only": 0, "failed": 0,
        "skipped_for_length": 0, "from_search_text": 0,
    }
    used, seen = 0, set()
    for item in results:
        if item.get("source") != "wiki" or not item.get("url"):
            continue
        key = page_identity(item)
        if key in seen:
            continue
        seen.add(key)
        summary["requested"] += 1
        text = str(item.get("_full_text") or "")
        if not text:
            summary["failed"] += 1
            continue
        if used + len(text) > DOCUMENT_CHAR_BUDGET:
            summary["skipped_for_length"] += 1
            continue
        used += len(text)
        item["document"] = {
            "text": text,
            "excerpt_only": False,
            "truncated": bool(item.get("_truncated")),
            "note": None,
        }
        fold_snippet(item)
        summary["returned"] += 1
        summary["from_search_text"] += 1
    return summary


# ------------------------------------------------------------
# MCP server
# ------------------------------------------------------------

mcp = FastMCP(
    "Sujeeth Foundation Graph",
    instructions=SERVER_INSTRUCTIONS,
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=STATELESS,
)


def tool_annotations(open_world: bool) -> ToolAnnotations:
    """Every tool here only reads. open_world is True when a result can come from a third-party website."""
    return ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        openWorldHint=open_world,
    )


# Closed world: the Foundation's own wiki, ontology and Function Apps.
CLOSED_WORLD = tool_annotations(open_world=False)
# Open world: results can include pages from approved third-party partner websites.
OPEN_WORLD = tool_annotations(open_world=True)


def admin_tool(fn):
    """Register a diagnostic or ontology tool only when MCP_ADMIN_TOOLS is on."""
    return mcp.tool(annotations=CLOSED_WORLD)(in_thread(fn)) if ADMIN_TOOLS else fn


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    """Liveness probe for the hosting platform. Does not call any backend."""
    from starlette.responses import JSONResponse
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/status", methods=["GET"])
async def status(request):
    """Instance, uptime, memory, CPU, tool-thread load and backend call times, for watch_all.py. Needs the trusted key
    header, so it is not public."""
    import hmac
    from starlette.responses import JSONResponse
    sent = request.headers.get(TRUSTED_KEY_HEADER, "")
    if not TRUSTED_KEY or not hmac.compare_digest(sent.encode(), TRUSTED_KEY.encode()):
        return JSONResponse({"error": "not found"}, status_code=404)
    with _tool_lock:
        in_flight, peak = _tool_state["in_flight"], _tool_state["peak_in_flight"]
    return JSONResponse({
        "status": "ok",
        "process": process_info(),
        "tool_pool": {"threads": TOOL_THREADS, "in_flight": in_flight, "queued": max(0, in_flight - TOOL_THREADS), "peak_in_flight": peak},
        "downstream": downstream_stats(),
    }, headers={"Cache-Control": "no-store"})


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


@mcp.tool(description=WEBSITE_SEARCH_DESCRIPTION, annotations=OPEN_WORLD)
@in_thread
def search_website(
    query: str,
    limit: int = 5,
) -> dict[str, Any]:
    """Resolve the question in MCP, then search approved websites with MCP-derived filters."""
    query = (query or "").strip()
    if not query:
        return {"error": "Please provide a query."}

    try:
        plan = resolve_ontology_plan(query)
        resolution = plan["resolution"]
        concepts, domains = resolution.get("matched_concepts") or [], resolution.get("domains") or []
        if not concepts:
            return {"status": "out_of_scope", "out_of_scope": True, "message": OUT_OF_SCOPE_MESSAGE,
                    "ontology": resolution_summary(resolution), "count": 0, "results": [],
                    "agent_instructions": OUT_OF_SCOPE_INSTRUCTIONS}
        if not domains:
            return {"status": "no_approved_domains_for_concepts", "out_of_scope": False,
                    "ontology": resolution_summary(resolution), "count": 0, "results": [],
                    "agent_instructions": CITE_AND_STATS + EXCERPT_INSTRUCTIONS}
        queries = _clean_queries(query, [q for q in plan.get("queries", []) if q.casefold() != query.casefold()])
        payload = post_json_with_retry(
            f"{WEBSEARCH_URL}/api/search",
            {"query": query, "queries": queries, "concepts": concepts, "domains": domains,
             "ontology_version": resolution.get("ontology_version"), "purpose": "search",
             "limit": max(1, min(int(limit), 5))}, WEBSEARCH_KEY)
        items, seen = [], set()
        for item in payload.get("results", []) or []:
            identity = page_identity(item)
            if identity not in seen:
                seen.add(identity)
                items.append(item)
        result = {"query": query, "queries": queries, "status": "completed", "resolution": resolution,
                  "concepts": concepts, "domains": domains, "count": len(items), "results": items}
        if isinstance(result, dict) and "agent_instructions" not in result:
            result["agent_instructions"] = CITE_AND_STATS + EXCERPT_INSTRUCTIONS
        return result
    except Exception as exc:
        return error_result(exc)


def run_wiki_search(query, full_documents, document_max_chars, concepts=None, domains=None, also=None,
                    include_documents=True):
    """Wiki search for the combined tool. Returns (payload, error) and never raises."""
    try:
        queries = _clean_queries(query, also or [])
        if len(queries) == 1:
            params = {"q": query, "limit": WIKI_POOL,
                      "concepts": ",".join(concepts or []), "domains": ",".join(domains or [])}
            if also:
                params["also"] = " ".join(also)
            if include_documents and full_documents:
                params.update({"full_text": "true", "max_chars": max(500, min(int(document_max_chars), 20000))})
            else:
                params["full_text"] = "false"
            return get_json(f"{WIKISEARCH_URL}/api/search", WIKISEARCH_KEY, params), None
        params = {"query": query, "queries": queries, "limit": WIKI_POOL,
                  "concepts": concepts or [], "domains": domains or []}
        if include_documents and full_documents:
            params.update({
                "full_text": "true",
                "max_chars": max(500, min(int(document_max_chars), 20000)),
            })
        else:
            params["full_text"] = "false"
        return post_json_with_retry(f"{WIKISEARCH_URL}/api/search", params, WIKISEARCH_KEY), None
    except Exception as exc:
        return {"results": []}, exc


@mcp.tool(description=WIKI_SEARCH_DESCRIPTION, annotations=CLOSED_WORLD)
@in_thread
def search_wiki(
    query: str,
    limit: int = 5,
) -> dict[str, Any]:
    """
    Search the Sujeeth Foundation GitHub wiki. Returns short excerpts that can stop
    mid-sentence. To read pages, call get_documents once with their urls.
    MCP resolves the question and generates related queries before the wiki is searched.
    Results contain excerpts. To read pages, call get_documents for selected result URLs.
    """
    query = (query or "").strip()
    if not query:
        return {"error": "Please provide a query."}

    try:
        plan = resolve_ontology_plan(query)
        resolution = plan["resolution"]
        concepts, domains = resolution.get("matched_concepts") or [], resolution.get("domains") or []
        if not concepts:
            return {"status": "out_of_scope", "out_of_scope": True, "message": OUT_OF_SCOPE_MESSAGE,
                    "ontology": resolution_summary(resolution), "count": 0, "results": [],
                    "agent_instructions": OUT_OF_SCOPE_INSTRUCTIONS}
        if not foundation_domain_allowed(resolution, domains):
            return {"query": query, "ontology": resolution_summary(resolution), "count": 0, "results": [],
                    "agent_instructions": CITE_AND_STATS + WIKI_DOCUMENT_INSTRUCTIONS}
        queries = _clean_queries(query, [q for q in plan.get("queries", []) if q.casefold() != query.casefold()])
        if len(queries) == 1:
            params = {"q": query, "limit": max(1, min(int(limit), 10)),
                      "concepts": ",".join(concepts), "domains": ",".join(domains), "full_text": "false"}
            payload = get_json(f"{WIKISEARCH_URL}/api/search", WIKISEARCH_KEY, params)
        else:
            params = {"query": query, "queries": queries, "limit": max(1, min(int(limit), 10)),
                      "concepts": concepts, "domains": domains, "full_text": False}
            payload = post_json_with_retry(f"{WIKISEARCH_URL}/api/search", params, WIKISEARCH_KEY)
        items, seen = [], set()
        for item in drop_excluded_wiki(normalize_results(payload, "wiki")):
            if item.get("source") not in {"wiki", None, ""}:
                continue
            identity = page_identity(item)
            if identity not in seen:
                seen.add(identity)
                items.append(item)
        results = items[:max(1, min(int(limit), 10))]
        response = {
            "query": query,
            "queries": queries,
            "ontology": resolution_summary(resolution),
            "count": len(results),
            "results": results,
        }
        response["agent_instructions"] = CITE_AND_STATS + WIKI_DOCUMENT_INSTRUCTIONS
        strip_internal(results)
        return response
    except Exception as exc:
        return error_result(exc)


def foundation_domain_allowed(resolution, domains):
    """True when the wiki belongs in this search: the ontology approved sujeeth.org (or a subdomain) for the question.
    When the Search App could not answer, there is no ontology verdict, so the wiki stays as the fallback."""
    if not WIKI_NEEDS_FOUNDATION_DOMAIN or (resolution or {}).get("status") == "unavailable":
        return True
    return any(str(d).lower().rstrip("/") == "sujeeth.org" or str(d).lower().endswith(".sujeeth.org") for d in domains or [])


_search_cache = {}  # key -> (expires, finished response)
_search_cache_lock = threading.Lock()


def clear_search_cache():
    with _search_cache_lock:
        _search_cache.clear()


def _search_cache_key(query, limit, full_documents, max_documents, document_max_chars):
    # Case and spacing do not change what a search finds, so they must not split the cache.
    return (" ".join(query.lower().split()), limit, bool(full_documents), max_documents, document_max_chars)


def _search_cache_get(key):
    if SEARCH_CACHE_SECONDS <= 0:
        return None
    with _search_cache_lock:
        hit = _search_cache.get(key)
    if hit and hit[0] > time.monotonic():
        return copy.deepcopy(hit[1])
    return None


def _search_cache_put(key, response):
    if SEARCH_CACHE_SECONDS <= 0 or response.get("status") not in ("completed", "out_of_scope"):
        return
    with _search_cache_lock:
        _search_cache[key] = (time.monotonic() + SEARCH_CACHE_SECONDS, copy.deepcopy(response))
        if len(_search_cache) > SEARCH_CACHE_MAX:
            for old in list(_search_cache)[: max(1, SEARCH_CACHE_MAX // 10)]:
                _search_cache.pop(old, None)


def search_one(query, limit=None, full_documents=FULL_DOCUMENTS_DEFAULT, max_documents=None, document_max_chars=12000,
               _plan=None, _request_started=None, _include_documents=True, _ontology_resolution_ms=None):
    """
    One query: Search Website Search and Wiki Search together using ontology resolution.
    Results flagged best_bet or spotlight are curated pages; present them first.
    Results come from as many different sources as possible (at most MAX_RESULTS_PER_DOMAIN
    from any one). limit defaults to SEARCH_RESULT_COUNT.
    Results are short excerpts that can stop mid-sentence. To read pages, call
    get_documents once with their urls. Set full_documents to true to attach
    full page text instead: every best bet gets one, plus up to max_documents other results
    (default: all of them), each up to document_max_chars, within the response's total
    character budget (uses many tokens). Third-party pages are always excerpts.
    Returns a suggested question only when two readings of the question could conflict. When no concept
    matches, the question is not about the Foundation: the response has out_of_scope true, a message to
    relay, and no results.
    """
    request_started = _request_started if _request_started is not None else time.perf_counter()
    query = (query or "").strip()
    if not query:
        return {"error": "Please provide a query."}

    limit = max(1, min(int(limit or RESULT_COUNT), 20))
    timings = {"cache_hit": False}
    ontology_started = time.perf_counter()
    if _plan is None:
        try:
            _plan = resolve_ontology_plan(query)
        except Exception as exc:
            _plan = {"resolution": {"status": "unavailable", "matched_concepts": [], "domains": []},
                     "queries": [query], "error": exc}
    timings["ontology_resolution_ms"] = (
        _ontology_resolution_ms if _ontology_resolution_ms is not None
        else round((time.perf_counter() - ontology_started) * 1000)
    )
    queries = _clean_queries(query, [q for q in _plan.get("queries", []) if q.casefold() != query.casefold()])
    full_documents = bool(full_documents and _include_documents)
    cache_key = _search_cache_key(query, limit, full_documents, max_documents, document_max_chars, queries)
    cached = _search_cache_get(cache_key)
    if cached is not None:
        timings = {"cache_hit": True, "ontology_resolution_ms": (
                       _ontology_resolution_ms if _ontology_resolution_ms is not None
                       else round((time.perf_counter() - ontology_started) * 1000)),
                   "query_variants": len(queries), "websearch_ms": None, "wikisearch_ms": None,
                   "tool_response_ready_ms": round((time.perf_counter() - request_started) * 1000)}
        timings["total_ms"] = timings["tool_response_ready_ms"]
        print(f"search_timings_ms {json.dumps(timings)}", file=sys.stderr, flush=True)
        if TIMINGS:
            cached["timings_ms"] = timings
        return cached
    timings["query_variants"] = len(queries)
    errors = []

    # MCP's AI model decides relevance, then MCP validates every proposed ID against the ontology and
    # derives approved domains. An unavailable model is a service failure, never an out-of-scope verdict.
    try:
        resolution = _plan["resolution"]
        concepts = resolution.get("matched_concepts") or []
        domains = resolution.get("domains") or []
        if resolution.get("status") == "unavailable":
            raise _plan.get("error") or RuntimeError("Ontology resolution is unavailable")
    except Exception as exc:
        website = {"results": []}
        resolution = {"status": "unavailable"}
        concepts, domains = [], []
        errors.append({"service": "ontology", **error_result(exc)})

    if concepts:
        wiki_pool = ThreadPoolExecutor(max_workers=1)
        sibling_queries = queries[1:]
        def timed_wiki_search():
            began = time.perf_counter()
            value = run_wiki_search(query, full_documents, document_max_chars,
                                    concepts, domains, sibling_queries, include_documents=_include_documents)
            return value, round((time.perf_counter() - began) * 1000)
        wiki_future = wiki_pool.submit(timed_wiki_search)
    else:
        wiki_pool = None
        wiki_future = None

    if concepts and domains:
        web_started = time.perf_counter()
        try:
            website = post_json_with_retry(
                f"{WEBSEARCH_URL}/api/search",
                {"query": query, "queries": queries, "concepts": concepts, "domains": domains,
                 "ontology_version": resolution.get("ontology_version"), "purpose": "search", "limit": CANDIDATE_POOL},
                WEBSEARCH_KEY,
            )
            # MCP's authorization decision remains authoritative; Websearch only reports/filter-searches results.
            website["resolution"] = resolution
        except Exception as exc:
            website = {"results": [], "resolution": resolution}
            errors.append({"service": "websearch", **error_result(exc)})
        timings["websearch_ms"] = round((time.perf_counter() - web_started) * 1000)
    else:
        timings["websearch_ms"] = None
        website = {"results": [], "status": "no_ontology_concept_match" if not concepts else "no_approved_domains_for_concepts",
                   "resolution": resolution}
    if wiki_future:
        wiki_wait_started = time.perf_counter()
        try:
            (wiki, wiki_error), wiki_elapsed = wiki_future.result()
            timings["wikisearch_ms"] = wiki_elapsed
        finally:
            wiki_pool.shutdown(wait=False)
        timings["wiki_wait_ms"] = round((time.perf_counter() - wiki_wait_started) * 1000)
    else:
        wiki, wiki_error = {"results": []}, None
        timings["wikisearch_ms"] = None
        timings["wiki_wait_ms"] = None
    if not foundation_domain_allowed(resolution, domains):
        wiki, wiki_error = {"results": []}, None   # sujeeth.org is not an appropriate source for this question
    if wiki_error is not None:
        errors.append({"service": "wikisearch", **error_result(wiki_error)})
    processing_started = time.perf_counter()
    wiki_items = drop_excluded_wiki([
        item for item in normalize_results(wiki, "wiki")
        if item.get("source") in {"wiki", None, ""}
    ])
    results, best_bet_count, spotlight_count = combine_with_best_bets(
        wiki_items, normalize_results(website, "website"), limit
    )
    quality = assess_quality(resolution, results, errors)
    out_of_scope = quality["out_of_scope"]
    if out_of_scope:
        # Nothing in the ontology matched, so the wiki keyword hits are noise too: return no results at all.
        results, best_bet_count, spotlight_count = [], 0, 0
    if best_bet_count or spotlight_count:
        quality["agent_instructions"] += PRIORITY_INSTRUCTIONS
    if spotlight_count:
        quality["agent_instructions"] += SPOTLIGHT_INSTRUCTIONS

    timings["result_processing_ms"] = round((time.perf_counter() - processing_started) * 1000)
    document_started = time.perf_counter()
    documents_summary = None
    if _include_documents and full_documents and results:
        documents_summary = attach_full_documents(
            results,
            max(1, min(int(max_documents or limit), 20)),
            max(500, min(int(document_max_chars), DOCUMENT_MAX_CHARS)),
        )
        reading = FULL_DOCUMENT_INSTRUCTIONS if documents_summary["returned"] else EXCERPT_INSTRUCTIONS
    else:
        reading = EXCERPT_INSTRUCTIONS
    if not out_of_scope:
        quality["agent_instructions"] += reading
    timings["document_fetch_ms"] = round((time.perf_counter() - document_started) * 1000)
    strip_internal(results)
    slim_results(results)
    response = {
        "query": query,
        "queries": queries,
        "status": "out_of_scope" if out_of_scope else "partial" if errors else "completed",
        "ontology": resolution_summary(resolution),
        # "count" is what this response returns. The candidates are what each source found before
        # the best results were chosen across sources, so they are usually larger.
        "retrieval": {
            "website_status": website.get("status"),
            "website_candidates": len(website.get("results", []) or []),
            "wiki_candidates": len(wiki_items),
            "search_mode": website.get("search_mode"),
            **({"domains": website.get("domains", domains)} if VERBOSE_ONTOLOGY else {}),
        },
        "count": len(results),
        "best_bet_count": best_bet_count,
        "spotlight_count": spotlight_count,
        "full_documents": documents_summary,
        "results": results,
        "service_errors": errors,
        **quality,
    }
    timings["tool_response_ready_ms"] = round((time.perf_counter() - request_started) * 1000)
    timings["total_ms"] = timings["tool_response_ready_ms"]
    if TIMINGS:
        response["timings_ms"] = timings
    _search_cache_put(cache_key, response)
    print(f"search_timings_ms {json.dumps(timings)}", file=sys.stderr, flush=True)
    return response


def _clean_queries(query, more_queries):
    """The first query and the extra ones, trimmed, without repeats, at most MAX_QUERIES in all."""
    queries, seen = [], set()
    for text in [query] + [q for q in (more_queries or []) if isinstance(q, str)]:
        text = (text or "").strip()
        key = " ".join(text.lower().split())
        if key and key not in seen:
            seen.add(key)
            queries.append(text)
    return queries[:MAX_QUERIES]


@mcp.tool(description=SEARCH_DESCRIPTION, annotations=OPEN_WORLD, structured_output=(REPLY_FORMAT == "both"))
@in_thread
def search_sujeeth_foundation(
    query: str,
    limit: int | None = None,
    more_queries: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve once, then send the query and same-topic variants in one request to each search service.

    Page text is not fetched by this tool. The chat agent can call get_documents for selected results when
    their excerpts are not enough. Separate topics should use separate tool calls.
    """
    request_started = time.perf_counter()
    query = (query or "").strip()
    if not query:
        return {"error": "Please provide a query."}
    ontology_started = time.perf_counter()
    try:
        plan = resolve_ontology_plan(query)
    except Exception as exc:
        plan = {"resolution": {"status": "unavailable", "matched_concepts": [], "domains": []},
                "queries": [query], "error": exc}
    ontology_resolution_ms = round((time.perf_counter() - ontology_started) * 1000)
    extras = more_queries if isinstance(more_queries, list) else []
    variants = _clean_queries(query, [
        q for q in plan.get("queries", []) if isinstance(q, str) and q.casefold() != query.casefold()
    ] + extras)
    plan = {**plan, "queries": variants}
    response = search_one(
        query, limit, full_documents=False,
        _plan=plan,
        _request_started=request_started, _include_documents=False,
        _ontology_resolution_ms=ontology_resolution_ms,
    )
    return response


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
    return post_json_with_retry(
        f"{WEBSEARCH_URL}/api/document",
        {
            "url": url,
            "chunk_id": chunk_id,
            "neighbors": max(0, min(int(neighbors), 3)),
            "max_chars": max_chars,
        },
        WEBSEARCH_KEY,
    )


_document_cache = {}  # url -> (expires, result read at DOCUMENT_MAX_CHARS)
_document_cache_lock = threading.Lock()


def fit_document(result, max_chars, cached=False):
    """A copy of a page read, cut to max_chars. truncated is true when text was cut here or by the search app."""
    out = dict(result)
    text = out.get("text")
    if isinstance(text, str) and len(text) > max_chars:
        out["total_chars"] = out.get("total_chars", len(text))
        out["text"] = text[:max_chars]
        out["truncated"] = True
    out["_cached"] = cached
    return out


def fetch_document(url=None, chunk_id=None, source=None, neighbors=1, max_chars=6000, refresh=False):
    """Fetch one document, reusing a recent copy of a whole-page read. Raises on failure.

    A page is read once at the largest size and kept by url, so a repeat read of the same page
    is served from memory whatever max_chars it asks for (a read at a larger size would otherwise
    miss the cache and fetch the page again). Only reads by url are kept. A read by chunk_id
    returns a passage with its neighbors, which depends on the arguments, so it always goes to
    the search apps. Failures are never kept. The result's _cached key says whether memory
    answered; callers that return the result to the agent leave it out.
    """
    if DOCUMENT_CACHE_SECONDS <= 0 or chunk_id or not url:
        return _fetch_document(url, chunk_id, source, neighbors, max_chars)
    key = str(url).strip().lower().rstrip("/")
    want = max(500, min(int(max_chars), DOCUMENT_MAX_CHARS))
    now = time.monotonic()
    with _document_cache_lock:
        hit = _document_cache.get(key)
    if hit and hit[0] > now and not refresh:
        return fit_document(hit[1], want, cached=True)
    result = _fetch_document(url, chunk_id, source, neighbors, DOCUMENT_MAX_CHARS)
    if not isinstance(result, dict):
        return result
    if result.get("text") and not result.get("error"):
        with _document_cache_lock:
            _document_cache[key] = (now + DOCUMENT_CACHE_SECONDS, dict(result))
            if len(_document_cache) > DOCUMENT_CACHE_MAX:
                for old in list(_document_cache)[: max(1, DOCUMENT_CACHE_MAX // 10)]:
                    _document_cache.pop(old, None)
    return fit_document(result, want)


def explain_fetch_error(exc):
    """A short, user-safe reason a page could not be read."""
    if isinstance(exc, requests.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        if status == 404:
            return "That document was not found. Use a url from a search result."
        if status == 403:
            return "That document is not available through this server."
    return error_result(exc).get("error", "Could not read the document.")


@mcp.tool(description=GET_DOCUMENTS_DESCRIPTION, annotations=OPEN_WORLD, structured_output=(REPLY_FORMAT == "both"))
@in_thread
def get_documents(urls: list[str], max_chars: int = DOCUMENT_DEFAULT_CHARS) -> dict[str, Any]:
    """Read the full text of the pages behind search results. The agent-facing description is GET_DOCUMENTS_DESCRIPTION."""
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
    max_chars = max(500, min(int(max_chars), DOCUMENT_MAX_CHARS))

    def read(url):
        began = time.perf_counter()
        cached = False
        try:
            doc = fetch_document(url, None, None, 1, max_chars)
            cached = bool(doc.get("_cached"))
            item = {"url": url, "status": "ok"}
            for key in ("title", "source", "domain", "text", "total_chars", "truncated", "excerpt_only", "note"):
                if doc.get(key) is not None:
                    item[key] = doc[key]
        except Exception as exc:
            item = {"url": url, "status": "error", "error": explain_fetch_error(exc)}
        return item, round((time.perf_counter() - began) * 1000), cached

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(wanted)) as pool:
        reads = list(pool.map(read, wanted))
    total_ms = round((time.perf_counter() - started) * 1000)
    documents = [item for item, _, _ in reads]
    # The log line always says what each read cost and whether memory answered it. The agent
    # sees those numbers only when SEARCH_TIMINGS is on: they are diagnostics, not content.
    print(f"get_documents_ms {json.dumps({'total': total_ms, 'each': [ms for _, ms, _ in reads], 'cached': [c for _, _, c in reads]})}",
          file=sys.stderr, flush=True)
    if TIMINGS:
        for item, ms, cached in reads:
            item["ms"], item["cached"] = ms, cached

    response = {
        "count": len(documents),
        "documents": documents,
        "agent_instructions": GET_DOCUMENTS_INSTRUCTIONS,
    }
    if skipped:
        response["not_read"] = skipped
        response["note"] = (f"Only the first {MAX_BATCH_DOCUMENTS} urls were read. Read the others in "
                            f"a separate call only if you still need them.")
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
            found = search_sujeeth_foundation.sync(query)  # same arguments as the agent, so the search cache gets the entry it will ask for
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
