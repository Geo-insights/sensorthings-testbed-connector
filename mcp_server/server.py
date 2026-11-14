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

from mcp_server import catalog_tools
from mcp_server import tools as tool_impls
from mcp_server.auth import auth_enabled
from mcp_server.catalog_schemas import (
    CompareResult,
    ContextSummary,
    MunicipalityResult,
    PolicyResult,
    RankResult,
    SearchMeasuresResult,
)
from mcp_server.errors import classify_frost_error, classify_supabase_error
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
from mcp_server.supabase_client import SupabaseCircuitOpen, SupabaseReadClient

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
        "Dutch environmental data and climate adaptation tools. "
        "Sensor tools: real-time readings from 10 sources (KNMI, Luchtmeetnet, BRO, etc.) "
        "with 35 normalized parameters. "
        "Catalog tools: 44 BGI (Blue-Green Infrastructure) measures with performance data, "
        "costs, goal scores, and per-municipality policy requirements. "
        "Use search_bgi_measures to find measures, get_measure_detail for full specs, "
        "get_policy_requirements for local regulations, and rank_measures for recommendations. "
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


# Global Supabase client (initialized on startup)
_supabase_client: SupabaseReadClient | None = None


def _get_supabase() -> SupabaseReadClient:
    global _supabase_client
    if _supabase_client is None:
        _supabase_client = SupabaseReadClient()
    return _supabase_client


