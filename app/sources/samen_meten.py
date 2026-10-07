"""Samen Meten RIVM citizen science STA federation — parser and entity builder.

Samen Meten is a RIVM citizen-science network of air quality sensors across the
Netherlands. The data is exposed as an OGC SensorThings API at
https://api-samenmeten.rivm.nl/v1.0

Source follows a polling-then-forwarding pattern: we query the remote STA,
parse Things + Observations, and re-publish them to our own FROST targets.

API surface used
----------------
- GET /Things?$expand=Locations,Datastreams($expand=ObservedProperty)&$top=100
  → paginated via @iot.nextLink
- GET /Datastreams({id})/Observations?$filter=phenomenonTime gt {ts}&$orderby=phenomenonTime asc&$top=100
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading
from app.sta.canonical import resolve

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Samen Meten ObservedProperty name → canonical key mapping
# The remote names look like "Particulate Matter < 2.5 µm" so we normalise
# them here before routing through canonical.resolve().
# ---------------------------------------------------------------------------

_SM_NAME_MAP: dict[str, str] = {
    # PM — many naming variants found in the wild
    "particulate matter < 2.5 µm": "pm2_5",
    "particulate matter < 2.5 um": "pm2_5",
    "pm2.5": "pm2_5",
    "pm25": "pm2_5",
    "fijnstof pm2.5": "pm2_5",
    "fijnstof pm25": "pm2_5",
    "pm 2.5": "pm2_5",
    "particulate matter < 10 µm": "pm10",
    "particulate matter < 10 um": "pm10",
    "fijnstof pm10": "pm10",
    "pm 10": "pm10",
}


def _map_observed_property_name(raw_name: str) -> str | None:
    """Map a Samen Meten ObservedProperty name to a canonical key.

    First checks the local name map (for PM names that can't reach canonical
    via resolve() alone), then falls back to canonical.resolve().

    Returns a canonical key string or None if unrecognised.
    """
    if not raw_name:
        return None
    normalised = raw_name.strip().lower()
    # Local overrides first
    if normalised in _SM_NAME_MAP:
        canonical_key = _SM_NAME_MAP[normalised]
        canon = resolve(canonical_key)
        return canon.value if canon is not None else None
    # Fall through to canonical resolver
    canon = resolve(raw_name)
    return canon.value if canon is not None else None


# ---------------------------------------------------------------------------
# Source-specific descriptions
# ---------------------------------------------------------------------------

_DESCRIPTIONS: dict[str, str] = {
    "pm2_5": "Particulate matter concentration (PM2.5) in ambient air.",
    "pm10": "Particulate matter concentration (PM10) in ambient air.",
    "temperature": "Outdoor air temperature at Samen Meten sensor location.",
    "air_temperature": "Outdoor air temperature at Samen Meten sensor location.",
    "humidity": "Relative humidity at Samen Meten sensor location.",
    "relative_humidity": "Relative humidity at Samen Meten sensor location.",
    "no2": "Nitrogen dioxide concentration in ambient air.",
    "o3": "Ozone concentration in ambient air.",
    "co": "Carbon monoxide concentration in ambient air.",
    "co2": "Carbon dioxide concentration in ambient air.",
    "so2": "Sulfur dioxide concentration in ambient air.",
    "nh3": "Ammonia concentration in ambient air.",
}


# ---------------------------------------------------------------------------
# parse_things_response
# ---------------------------------------------------------------------------


def parse_things_response(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse a STA /Things response into a list of discovered sensor dicts.

    Each returned dict has the shape::

        {
            "thing_id": int,
            "name": str,
            "lat": float,
            "lon": float,
            "datastreams": [{"ds_id": int, "observed_property": str}],
        }

    - ``observed_property`` is the canonical key (e.g. "pm2_5").
    - Things without a usable location are silently skipped.
    - Datastreams with unrecognised ObservedProperty names are silently skipped.
    - The ``@iot.nextLink`` key (if present) signals more pages; callers must
      handle pagination themselves.
    """
    things: list[dict[str, Any]] = []
    value = data.get("value", [])
    if not isinstance(value, list):
        logger.warning("samen_meten: Things response 'value' is not a list")
        return things

    for thing in value:
        if not isinstance(thing, dict):
            continue

        thing_id = thing.get("@iot.id") or thing.get("id")
        thing_name = str(thing.get("name", "")).strip()
        if not thing_id or not thing_name:
            continue

        # --- Location ---
        lat, lon = _extract_location(thing)
        if lat is None or lon is None:
            logger.debug("samen_meten: Thing %s has no usable location — skipped", thing_id)
            continue

        # --- Datastreams ---
        datastreams_raw = thing.get("Datastreams", [])
        if not isinstance(datastreams_raw, list):
            datastreams_raw = []

        datastreams: list[dict[str, Any]] = []
        for ds in datastreams_raw:
            if not isinstance(ds, dict):
                continue
            ds_id = ds.get("@iot.id") or ds.get("id")
            if not ds_id:
                continue

            op = ds.get("ObservedProperty")
            if not isinstance(op, dict):
                continue
            op_name = str(op.get("name", "")).strip()
            canonical = _map_observed_property_name(op_name)
            if canonical is None:
                logger.debug(
                    "samen_meten: Thing %s DS %s observed_property %r — not canonical, skipped",
                    thing_id, ds_id, op_name,
                )
                continue

            datastreams.append({"ds_id": int(ds_id), "observed_property": canonical})

        things.append(
            {
                "thing_id": int(thing_id),
                "name": thing_name,
                "lat": float(lat),
                "lon": float(lon),
                "datastreams": datastreams,
            }
        )

    return things


