"""Request limits for the public MCP endpoint.

Hosted AI assistants do not call this server from their users' devices. Claude and ChatGPT connect from the
provider's own cloud, and the Foundry agent connects from the project's data proxy. Every visitor behind one
provider therefore arrives from the same few addresses, so a per-address limit sized for one person throttles
all of them at once. This module keeps two kinds of per-address bucket:

  public   Any address that is not trusted. Limit: MCP_RATE_LIMIT_PER_IP.
  trusted  Addresses (or callers) known to be an AI provider or our own Foundry project. Limit:
           MCP_RATE_LIMIT_TRUSTED_PER_IP, much higher, because one address stands for many people.

A caller is trusted when ANY of these is true:
  1. Its address is in MCP_TRUSTED_RANGES (comma-separated CIDRs; default is Anthropic's published outbound range).
  2. Its address is in a range published in a JSON manifest listed in MCP_TRUSTED_RANGE_URLS (default is OpenAI's
     ChatGPT connectors manifest). Manifests are fetched at start and refreshed every MCP_TRUSTED_REFRESH_SECONDS.
  3. It sends the shared key in the header MCP_TRUSTED_KEY_HEADER, and the key equals MCP_TRUSTED_KEY. Use this for
     the Foundry project: Foundry does not publish a stable address for its tool calls, but a Foundry project
     connection can send a custom header, so the agent identifies itself with the key instead of an address.
  4. Its User-Agent matches MCP_TRUSTED_USER_AGENTS (a regular expression; off by default). A User-Agent can be
     faked, so this only decides which bucket a caller counts in. It never grants access to anything.

Trust only changes which per-address limit applies. It does not authenticate anyone, and the global limit
(MCP_RATE_LIMIT_GLOBAL) still counts every request, trusted or not.

Counts are in memory, so each running instance applies its own limits.
"""
from __future__ import annotations

import hmac
import ipaddress
import re
import sys
import threading
import time
from collections import deque


def _log(message):
    print(message, file=sys.stderr, flush=True)


def parse_networks(items):
    """Turn CIDR strings into network objects. A bad entry is logged and skipped, never fatal."""
    networks = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            _log(f"rate_limit: ignoring invalid range {text!r}")
    return networks


def networks_from_manifest(data):
    """Read the ranges from a provider manifest shaped {"creationTime": ..., "prefixes": [{"ipv4Prefix": ...}]}."""
    items = []
    prefixes = data.get("prefixes") if isinstance(data, dict) else None
    for entry in prefixes or []:
        if isinstance(entry, dict):
            items.extend(entry.get(key) for key in ("ipv4Prefix", "ipv6Prefix") if entry.get(key))
        elif isinstance(entry, str):
            items.append(entry)
    return parse_networks(items)


