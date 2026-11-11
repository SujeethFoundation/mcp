"""Tests for rate_limit.py: the public and trusted buckets, the shared key, manifests and the global limit."""
import asyncio

from rate_limit import RateLimitMiddleware, TrustedRanges, networks_from_manifest, parse_networks

ANTHROPIC_IP = "160.79.105.7"      # inside 160.79.104.0/21
OPENAI_IP = "4.151.71.180"         # inside 4.151.71.176/28 from the sample manifest
STRANGER_IP = "203.0.113.9"


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def call(mw, ip="203.0.113.9", path="/mcp", headers=None, via_xff=True):
    """Send one request through the middleware and return the status it answered with."""
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "path": path, "headers": hdrs, "client": ("10.0.0.1", 1234)}
    if via_xff:
        scope["headers"].append((b"x-forwarded-for", ip.encode()))
    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, None, send))
    return sent[0]["status"]


def make(per_ip=3, global_limit=0, trusted_per_ip=10, fixed=("160.79.104.0/21",), **kwargs):
    ranges = TrustedRanges(fixed=fixed)
    return RateLimitMiddleware(_ok_app, per_ip, global_limit, trusted=ranges, trusted_per_ip=trusted_per_ip, **kwargs)


def test_public_address_is_limited_at_the_public_limit():
    mw = make(per_ip=3)
    assert [call(mw, STRANGER_IP) for _ in range(4)] == [200, 200, 200, 429]


def test_other_addresses_are_not_affected_by_one_address_being_limited():
    mw = make(per_ip=1)
    assert call(mw, STRANGER_IP) == 200
    assert call(mw, STRANGER_IP) == 429
    assert call(mw, "203.0.113.10") == 200


def test_anthropic_range_gets_the_trusted_limit():
    mw = make(per_ip=3, trusted_per_ip=10)
    statuses = [call(mw, ANTHROPIC_IP) for _ in range(11)]
    assert statuses[:10] == [200] * 10 and statuses[10] == 429


def test_trusted_and_public_buckets_do_not_share_counts():
    mw = make(per_ip=2, trusted_per_ip=5)
    assert [call(mw, STRANGER_IP) for _ in range(3)] == [200, 200, 429]
    assert call(mw, ANTHROPIC_IP) == 200


def test_trusted_limit_zero_means_no_per_address_limit_for_trusted_callers():
    mw = make(per_ip=1, trusted_per_ip=0)
    assert all(call(mw, ANTHROPIC_IP) == 200 for _ in range(50))
    assert [call(mw, STRANGER_IP) for _ in range(2)] == [200, 429]


def test_global_limit_still_counts_trusted_callers():
    mw = make(per_ip=100, trusted_per_ip=100, global_limit=4)
    assert [call(mw, ANTHROPIC_IP) for _ in range(5)] == [200, 200, 200, 200, 429]
    assert call(mw, STRANGER_IP) == 429


def test_shared_key_makes_a_caller_trusted_even_from_an_unknown_address():
    mw = make(per_ip=1, trusted_per_ip=5, trusted_key="s3cret")
    headers = {"x-mcp-client-key": "s3cret"}
    assert all(call(mw, STRANGER_IP, headers=headers) == 200 for _ in range(5))
    assert call(mw, STRANGER_IP, headers=headers) == 429


def test_wrong_or_missing_key_does_not_make_a_caller_trusted():
    mw = make(per_ip=1, trusted_per_ip=5, trusted_key="s3cret")
    assert call(mw, STRANGER_IP, headers={"x-mcp-client-key": "wrong"}) == 200
    assert call(mw, STRANGER_IP, headers={"x-mcp-client-key": "wrong"}) == 429
    assert call(mw, "203.0.113.20") == 200
    assert call(mw, "203.0.113.20") == 429


def test_a_key_header_is_ignored_when_no_key_is_configured():
    mw = make(per_ip=1, trusted_per_ip=5, trusted_key="")
    assert call(mw, STRANGER_IP, headers={"x-mcp-client-key": ""}) == 200
    assert call(mw, STRANGER_IP, headers={"x-mcp-client-key": ""}) == 429


def test_custom_key_header_name():
    mw = make(per_ip=1, trusted_per_ip=5, trusted_key="k", key_header="X-Foundry-Key")
    assert [call(mw, STRANGER_IP, headers={"X-Foundry-Key": "k"}) for _ in range(3)] == [200, 200, 200]


