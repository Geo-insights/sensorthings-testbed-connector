"""MCP tool implementations for Geo-Insights environmental data.

Each tool queries the FROST SensorThings API and returns structured results
with source attribution for CC-BY compliance.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from app.sta.canonical import CanonicalDatastream, DatastreamMeta, resolve
from mcp_server.errors import invalid_coordinates, invalid_parameter
from mcp_server.frost_client import CircuitOpen, FrostReadClient
from mcp_server.schemas import (
    FreshnessEntry,
    FreshnessResult,
    HistoryReading,
    HistoryResult,
    ParameterInfo,
    ParametersResult,
    Reading,
    ReadingsResult,
    SourceInfo,
    SourcesResult,
    StationHistory,
)

logger = logging.getLogger(__name__)

# Source attribution for CC-BY compliance (included in every response)
_ATTRIBUTION = {
    "knmi": "Data: KNMI (Royal Netherlands Meteorological Institute), CC-BY 4.0",
    "luchtmeetnet": "Data: RIVM/Luchtmeetnet, CC-BY 4.0",
    "samen_meten": "Data: RIVM Samen Meten, CC-BY 4.0",
    "bro": "Data: BRO (Basisregistratie Ondergrond), CC-BY 4.0",
    "sensor_community": "Data: Sensor.Community, ODbL",
    "meet_je_stad": "Data: Meet je Stad, CC-BY-SA 4.0",
    "tgv": "Data: TGV (Testbed Geoconnected Vitality)",
    "bridge": "Data: Bridge sensors",
    "ohnics": "Data: Ohnics",
    "levellog": "Data: Levellog",
}

_DEFAULT_ATTRIBUTION = "Data: Geo-Insights (geo-insights.nl), various CC-BY sources"


def _get_attribution(source_name: str) -> str:
    key = source_name.lower().replace(" ", "_").replace("-", "_")
    return _ATTRIBUTION.get(key, _DEFAULT_ATTRIBUTION)


async def list_sources(client: FrostReadClient) -> SourcesResult:
    """List all environmental data sources with station counts."""
    # Group Things by their source property
    sources: dict[str, dict[str, Any]] = {}
    try:
        things_data = await client.get_things(
            top=1000,
            expand="Locations,Datastreams($count=true;$top=0)",
        )
    except (httpx.HTTPError, Exception) as exc:
        logger.error("FROST query failed for list_sources: %s: %s", type(exc).__name__, exc)
        raise

    for thing in things_data.get("value", []):
        props = thing.get("properties", {})
        source = props.get("source", "unknown")
        if source not in sources:
            sources[source] = {
                "name": source,
                "station_count": 0,
                "datastream_count": 0,
                "source_attribution": _get_attribution(source),
            }
        sources[source]["station_count"] += 1
        ds_count = thing.get("Datastreams@iot.count", 0)
        sources[source]["datastream_count"] += ds_count

    return SourcesResult(
        sources=[SourceInfo(**s) for s in sources.values()]
    )


async def get_parameters() -> ParametersResult:
    """List all available environmental parameters with units and aliases.

    Returns all 35 canonical parameter names. Each is a valid value for the
    ``parameter`` argument in ``get_latest_readings``.
    This is a pure in-memory lookup — no FROST query needed.
    """
    from app.sta.canonical import _ALIASES

    # Build reverse alias map: canonical name -> list of aliases
    alias_map: dict[str, list[str]] = {}
    for alias, canonical in _ALIASES.items():
        alias_map.setdefault(canonical.value, []).append(alias)

    params: list[ParameterInfo] = []
    for member in CanonicalDatastream:
        meta: DatastreamMeta = member.meta
        ucum_code = meta.ucum if meta.ucum else meta.unit
        params.append(ParameterInfo(
            name=member.value,
            display_name=meta.display_name,
            unit_display=meta.unit,
            unit_ucum=ucum_code,
            definition_url=meta.definition,
            aliases=sorted(
                set(alias_map.get(member.value, [])) | {member.value}
            ),
        ))

    return ParametersResult(parameters=params)


async def get_latest_readings(
    client: FrostReadClient,
    lat: float,
    lon: float,
    radius_km: float = 10.0,
    parameter: str | None = None,
    limit: int = 100,
) -> ReadingsResult:
    """Get latest environmental readings near a location."""
    # Validate coordinates
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise ToolError(json.dumps(invalid_coordinates(lat, lon)))

    # Validate parameter if provided
    canonical = None
    if parameter:
        canonical = resolve(parameter)
        if canonical is None:
            valid = sorted(m.value for m in CanonicalDatastream)
            raise ToolError(json.dumps(invalid_parameter(parameter, valid)))

    # Clamp limit
    limit = max(1, min(limit, 500))
    radius_m = radius_km * 1000

    # Build FROST OData query
    # Get Datastreams near the location with their latest observation
    geo_filter = (
        f"Thing/Locations/location/geography'POINT({lon} {lat})' lt {radius_m}"
    )
    filter_parts = [f"geo.distance({geo_filter})"]

    if canonical:
        # Filter by the canonical datastream name in the Datastream name/description
        filter_parts.append(
            f"substringof('{canonical.value}',name)"
        )

    try:
        data = await client.get_datastreams(
            top=limit,
            filter_=" and ".join(filter_parts) if len(filter_parts) > 1 else filter_parts[0],
            expand=(
                "Thing/Locations,"
                "ObservedProperty,"
                "Observations($top=1;$orderby=phenomenonTime desc)"
            ),
        )
    except CircuitOpen:
        raise
    except (httpx.HTTPError, Exception) as exc:
        logger.error("FROST query failed for get_latest_readings: %s", exc)
        raise

    readings: list[Reading] = []
    for ds in data.get("value", []):
        observations = ds.get("Observations", [])
        if not observations:
            continue

        obs = observations[0]
        thing = ds.get("Thing", {})
        locations = thing.get("Locations", [])
        location = locations[0] if locations else {}
        coords = location.get("location", {}).get("coordinates", [None, None])

        source = thing.get("properties", {}).get("source", "unknown")

        readings.append(Reading(
            parameter=ds.get("ObservedProperty", {}).get("name", ds.get("name", "")),
            value=obs.get("result"),
            unit=ds.get("unitOfMeasurement", {}).get("symbol", ""),
            timestamp=obs.get("phenomenonTime"),
            station_name=thing.get("name", ""),
            lat=coords[1] if len(coords) > 1 else None,
            lon=coords[0] if len(coords) > 0 else None,
            source=source,
            source_attribution=_get_attribution(source),
        ))

    return ReadingsResult(readings=readings, count=len(readings))


async def get_readings_history(
    client: FrostReadClient,
    lat: float,
    lon: float,
    parameter: str,
    start: str,
    end: str | None = None,
    radius_km: float = 10.0,
    limit: int = 100,
) -> HistoryResult:
    """Get historical environmental readings near a location over a time range."""
    import datetime

    # Validate coordinates
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        raise ToolError(json.dumps(invalid_coordinates(lat, lon)))

    # Validate parameter (required, must resolve)
    canonical = resolve(parameter)
    if canonical is None:
        valid = sorted(m.value for m in CanonicalDatastream)
        raise ToolError(json.dumps(invalid_parameter(parameter, valid)))

    # Parse start time
    try:
        start_dt = datetime.datetime.fromisoformat(start)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=datetime.UTC)
    except (ValueError, TypeError):
        raise ToolError(json.dumps({
            "error": "invalid_start_time",
            "message": f"Cannot parse start time '{start}' as ISO 8601",
            "fix": "Use ISO 8601 format, e.g. '2026-11-01T00:00:00Z'",
        })) from None

    # Parse end time (default: now)
    now = datetime.datetime.now(datetime.UTC)
    if end is not None:
        try:
            end_dt = datetime.datetime.fromisoformat(end)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=datetime.UTC)
        except (ValueError, TypeError):
            raise ToolError(json.dumps({
                "error": "invalid_end_time",
                "message": f"Cannot parse end time '{end}' as ISO 8601",
                "fix": "Use ISO 8601 format, e.g. '2026-11-14T00:00:00Z'",
            })) from None
    else:
        end_dt = now

    # Max time window: 30 days
    if (end_dt - start_dt).total_seconds() > 30 * 86400:
        raise ToolError(json.dumps({
            "error": "time_window_too_large",
            "message": "Time window exceeds maximum of 30 days",
            "fix": "Reduce the time range to 30 days or less",
        }))

    # Clamp radius and limit
    radius_km = min(radius_km, 50.0)
    limit = max(1, min(limit, 1000))
    radius_m = radius_km * 1000

    # Format times for OData
    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build FROST OData query
    geo_filter = (
        f"Thing/Locations/location/geography'POINT({lon} {lat})' lt {radius_m}"
    )
    filter_parts = [
        f"geo.distance({geo_filter})",
        f"substringof('{canonical.value}',name)",
    ]

    try:
        data = await client.get_datastreams(
            top=100,  # max 100 stations
            filter_=" and ".join(filter_parts),
            expand=(
                "Thing/Locations,"
                "ObservedProperty,"
                f"Observations("
                f"$filter=phenomenonTime ge {start_iso} and phenomenonTime le {end_iso};"
                f"$orderby=phenomenonTime asc;"
                f"$top={limit})"
            ),
        )
    except CircuitOpen:
        raise
    except (httpx.HTTPError, Exception) as exc:
        logger.error("FROST query failed for get_readings_history: %s", exc)
        raise

    # Group results by station (Thing)
    stations: list[StationHistory] = []
    total_readings = 0
    for ds in data.get("value", []):
        observations = ds.get("Observations", [])
        if not observations:
            continue

        thing = ds.get("Thing", {})
        locations = thing.get("Locations", [])
        location = locations[0] if locations else {}
        coords = location.get("location", {}).get("coordinates", [None, None])
        source = thing.get("properties", {}).get("source", "unknown")
        unit = ds.get("unitOfMeasurement", {}).get("symbol", "")

        readings: list[HistoryReading] = []
        for obs in observations:
            ts = obs.get("phenomenonTime")
            if ts:
                readings.append(HistoryReading(
                    timestamp=ts,
                    value=obs.get("result"),
                    unit=unit,
                ))

        if readings:
            total_readings += len(readings)
            stations.append(StationHistory(
                station_name=thing.get("name", ""),
                source=source,
                source_attribution=_get_attribution(source),
                lat=coords[1] if len(coords) > 1 else None,
                lon=coords[0] if len(coords) > 0 else None,
                readings=readings,
            ))

    return HistoryResult(
        stations=stations,
        parameter=canonical.value,
        period_start=start_iso,
        period_end=end_iso,
        total_readings=total_readings,
    )


async def get_data_freshness(client: FrostReadClient) -> FreshnessResult:
    """Check data freshness per source.

    Never fails entirely — marks individual sources as "unreachable" if the
    FROST query times out, so callers always get a usable response.
    """
    import datetime

    now = datetime.datetime.now(datetime.UTC)

    # Step 1: Get all Things (lightweight, no $expand on Observations)
    try:
        things_data = await client.get_things(top=1000)
    except CircuitOpen:
        raise
    except (httpx.HTTPError, Exception) as exc:
        logger.error("FROST query failed for get_data_freshness (Things): %s: %s", type(exc).__name__, exc)
        raise

    # Group things by source
    sources: dict[str, list[int]] = {}
    for thing in things_data.get("value", []):
        source = thing.get("properties", {}).get("source", "unknown")
        thing_id = thing.get("@iot.id")
        if thing_id is not None:
            sources.setdefault(source, []).append(thing_id)

    # Step 2: For each source, query ONE latest observation (much lighter)
    entries: list[FreshnessEntry] = []
    for source, _thing_ids in sorted(sources.items()):
        attribution = _get_attribution(source)
        try:
            # Get the single most recent observation across all datastreams for this source
            obs_data = await client.get_observations(
                top=1,
                orderby="phenomenonTime desc",
                filter_=f"Datastream/Thing/properties/source eq '{source}'",
            )
            observations = obs_data.get("value", [])
            if observations:
                obs_time_str = observations[0].get("phenomenonTime", "")
                if obs_time_str:
                    obs_time_str_clean = obs_time_str.split("/")[0]
                    if obs_time_str_clean.endswith("Z"):
                        obs_time_str_clean = obs_time_str_clean[:-1] + "+00:00"
                    obs_time = datetime.datetime.fromisoformat(obs_time_str_clean)
                    if obs_time.tzinfo is None:
                        obs_time = obs_time.replace(tzinfo=datetime.UTC)
                    age = int((now - obs_time).total_seconds())
                    if age < 3600:
                        status = "fresh"
                    elif age < 86400:
                        status = "stale"
                    else:
                        status = "offline"
                    entries.append(FreshnessEntry(
                        source=source,
                        source_attribution=attribution,
                        latest_observation=obs_time_str,
                        age_seconds=age,
                        status=status,
                    ))
                else:
                    entries.append(FreshnessEntry(
                        source=source,
                        source_attribution=attribution,
                        status="no_data",
                    ))
            else:
                entries.append(FreshnessEntry(
                    source=source,
                    source_attribution=attribution,
                    status="no_data",
                ))
        except (httpx.HTTPError, Exception) as exc:
            logger.warning("FROST query failed for source %s freshness: %s: %s", source, type(exc).__name__, exc)
            entries.append(FreshnessEntry(
                source=source,
                source_attribution=attribution,
                status="unreachable",
                error=type(exc).__name__,
            ))

    # Sort: fresh first, then stale, then offline, then unreachable
    status_order = {"fresh": 0, "stale": 1, "offline": 2, "no_data": 3, "unreachable": 4}
    entries.sort(key=lambda x: (status_order.get(x.status, 5), x.age_seconds or 999999))
    return FreshnessResult(sources=entries)
