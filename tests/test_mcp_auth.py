"""Tests for MCP server auth module.

protects: JWT validation, bearer extraction, tiered rate limits, OAuth discovery
fails_when: valid tokens rejected, invalid tokens accepted as authenticated,
            tiered rate limits not applied, discovery endpoint wrong shape
no_claim: actual Supabase auth flow, real network behavior
"""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt
import pytest

# Shared test secret (never used in production)
_TEST_SECRET = "test-jwt-secret-for-unit-tests-only-32chars!"


def _make_token(
    sub: str = "user-123",
    email: str = "test@example.com",
    role: str = "authenticated",
    exp_delta: int = 3600,
    secret: str = _TEST_SECRET,
    audience: str = "authenticated",
    mcp_scopes: list[str] | None = None,
) -> str:
    """Create a test Supabase-style JWT."""
    payload: dict[str, object] = {
        "sub": sub,
        "email": email,
        "role": role,
        "aud": audience,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
    }
    if mcp_scopes is not None:
        payload["app_metadata"] = {"mcp_scopes": mcp_scopes}
    return jwt.encode(payload, secret, algorithm="HS256")


# -- extract_bearer --------------------------------------------------------


def test_extract_bearer_valid():
    from mcp_server.auth import extract_bearer

    token = "eyJhbGciOiJIUzI1NiJ9.test"
    assert extract_bearer(f"Bearer {token}") == token


def test_extract_bearer_case_insensitive():
    from mcp_server.auth import extract_bearer

    assert extract_bearer("bearer tok123") == "tok123"
    assert extract_bearer("BEARER tok123") == "tok123"


def test_extract_bearer_missing():
    from mcp_server.auth import extract_bearer

    assert extract_bearer("") is None
    assert extract_bearer("Basic dXNlcjpwYXNz") is None
    assert extract_bearer("just-a-token") is None


# -- validate_token --------------------------------------------------------


def test_validate_token_disabled():
    """When JWT secret is empty, all tokens return ANONYMOUS."""
    from mcp_server.auth import ANONYMOUS, validate_token

    with patch("mcp_server.auth.settings") as mock_settings:
        mock_settings.supabase_jwt_secret = ""
        result = validate_token("any-token")
        assert result is ANONYMOUS
        assert not result.authenticated


def test_validate_token_valid():
    """A valid Supabase JWT returns an authenticated context."""
    from mcp_server.auth import validate_token

    token = _make_token()
    with patch("mcp_server.auth.settings") as mock_settings:
        mock_settings.supabase_jwt_secret = _TEST_SECRET
        result = validate_token(token)
        assert result.authenticated
        assert result.user_id == "user-123"
        assert result.email == "test@example.com"
        assert result.role == "authenticated"


def test_validate_token_expired():
    """An expired JWT returns ANONYMOUS."""
    from mcp_server.auth import ANONYMOUS, validate_token

    token = _make_token(exp_delta=-3600)  # expired 1h ago
    with patch("mcp_server.auth.settings") as mock_settings:
        mock_settings.supabase_jwt_secret = _TEST_SECRET
        result = validate_token(token)
        assert result is ANONYMOUS


def test_validate_token_wrong_secret():
    """A JWT signed with a different secret returns ANONYMOUS."""
    from mcp_server.auth import ANONYMOUS, validate_token

    token = _make_token(secret="wrong-secret-xxxxxxxxxxxxxxxxxxxxxxxx")
    with patch("mcp_server.auth.settings") as mock_settings:
        mock_settings.supabase_jwt_secret = _TEST_SECRET
        result = validate_token(token)
        assert result is ANONYMOUS


def test_validate_token_wrong_audience():
    """A JWT with the wrong audience claim returns ANONYMOUS."""
    from mcp_server.auth import ANONYMOUS, validate_token

    token = _make_token(audience="wrong-audience")
    with patch("mcp_server.auth.settings") as mock_settings:
        mock_settings.supabase_jwt_secret = _TEST_SECRET
        result = validate_token(token)
        assert result is ANONYMOUS