def test_user_agent_rule_moves_a_caller_to_the_trusted_bucket():
    mw = make(per_ip=1, trusted_per_ip=5, trusted_agents=r"openai-mcp")
    headers = {"user-agent": "openai-mcp/1.0.0"}
    assert all(call(mw, STRANGER_IP, headers=headers) == 200 for _ in range(5))
    assert call(mw, "203.0.113.30", headers={"user-agent": "curl/8"}) == 200
    assert call(mw, "203.0.113.30", headers={"user-agent": "curl/8"}) == 429


def test_forged_first_forwarded_for_entry_does_not_help_a_stranger():
    mw = make(per_ip=1, trusted_per_ip=5)
    headers = {"x-forwarded-for": f"{ANTHROPIC_IP}, {STRANGER_IP}"}
    assert call(mw, STRANGER_IP, headers=headers, via_xff=False) == 200
    assert call(mw, STRANGER_IP, headers=headers, via_xff=False) == 429


def test_health_routes_are_never_limited():
    mw = make(per_ip=1, global_limit=1)
    assert all(call(mw, STRANGER_IP, path="/healthz") == 200 for _ in range(5))
    assert all(call(mw, STRANGER_IP, path="/") == 200 for _ in range(5))


def test_window_expires():
    mw = make(per_ip=1)
    mw.window = 0.05
    assert call(mw, STRANGER_IP) == 200
    assert call(mw, STRANGER_IP) == 429
    import time
    time.sleep(0.08)
    assert call(mw, STRANGER_IP) == 200


def test_unparseable_address_is_treated_as_public_and_does_not_crash():
    mw = make(per_ip=1)
    assert call(mw, "not-an-ip") == 200
    assert call(mw, "not-an-ip") == 429


# ---- ranges and manifests ----

SAMPLE = {"creationTime": "2026-07-08T21:09:35Z", "prefixes": [
    {"ipv4Prefix": "4.151.71.176/28"}, {"ipv6Prefix": "2603:1030::/64"}, {"ipv4Prefix": "garbage"}, "172.170.1.80/28", 7]}


def test_manifest_parsing_reads_v4_v6_and_skips_bad_entries():
    nets = networks_from_manifest(SAMPLE)
    assert {str(n) for n in nets} == {"4.151.71.176/28", "2603:1030::/64", "172.170.1.80/28"}
    assert networks_from_manifest({"nothing": 1}) == []
    assert networks_from_manifest(None) == []


def test_parse_networks_skips_invalid():
    assert [str(n) for n in parse_networks(["160.79.104.0/21", "", "nope", "10.0.0.1"])] == ["160.79.104.0/21", "10.0.0.1/32"]


def test_ranges_from_a_manifest_are_trusted_after_refresh():
    ranges = TrustedRanges(manifest_urls=["https://example.test/m.json"], fetch=lambda url: SAMPLE)
    assert not ranges.contains(OPENAI_IP)
    assert ranges.refresh() == 3
    assert ranges.contains(OPENAI_IP) and ranges.contains("172.170.1.85") and ranges.contains("2603:1030::5")
    assert not ranges.contains(STRANGER_IP)


def test_a_failed_refresh_keeps_the_previous_ranges():
    state = {"fail": False}

    def fetch(url):
        if state["fail"]:
            raise RuntimeError("network down")
        return SAMPLE

    ranges = TrustedRanges(manifest_urls=["https://example.test/m.json"], fetch=fetch)
    ranges.refresh()
    state["fail"] = True
    ranges.refresh()
    assert ranges.contains(OPENAI_IP)


def test_an_empty_manifest_does_not_wipe_the_previous_ranges():
    data = {"value": SAMPLE}
    ranges = TrustedRanges(manifest_urls=["https://example.test/m.json"], fetch=lambda url: data["value"])
    ranges.refresh()
    data["value"] = {"prefixes": []}
    ranges.refresh()
    assert ranges.contains(OPENAI_IP)


def test_fixed_ranges_work_without_any_manifest():
    ranges = TrustedRanges(fixed=["160.79.104.0/21"])
    assert ranges.contains("160.79.111.255") and not ranges.contains("160.79.112.0")
    assert ranges.start() is None


def test_middleware_uses_manifest_ranges():
    ranges = TrustedRanges(manifest_urls=["https://example.test/m.json"], fetch=lambda url: SAMPLE)
    ranges.refresh()
    mw = RateLimitMiddleware(_ok_app, 1, 0, trusted=ranges, trusted_per_ip=4)
    assert [call(mw, OPENAI_IP) for _ in range(5)] == [200, 200, 200, 200, 429]
