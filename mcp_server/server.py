"""MCP Server for Geo-Insights environmental data.

Streamable HTTP transport exposing 5 free tools + 2 resources that query the
FROST SensorThings API. Rate-limited and logged for analytics.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.routing import Route

from mcp_server import tools as tool_impls
from mcp_server.auth import auth_enabled
from mcp_server.errors import classify_frost_error
from mcp_server.freshness import freshness_endpoint
from mcp_server.frost_client import CircuitOpen, FrostReadClient
from mcp_server.middleware import request_logger
from mcp_server.oauth_discovery import oauth_protected_resource_endpoint
from mcp_server.schemas import (
    FreshnessResult,
    HistoryResult,
    ParametersResult,
    ReadingsResult,
    SourcesResult,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Tool annotations shared by all tools (read-only, queries external data)
_READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

# Create the MCP server
mcp = MCPServer(
    "Geo-Insights Environmental Data",
    instructions=(
        "Query real-time Dutch environmental data from 10 sources "
        "(KNMI, Luchtmeetnet, BRO, Samen Meten, Sensor.Community, and more). "
        "All data is normalized to canonical parameter names with UCUM units. "
        "No authentication required."
    ),
)

# Global FROST client (initialized on startup)
_frost_client: FrostReadClient | None = None


def _get_client() -> FrostReadClient:
    global _frost_client
    if _frost_client is None:
        _frost_client = FrostReadClient()
    return _frost_client


def _raise_frost_error(exc: Exception) -> None:
    """Convert FROST errors to ToolError (sets isError=true at protocol level)."""
    raise ToolError(json.dumps(classify_frost_error(exc)))


# -- MCP Resources (static catalogs, no tool call needed) ------------------


@mcp.resource(
    "sensorthings://sources",
    name="sources",
    description=(
        "Catalog of all environmental data sources with station counts "
        "and geographic coverage. Returns a JSON array of sources."
    ),
    mime_type="application/json",
)
async def sources_resource() -> str:
    """Read handler for the sources resource."""
    result = await tool_impls.list_sources(_get_client())
    return json.dumps(result.model_dump())


@mcp.resource(
    "sensorthings://parameters",
    name="parameters",
    description=(
        "All 35 canonical environmental parameters with units, aliases, "
        "and CF Conventions definition URLs. In-memory lookup, no FROST query."
    ),
    mime_type="application/json",
)
async def parameters_resource() -> str:
    """Read handler for the parameters resource."""
    result = await tool_impls.get_parameters()
    return json.dumps(result.model_dump())


# -- MCP Tools -------------------------------------------------------------


@mcp.tool(
    title="List Environmental Data Sources",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def list_sources() -> SourcesResult:
    """List all environmental data sources with station counts and geographic coverage.

    Returns sources with name, station_count, datastream_count, and
    source_attribution fields. Use this to discover what data is available.
    """
    start = time.monotonic()
    try:
        result = await tool_impls.list_sources(_get_client())
        request_logger.log("list_sources", int((time.monotonic() - start) * 1000), "", "", "ok")
        return result
    except (CircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("list_sources", int((time.monotonic() - start) * 1000), "", "", "error")
        _raise_frost_error(exc)


@mcp.tool(
    title="List Environmental Parameters",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def get_parameters() -> ParametersResult:
    """List all 35 available environmental parameters with units and aliases.

    Returns parameters with name, display_name, unit (UCUM), definition_url
    (CF Conventions), and aliases (alternative names you can use in
    get_latest_readings). No FROST query needed — this is an in-memory lookup.
    """
    start = time.monotonic()
    result = await tool_impls.get_parameters()
    request_logger.log("get_parameters", int((time.monotonic() - start) * 1000), "", "", "ok")
    return result


@mcp.tool(
    title="Get Latest Environmental Readings",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def get_latest_readings(
    lat: float,
    lon: float,
    radius_km: float = 10.0,
    parameter: str | None = None,
    limit: int = 100,
) -> ReadingsResult:
    """Get latest environmental readings near a location.

    Args:
        lat: Latitude in WGS84 (e.g. 51.92 for Rotterdam)
        lon: Longitude in WGS84 (e.g. 4.47 for Rotterdam)
        radius_km: Search radius in kilometers (default 10, max 50)
        parameter: Optional filter by parameter name or alias (e.g. "pm2_5",
                   "temperature", "pm25", "no2"). Use get_parameters() to see
                   all valid names and aliases.
        limit: Maximum number of readings to return (default 100, max 500)

    Returns readings with parameter, value, unit, timestamp, station_name,
    coordinates, source, and source_attribution.
    """
    start = time.monotonic()
    try:
        result = await tool_impls.get_latest_readings(
            _get_client(), lat, lon, radius_km, parameter, limit
        )
        request_logger.log("get_latest_readings", int((time.monotonic() - start) * 1000), "", "", "ok")
        return result
    except ToolError:
        request_logger.log("get_latest_readings", int((time.monotonic() - start) * 1000), "", "", "validation_error")
        raise
    except (CircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("get_latest_readings", int((time.monotonic() - start) * 1000), "", "", "error")
        _raise_frost_error(exc)


@mcp.tool(
    title="Get Historical Readings",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def get_readings_history(
    lat: float,
    lon: float,
    parameter: str,
    start: str,
    end: str | None = None,
    radius_km: float = 10.0,
    limit: int = 100,
) -> HistoryResult:
    """Get historical environmental readings near a location over a time range.

    Args:
        lat: Latitude in WGS84 (e.g. 51.92 for Rotterdam)
        lon: Longitude in WGS84 (e.g. 4.47 for Rotterdam)
        parameter: Parameter name or alias (e.g. "temperature", "pm25", "no2").
                   Use get_parameters() to see all valid names and aliases.
        start: Start of time range in ISO 8601 (e.g. "2026-11-01T00:00:00Z")
        end: End of time range in ISO 8601 (default: now)
        radius_km: Search radius in kilometers (default 10, max 50)
        limit: Maximum readings per station (default 100, max 1000)

    Returns stations with their historical readings, parameter info, and
    time range metadata.
    """
    start_t = time.monotonic()
    try:
        result = await tool_impls.get_readings_history(
            _get_client(), lat, lon, parameter, start, end, radius_km, limit
        )
        request_logger.log("get_readings_history", int((time.monotonic() - start_t) * 1000), "", "", "ok")
        return result
    except ToolError:
        request_logger.log("get_readings_history", int((time.monotonic() - start_t) * 1000), "", "", "validation_error")
        raise
    except (CircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("get_readings_history", int((time.monotonic() - start_t) * 1000), "", "", "error")
        _raise_frost_error(exc)


@mcp.tool(
    title="Check Data Freshness",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def get_data_freshness() -> FreshnessResult:
    """Check how fresh the data is for each of the 10 sources.

    Returns sources with latest_observation timestamp, age_seconds, and a
    status classification: "fresh" (<1h), "stale" (<24h), or "offline" (>24h).
    Use this to check if a specific data source is currently delivering data.
    """
    start = time.monotonic()
    try:
        result = await tool_impls.get_data_freshness(_get_client())
        request_logger.log("get_data_freshness", int((time.monotonic() - start) * 1000), "", "", "ok")
        return result
    except (CircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("get_data_freshness", int((time.monotonic() - start) * 1000), "", "", "error")
        _raise_frost_error(exc)


# Combine MCP transport with the freshness monitoring endpoint.
# The MCP app owns /mcp; we mount it inside a parent Starlette app
# that also serves /freshness for healthchecks and operational monitoring.
app = mcp.streamable_http_app(
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "mcp.geo-insights.nl",
            "localhost",
            "localhost:8030",
        ],
    ),
)
app.routes.insert(0, Route("/freshness", freshness_endpoint))
if auth_enabled():
    app.routes.insert(
        0, Route("/.well-known/oauth-protected-resource", oauth_protected_resource_endpoint)
    )

# Glama connector ownership verification
# https://glama.ai/mcp/connectors — must be served at /.well-known/glama.json
_GLAMA_CLAIM = {
    "$schema": "https://glama.ai/mcp/schemas/connector.json",
    "claim": "glama_claim_m6KSXae8bIt80Mim9KtadQUJo0DMEEzM",
}


async def _glama_claim_endpoint(request):  # type: ignore[no-untyped-def]
    from starlette.responses import JSONResponse as _JSONResponse
    return _JSONResponse(_GLAMA_CLAIM)


app.routes.insert(0, Route("/.well-known/glama.json", _glama_claim_endpoint))