def test_validate_token_malformed():
    """A garbage string returns ANONYMOUS."""
    from mcp_server.auth import ANONYMOUS, validate_token

    with patch("mcp_server.auth.settings") as mock_settings:
        mock_settings.supabase_jwt_secret = _TEST_SECRET
        result = validate_token("not.a.jwt")
        assert result is ANONYMOUS


# -- AuthContext.rate_limit_rpm --------------------------------------------


def test_auth_context_rate_limit_tiers():
    """Authenticated users get a higher rate limit."""
    from mcp_server.auth import ANONYMOUS, AuthContext

    with patch("mcp_server.auth.settings") as mock_settings:
        mock_settings.rate_limit_rpm = 100
        mock_settings.rate_limit_authenticated_rpm = 500
        assert ANONYMOUS.rate_limit_rpm == 100
        authed = AuthContext(authenticated=True, user_id="u1")
        assert authed.rate_limit_rpm == 500


# -- Tiered rate limiter ---------------------------------------------------


def test_rate_limiter_tiered():
    """Authenticated callers get a separate, higher-capacity bucket."""
    from mcp_server.middleware import TokenBucketRateLimiter

    limiter = TokenBucketRateLimiter(rpm=2, authenticated_rpm=5)
    ip = "1.2.3.4"

    # Anonymous: 2 RPM → 2 allowed, then blocked
    assert limiter.check(ip, authenticated=False) == (True, 0)
    assert limiter.check(ip, authenticated=False) == (True, 0)
    allowed, _ = limiter.check(ip, authenticated=False)
    assert not allowed

    # Authenticated same IP: separate bucket, 5 RPM
    assert limiter.check(ip, authenticated=True) == (True, 0)
    assert limiter.check(ip, authenticated=True) == (True, 0)
    assert limiter.check(ip, authenticated=True) == (True, 0)
    assert limiter.check(ip, authenticated=True) == (True, 0)
    assert limiter.check(ip, authenticated=True) == (True, 0)
    allowed, _ = limiter.check(ip, authenticated=True)
    assert not allowed


# -- RequestLogger auth fields ---------------------------------------------


def test_request_logger_includes_auth_fields(capsys):
    """Logger records authenticated flag and user_id."""
    import json

    from mcp_server.middleware import RequestLogger

    logger = RequestLogger()
    logger.log("test_tool", 42, "1.2.3.4", "TestAgent", "ok", authenticated=True, user_id="u-abc")
    output = capsys.readouterr().out.strip()
    record = json.loads(output)
    assert record["authenticated"] is True
    assert record["user_id"] == "u-abc"


def test_request_logger_anon_no_user_id(capsys):
    """Anonymous calls don't include user_id in the log."""
    import json

    from mcp_server.middleware import RequestLogger

    logger = RequestLogger()
    logger.log("test_tool", 42, "1.2.3.4", "TestAgent", "ok")
    output = capsys.readouterr().out.strip()
    record = json.loads(output)
    assert record["authenticated"] is False
    assert "user_id" not in record


# -- OAuth Protected Resource Metadata -------------------------------------


@pytest.mark.asyncio
async def test_oauth_discovery_endpoint():
    """The .well-known endpoint returns RFC 9728 metadata."""
    # Build a minimal Starlette app with just the discovery endpoint
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from mcp_server.oauth_discovery import oauth_protected_resource_endpoint

    test_app = Starlette(routes=[
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource_endpoint),
    ])
    client = TestClient(test_app)
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    data = resp.json()
    assert data["resource"] == "https://mcp.geo-insights.nl"
    assert "authorization_servers" in data
    assert len(data["authorization_servers"]) == 1
    assert "supabase" in data["authorization_servers"][0]
    assert "mcp:tools:free" in data["scopes_supported"]
    assert "mcp:tools:premium" in data["scopes_supported"]
    assert data["bearer_methods_supported"] == ["header"]
    assert "Cache-Control" in resp.headers


# -- Scope extraction from JWT app_metadata --------------------------------


