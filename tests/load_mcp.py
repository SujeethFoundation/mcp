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

Fixed-rate mode sends searches at a steady rate instead of playing visitors. It uses a pool of workers, so one
machine can send far more than the visitor mode can:
    python tests/load_mcp.py --url <...>/mcp --rate 30 --duration 60
It prints a warning if this machine could not keep up, because then the server was not the limit.

Ramp mode finds where failures start in one run: the rate climbs steadily from --rate to --ramp-to over --duration,
and the report shows, by when each request STARTED, the offered rate, latency, failures and answers slower than
--sla. Raise --workers so this machine never holds the rate back (a pool that fills up hides the overload):
    python tests/load_mcp.py --url <...>/mcp --rate 20 --ramp-to 120 --duration 600 --workers 4000 --timeout 45 --sla 45

--handshake adds an initialize, a notifications/initialized and a tools/list before each call, three more requests.
Some clients do this on every call in stateless mode. It matters for the per-minute request limits, so use it to see
how many requests a real chat might cost. Press Ctrl+C to stop early; what finished is still reported.
"""
import argparse
import json
import math
import os
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


NO_ANSWER = []  # (seconds since the run started, kind) for every request that got no HTTP answer
_no_answer_lock = threading.Lock()
RUN_START = [time.perf_counter()]


def note_no_answer(exc):
    """Remember what kind of failure it was (reset, timeout, refused, TLS...) and when, so a few can be explained."""
    reason = getattr(exc, "reason", None)  # urllib wraps the real error in URLError.reason
    inner = reason if isinstance(reason, BaseException) else exc
    kind = type(inner).__name__
    if kind in ("URLError", "error") and reason is not None and not isinstance(reason, BaseException):
        kind = f"URLError({str(reason)[:40]})"
    # urllib wraps a failure to CONNECT (including the TLS handshake) in URLError and lets a failure while
    # WAITING FOR THE REPLY through unwrapped. That tells a path that never opened (this machine, the network or
    # the platform's front end) from a server that accepted the request and never answered.
    kind = ("while connecting: " if isinstance(exc, urllib.error.URLError) else "after connecting, waiting for the reply: ") + kind
    with _no_answer_lock:
        NO_ANSWER.append((time.perf_counter() - RUN_START[0], kind))


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
        except Exception as exc:
            note_no_answer(exc)
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


# Words that can follow a question, so the same question arrives worded differently.
VARIATIONS = [
    "for a first-generation college student", "if I have no experience", "at a small company", "as an international student",
    "in healthcare", "in tech", "for a nonprofit job", "before graduation", "while working part time", "as a career changer",
    "this semester", "for an internship", "in my first job", "for a remote role", "in a new city", "with a low GPA",
    "after a layoff", "at a big company", "in finance", "in marketing", "in education", "for a government job",
]


def pick_question(vary_share=0.0, rng=random):
    """A question to ask. With vary_share above zero, that share of questions gets extra words and a random number, so
    it is a question nobody asked before: the result cache and the question-embedding cache cannot answer it, and every
    search does its full work. Without it, 40 fixed questions are asked over and over and the caches answer most of them."""
    question = rng.choice(QUESTIONS)
    if vary_share > 0 and rng.random() < vary_share:
        question = f"{question.rstrip('?.')} {rng.choice(VARIATIONS)}? ref {rng.randrange(10 ** 6)}"
    return question


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


def classify_search(payload):
    """What a search answer really contained. The server answers HTTP 200 even when a backend failed, and says so
    inside the answer, so 'HTTP 200' alone does not mean the search worked."""
    if not isinstance(payload, dict) or not payload:
        return "unreadable"
    if str(payload.get("status") or "") == "resolution_failed":
        return "resolution_failed"
    if payload.get("service_errors") or payload.get("service_degraded") or str(payload.get("status") or "") == "partial":
        return "degraded"
    if not payload.get("results"):
        return "empty"
    return "ok"


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
    ap.add_argument("--vary-share", type=float, default=0.0,
                    help="share of searches (0 to 1) sent as questions nobody asked before, so caches cannot answer them. "
                         "1 is the worst case for the app; 0.7 is a plausible mix; 0 asks the same 40 questions (caches answer most)")
    ap.add_argument("--handshake", action="store_true", help="send initialize, initialized and tools/list before each call")
    ap.add_argument("--rate", type=float, default=0.0, help="fixed-rate mode: searches per second (ignores --visitors/--calls/--ramp/--think)")
    ap.add_argument("--duration", type=float, default=60.0, help="fixed-rate mode: seconds to keep sending")
    ap.add_argument("--workers", type=int, default=500, help="fixed-rate mode: most requests this machine keeps in flight at once")
    ap.add_argument("--ignore-first", type=float, default=-1.0, help="also report the searches that finished after this many "
                    "seconds, to leave out the warm-up (default: 60 for a fixed-rate run of 180s or more)")
    ap.add_argument("--ramp-to", type=float, default=0.0, help="fixed-rate mode: climb steadily from --rate to this rate over --duration")
    ap.add_argument("--sla", type=float, default=45.0, help="seconds; an answer slower than this, or any failure, counts as an SLA breach")
    ap.add_argument("--max-requests", type=int, default=600, help="stop after this many HTTP requests")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()
    if urlparse(args.url).scheme not in ("http", "https"):
        sys.exit("--url must start with http:// or https://")
    if args.rate > 0:
        average = (args.rate + (args.ramp_to or args.rate)) / 2
        args.max_requests = max(args.max_requests, int(average * args.duration * 1.5) + 100)

    lock = threading.Lock()
    results = []  # (kind, status, seconds, tool_failed)
    timeline = []  # (seconds into the run when it finished, kind, status, seconds, outcome)
    outcomes = {}   # what the searches returned: ok, degraded, empty, resolution_failed, unreadable
    service_errors = {}  # "service: message" -> count, from the answers' service_errors
    state = {"stop": False, "sent": 0, "in_flight": 0, "peak": 0, "calls": 0, "queued": 0, "peak_queued": 0, "max_lag": 0.0, "scheduled": 0}

    def record(client, kind, method, params=None, notify=False):
        with lock:
            if state["stop"] or state["sent"] >= args.max_requests:
                state["stop"] = True
                return None
            state["sent"] += 1
            state["last_sent_at"] = time.perf_counter()
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        status, seconds, message = client.rpc(method, params, notify)
        failed = False
        payload = {}
        outcome = ""
        if kind in ("search", "get_documents") and status == 200:
            payload, failed = tool_payload(message)
            if kind == "search":
                outcome = "failed" if failed else classify_search(payload)
        with lock:
            state["in_flight"] -= 1
            state["last_done"] = time.perf_counter()
            results.append((kind, status, seconds, failed))
            timeline.append((state["last_done"] - RUN_START[0], kind, status, seconds, outcome))
            if kind == "search" and status == 200:
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
                for err in (payload.get("service_errors") or [])[:3]:
                    if isinstance(err, dict):
                        key = f"{err.get('service', '?')}: {str(err.get('error', ''))[:60]}"
                        service_errors[key] = service_errors.get(key, 0) + 1
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
                                                           "arguments": {"query": pick_question(args.vary_share)}})
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

    def one_search():
        with lock:
            state["queued"] -= 1
        client = Client(args.url, args.key, args.key_header, args.timeout)
        if args.handshake:
            record(client, "initialize", "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                        "clientInfo": {"name": "load-test", "version": "1"}})
            record(client, "initialized", "notifications/initialized", notify=True)
            record(client, "tools/list", "tools/list", {})
        sent = record(client, "search", "tools/call", {"name": "search_sujeeth_foundation",
                                                       "arguments": {"query": pick_question(args.vary_share)}})
        if sent is None:
            return
        with lock:
            state["calls"] += 1
        status, payload = sent
        urls = [r.get("url") for r in payload.get("results", []) if isinstance(r, dict) and r.get("url")
                and ("sujeeth.org" in str(r.get("url")) or "github.com" in str(r.get("url")))]
        if status == 200 and urls and random.random() < args.read_chance:
            record(client, "get_documents", "tools/call", {"name": "get_documents",
                                                           "arguments": {"urls": urls[:2], "max_chars": 6000}})

    def due_time(n):
        """Seconds after the start at which the n-th search is due. A steady rate, or a linear ramp from --rate
        to --ramp-to over --duration (arrivals so far = r0*t + (r1-r0)*t^2/(2*T); this solves it for t)."""
        r0, r1, total = args.rate, (args.ramp_to or args.rate), args.duration
        if r1 == r0:
            return n / r0
        a = (r1 - r0) / (2 * total)
        disc = r0 * r0 + 4 * a * n
        return float("inf") if disc < 0 else (-r0 + math.sqrt(disc)) / (2 * a)

    def generator():
        """Open loop: start searches on schedule, whether or not earlier ones have finished."""
        try:
            threading.stack_size(256 * 1024)  # thousands of waiting workers stay cheap
        except (ValueError, RuntimeError):
            pass
        pool = ThreadPoolExecutor(max_workers=max(1, args.workers))
        t0, n = time.perf_counter(), 0
        while not state["stop"] and time.perf_counter() - t0 < args.duration:
            due = t0 + due_time(n)
            now = time.perf_counter()
            if now < due:
                time.sleep(min(0.002, due - now))
                continue
            with lock:
                state["queued"] += 1
                state["scheduled"] += 1
                state["peak_queued"] = max(state["peak_queued"], state["queued"])
                state["max_lag"] = max(state["max_lag"], now - due)
            pool.submit(one_search)
            n += 1
        pool.shutdown(wait=True)

    if args.rate > 0:
        shape = (f"Ramp from {args.rate:g} to {args.ramp_to:g} searches a second over {args.duration:g}s"
                 if args.ramp_to and args.ramp_to != args.rate else f"Fixed rate {args.rate:g} searches a second for {args.duration:g}s")
        print(f"{shape} (up to {args.workers} in flight, SLA {args.sla:g}s), "
              f"handshake {'on' if args.handshake else 'off'}, read chance {args.read_chance:g}, "
              f"key {'sent' if args.key else 'NOT sent'}")
    else:
        print(f"{args.visitors} visitors x {args.calls} questions, think {args.think}s, ramp {args.ramp}s, "
          f"handshake {'on' if args.handshake else 'off'}, key {'sent' if args.key else 'NOT sent'} (cap {args.max_requests} requests)")
    if not args.key:
        print("  No key: this machine counts in the public bucket (120 requests a minute by default) and will hit HTTP 429.")
    if args.vary_share > 0:
        print(f"  Questions: {args.vary_share:.0%} are new wordings nobody asked before, so the result cache and the question-embedding cache answer few of them.")
    else:
        print(f"  Questions: the same {len(QUESTIONS)} asked over and over, so caches will answer most of them. Add --vary-share 1 to measure the uncached cost.")
    print("A progress line prints every 10 seconds. Press Ctrl+C to stop early; the results so far are still reported.")

    started = time.perf_counter()
    RUN_START[0] = started
    if args.rate > 0:
        threads = [threading.Thread(target=generator, daemon=True)]
    else:
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
    if args.rate == 0 and state["sent"] >= args.max_requests:
        print(f"NOTE: the --max-requests cap ({args.max_requests}) stopped this run. Visitors did not finish their "
              f"{args.calls} questions, so this was a burst of first requests, not {args.visitors} x {args.calls} calls. "
              "Raise --max-requests or use fewer visitors to test the shape you meant.")
    if args.rate > 0:
        sending = max(1e-9, state.get("last_sent_at", started) - started)  # until the last request really left
        print(f"Target {args.rate:g}{('-' + format(args.ramp_to, 'g')) if args.ramp_to and args.ramp_to != args.rate else ''} searches a second. Scheduled {state['scheduled']}; the last of the {state['sent']} "
              f"sent left this machine at {sending:.0f}s ({state['sent'] / sending:.1f} requests a second on average). "
              f"Most in flight at once: {state['peak']}.")
        if state["peak"] >= args.workers:
            print(f"WARNING: all {args.workers} workers were busy at once. When the server slows down, this machine stops "
                  "sending at the target rate and the latencies below start late, so they are understated.")
        if state["peak_queued"] > 5 or state["max_lag"] > 1.0:
            print(f"WARNING: this machine fell behind (up to {state['peak_queued']} searches waiting for a free worker, "
                  f"up to {state['max_lag']:.1f}s late starting one). Latencies below are understated and the rate was not "
                  "reached. Raise --workers or send from more than one machine.")
    by_status = {}
    for _, status, _, _ in done:
        by_status[status] = by_status.get(status, 0) + 1
    print("Results by HTTP status:", ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items(), key=lambda kv: str(kv[0]))))
    print("Per request type (HTTP 200/202 only):")
    for kind in ("initialize", "initialized", "tools/list", "search", "get_documents"):
        times = [s for k, st, s, _ in done if k == kind and st in (200, 202)]
        if times:
            print(f"  {kind:14s} n={len(times):4d}  median {statistics.median(times):5.2f}s  p90 {percentile(times, 90):5.2f}s  "
                  f"p95 {percentile(times, 95):5.2f}s  p99 {percentile(times, 99):5.2f}s  slowest {max(times):5.2f}s")
    with lock:
        events = list(timeline)
    if wall > 60 and events:
        print("Timeline of searches, in 30-second buckets by when they finished:")
        print("  seconds    done    ok  median     p95  slowest  no-answer  not-ok answers")
        width = 30
        for lo in range(0, int(wall) + 1, width):
            bucket = [(st, sec, oc) for at, k, st, sec, oc in events if k == "search" and lo <= at < lo + width]
            late = [t for t, _ in NO_ANSWER if lo <= t < lo + width]
            oks = [sec for st, sec, oc in bucket if st == 200]
            notok = sum(1 for st, sec, oc in bucket if st == 200 and oc != "ok")
            if not bucket and not late:
                continue
            med = f"{statistics.median(oks):6.2f}s" if oks else "     -"
            p95 = f"{percentile(oks, 95):6.2f}s" if oks else "     -"
            top = f"{max(oks):6.2f}s" if oks else "     -"
            print(f"  {lo:3d}-{lo + width:<4d} {len(bucket):6d} {len(oks):6d}  {med} {p95} {top}  {len(late):9d}  {notok}")
    if args.rate > 0 and events:
        searches = [(max(0.0, at - sec), st, sec, oc) for at, k, st, sec, oc in events if k == "search"]
        width = 30 if args.duration >= 180 else 10
        top = max(t for t, _, _, _ in searches)
        print(f"Offered load and outcome, by when each search STARTED ({width}-second buckets; SLA {args.sla:g}s):")
        print("  start      offered/s  answered  median     p95   failed  late  not-ok   breach%")
        first_breach, total_breach = None, 0
        for lo in range(0, int(top) + 1, width):
            bucket = [(st, sec, oc) for t, st, sec, oc in searches if lo <= t < lo + width]
            if not bucket:
                continue
            answered = [sec for st, sec, oc in bucket if st == 200]
            failed = sum(1 for st, sec, oc in bucket if st != 200)
            late = sum(1 for st, sec, oc in bucket if st == 200 and sec > args.sla)
            notok = sum(1 for st, sec, oc in bucket if st == 200 and oc != "ok")
            breaches = failed + late + notok
            total_breach += breaches
            pct = 100.0 * breaches / len(bucket)
            mark = ""
            if first_breach is None and pct >= 1.0:
                first_breach = (lo, len(bucket) / width)
                mark = "  <- first bucket with 1% or more breaches"
            med = f"{statistics.median(answered):6.2f}s" if answered else "     -"
            p95 = f"{percentile(answered, 95):6.2f}s" if answered else "     -"
            print(f"  {lo:3d}-{lo + width:<4d} {len(bucket) / width:9.1f} {len(answered):9d}  {med} {p95} {failed:7d} {late:5d} {notok:7d} {pct:8.1f}{mark}")
        print(f"SLA breaches (failed, slower than {args.sla:g}s, or not a working answer): {total_breach} of {len(searches)} "
              f"({100.0 * total_breach / len(searches):.1f}%)."
              + (f" First bucket with 1% or more began at {first_breach[0]}s, at about {first_breach[1]:.0f} searches a second offered."
                 if first_breach else " No bucket reached 1%."))
    cutoff = args.ignore_first if args.ignore_first >= 0 else (60.0 if args.rate > 0 and args.duration >= 180 else 0.0)
    if cutoff > 0:
        steady = [sec for at, k, st, sec, oc in events if k == "search" and st == 200 and at >= cutoff]
        if steady:
            print(f"Searches that finished after the first {cutoff:.0f}s (warm-up left out): n={len(steady)}  "
                  f"median {statistics.median(steady):.2f}s  p90 {percentile(steady, 90):.2f}s  p95 {percentile(steady, 95):.2f}s  "
                  f"p99 {percentile(steady, 99):.2f}s  slowest {max(steady):.2f}s")
    if outcomes:
        total = sum(outcomes.values())
        good = outcomes.get("ok", 0)
        order = ["ok", "degraded", "empty", "resolution_failed", "unreadable", "failed"]
        print("What the answered searches actually contained (HTTP 200 does not mean the search worked): "
              + ", ".join(f"{k} {outcomes[k]}" for k in order if outcomes.get(k)) + f"  [{good / total:.1%} returned results with no backend errors]")
        if good < total:
            for key, n in sorted(service_errors.items(), key=lambda kv: -kv[1])[:6]:
                print(f"    backend error seen {n}x: {key}")
            print("  degraded = a backend failed or timed out and the server answered with what it had; resolution_failed = "
                  "no search ran; empty = the search ran and found nothing. Only 'ok' counts as a working search.")
    tool_failed = sum(1 for _, st, _, f in done if f)
    if tool_failed:
        print(f"  {tool_failed} tool calls returned HTTP 200 but reported an error (a backend app failed or timed out).")
    calls = state["calls"]
    minutes = max(wall / 60, 1e-9)
    print(f"Requests per search call: {len(done) / calls:.1f}" if calls else "No search calls were sent.")
    print(f"Request rate: {len(done) / minutes:.0f} requests a minute over the whole run (peak minute is higher).")
    if by_status.get("error"):
        kinds = {}
        for _, kind in NO_ANSWER:
            kinds[kind] = kinds.get(kind, 0) + 1
        when = [t for t, _ in NO_ANSWER]
        print(f"{by_status['error']} requests got no HTTP answer at all. By kind: "
              + ", ".join(f"{k} x{v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])) + ".")
        if when:
            print(f"  They happened between {min(when):.0f}s and {max(when):.0f}s into the run. A reset or refused "
                  "connection means the server or platform dropped it; a timeout means it waited past --timeout; "
                  "a TLS or DNS error usually points at this machine's network.")
    if by_status.get(429):
        print("HTTP 429 means a rate limit stopped requests: check the key, MCP_TRUSTED_KEY on the app, and the "
              "`rate_limited bucket=...` lines in its log. This run is not a capacity measurement.")
    print("Now read the MCP app's instance count and Http 4xx/5xx, AI Search throttling and latency, and the embedding "
          "deployment's tokens per minute for this window.")


if __name__ == "__main__":
    main()