def _raise_supabase_error(exc: Exception) -> None:
    """Convert Supabase errors to ToolError."""
    raise ToolError(json.dumps(classify_supabase_error(exc)))


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

    Call this first to discover which data sources are available and how many
    stations each one has. Returns sources with name, station_count,
    datastream_count, and source_attribution (CC-BY license info).

    Sources include KNMI (weather), Luchtmeetnet (air quality), BRO
    (groundwater), Samen Meten (citizen science), Sensor.Community, and more.

    Typical workflow: list_sources → get_parameters → get_latest_readings.

    On error, returns a structured error with cause (timeout, circuit_breaker_open,
    connection_failed) and retry_after_seconds.
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

    Returns parameters with name, display_name, unit_display (human-readable),
    unit_ucum (formal UCUM code), definition_url (CF Conventions), and aliases.
    Use aliases in the 'parameter' argument of get_latest_readings or
    get_readings_history (e.g. "pm25", "temperature", "no2").

    No FROST query needed — this is an instant in-memory lookup. Call it to
    discover valid parameter names before querying readings.
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
    """Get the most recent environmental readings near a location.

    Args:
        lat: Latitude in WGS84 (e.g. 51.92 for Rotterdam)
        lon: Longitude in WGS84 (e.g. 4.47 for Rotterdam)
        radius_km: Search radius in kilometers (default 10, max 50)
        parameter: Optional filter by parameter name or alias (e.g. "pm2_5",
                   "temperature", "pm25", "no2"). Use get_parameters() to see
                   all valid names and aliases.
        limit: Maximum number of readings to return (default 100, max 500)

    Returns the single most recent reading per datastream within the radius.
    Each reading includes parameter, value, unit, timestamp, station_name,
    lat, lon, source, and source_attribution.

    For time-range queries, use get_readings_history instead.

    Invalid coordinates return an error with the valid ranges.
    An unrecognized parameter returns an error with the full list of valid names.
    If the data source is unreachable, returns cause (timeout, circuit_breaker_open)
    and retry_after_seconds.
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

    Use this for trend analysis, comparisons, and time-series queries.
    Unlike get_latest_readings (which returns only the most recent value),
    this returns multiple observations per station within the time window.

    Args:
        lat: Latitude in WGS84 (e.g. 51.92 for Rotterdam)
        lon: Longitude in WGS84 (e.g. 4.47 for Rotterdam)
        parameter: Parameter name or alias (required — e.g. "temperature",
                   "pm25", "no2"). Use get_parameters() for valid names.
        start: Start of time range in ISO 8601 (e.g. "2026-11-01T00:00:00Z")
        end: End of time range in ISO 8601 (default: now)
        radius_km: Search radius in kilometers (default 10, max 50)
        limit: Maximum readings per station (default 100, max 1000)

    Returns stations grouped by location, each with a time-ordered array of
    readings, plus parameter info and the actual time range covered.

    Invalid coordinates, unrecognized parameters, or unparseable dates return
    a structured error. Data source failures include cause and retry timing.
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
    status classification: "fresh" (<1h), "stale" (<24h), "offline" (>24h),
    or "unreachable" (query failed for that source).

    Use this to verify data availability before querying readings, or to
    monitor source health. Individual source failures are reported per-source
    (status: "unreachable") rather than failing the entire call.

    This is also the server's health check — if this tool works, the server
    is operational.
    """
    start = time.monotonic()
    try:
        result = await tool_impls.get_data_freshness(_get_client())
        request_logger.log("get_data_freshness", int((time.monotonic() - start) * 1000), "", "", "ok")
        return result
    except (CircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("get_data_freshness", int((time.monotonic() - start) * 1000), "", "", "error")
        _raise_frost_error(exc)


# -- BGI Catalog Tools (Supabase-backed) ------------------------------------


@mcp.tool(
    title="Search BGI Measures",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def search_bgi_measures(
    goal: str | None = None,
    category: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
) -> SearchMeasuresResult:
    """Search the Blue-Green Infrastructure measure catalog (44 measures).

    All parameters are optional — call with no args to list all measures.

    Args:
        goal: Filter by climate goal: flood, heat, drought, biodiversity, social
        category: Filter by category: water_retention, heat_mitigation,
                  surface_water, biodiversity
        keyword: Search in name, Dutch name, and description (e.g. "dak", "roof")
        limit: Max results (default 20, max 50)

    Returns measures with goal scores, category, scale, and maintenance level.
    Use get_measure_detail for full specs on a specific measure.
    """
    start = time.monotonic()
    try:
        result = await catalog_tools.search_bgi_measures(
            _get_supabase(), goal, category, keyword, limit
        )
        request_logger.log("search_bgi_measures", int((time.monotonic() - start) * 1000), "", "", "ok")
        return result
    except ToolError:
        raise
    except (SupabaseCircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("search_bgi_measures", int((time.monotonic() - start) * 1000), "", "", "error")
        _raise_supabase_error(exc)


@mcp.tool(
    title="Get BGI Measure Detail",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def get_measure_detail(measure_id_or_slug: str) -> str:
    """Get full detail for one BGI measure: performance, costs, scores, constraints.

    Args:
        measure_id_or_slug: UUID or slug (e.g. "green-roof", "bioswale",
                           "permeable-paving"). Use search_bgi_measures to find
                           valid slugs.

    Returns complete measure spec: description, all goal scores with confidence,
    performance parameters (retention, cooling, infiltration, etc.) with ranges
    and units, CAPEX/OPEX costs, site requirements, co-benefits, constraint
    compatibility, and evidence count.
    """
    start = time.monotonic()
    try:
        result = await catalog_tools.get_measure_detail(_get_supabase(), measure_id_or_slug)
        request_logger.log("get_measure_detail", int((time.monotonic() - start) * 1000), "", "", "ok")
        return json.dumps(result.model_dump())
    except ToolError:
        request_logger.log("get_measure_detail", int((time.monotonic() - start) * 1000), "", "", "validation_error")
        raise
    except (SupabaseCircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("get_measure_detail", int((time.monotonic() - start) * 1000), "", "", "error")
        _raise_supabase_error(exc)


@mcp.tool(
    title="Get Municipality Policy Requirements",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def get_policy_requirements(
    gemeente_code_or_name: str,
    measure_slug: str | None = None,
) -> PolicyResult:
    """Get BGI policy requirements for a Dutch municipality.

    Extracted from Officiele Bekendmakingen (official government publications).
    Shows what's required, recommended, or subsidized per gemeente.

    Args:
        gemeente_code_or_name: CBS code (e.g. "GM0599") or municipality name
                              (e.g. "Rotterdam"). Use lookup_municipality to find codes.
        measure_slug: Optional — filter to one measure's requirements

    Returns requirements grouped by type (subsidy, regulation, obligation)
    with document titles, dates, amounts, and source URLs.
    """
    start = time.monotonic()
    try:
        result = await catalog_tools.get_policy_requirements(
            _get_supabase(), gemeente_code_or_name, measure_slug
        )
        request_logger.log("get_policy_requirements", int((time.monotonic() - start) * 1000), "", "", "ok")
        return result
    except ToolError:
        request_logger.log("get_policy_requirements", int((time.monotonic() - start) * 1000), "", "", "validation_error")
        raise
    except (SupabaseCircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("get_policy_requirements", int((time.monotonic() - start) * 1000), "", "", "error")
        _raise_supabase_error(exc)


@mcp.tool(
    title="Lookup Municipality",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def lookup_municipality(
    name: str | None = None,
    province: str | None = None,
) -> MunicipalityResult:
    """Find Dutch municipalities by name or province.

    Returns CBS codes needed for other tools (get_policy_requirements,
    rank_measures). All 342 Dutch municipalities are searchable.

    Args:
        name: Partial name match (e.g. "Rotter" finds Rotterdam)
        province: Filter by province name (e.g. "Zuid-Holland")
    """
    start = time.monotonic()
    try:
        result = await catalog_tools.lookup_municipality(_get_supabase(), name, province)
        request_logger.log("lookup_municipality", int((time.monotonic() - start) * 1000), "", "", "ok")
        return result
    except (SupabaseCircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("lookup_municipality", int((time.monotonic() - start) * 1000), "", "", "error")
        _raise_supabase_error(exc)


@mcp.tool(
    title="Rank BGI Measures for Municipality",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def rank_measures(
    gemeente_code: str,
    goal: str | None = None,
    policy_profile: str = "Balanced",
    limit: int = 10,
) -> RankResult:
    """Rank BGI measures for a municipality using policy-weighted scoring.

    Combines goal alignment, cost-effectiveness, and maintenance into a
    single ranked list. Use different policy profiles to see how priorities
    change the ranking.

    Args:
        gemeente_code: CBS municipality code (e.g. "GM0599" for Rotterdam).
                      Use lookup_municipality to find codes.
        goal: Optional boost for a specific goal (flood, heat, drought,
              biodiversity, social)
        policy_profile: Scoring profile — one of: Balanced, Flood-focused,
                       Heat-focused, Drought-focused, Biodiversity-first,
                       Cost-efficient (default: Balanced)
        limit: Max results (default 10, max 50)
    """
    start = time.monotonic()
    try:
        result = await catalog_tools.rank_measures(
            _get_supabase(), gemeente_code, goal, policy_profile, limit
        )
        request_logger.log("rank_measures", int((time.monotonic() - start) * 1000), "", "", "ok")
        return result
    except ToolError:
        request_logger.log("rank_measures", int((time.monotonic() - start) * 1000), "", "", "validation_error")
        raise
    except (SupabaseCircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("rank_measures", int((time.monotonic() - start) * 1000), "", "", "error")
        _raise_supabase_error(exc)


@mcp.tool(
    title="Get Environmental Context for Location",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def get_context_summary(lat: float, lon: float) -> ContextSummary:
    """Get environmental and socioeconomic context for a location in the Netherlands.

    Returns the neighborhood (buurt), municipality, population density,
    green space accessibility, health indicators, and livability scores.

    Args:
        lat: Latitude in WGS84 (e.g. 51.92 for Rotterdam)
        lon: Longitude in WGS84 (e.g. 4.47 for Rotterdam)

    Useful for understanding the local context before recommending BGI measures.
    """
    start = time.monotonic()
    try:
        result = await catalog_tools.get_context_summary(_get_supabase(), lat, lon)
        request_logger.log("get_context_summary", int((time.monotonic() - start) * 1000), "", "", "ok")
        return result
    except ToolError:
        request_logger.log("get_context_summary", int((time.monotonic() - start) * 1000), "", "", "validation_error")
        raise
    except (SupabaseCircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("get_context_summary", int((time.monotonic() - start) * 1000), "", "", "error")
        _raise_supabase_error(exc)


@mcp.tool(
    title="Compare BGI Measures",
    annotations=_READ_ONLY_ANNOTATIONS,
)
async def compare_measures(measure_slugs: list[str]) -> CompareResult:
    """Side-by-side comparison of 2-5 BGI measures.

    Args:
        measure_slugs: List of 2-5 measure slugs (e.g. ["green-roof", "bioswale"]).
                      Use search_bgi_measures to find valid slugs.

    Returns aligned comparison with goal scores, key performance parameters
    (retention, cooling, infiltration), costs, maintenance level, and scale.
    """
    start = time.monotonic()
    try:
        result = await catalog_tools.compare_measures(_get_supabase(), measure_slugs)
        request_logger.log("compare_measures", int((time.monotonic() - start) * 1000), "", "", "ok")
        return result
    except ToolError:
        request_logger.log("compare_measures", int((time.monotonic() - start) * 1000), "", "", "validation_error")
        raise
    except (SupabaseCircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("compare_measures", int((time.monotonic() - start) * 1000), "", "", "error")
        _raise_supabase_error(exc)


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
