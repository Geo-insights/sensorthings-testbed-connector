"""GET /freshness HTTP endpoint for operational monitoring.

Separate from the MCP get_data_freshness tool — this is a plain JSON HTTP
endpoint for health dashboards and monitoring, not for AI agents.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_server.frost_client import CircuitOpen, FrostReadClient
from mcp_server.tools import get_data_freshness


async def freshness_endpoint(request: Request) -> JSONResponse:
    """Return per-source data freshness as plain JSON."""
    client: FrostReadClient = request.app.state.frost_client
    try:
        data = await get_data_freshness(client)
        return JSONResponse({"sources": data, "status": "ok"})
    except CircuitOpen:
        return JSONResponse(
            {"status": "error", "message": "FROST circuit breaker is open"},
            status_code=503,
        )
    except Exception:
        return JSONResponse(
            {"status": "error", "message": "Failed to query data freshness"},
            status_code=502,
        )
