"""Watch all four Foundation apps (mcp, websearch, wikisearch, ontology) at once: which instance answered, how long it has
been up, how much memory and CPU it uses, how long the health call took, and whether it restarted. Use it during a load test
to tell a restart (process id changes or uptime drops) from instances simply coming and going.

    $env:WEBSEARCH_FUNCTION_KEY = "<key>"
    $env:WIKISEARCH_FUNCTION_KEY = "<key>"
    $env:ONTOLOGY_FUNCTION_KEY = "<key>"
    $env:MCP_TRUSTED_KEY = "<the value set on the MCP app>"
    $env:WIKISEARCH_URL = "https://<wikisearch-app>.azurewebsites.net"
    $env:ONTOLOGY_URL = "https://<ontology-app>.azurewebsites.net"
    python watch_all.py --every 5 --duration 600 --csv run1.csv

CPU is the share of one core the process used since the last poll of the same instance (CPU seconds divided by clock
seconds). A Python process is held near 100% by the interpreter lock whatever the core count, so a steady 90% or more means
that instance is out of CPU: add instances, shrink the work per request, or cache. Memory is shown against the container
limit when the platform exposes it. The mcp line also shows tool threads in use and, per backend app, the median and p95
time of its calls over the last minute and how many calls a minute, which is what to read when deciding what to cache.

Keys are sent in headers and never printed. Each poll reaches whichever instance the platform picks, so poll every few
seconds or an instance can go unseen. Needs the small health patches in each app (see HEALTH_PATCHES.md); an app without
them still shows up/down and health latency, but no instance, uptime or memory.

Press Ctrl+C to stop; the summary is printed either way.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

# name -> (base URL variable, default base URL, health path, key variable, header that carries the key)
APPS = {
    "mcp": ("MCP_URL", "https://sujeethmcp-c3hjg6g4c2f2f2dn.eastus-01.azurewebsites.net", "/status",
            "MCP_TRUSTED_KEY", os.getenv("MCP_TRUSTED_KEY_HEADER", "x-mcp-client-key")),
    "websearch": ("WEBSEARCH_URL", "https://websearch-h9arcweeftfrd8e0.eastus-01.azurewebsites.net", "/api/health",
                  "WEBSEARCH_FUNCTION_KEY", "x-functions-key"),
    "wikisearch": ("WIKISEARCH_URL", "", "/api/health", "WIKISEARCH_FUNCTION_KEY", "x-functions-key"),
    "ontology": ("ONTOLOGY_URL", "", "/api/health", "ONTOLOGY_FUNCTION_KEY", "x-functions-key"),
}
SLOW_MS = 2000
HIGH_CPU = 85.0      # percent of one core (or of the container limit when known)
HIGH_MEM = 85.0      # percent of the container memory limit
WINDOW_SECONDS = 30


def short(name):
    name = str(name or "?")
    return name if len(name) <= 12 else name[:6] + ".." + name[-4:]


def fetch(url, header, key, timeout):
    """(HTTP status or error name, milliseconds, parsed JSON or None)."""
    headers = {"Accept": "application/json", "Cache-Control": "no-cache"}
    if key:
        headers[header] = key
    request = urllib.request.Request(url, headers=headers)
    began = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            status = response.status
    except urllib.error.HTTPError as exc:
        return f"HTTP {exc.code}", (time.perf_counter() - began) * 1000, None
    except Exception as exc:
        return type(exc).__name__, (time.perf_counter() - began) * 1000, None
    ms = (time.perf_counter() - began) * 1000
    try:
        return status, ms, json.loads(raw)
    except ValueError:
        return status, ms, None


def extras(app, body):
    """A few app-specific numbers worth seeing next to every poll."""
    if app == "mcp":
        pool = body.get("tool_pool") or {}
        text = ""
        if pool:
            text = f"tools {pool.get('in_flight', 0)}/{pool.get('threads', '?')} queued {pool.get('queued', 0)} peak {pool.get('peak_in_flight', 0)}"
        down = body.get("downstream") or {}
        if down:
            parts = []
            for name in ("websearch", "wikisearch", "ontology"):
                d = down.get(name)
                if d and d.get("last_minute_calls"):
                    parts.append(f"{name[:4]} {d.get('p50_ms')}/{d.get('p95_ms')}ms {d.get('last_minute_calls')}/m"
                                 + (f" err{d['errors']}" if d.get("errors") else ""))
            if parts:
                text += "  calls " + ", ".join(parts)
        return text
    elif app == "websearch":
        cache, index = body.get("query_cache") or {}, body.get("local_index") or {}
        rate = cache.get("hit_rate")
        return f"cache {('%.0f%%' % (rate * 100)) if rate is not None else '-'} chunks {index.get('chunks', 0)}"
    elif app == "wikisearch":
        return f"docs {body.get('document_count', 0)} from {body.get('loaded_from')} age {body.get('cache_age_seconds', '-')}s"
    elif app == "ontology":
        return f"ontology {body.get('ontology_version', '?')}"
    return ""


class AppWatch:
    def __init__(self, name, url, header, key):
        self.name, self.url, self.header, self.key = name, url, header, key
        self.instances = {}     # instance -> dict(first, pid, uptime, polls, restarts, last_seen)
        self.recent = {}        # instance -> time last seen, to count instances in the last WINDOW_SECONDS
        self.polls = self.failures = 0
        self.latencies = []
        self.restarts = []      # (clock, instance, why)
        self.warned_no_process = False

    def poll(self, timeout):
        clock = time.strftime("%H:%M:%S")
        now = time.time()
        status, ms, body = fetch(self.url, self.header, self.key, timeout)
        self.polls += 1
        row = {"time": clock, "app": self.name, "instance": "", "pid": "", "uptime": "", "rss_mb": "", "cpu_pct": "", "cpu_seconds": "",
               "mem_limit_mb": "", "cpu_limit_cores": "", "throttled_s": "", "ms": round(ms), "status": status}
        ok = status == 200 and isinstance(body, dict)
        if not ok:
            self.failures += 1
            line = f"{clock}  {self.name:10s}  FAILED ({status}) after {ms:.0f} ms"
            return line, row, now
        self.latencies.append(ms)
        proc = body.get("process") or {}
        flag = ""
        if not proc:
            if not self.warned_no_process:
                self.warned_no_process = True
                flag = "  <- no 'process' section: deploy the health patch for this app"
            line = f"{clock}  {self.name:10s}  up, no instance info   hc {ms:5.0f} ms{flag}"
            return line, row, now
        name, pid, up, rss = proc.get("instance"), proc.get("pid"), int(proc.get("uptime_seconds") or 0), proc.get("rss_mb")
        record = self.instances.get(name)
        if record is None:
            self.instances[name] = {"n": len(self.instances) + 1, "first": clock, "pid": pid, "uptime": up, "polls": 1, "restarts": 0,
                                    "cpu_prev": None, "cpu": [], "max_rss": 0.0, "mem_limit": None, "cpu_limit": None, "throttled": 0.0}
            flag = "  <- NEW instance" if len(self.instances) > 1 else "  <- first instance seen"
        else:
            if pid != record["pid"] or up < record["uptime"] - 1:
                why = "process id changed" if pid != record["pid"] else "uptime went back down"
                record["restarts"] += 1
                self.restarts.append((clock, name, why))
                flag = f"  <- RESTART ({why})"
            record.update(pid=pid, uptime=up, polls=record["polls"] + 1)
        self.recent[name] = now
        record = self.instances[name]
        cpu_s, mem_limit, cpu_limit, thr = proc.get("cpu_seconds"), proc.get("mem_limit_mb"), proc.get("cpu_limit_cores"), proc.get("cpu_throttled_seconds")
        cpu_pct = cap_pct = None
        prev = record["cpu_prev"]
        if isinstance(cpu_s, (int, float)):
            if prev and cpu_s >= prev[0] and now > prev[1]:
                cpu_pct = 100.0 * (cpu_s - prev[0]) / (now - prev[1])
                cap_pct = cpu_pct / (100.0 * cpu_limit) * 100.0 if cpu_limit else cpu_pct
                record["cpu"].append(cpu_pct)
            record["cpu_prev"] = (cpu_s, now)
        if isinstance(thr, (int, float)):
            if record["throttled"] and thr > record["throttled"] + 0.05:
                flag += f"  THROTTLED +{thr - record['throttled']:.1f}s"
            record["throttled"] = thr
        if isinstance(rss, (int, float)):
            record["max_rss"] = max(record["max_rss"], rss)
        record["mem_limit"], record["cpu_limit"] = mem_limit, cpu_limit
        if cap_pct is not None and cap_pct >= HIGH_CPU:
            flag += "  CPU HIGH"
        if isinstance(rss, (int, float)) and mem_limit and 100.0 * rss / mem_limit >= HIGH_MEM:
            flag += "  MEM HIGH"
        if ms > SLOW_MS:
            flag += "  SLOW"
        cpu_text = f"cpu {cpu_pct:4.0f}%" if cpu_pct is not None else "cpu    -"
        if isinstance(rss, (int, float)):
            mem = f"{rss:4.0f}/{mem_limit} MB" if mem_limit else f"{rss:4.0f} MB"
        else:
            mem = "- MB"
        label = f"#{self.instances[name]['n']} {short(name)}"
        line = (f"{clock}  {self.name:10s}  {label:15s} pid {pid!s:>6s} up {up:6d}s  {cpu_text}  {mem:>11s}  hc {ms:5.0f} ms  "
                f"{extras(self.name, body)}{flag}")
        row.update(instance=name, pid=pid, uptime=up, rss_mb=rss if rss is not None else "",
                   cpu_pct=round(cpu_pct, 1) if cpu_pct is not None else "", cpu_seconds=cpu_s if cpu_s is not None else "",
                   mem_limit_mb=mem_limit or "", cpu_limit_cores=cpu_limit or "", throttled_s=thr if thr is not None else "")
        return line, row, now

    def in_window(self, now):
        return sum(1 for t in self.recent.values() if now - t <= WINDOW_SECONDS)


def percentile(values, p):
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((p / 100) * (len(ordered) - 1))))]


def main():
    ap = argparse.ArgumentParser(description="Watch the mcp, websearch, wikisearch and ontology apps together.")
    ap.add_argument("--every", type=float, default=5.0, help="seconds between polls of each app")
    ap.add_argument("--duration", type=float, default=0.0, help="seconds to keep polling (0 = until Ctrl+C)")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--only", default="", help="comma-separated apps to watch, e.g. mcp,websearch")
    ap.add_argument("--csv", default="", help="also append every poll to this CSV file, to line up with a load test")
    for app, (url_var, default_url, _, key_var, _) in APPS.items():
        ap.add_argument(f"--{app}-url", default=os.getenv(url_var, default_url), help=f"base URL (or set {url_var})")
    args = ap.parse_args()

    wanted = [x.strip() for x in args.only.split(",") if x.strip()] or list(APPS)
    unknown = [x for x in wanted if x not in APPS]
    if unknown:
        sys.exit(f"Unknown app(s): {', '.join(unknown)}. Choose from {', '.join(APPS)}.")
    watches = []
    for app in wanted:
        _, _, path, key_var, header = APPS[app]
        base = str(getattr(args, f"{app}_url") or "").rstrip("/")
        if not base or urlparse(base).scheme not in ("http", "https"):
            sys.exit(f"No base URL for {app}. Pass --{app}-url or set {APPS[app][0]}.")
        key = os.getenv(key_var, "")
        if not key:
            print(f"Note: {key_var} is not set, so {app} is polled without a key.")
        watches.append(AppWatch(app, base + path, header, key))

    writer = out = None
    if args.csv:
        fresh = not os.path.exists(args.csv) or os.path.getsize(args.csv) == 0
        out = open(args.csv, "a", newline="", encoding="utf-8")
        writer = csv.DictWriter(out, fieldnames=["time", "app", "instance", "pid", "uptime", "rss_mb", "cpu_pct", "cpu_seconds", "mem_limit_mb",
                                                  "cpu_limit_cores", "throttled_s", "ms", "status"])
        if fresh:
            writer.writeheader()

    print(f"Watching {', '.join(w.name for w in watches)} every {args.every:g}s"
          + (f" for {args.duration:g}s" if args.duration else " until Ctrl+C") + ".")
    started = time.time()
    last_rollup = started
    pool = ThreadPoolExecutor(max_workers=len(watches))
    try:
        while not args.duration or time.time() - started < args.duration:
            tick = time.time()
            for line, row, _ in pool.map(lambda w: w.poll(args.timeout), watches):
                print(line, flush=True)
                if writer:
                    writer.writerow(row)
            if out:
                out.flush()
            if time.time() - last_rollup >= WINDOW_SECONDS:
                last_rollup = time.time()
                counts = ", ".join(f"{w.name} {w.in_window(last_rollup)}" for w in watches)
                print(f"          instances seen in the last {WINDOW_SECONDS}s: {counts}", flush=True)
            time.sleep(max(0.0, args.every - (time.time() - tick)))
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        pool.shutdown(wait=False)
        if out:
            out.close()

    print("\nSummary")
    for w in watches:
        lat = w.latencies
        health = (f"health call median {percentile(lat, 50):.0f} ms, p95 {percentile(lat, 95):.0f} ms, slowest {max(lat):.0f} ms"
                  if lat else "no successful polls")
        print(f"  {w.name:10s} polls {w.polls}, failed {w.failures}, instances seen {len(w.instances)}, "
              f"restarts {len(w.restarts)}; {health}")
        for name, rec in w.instances.items():
            cpu = rec["cpu"]
            cpu_text = f"cpu avg {sum(cpu) / len(cpu):.0f}% max {max(cpu):.0f}%" if cpu else "cpu -"
            mem_text = f"memory max {rec['max_rss']:.0f}" + (f" of {rec['mem_limit']} MB" if rec["mem_limit"] else " MB")
            print(f"      #{rec['n']} {short(name):12s} first seen {rec['first']}, polled {rec['polls']}x, restarts {rec['restarts']}, "
                  f"last uptime {rec['uptime']}s, {cpu_text}, {mem_text}"
                  + (f", throttled {rec['throttled']:.1f}s in total" if rec["throttled"] else ""))
        for clock, name, why in w.restarts:
            print(f"      RESTART at {clock}: {short(name)} ({why})")
    quiet = [w.name for w in watches if len(w.instances) > 1 and not w.restarts]
    if quiet:
        print(f"Several instances and no restarts for {', '.join(quiet)}: a memory or uptime change there is most likely "
              "instances being added or removed.")


if __name__ == "__main__":
    main()
