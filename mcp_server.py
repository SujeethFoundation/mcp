
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP


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
        })
    return results


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
    if errors:
        reasons.append("One or more search services failed.")

    confidences = []
    for match in matches:
        if isinstance(match, dict):
            try:
                confidences.append(float(match.get("confidence")))
            except (TypeError, ValueError):
                pass

    if len(confidences) >= 2:
        ranked = sorted(confidences, reverse=True)
        if ranked[0] - ranked[1] <= 0.10:
            reasons.append("Leading ontology matches are very close.")

    needs_clarification = bool(reasons)
    question = None

    if needs_clarification:
        if not concepts:
            question = (
                "Which Foundation area are you asking about: the Career Circle "
                "Program, students, volunteers, books, or resources?"
            )
        elif len(matches) >= 2 and len(confidences) >= 2:
            first = matches[0].get("name", "the first topic")
            second = matches[1].get("name", "the second topic")
            question = f"Are you asking about {first} or {second}?"
        else:
            question = (
                "Could you clarify whether you mean the Career Circle Program, "
                "a specific person, a book, or another Foundation resource?"
            )

    return {
        "needs_clarification": needs_clarification,
        "reasons": reasons,
        "suggested_question": question,
        "agent_instructions": (
            "If clarification is needed, ask the suggested question before "
            "making a definitive claim. Use ontology context to explain "
            "available concepts. Never guess a domain or search outside "
            "ontology-approved domains. Otherwise answer from the results "
            "and cite their URLs."
        ),
    }


# ------------------------------------------------------------
# MCP server
# ------------------------------------------------------------

mcp = FastMCP(
    "Sujeeth Foundation Search",
    host="127.0.0.1",
    port=8000,
)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
            concept_id = concept.strip().lower()
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

        results = [
            item for item in normalize_results(payload, "wiki")
            if item.get("source") == "wiki"
        ]

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
            "needs_clarification": True,
            "suggested_question": (
                "Could you clarify which Foundation topic or resource you mean?"
            ),
            "agent_instructions": (
                "Ontology resolution failed. Do not guess; ask the user "
                "to clarify the Foundation topic."
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

    combined = normalize_results(website, "website")
    combined.extend(
        item for item in normalize_results(wiki, "wiki")
        if item.get("source") in {"wiki", None, ""}
    )

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
            "count": len(wiki.get("results", []) or []),
        },
        "count": len(results),
        "results": results,
        "service_errors": errors,
        **quality,
    }


if __name__ == "__main__":
    print("Starting Sujeeth Foundation MCP Server")
    print(f"Website Search: {WEBSEARCH_URL}")
    print(f"Wiki Search: {WIKISEARCH_URL}")
    print(f"Ontology: {ONTOLOGY_URL}")
    print("MCP endpoint: http://127.0.0.1:8000/mcp")
    mcp.run(
        transport="streamable-http",
    )