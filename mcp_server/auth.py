"""Optional Supabase JWT authentication for the MCP server.

When ``SUPABASE_JWT_SECRET`` is set, Bearer tokens are validated and the caller
gets an elevated rate limit. When the secret is empty, auth is disabled and all
callers are treated as anonymous (free tier).

Design:
- **Permissive by default** — a missing or invalid token never blocks the
  request; it just falls back to anonymous (lower rate limit).
- **Scope-based gating** — tools can require ``mcp:tools:premium`` via
  ``require_scope()``. Scopes are read from the JWT ``app_metadata.mcp_scopes``
  claim (a JSON array set by Supabase admin or Stripe webhook).
- **Zero extra latency** — JWT signature verification is local (HMAC-SHA256),
  no round-trip to Supabase.
- **Organization API keys** (platform Epic 2 step 12, option B, 2026-11-19) — an
  agent sends its organization's platform API key in ``X-API-Key``; the key
  resolves to the org, and the org's ``api`` plan decides premium: API Pro (or
  Enterprise) unlocks ``mcp:tools:premium``. One plan, billed per org, for the
  REST API and MCP alike. Per-user premium from a JWT keeps working.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import jwt
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server.config import settings

logger = logging.getLogger(__name__)

# Well-known scope names
SCOPE_FREE = "mcp:tools:free"
SCOPE_PREMIUM = "mcp:tools:premium"


@dataclass(frozen=True)
class AuthContext:
    """Caller identity extracted from a validated JWT (or anonymous fallback)."""

    authenticated: bool = False
    user_id: str | None = None
    email: str | None = None
    role: str = "anon"
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({SCOPE_FREE}))
    # Set when the caller used an organization API key.
    org_id: str | None = None
    auth_method: str = "anon"  # "anon", "jwt" or "api_key"
    # The key could not be checked (Supabase unreachable). Free tools still work;
    # a premium tool says "retry" instead of wrongly saying "upgrade".
    auth_unavailable: bool = False

    @property
    def rate_limit_rpm(self) -> int:
        """Return the rate limit tier for this caller."""
        if self.authenticated:
            return settings.rate_limit_authenticated_rpm
        return settings.rate_limit_rpm

    def has_scope(self, scope: str) -> bool:
        """Check if the caller has a specific scope."""
        return scope in self.scopes


# Singleton anonymous context (avoids allocation on every unauthenticated request)
ANONYMOUS = AuthContext()


def auth_enabled() -> bool:
    """True when a JWT secret is configured."""
    return bool(settings.supabase_jwt_secret)


def validate_token(bearer_token: str) -> AuthContext:
    """Validate a Supabase JWT and return the caller context.

    Returns ``ANONYMOUS`` (not raises) when:
    - Auth is disabled (no JWT secret configured)
    - Token is expired, malformed, or has an invalid signature

    This keeps auth **permissive** — bad tokens degrade to anon, never block.
    """
    if not auth_enabled():
        return ANONYMOUS

    try:
        payload: dict[str, Any] = jwt.decode(
            bearer_token,
            settings.supabase_jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.ExpiredSignatureError:
        logger.debug("JWT expired")
        return ANONYMOUS
    except jwt.InvalidTokenError as exc:
        logger.debug("JWT invalid: %s", exc)
        return ANONYMOUS

    # Extract scopes from app_metadata.mcp_scopes (set by admin/webhook)
    app_metadata = payload.get("app_metadata", {})
    raw_scopes = app_metadata.get("mcp_scopes", [])
    if isinstance(raw_scopes, list):
        scopes = frozenset(raw_scopes) | {SCOPE_FREE}
    else:
        scopes = frozenset({SCOPE_FREE})

    return AuthContext(
        authenticated=True,
        user_id=payload.get("sub"),
        email=payload.get("email"),
        role=payload.get("role", "authenticated"),
        scopes=scopes,
        auth_method="jwt",
    )


def require_scope(ctx: AuthContext, scope: str) -> None:
    """Raise ToolError if the caller lacks the required scope.

    Call this at the top of any premium tool handler::

        require_scope(auth_ctx, SCOPE_PREMIUM)

    Anonymous callers and authenticated callers without the scope both get a
    structured error telling them how to upgrade.
    """
    if ctx.has_scope(scope):
        return
    if ctx.auth_unavailable:
        raise ToolError(json.dumps({
            "error": "auth_unavailable",
            "message": "Your API key could not be checked right now.",
            "fix": "Retry in a moment; free tools keep working meanwhile.",
            "retryable": True,
        }))
    raise ToolError(json.dumps({
        "error": "premium_required",
        "message": f"This tool requires the '{scope}' scope",
        "fix": (
            "Send your organization's Geo Insights API key in the X-API-Key header; "
            "premium MCP tools come with the API Pro plan: visit https://geo-insights.nl "
            "(organization settings, billing). A Supabase JWT carrying this scope also works; "
            "see /.well-known/oauth-protected-resource."
        ),
        "authenticated": ctx.authenticated,
        "scopes": sorted(ctx.scopes),
        "required_scope": scope,
    }))


def extract_bearer(authorization: str) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header.

    Returns ``None`` if the header is missing, empty, or not a Bearer scheme.
    """
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


