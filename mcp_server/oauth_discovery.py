"""OAuth 2.1 Protected Resource Metadata (RFC 9728).

Returns a JSON document at ``/.well-known/oauth-protected-resource`` that tells
MCP clients where to obtain access tokens. Only mounted when auth is enabled
(``SUPABASE_JWT_SECRET`` is set).

Supabase exposes standard OAuth-compatible endpoints at
``<project_url>/auth/v1/authorize`` and ``<project_url>/auth/v1/token``.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_server.config import settings


async def oauth_protected_resource_endpoint(request: Request) -> JSONResponse:
    """RFC 9728 Protected Resource Metadata."""
    base = settings.supabase_project_url.rstrip("/")
    return JSONResponse(
        {
            "resource": "https://mcp.geo-insights.nl",
            "authorization_servers": [base],
            "scopes_supported": [
                "mcp:tools:free",
                "mcp:tools:premium",
            ],
            "bearer_methods_supported": ["header"],
            "resource_documentation": "https://mcp.geo-insights.nl/freshness",
        },
        headers={"Cache-Control": "public, max-age=86400"},
    )
