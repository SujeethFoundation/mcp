"""Load test for the MCP server (the /mcp endpoint). Stdlib only.

It plays visitors who each make a few search calls the way the Foundry agent does: search, then sometimes read one or
two of the pages found, with a pause between questions. Each visitor's calls are separate HTTP requests, because the
server is stateless.

Send the shared key so the test counts in the trusted bucket and measures the server, not the limiter:
    $env:MCP_TRUSTED_KEY = "<the value set on the MCP app>"
    python tests/load_mcp.py --url https://<mcp-app>.azurewebsites.net/mcp --visitors 10 --calls 3 --think 20 --ramp 30

Raise MCP_RATE_LIMIT_GLOBAL on the app for the run (default 600 a minute per instance) and put it back afterwards.
Each search calls the website search, the wiki search and Azure AI Search (with an embedding call), so a run costs
a little. Page reads are cached by the server for 15 minutes, so they look faster here than a first read would.

--handshake adds an initialize, a notifications/initialized and a tools/list before each call, three more requests.
Some clients do this on every call in stateless mode. It matters for the per-minute request limits, so use it to see
how many requests a real chat might cost. Press Ctrl+C to stop early; what finished is still reported.
"""
import argparse
import json
import os
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

QUESTIONS = [
    "What is a Career Circle?", "How do I become a mentor?", "What happens in Week 1?", "What happens in Week 2?",
    "What happens in Week 3?", "What happens in Week 4?", "What happens in Week 5?", "What happens in Week 6?",
    "How do I volunteer?", "How can my school partner with the Foundation?", "What should I put on my resume with no experience?",
    "How do I write a cover letter?", "How do I ask someone for an informational interview?", "How do I follow up after meeting a mentor?",
    "How do I improve my LinkedIn profile?", "What should I say in a networking message?", "How do I prepare for a behavioral interview?",
    "What questions should I ask a mentor?", "How do I negotiate a job offer?", "How do I find job openings?",
    "What is the mentor FAQ?", "How much time does mentoring take?", "Who can join a Career Circle?", "What is the Foundation's mission?",
    "How do I follow up after an interview?", "What is a good thank-you email?", "How do I build a base resume?",
    "How do I research a company before an interview?", "What are career stories from past students?", "How do I donate?",
    "What does a mentor do in week 4?", "How do I tailor my resume to a job posting?", "How do I explain a gap on my resume?",
    "How do I ask for a referral?", "What is an elevator pitch?", "How do I network at a career fair?",
    "What should I wear to an interview?", "How do I handle a rejection?", "How should I prepare for a phone screen?", "What are good questions to ask an interviewer?",
]


def percentile(values, p):
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))]


