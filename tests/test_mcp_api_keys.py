"""MCP premium through organization API keys (platform Epic 2 step 12, option B).

protects: an org's API key unlocks premium MCP tools exactly when the org has API Pro
          (or Enterprise); free tools stay anonymous; keys are only ever sent hashed;
          lookups are cached and bounded; a Supabase outage says "retry", not "upgrade".
fails_when: a free org gets premium, a paying org does not, the raw key is sent,
            revocation never takes effect, or rank_measures ignores the key.
no_claim: the real Supabase RPC (covered by the platform's migration 047 tests).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server.auth import (
    ANONYMOUS,
    SCOPE_PREMIUM,
    ApiKeyResolver,
    AuthContext,
    auth_from_request,
    require_scope,
)

PRO_KEY = "gi_0123abcd_" + "p" * 30
FREE_KEY = "gi_89efabcd_" + "f" * 30
WRITE_ONLY_KEY = "gi_00000000_" + "w" * 30
UNKNOWN_KEY = "gi_ffffffff_" + "u" * 30
ORGS = {
    hashlib.sha256(PRO_KEY.encode()).hexdigest(): ("org-pro", ["read"]),
    hashlib.sha256(FREE_KEY.encode()).hexdigest(): ("org-free", ["read"]),
    hashlib.sha256(WRITE_ONLY_KEY.encode()).hexdigest(): ("org-pro", ["write"]),
}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def supabase(subscriptions: dict[str, list[dict]] | None = None, fail: bool = False):
    """A fake PostgREST: resolve_api_key by hash, org_subscriptions by org."""
    subs = subscriptions if subscriptions is not None else {"org-pro": [{"tier": "pro", "expires_at": None}]}
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if fail:
            return httpx.Response(503)
        if request.url.path.endswith("/rpc/resolve_api_key"):
            key_hash = json.loads(request.content)["p_key_hash"]
            if key_hash not in ORGS:
                return httpx.Response(200, json=[])
            org, scopes = ORGS[key_hash]
            return httpx.Response(200, json=[{"key_id": "k", "org_id": org, "project_id": None, "scopes": scopes}])
        if request.url.path.endswith("/org_subscriptions"):
            org = request.url.params["org_id"].removeprefix("eq.")
            assert request.url.params["product"] == "eq.api"
            return httpx.Response(200, json=subs.get(org, []))
        return httpx.Response(404)

    return httpx.MockTransport(handler), calls


def resolver(transport, clock=None, **kw) -> ApiKeyResolver:
    return ApiKeyResolver("https://sb.example.test", "service-role-test", transport=transport,
                          clock=clock or Clock(), **kw)


@pytest.mark.asyncio
async def test_pro_org_key_unlocks_premium():
    transport, _ = supabase()
    ctx = await resolver(transport).resolve(PRO_KEY)
    assert ctx.has_scope(SCOPE_PREMIUM) and ctx.org_id == "org-pro" and ctx.auth_method == "api_key"
    require_scope(ctx, SCOPE_PREMIUM)  # does not raise


@pytest.mark.asyncio
async def test_free_org_key_is_authenticated_but_not_premium():
    transport, _ = supabase()
    ctx = await resolver(transport).resolve(FREE_KEY)
    assert ctx.authenticated and not ctx.has_scope(SCOPE_PREMIUM)
    with pytest.raises(ToolError) as exc:
        require_scope(ctx, SCOPE_PREMIUM)
    body = json.loads(str(exc.value))
    assert body["error"] == "premium_required" and "X-API-Key" in body["fix"] and "API Pro" in body["fix"]


@pytest.mark.asyncio
async def test_enterprise_counts_and_an_expired_pro_does_not():
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    transport, _ = supabase({"org-pro": [{"tier": "pro", "expires_at": past}]})
    assert not (await resolver(transport).resolve(PRO_KEY)).has_scope(SCOPE_PREMIUM)
    transport, _ = supabase({"org-pro": [{"tier": "enterprise", "expires_at": future}]})
    assert (await resolver(transport).resolve(PRO_KEY)).has_scope(SCOPE_PREMIUM)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [UNKNOWN_KEY, WRITE_ONLY_KEY, "not-a-key", "gi_short_x"])
async def test_unusable_keys_fall_back_to_anonymous(key):
    """Permissive like the JWT path: a bad key never blocks free tools, it just isn't premium."""
    transport, _ = supabase()
    assert await resolver(transport).resolve(key) is ANONYMOUS