def _extract_location(thing: dict[str, Any]) -> tuple[float | None, float | None]:
    """Extract (lat, lon) from a STA Thing dict that has been expanded with Locations."""
    locations = thing.get("Locations", [])
    if not isinstance(locations, list):
        return None, None
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        geo = loc.get("location")
        if not isinstance(geo, dict):
            continue
        if geo.get("type") == "Point":
            coords = geo.get("coordinates")
            if isinstance(coords, (list, tuple)) and len(coords) >= 2:
                try:
                    return float(coords[1]), float(coords[0])  # lat, lon from [lon, lat]
                except (TypeError, ValueError):
                    continue
    return None, None


# ---------------------------------------------------------------------------
# parse_observations
# ---------------------------------------------------------------------------


def parse_observations(
    observations: list[dict[str, Any]],
    sensor_id: str,
    sensor_name: str,
    observed_property: str,
    thing_name: str,
) -> list[SensorReading]:
    """Convert STA Observation objects into SensorReadings.

    :param observations: List of STA observation dicts with keys
        ``phenomenonTime`` and ``result``.
    :param sensor_id: Sensor identifier string (e.g. "sm-12345").
    :param sensor_name: Human-readable sensor name.
    :param observed_property: Canonical observed property key (e.g. "pm2_5").
    :param thing_name: Thing name used for the SensorReading ``thing_name`` field.
    """
    from app.sta.canonical import CanonicalDatastream

    canon = resolve(observed_property)
    if canon is None:
        logger.warning(
            "samen_meten: parse_observations called with non-canonical %r — skipped",
            observed_property,
        )
        return []

    meta = CanonicalDatastream(canon.value).meta

    readings: list[SensorReading] = []
    for obs in observations:
        if not isinstance(obs, dict):
            continue
        result = obs.get("result")
        if result is None:
            continue
        try:
            value = float(result)
        except (TypeError, ValueError):
            continue

        ts_raw = obs.get("phenomenonTime", "")
        if ts_raw:
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(UTC)
        else:
            ts = datetime.now(UTC)

        readings.append(
            SensorReading(
                sensor_id=sensor_id,
                sensor_name=sensor_name,
                observed_property=canon.value,
                observed_property_name=meta.display_name,
                unit=meta.unit,
                value=value,
                timestamp=ts,
                thing_name=thing_name,
                location="samen_meten",
                quality="good",
            )
        )

    return readings


# ---------------------------------------------------------------------------
# build_entity_set
# ---------------------------------------------------------------------------


def build_entity_set(
    thing_name: str,
    lat: float,
    lon: float,
    observed_properties: list[str],
) -> dict[str, Any]:
    """Build a CLIMATE_ADAPTATION_ENTITY_SETS-format dict for one Samen Meten thing.

    :param thing_name: Original Thing name from the remote STA (used as suffix).
    :param lat: WGS-84 latitude.
    :param lon: WGS-84 longitude.
    :param observed_properties: List of canonical observed property keys.

    Thing name: "SamenMeten {thing_name}".
    Site key: "samen_meten".
    """
    full_thing_name = f"SamenMeten {thing_name}"
    # Sanitise sensor_id: strip spaces/special chars for a URL-safe id
    sensor_id = f"sm-{thing_name.lower().replace(' ', '-').replace('/', '-')}"

    valid_props = [p for p in observed_properties if p in _DESCRIPTIONS]

    return {
        "site_key": "samen_meten",
        "site_name": "Samen Meten RIVM",
        "thing": {
            "name": full_thing_name,
            "description": (
                f"Samen Meten RIVM citizen science sensor {thing_name}."
            ),
            "properties": {
                "site": "samen_meten",
                "source": "samen_meten",
                "network": "SamenMeten",
                "operator": "RIVM",
                "original_name": thing_name,
            },
        },
        "location": {
            "name": f"SamenMeten {thing_name} location",
            "description": f"Samen Meten measurement point {thing_name}.",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {},
        },
        "sensors": [
            {
                "sensor_id": sensor_id,
                "name": f"SamenMeten {thing_name} sensor",
                "description": f"Samen Meten citizen science sensor ({thing_name}).",
                "encodingType": "application/json",
                "metadata": "https://samenmeten.nl",
                "observed_properties": valid_props,
                "properties": {},
            }
        ],
        "observed_properties": {k: v for k, v in _DESCRIPTIONS.items() if k in valid_props},
    }
