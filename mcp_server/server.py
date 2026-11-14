"""MCP Server for Geo-Insights environmental data.

Streamable HTTP transport exposing 4 free tools that query the FROST
SensorThings API. Rate-limited and logged for analytics.
"""

from __future__ import annotations

import json
import logging
import time

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.routing import Route

from mcp_server import tools as tool_impls
from mcp_server.errors import (
    sensor_data_unavailable,
)
from mcp_server.freshness import freshness_endpoint
from mcp_server.frost_client import CircuitOpen, FrostReadClient
from mcp_server.middleware import request_logger

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Create the MCP server
mcp = FastMCP(
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


def _handle_frost_error(exc: Exception) -> str:
    """Convert FROST errors to agent-ready JSON error strings."""
    if isinstance(exc, CircuitOpen):
        return json.dumps(sensor_data_unavailable(retry_after_seconds=60))
    if isinstance(exc, httpx.HTTPStatusError):
        return json.dumps(sensor_data_unavailable(retry_after_seconds=30))
    return json.dumps(sensor_data_unavailable(retry_after_seconds=60))


@mcp.tool()
async def list_sources() -> str:
    """List all environmental data sources with station counts and geographic coverage.

    Returns a JSON array of sources, each with name, station_count,
    datastream_count, and source_attribution fields.
    """
    start = time.monotonic()
    try:
        result = await tool_impls.list_sources(_get_client())
        request_logger.log("list_sources", int((time.monotonic() - start) * 1000), "", "", "ok")
        return json.dumps(result)
    except (CircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("list_sources", int((time.monotonic() - start) * 1000), "", "", "error")
        return _handle_frost_error(exc)


@mcp.tool()
async def get_parameters() -> str:
    """List all 35 available environmental parameters with units and aliases.

    Returns a JSON array of parameters. Each entry includes name, display_name,
    unit (UCUM), definition_url (CF Conventions), and aliases (alternative names
    you can use in get_latest_readings).
    No FROST query needed — this is an in-memory lookup.
    """
    start = time.monotonic()
    result = await tool_impls.get_parameters()
    request_logger.log("get_parameters", int((time.monotonic() - start) * 1000), "", "", "ok")
    return json.dumps(result)


@mcp.tool()
async def get_latest_readings(
    lat: float,
    lon: float,
    radius_km: float = 10.0,
    parameter: str | None = None,
    limit: int = 100,
) -> str:
    """Get latest environmental readings near a location.

    Args:
        lat: Latitude in WGS84 (e.g. 51.92 for Rotterdam)
        lon: Longitude in WGS84 (e.g. 4.47 for Rotterdam)
        radius_km: Search radius in kilometers (default 10, max 50)
        parameter: Optional filter by parameter name or alias (e.g. "pm2_5",
                   "temperature", "pm25", "no2"). Use get_parameters() to see
                   all valid names and aliases.
        limit: Maximum number of readings to return (default 100, max 500)

    Returns JSON with readings array. Each reading includes parameter, value,
    unit, timestamp, station_name, coordinates, source, and source_attribution.
    """
    start = time.monotonic()
    try:
        result = await tool_impls.get_latest_readings(
            _get_client(), lat, lon, radius_km, parameter, limit
        )
        if result.get("error"):
            request_logger.log("get_latest_readings", int((time.monotonic() - start) * 1000), "", "", "validation_error")
            return json.dumps(result)
        request_logger.log("get_latest_readings", int((time.monotonic() - start) * 1000), "", "", "ok")
        return json.dumps(result)
    except (CircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("get_latest_readings", int((time.monotonic() - start) * 1000), "", "", "error")
        return _handle_frost_error(exc)


@mcp.tool()
async def get_data_freshness() -> str:
    """Check how fresh the data is for each of the 10 sources.

    Returns a JSON array of sources with latest_observation timestamp,
    age_seconds, and a status classification: "fresh" (<1h), "stale" (<24h),
    or "offline" (>24h). Use this to check if a specific data source is
    currently delivering data.
    """
    start = time.monotonic()
    try:
        result = await tool_impls.get_data_freshness(_get_client())
        request_logger.log("get_data_freshness", int((time.monotonic() - start) * 1000), "", "", "ok")
        return json.dumps(result)
    except (CircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("get_data_freshness", int((time.monotonic() - start) * 1000), "", "", "error")
        return _handle_frost_error(exc)


# The MCP SDK handles Streamable HTTP transport via its own ASGI app.
# We add the /freshness route for operational monitoring.
app = mcp.streamable_http_app()
app.routes.append(Route("/freshness", freshness_endpoint))