class Client:
    def __init__(self, url, key, key_header, timeout):
        self.url, self.timeout = url, timeout
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if key:
            self.headers[key_header] = key
        self.next_id = 0

    def post(self, body):
        """One request. Returns (status, seconds, parsed_json_or_None)."""
        data = json.dumps(body).encode()
        request = urllib.request.Request(self.url, data=data, headers=self.headers, method="POST")
        began = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw, ctype = response.status, response.read().decode("utf-8", "replace"), response.headers.get("content-type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, time.perf_counter() - began, None
        except Exception:
            return "error", time.perf_counter() - began, None
        return status, time.perf_counter() - began, parse_body(raw, ctype)

    def rpc(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self.next_id += 1
            body["id"] = self.next_id
        return self.post(body)


def parse_body(raw, content_type):
    """The server may answer with JSON or with a server-sent event holding the JSON."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        if "event-stream" in content_type or raw.startswith(("event:", "data:")):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return None
        return json.loads(raw)
    except ValueError:
        return None


def tool_payload(message):
    """(structured result dict, is_error) from a tools/call answer."""
    result = (message or {}).get("result") or {}
    payload = result.get("structuredContent")
    if payload is None:
        try:
            payload = json.loads((result.get("content") or [{}])[0].get("text") or "null")
        except ValueError:
            payload = None
    if isinstance(payload, dict) and set(payload) == {"result"} and isinstance(payload["result"], dict):
        payload = payload["result"]
    failed = bool(result.get("isError") or (message or {}).get("error") or (isinstance(payload, dict) and payload.get("error")))
    return payload if isinstance(payload, dict) else {}, failed


def main():
    ap = argparse.ArgumentParser(description="Load test for the MCP server.")
    ap.add_argument("--url", required=True, help="the MCP endpoint, ending in /mcp")
    ap.add_argument("--key", default=os.getenv("MCP_TRUSTED_KEY", ""), help="the trusted key (or set MCP_TRUSTED_KEY); never printed")
    ap.add_argument("--key-header", default="x-mcp-client-key")
    ap.add_argument("--visitors", type=int, default=5)
    ap.add_argument("--calls", type=int, default=3, help="questions per visitor")
    ap.add_argument("--think", type=float, default=20.0, help="seconds a visitor waits between questions")
    ap.add_argument("--ramp", type=float, default=10.0, help="spread the visitors' start times over this many seconds")
    ap.add_argument("--read-chance", type=float, default=0.5, help="chance a visitor reads pages after a search (0 to 1)")
    ap.add_argument("--handshake", action="store_true", help="send initialize, initialized and tools/list before each call")
    ap.add_argument("--max-requests", type=int, default=600, help="stop after this many HTTP requests")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()
    if urlparse(args.url).scheme not in ("http", "https"):
        sys.exit("--url must start with http:// or https://")

    lock = threading.Lock()
    results = []  # (kind, status, seconds, tool_failed)
    state = {"stop": False, "sent": 0, "in_flight": 0, "peak": 0, "calls": 0}

    def record(client, kind, method, params=None, notify=False):
        with lock:
            if state["stop"] or state["sent"] >= args.max_requests:
                state["stop"] = True
                return None
            state["sent"] += 1
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        status, seconds, message = client.rpc(method, params, notify)
        failed = False
        payload = {}
        if kind in ("search", "get_documents") and status == 200:
            payload, failed = tool_payload(message)
        with lock:
            state["in_flight"] -= 1
            state["last_done"] = time.perf_counter()
            results.append((kind, status, seconds, failed))
        return status, payload

    def nap(seconds):
        """Sleep, but wake up as soon as the run is stopped (cap reached or Ctrl+C)."""
        end = time.perf_counter() + seconds
        while not state["stop"] and time.perf_counter() < end:
            time.sleep(min(0.25, max(0.0, end - time.perf_counter())))

    def visitor(index):
        client = Client(args.url, args.key, args.key_header, args.timeout)
        nap(random.uniform(0, args.ramp))
        for n in range(args.calls):
            if state["stop"]:
                return
            if args.handshake:
                record(client, "initialize", "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                            "clientInfo": {"name": "load-test", "version": "1"}})
                record(client, "initialized", "notifications/initialized", notify=True)
                record(client, "tools/list", "tools/list", {})
            sent = record(client, "search", "tools/call", {"name": "search_sujeeth_foundation",
                                                           "arguments": {"query": random.choice(QUESTIONS)}})
            if sent is None:
                return
            with lock:
                state["calls"] += 1
            status, payload = sent
            urls = [r.get("url") for r in payload.get("results", []) if isinstance(r, dict) and r.get("url")
                    and ("sujeeth.org" in str(r.get("url")) or "github.com" in str(r.get("url")))]
            if status == 200 and urls and random.random() < args.read_chance:
                if args.handshake:
                    record(client, "tools/list", "tools/list", {})
                record(client, "get_documents", "tools/call", {"name": "get_documents",
                                                               "arguments": {"urls": urls[:2], "max_chars": 6000}})
            if n < args.calls - 1:
                nap(random.uniform(0.5 * args.think, 1.5 * args.think))

    print(f"{args.visitors} visitors x {args.calls} questions, think {args.think}s, ramp {args.ramp}s, "
          f"handshake {'on' if args.handshake else 'off'}, key {'sent' if args.key else 'NOT sent'} (cap {args.max_requests} requests)")
    if not args.key:
        print("  No key: this machine counts in the public bucket (120 requests a minute by default) and will hit HTTP 429.")
    print("A progress line prints every 10 seconds. Press Ctrl+C to stop early; the results so far are still reported.")

    started = time.perf_counter()
    threads = [threading.Thread(target=visitor, args=(i,), daemon=True) for i in range(args.visitors)]
    for t in threads:
        t.start()
    try:
        last_print = started
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
            if time.perf_counter() - last_print < 10:
                continue
            last_print = time.perf_counter()
            with lock:
                ok = sum(1 for _, s, _, f in results if s in (200, 202) and not f)
                limited = sum(1 for _, s, _, _ in results if s == 429)
                tool_failed = sum(1 for _, s, _, f in results if f)
                other = sum(1 for _, s, _, f in results if s not in (200, 202, 429))
                print(f"  [{time.perf_counter() - started:4.0f}s] sent {state['sent']}, ok {ok}, waiting {state['in_flight']}, "
                      f"limited (429) {limited}, tool errors {tool_failed}, other failures {other}", flush=True)
    except KeyboardInterrupt:
        state["stop"] = True
        print("\nStopping. Reporting what finished so far (requests still on their way are not counted).")
    wall = state.get("last_done", time.perf_counter()) - started  # to the last finished request, not the idle tail

    with lock:
        done = list(results)
    if not done:
        print("No requests finished.")
        return
    print(f"\nFinished in {wall:.0f}s. Requests sent: {state['sent']}. Highest number waiting at once: {state['peak']}.")
    if state["sent"] >= args.max_requests and not args.max_requests == 0:
        print(f"NOTE: the --max-requests cap ({args.max_requests}) stopped this run. Visitors did not finish their "
              f"{args.calls} questions, so this was a burst of first requests, not {args.visitors} x {args.calls} calls. "
              "Raise --max-requests or use fewer visitors to test the shape you meant.")
    by_status = {}
    for _, status, _, _ in done:
        by_status[status] = by_status.get(status, 0) + 1
    print("Results by HTTP status:", ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items(), key=lambda kv: str(kv[0]))))
    print("Per request type (HTTP 200/202 only):")
    for kind in ("initialize", "initialized", "tools/list", "search", "get_documents"):
        times = [s for k, st, s, _ in done if k == kind and st in (200, 202)]
        if times:
            print(f"  {kind:14s} n={len(times):4d}  median {statistics.median(times):5.2f}s  p90 {percentile(times, 90):5.2f}s  "
                  f"p95 {percentile(times, 95):5.2f}s  slowest {max(times):5.2f}s")
    tool_failed = sum(1 for _, st, _, f in done if f)
    if tool_failed:
        print(f"  {tool_failed} tool calls returned HTTP 200 but reported an error (a backend app failed or timed out).")
    calls = state["calls"]
    minutes = max(wall / 60, 1e-9)
    print(f"Requests per search call: {len(done) / calls:.1f}" if calls else "No search calls were sent.")
    print(f"Request rate: {len(done) / minutes:.0f} requests a minute over the whole run (peak minute is higher).")
    if by_status.get("error"):
        print(f"{by_status['error']} requests got no HTTP answer at all (connection refused, DNS or timeout): "
              "check --url and that the app is running.")
    if by_status.get(429):
        print("HTTP 429 means a rate limit stopped requests: check the key, MCP_TRUSTED_KEY on the app, and the "
              "`rate_limited bucket=...` lines in its log. This run is not a capacity measurement.")
    print("Now read the MCP app's instance count and Http 4xx/5xx, AI Search throttling and latency, and the embedding "
          "deployment's tokens per minute for this window.")


if __name__ == "__main__":
    main()
