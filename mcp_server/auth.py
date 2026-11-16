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
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

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
    raise ToolError(json.dumps({
        "error": "premium_required",
        "message": f"This tool requires the '{scope}' scope",
        "fix": (
            "Authenticate with a Supabase JWT that includes this scope. "
            "Visit https://geo-insights.nl to subscribe, or check "
            "/.well-known/oauth-protected-resource for auth details."
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
