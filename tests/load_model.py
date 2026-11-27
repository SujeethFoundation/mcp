"""Time the ontology model call by itself, at rising concurrency, with no MCP server, no platform and no scale-out in between.

It runs the same code a search runs to resolve a question (resolve_ontology_plan), from this machine, against the model
deployment named in your settings, with questions nobody has asked, so the plan cache never answers.

What it answers: does one call take the same time when 1, 8, 32 or 64 are in flight at once?
  - Time stays about the same as concurrency rises: the deployment scales, so any slowness comes from the app or platform.
  - Time climbs with concurrency: the deployment itself slows under parallel calls, and no app setting or number of
    instances will get around it. The rows show where it starts.

    python tests/load_model.py                       levels 1,4,8,16,32,64; 3 calls per level at each concurrency
    python tests/load_model.py --levels 1,8,24 --rounds 4

Needs the same settings as the MCP app: MCP_ONTOLOGY_MODEL_ENDPOINT, MCP_ONTOLOGY_MODEL_API_KEY, ONTOLOGY_URL and
ONTOLOGY_FUNCTION_KEY (environment variables or local.settings.json). It makes real model calls and spends tokens:
about levels x rounds x concurrency calls (the default is roughly 375).
"""
import argparse
import os
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

os.environ["MCP_ONTOLOGY_MODEL_MAX_CONCURRENCY"] = "1000"      # no slot queue of our own: measure the deployment
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import mcp_server  # noqa: E402

STEMS = ["How do I volunteer", "What does a mentor do", "Can I become a mentor", "How can I donate", "Who runs the foundation",
         "What are Career Circles", "How do I join a club", "Where do students meet", "How do I partner with you",
         "What is the time commitment", "Do you help with resumes", "How do I apply for an internship"]
TAILS = ["at Mizzou", "as a sophomore", "remotely", "this spring", "if I work nights", "for my company", "with no experience",
         "in St. Louis", "from another state", "as an alum", "for a class project", "on weekends"]


def question():
    return f"{random.choice(STEMS)} {random.choice(TAILS)} {random.randrange(10**6)}?"


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else float("nan")


def run_level(catalog, concurrency, rounds):
    calls = concurrency * rounds
    errors, times = {}, []

    def one(_):
        began = time.perf_counter()
        try:
            mcp_server.resolve_ontology_plan(question(), catalog)
            return time.perf_counter() - began, None
        except Exception as exc:
            return time.perf_counter() - began, f"{type(exc).__name__}: {str(exc)[:80]}"
    wall = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for seconds, error in pool.map(one, range(calls)):
            if error:
                errors[error] = errors.get(error, 0) + 1
            else:
                times.append(seconds)
    wall = time.perf_counter() - wall
    return calls, times, errors, wall


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--levels", default="1,4,8,16,32,64", help="concurrency levels, comma separated")
    parser.add_argument("--rounds", type=int, default=3, help="calls per worker at each level (calls = level x rounds)")
    args = parser.parse_args(argv)
    levels = [max(1, int(x)) for x in args.levels.split(",") if x.strip()]
    if not mcp_server.ONTOLOGY_MODEL_ENDPOINT or not mcp_server.ONTOLOGY_MODEL_API_KEY:
        sys.exit("Set MCP_ONTOLOGY_MODEL_ENDPOINT and MCP_ONTOLOGY_MODEL_API_KEY first.")
    catalog = mcp_server.ontology_catalog()
    total = sum(level * args.rounds for level in levels)
    print(f"Model {mcp_server.ONTOLOGY_MODEL_NAME}, reasoning {mcp_server.ONTOLOGY_REASONING_EFFORT}, max output tokens "
          f"{mcp_server.ONTOLOGY_MODEL_MAX_OUTPUT_TOKENS}. About {total} real model calls.")
    print(f"{'in flight':>9} {'calls':>6} {'ok':>5} {'median':>8} {'p95':>8} {'slowest':>8} {'calls/s':>8}  errors")
    for level in levels:
        calls, times, errors, wall = run_level(catalog, level, args.rounds)
        median = statistics.median(times) if times else float("nan")
        print(f"{level:>9} {calls:>6} {len(times):>5} {median:>7.2f}s {pct(times, .95):>7.2f}s {max(times, default=float('nan')):>7.2f}s "
              f"{len(times) / wall:>8.1f}  {dict(errors) if errors else ''}")
    print("\nSame median at every level = the deployment scales; look at the app and platform. A median that rises with the "
          "level = the deployment slows under parallel calls; no instance count gets around it.")


if __name__ == "__main__":
    main()