def test_validate_token_extracts_scopes():
    """Scopes from app_metadata.mcp_scopes are extracted into AuthContext."""
    from mcp_server.auth import SCOPE_FREE, SCOPE_PREMIUM, validate_token

    token = _make_token(mcp_scopes=[SCOPE_FREE, SCOPE_PREMIUM])
    with patch("mcp_server.auth.settings") as mock_settings:
        mock_settings.supabase_jwt_secret = _TEST_SECRET
        result = validate_token(token)
        assert result.authenticated
        assert result.has_scope(SCOPE_FREE)
        assert result.has_scope(SCOPE_PREMIUM)


def test_validate_token_default_free_scope():
    """Without app_metadata, authenticated users still get mcp:tools:free."""
    from mcp_server.auth import SCOPE_FREE, SCOPE_PREMIUM, validate_token

    token = _make_token()  # no mcp_scopes
    with patch("mcp_server.auth.settings") as mock_settings:
        mock_settings.supabase_jwt_secret = _TEST_SECRET
        result = validate_token(token)
        assert result.authenticated
        assert result.has_scope(SCOPE_FREE)
        assert not result.has_scope(SCOPE_PREMIUM)


def test_anonymous_has_free_scope():
    """Anonymous callers have mcp:tools:free by default."""
    from mcp_server.auth import ANONYMOUS, SCOPE_FREE, SCOPE_PREMIUM

    assert ANONYMOUS.has_scope(SCOPE_FREE)
    assert not ANONYMOUS.has_scope(SCOPE_PREMIUM)


# -- require_scope ---------------------------------------------------------


def test_require_scope_passes_with_correct_scope():
    """require_scope does nothing when the caller has the required scope."""
    from mcp_server.auth import SCOPE_PREMIUM, AuthContext, require_scope

    ctx = AuthContext(
        authenticated=True, user_id="u1",
        scopes=frozenset({"mcp:tools:free", SCOPE_PREMIUM}),
    )
    require_scope(ctx, SCOPE_PREMIUM)  # should not raise


def test_require_scope_raises_without_scope():
    """require_scope raises ToolError when scope is missing."""
    import json

    from mcp.server.mcpserver.exceptions import ToolError

    from mcp_server.auth import SCOPE_PREMIUM, AuthContext, require_scope

    ctx = AuthContext(authenticated=True, user_id="u1")  # only free scope
    with pytest.raises(ToolError) as exc_info:
        require_scope(ctx, SCOPE_PREMIUM)

    error = json.loads(str(exc_info.value))
    assert error["error"] == "premium_required"
    assert error["required_scope"] == SCOPE_PREMIUM
    assert error["authenticated"] is True
    assert "geo-insights.nl" in error["fix"]


def test_require_scope_raises_for_anonymous():
    """require_scope raises for anonymous callers requesting premium."""
    import json

    from mcp.server.mcpserver.exceptions import ToolError

    from mcp_server.auth import ANONYMOUS, SCOPE_PREMIUM, require_scope

    with pytest.raises(ToolError) as exc_info:
        require_scope(ANONYMOUS, SCOPE_PREMIUM)

    error = json.loads(str(exc_info.value))
    assert error["error"] == "premium_required"
    assert error["authenticated"] is False


# -- auth_from_headers -----------------------------------------------------


def test_auth_from_headers_valid_token():
    """auth_from_headers extracts auth from an Authorization header."""
    from mcp_server.auth import SCOPE_PREMIUM, auth_from_headers

    token = _make_token(mcp_scopes=["mcp:tools:free", SCOPE_PREMIUM])
    with patch("mcp_server.auth.settings") as mock_settings:
        mock_settings.supabase_jwt_secret = _TEST_SECRET
        ctx = auth_from_headers({"authorization": f"Bearer {token}"})
        assert ctx.authenticated
        assert ctx.has_scope(SCOPE_PREMIUM)


def test_auth_from_headers_no_header():
    """Missing headers return ANONYMOUS."""
    from mcp_server.auth import ANONYMOUS, auth_from_headers

    assert auth_from_headers(None) is ANONYMOUS
    assert auth_from_headers({}) is ANONYMOUS
    assert auth_from_headers({"content-type": "application/json"}) is ANONYMOUS
