"""Optional Supabase JWT authentication for the MCP server.

When ``SUPABASE_JWT_SECRET`` is set, Bearer tokens are validated and the caller
gets an elevated rate limit. When the secret is empty, auth is disabled and all
callers are treated as anonymous (free tier).

Design:
- **Permissive by default** — a missing or invalid token never blocks the
  request; it just falls back to anonymous (lower rate limit).
- **No scopes yet** — Phase 2 prep only. Scope-based gating (``mcp:tools:premium``)
  is a Phase 3 concern.
- **Zero extra latency** — JWT signature verification is local (HMAC-SHA256),
  no round-trip to Supabase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import jwt

from mcp_server.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthContext:
    """Caller identity extracted from a validated JWT (or anonymous fallback)."""

    authenticated: bool = False
    user_id: str | None = None
    email: str | None = None
    role: str = "anon"

    @property
    def rate_limit_rpm(self) -> int:
        """Return the rate limit tier for this caller."""
        if self.authenticated:
            return settings.rate_limit_authenticated_rpm
        return settings.rate_limit_rpm


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

    return AuthContext(
        authenticated=True,
        user_id=payload.get("sub"),
        email=payload.get("email"),
        role=payload.get("role", "authenticated"),
    )


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
