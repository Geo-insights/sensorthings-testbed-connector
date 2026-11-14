"""MCP tool implementations for Geo-Insights environmental data.

Each tool queries the FROST SensorThings API and returns structured results
with source attribution for CC-BY compliance.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.sta.canonical import CanonicalDatastream, DatastreamMeta, resolve
from mcp_server.errors import invalid_coordinates, invalid_parameter
from mcp_server.frost_client import CircuitOpen, FrostReadClient

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


async def list_sources(client: FrostReadClient) -> list[dict[str, Any]]:
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

    return list(sources.values())


async def get_parameters() -> list[dict[str, Any]]:
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

    result: list[dict[str, Any]] = []
    for member in CanonicalDatastream:
        meta: DatastreamMeta = member.meta
        ucum_code = meta.ucum if meta.ucum else meta.unit
        result.append({
            "name": member.value,
            "display_name": meta.display_name,
            "unit_display": meta.unit,
            "unit_ucum": ucum_code,
            "definition_url": meta.definition,
            "aliases": sorted(
                set(alias_map.get(member.value, [])) | {member.value}
            ),
        })

    return result


async def get_latest_readings(
    client: FrostReadClient,
    lat: float,
    lon: float,
    radius_km: float = 10.0,
    parameter: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Get latest environmental readings near a location."""
    # Validate coordinates
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return {"error": True, **invalid_coordinates(lat, lon)}

    # Validate parameter if provided
    canonical = None
    if parameter:
        canonical = resolve(parameter)
        if canonical is None:
            valid = sorted(m.value for m in CanonicalDatastream)
            return {"error": True, **invalid_parameter(parameter, valid)}

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

    readings: list[dict[str, Any]] = []
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

        readings.append({
            "parameter": ds.get("ObservedProperty", {}).get("name", ds.get("name", "")),
            "value": obs.get("result"),
            "unit": ds.get("unitOfMeasurement", {}).get("symbol", ""),
            "timestamp": obs.get("phenomenonTime"),
            "station_name": thing.get("name", ""),
            "lat": coords[1] if len(coords) > 1 else None,
            "lon": coords[0] if len(coords) > 0 else None,
            "source": source,
            "source_attribution": _get_attribution(source),
        })

    return {"readings": readings, "count": len(readings)}


async def get_data_freshness(client: FrostReadClient) -> list[dict[str, Any]]:
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
    result: list[dict[str, Any]] = []
    for source, _thing_ids in sorted(sources.items()):
        entry: dict[str, Any] = {
            "source": source,
            "source_attribution": _get_attribution(source),
        }
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
                    entry["latest_observation"] = obs_time_str
                    entry["age_seconds"] = age
                    if age < 3600:
                        entry["status"] = "fresh"
                    elif age < 86400:
                        entry["status"] = "stale"
                    else:
                        entry["status"] = "offline"
                else:
                    entry["status"] = "no_data"
                    entry["age_seconds"] = None
            else:
                entry["status"] = "no_data"
                entry["age_seconds"] = None
        except (httpx.HTTPError, Exception) as exc:
            logger.warning("FROST query failed for source %s freshness: %s: %s", source, type(exc).__name__, exc)
            entry["status"] = "unreachable"
            entry["age_seconds"] = None
            entry["error"] = type(exc).__name__

        result.append(entry)

    # Sort: fresh first, then stale, then offline, then unreachable
    status_order = {"fresh": 0, "stale": 1, "offline": 2, "no_data": 3, "unreachable": 4}
    result.sort(key=lambda x: (status_order.get(x["status"], 5), x.get("age_seconds") or 999999))
    return result