@pytest.mark.asyncio
async def test_malformed_keys_are_not_even_looked_up():
    transport, calls = supabase()
    await resolver(transport).resolve("Bearer something-else")
    assert calls == []


@pytest.mark.asyncio
async def test_only_the_hash_is_sent_never_the_key():
    transport, calls = supabase()
    await resolver(transport).resolve(PRO_KEY)
    sent = b"".join(c.content for c in calls) + str([str(c.url) for c in calls]).encode()
    assert PRO_KEY.encode() not in sent
    assert hashlib.sha256(PRO_KEY.encode()).hexdigest().encode() in sent


@pytest.mark.asyncio
async def test_results_are_cached_for_the_ttl_including_unknown_keys():
    clock = Clock()
    transport, calls = supabase()
    r = resolver(transport, clock=clock, ttl=60)
    for _ in range(5):
        await r.resolve(PRO_KEY)
        await r.resolve(UNKNOWN_KEY)
    assert len(calls) == 3  # pro: rpc + subscriptions; unknown: rpc only
    clock.now += 61
    await r.resolve(PRO_KEY)
    assert len(calls) == 5, "after the ttl the plan is checked again (a cancelled plan takes effect)"


@pytest.mark.asyncio
async def test_cache_is_bounded():
    transport, _ = supabase()
    r = resolver(transport, max_entries=3)
    for i in range(10):
        await r.resolve(f"gi_{i:08x}_" + "x" * 30)
    assert len(r._cache) <= 3


@pytest.mark.asyncio
async def test_supabase_outage_says_retry_and_is_not_cached():
    transport, calls = supabase(fail=True)
    r = resolver(transport)
    ctx = await r.resolve(PRO_KEY)
    assert ctx.auth_unavailable and not ctx.has_scope(SCOPE_PREMIUM)
    with pytest.raises(ToolError) as exc:
        require_scope(ctx, SCOPE_PREMIUM)
    assert json.loads(str(exc.value))["error"] == "auth_unavailable"
    await r.resolve(PRO_KEY)
    assert len(calls) == 2, "a failure must not be cached"


@pytest.mark.asyncio
async def test_without_a_service_key_keys_are_ignored():
    transport, calls = supabase()
    r = ApiKeyResolver("https://sb.example.test", "", transport=transport)
    assert await r.resolve(PRO_KEY) is ANONYMOUS and calls == []


@pytest.mark.asyncio
async def test_api_key_header_wins_and_jwt_path_still_works():
    transport, _ = supabase()
    r = resolver(transport)
    ctx = await auth_from_request({"x-api-key": PRO_KEY, "authorization": "Bearer junk"}, r)
    assert ctx.auth_method == "api_key"
    with patch("mcp_server.auth.auth_from_headers", return_value=AuthContext(authenticated=True)) as jwt_path:
        await auth_from_request({"authorization": "Bearer t"}, r)
    jwt_path.assert_called_once()
    assert await auth_from_request(None, r) is ANONYMOUS


@pytest.mark.asyncio
async def test_rank_measures_honours_an_org_key_end_to_end():
    """Through the real tool: gating on, a Pro key passes, a free key is refused."""
    from mcp_server import server

    transport, _ = supabase()
    sentinel = object()
    gated = dataclasses.replace(server.settings, premium_gating_enabled=True)  # settings is frozen
    with patch.object(server, "settings", gated), \
            patch.object(server, "_key_resolver", resolver(transport)), \
            patch.object(server.catalog_tools, "rank_measures", return_value=sentinel) as ranked:

        async def call(key: str):
            return await server.rank_measures("GM0599", ctx=SimpleNamespace(headers={"x-api-key": key}))

        # catalog_tools.rank_measures is awaited, so return an awaitable sentinel
        async def fake(*args, **kwargs):
            return sentinel

        ranked.side_effect = fake
        assert await call(PRO_KEY) is sentinel
        with pytest.raises(ToolError) as exc:
            await call(FREE_KEY)
        assert json.loads(str(exc.value))["error"] == "premium_required"