def auth_from_headers(headers: dict[str, str] | None) -> AuthContext:
    """Extract and validate auth from HTTP request headers.

    Convenience for use inside MCP tool handlers via the SDK Context::

        @mcp.tool()
        async def my_premium_tool(x: int, ctx: Context) -> str:
            auth = auth_from_headers(ctx.headers)
            require_scope(auth, SCOPE_PREMIUM)
            ...

    Returns ANONYMOUS if headers are None or no valid Bearer token is present.
    """
    if not headers:
        return ANONYMOUS
    auth_header = headers.get("authorization", "")
    token = extract_bearer(auth_header)
    if not token:
        return ANONYMOUS
    return validate_token(token)


# -- Organization API keys (option B) ----------------------------------------

API_KEY_HEADER = "x-api-key"
# Same format the platform issues: gi_<8 hex>_<secret>.
API_KEY_RE = re.compile(r"^gi_[0-9a-f]{8}_[A-Za-z0-9_-]{20,}$")
PREMIUM_TIERS = frozenset({"pro", "enterprise"})


def _active(expires_at: str | None) -> bool:
    if not expires_at:
        return True
    try:
        return datetime.fromisoformat(expires_at.replace("Z", "+00:00")) > datetime.now(UTC)
    except ValueError:
        return False


class ApiKeyResolver:
    """Organization API key -> org -> that org's ``api`` plan.

    Uses the platform's own rules: ``public.resolve_api_key`` (migration 047;
    executable by service_role only) and ``public.org_subscriptions`` (active or
    trialing, not expired). The key never leaves this process: only its sha256 is
    sent, as the platform stores it.

    Results are cached for ``ttl`` seconds, unknown keys included, so a burst of
    calls (or of made-up keys) costs one lookup. A revoked key or a cancelled plan
    therefore takes up to ``ttl`` seconds to stop working. The cache is bounded,
    oldest first, so many distinct keys cannot grow it without limit. Failures are
    not cached: the next call tries again.
    """

    def __init__(
        self,
        url: str = settings.supabase_project_url,
        service_key: str = settings.supabase_service_role_key,
        *,
        ttl: float = 60.0,
        max_entries: int = 10_000,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.enabled = bool(service_key)
        self._client = httpx.AsyncClient(
            base_url=f"{url.rstrip('/')}/rest/v1",
            headers={"apikey": service_key, "Authorization": f"Bearer {service_key}",
                     "Accept": "application/json"},
            timeout=httpx.Timeout(5.0),
            transport=transport,
        )
        self._ttl = ttl
        self._max = max_entries
        self._clock = clock
        self._cache: OrderedDict[str, tuple[float, AuthContext]] = OrderedDict()

    def _remember(self, key_hash: str, ctx: AuthContext) -> AuthContext:
        self._cache[key_hash] = (self._clock() + self._ttl, ctx)
        self._cache.move_to_end(key_hash)
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)
        return ctx

    async def resolve(self, raw_key: str) -> AuthContext:
        raw_key = raw_key.strip()
        if not self.enabled or not API_KEY_RE.match(raw_key):
            return ANONYMOUS
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        hit = self._cache.get(key_hash)
        if hit is not None and hit[0] > self._clock():
            return hit[1]
        try:
            resp = await self._client.post("/rpc/resolve_api_key", json={"p_key_hash": key_hash})
            resp.raise_for_status()
            rows = resp.json()
            row = rows[0] if isinstance(rows, list) and rows else None
            if row is None or not row.get("org_id") or "read" not in (row.get("scopes") or []):
                return self._remember(key_hash, ANONYMOUS)
            org_id = str(row["org_id"])
            subs = await self._client.get("/org_subscriptions", params={
                "select": "tier,expires_at", "org_id": f"eq.{org_id}", "product": "eq.api",
                "status": "in.(active,trialing)",
            })
            subs.raise_for_status()
            tiers = {s.get("tier") for s in subs.json() if _active(s.get("expires_at"))}
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("API key check failed: %s", type(exc).__name__)
            return AuthContext(auth_unavailable=True)
        premium = bool(tiers & PREMIUM_TIERS)
        return self._remember(key_hash, AuthContext(
            authenticated=True,
            role="api_key",
            org_id=org_id,
            auth_method="api_key",
            scopes=frozenset({SCOPE_FREE, SCOPE_PREMIUM} if premium else {SCOPE_FREE}),
        ))

    async def aclose(self) -> None:
        await self._client.aclose()


async def auth_from_request(headers: Mapping[str, str] | None, resolver: ApiKeyResolver) -> AuthContext:
    """The caller's context: an organization API key if one is sent, else a JWT, else anonymous.

    Async because checking a key is a network call; running that inside a sync
    helper on the event loop would stall every concurrent request.
    """
    if not headers:
        return ANONYMOUS
    key = headers.get(API_KEY_HEADER) or headers.get("X-API-Key")
    if key:
        return await resolver.resolve(key)
    return auth_from_headers(dict(headers))