class TrustedRanges:
    """The addresses that count in the trusted bucket: fixed ranges plus ranges read from manifests."""

    def __init__(self, fixed=(), manifest_urls=(), refresh_seconds=86400, fetch=None):
        self.fixed = parse_networks(fixed)
        self.manifest_urls = [u for u in (str(x).strip() for x in manifest_urls) if u]
        self.refresh_seconds = refresh_seconds
        self._fetch = fetch or self._fetch_json
        self._from_manifests = {}  # url -> list of networks; a failed refresh keeps the last good list
        self._lock = threading.Lock()

    @staticmethod
    def _fetch_json(url):
        import requests  # imported here so this module has no third-party import at load time

        response = requests.get(url, timeout=10, headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json()

    def refresh(self):
        """Fetch every manifest. A manifest that fails keeps its last good ranges. Returns the number of ranges."""
        for url in self.manifest_urls:
            try:
                networks = networks_from_manifest(self._fetch(url))
            except Exception as exc:
                _log(f"rate_limit: could not refresh {url} ({type(exc).__name__}); keeping the previous ranges")
                continue
            if not networks:
                _log(f"rate_limit: {url} held no usable ranges; keeping the previous ranges")
                continue
            with self._lock:
                self._from_manifests[url] = networks
        return self.count()

    def count(self):
        with self._lock:
            return len(self.fixed) + sum(len(v) for v in self._from_manifests.values())

    def start(self):
        """Load the manifests now, without delaying startup, and again every refresh_seconds."""
        if not self.manifest_urls:
            return None

        def work():
            while True:
                total = self.refresh()
                _log(f"rate_limit: {total} trusted ranges loaded")
                if self.refresh_seconds <= 0:
                    return
                time.sleep(self.refresh_seconds)

        thread = threading.Thread(target=work, name="trusted-ranges", daemon=True)
        thread.start()
        return thread

    def contains(self, address):
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        with self._lock:
            groups = [self.fixed, *self._from_manifests.values()]
        return any(ip in network for group in groups for network in group if ip.version == network.version)


class RateLimitMiddleware:
    """Per-address and global request limits that answer HTTP 429. See the module notes for who is trusted."""

    def __init__(self, app, per_ip, global_limit, window=60.0, trusted=None, trusted_per_ip=0,
                 trusted_key="", key_header="x-mcp-client-key", trusted_agents=None):
        self.app = app
        self.per_ip = per_ip
        self.global_limit = global_limit
        self.window = window
        self.trusted = trusted
        self.trusted_per_ip = trusted_per_ip
        self.trusted_key = str(trusted_key or "")
        self.key_header = key_header.lower().encode("latin-1")
        self.trusted_agents = re.compile(trusted_agents, re.IGNORECASE) if trusted_agents else None
        self.hits = {}
        self.all_hits = deque()
        self.last_logged = {}

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

    def is_trusted(self, scope, ip):
        headers = {name: value for name, value in scope.get("headers", [])}
        if self.trusted_key:
            sent = headers.get(self.key_header)
            if sent is not None and hmac.compare_digest(sent, self.trusted_key.encode("utf-8")):
                return True
        if self.trusted is not None and self.trusted.contains(ip):
            return True
        if self.trusted_agents is not None:
            agent = headers.get(b"user-agent", b"").decode("latin-1")
            if self.trusted_agents.search(agent):
                return True
        return False

    def _note_limited(self, key, bucket, ip, limit, now):
        """One log line per address per window, so a flood cannot fill the log."""
        if now - self.last_logged.get(key, -self.window) < self.window:
            return
        if len(self.last_logged) > 5000:
            self.last_logged = {k: v for k, v in self.last_logged.items() if now - v < self.window}
        self.last_logged[key] = now
        _log(f"rate_limited bucket={bucket} ip={ip} limit_per_minute={limit}")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in ("/healthz", "/"):
            await self.app(scope, receive, send)
            return
        now = time.monotonic()
        while self.all_hits and now - self.all_hits[0] > self.window:
            self.all_hits.popleft()
        if len(self.hits) > 5000:
            self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] <= self.window}

        ip = self.client_ip(scope)
        trusted = self.is_trusted(scope, ip)
        bucket, limit = ("trusted", self.trusted_per_ip) if trusted else ("public", self.per_ip)
        key = f"{bucket}:{ip}"
        recent = self.hits.setdefault(key, deque())
        while recent and now - recent[0] > self.window:
            recent.popleft()

        over_address = bool(limit) and len(recent) >= limit
        over_global = bool(self.global_limit) and len(self.all_hits) >= self.global_limit
        if over_address or over_global:
            self._note_limited(key, bucket if over_address else "global", ip,
                               limit if over_address else self.global_limit, now)
            await send({"type": "http.response.start", "status": 429, "headers": [
                (b"content-type", b"application/json"), (b"retry-after", b"30")]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"Too many requests. Please try again shortly."}'})
            return
        recent.append(now)
        self.all_hits.append(now)
        await self.app(scope, receive, send)
