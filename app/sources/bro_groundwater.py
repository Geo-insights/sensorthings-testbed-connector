"""BRO groundwater monitoring well entity definitions and response mapping.

BRO (Basisregistratie Ondergrond) is the Dutch national subsurface data register.
The BRO groundwater monitoring API exposes well registrations (GMW) and
groundwater level dossiers (GLD) via a JSON REST API.

Multi-step query flow:
  1. POST /gmw/v1/objects/search  — discover wells within a bounding box
  2. GET  /gld/v1/objects?gmwBroId=X  — list GLD dossiers linked to each well
  3. GET  /gld/v1/objects/{gldBroId}  — fetch observations for each dossier

References:
  https://publiek.broservices.nl/gm/gmw/v1/
  https://publiek.broservices.nl/gm/gld/v1/
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.models import SensorReading
from app.sources.normalizers import LevellogNormalizer
from app.sta.canonical import CanonicalDatastream

logger = logging.getLogger(__name__)

# Source-specific description for the single observed property.
_DESCRIPTIONS: dict[str, str] = {
    "water_level": "Groundwater level relative to reference datum (BRO GLD).",
}

# Canonical info used in entity descriptions and readings.
_WATER_LEVEL_META = CanonicalDatastream.WATER_LEVEL.meta


def discover_wells(
    response: dict[str, Any],
    max_wells: int = 500,
) -> list[dict[str, Any]]:
    """Extract monitoring well records from a BRO GMW search response.

    Args:
        response: Parsed JSON body of a POST /gmw/v1/objects/search call.
            Expected shape: ``{"broObjects": [{"broId": "...", ...}]}``.
        max_wells: Maximum number of wells to return.  When the response
            contains more objects a WARNING is logged and the list is
            truncated to ``max_wells``.

    Returns:
        List of dicts, each with ``{"well_id": str, "lat": float, "lon": float}``.
        Wells whose location cannot be parsed are silently skipped.
    """
    bro_objects = response.get("broObjects", [])
    if not isinstance(bro_objects, list):
        logger.warning("BRO GMW search response: 'broObjects' is not a list")
        return []

    total = len(bro_objects)
    if total > max_wells:
        logger.warning(
            "BRO: %d wells found, capping at %d (increase BRO_MAX_WELLS to see all)",
            total,
            max_wells,
        )
        bro_objects = bro_objects[:max_wells]

    wells: list[dict[str, Any]] = []
    for obj in bro_objects:
        if not isinstance(obj, dict):
            continue
        well_id = str(obj.get("broId", "")).strip()
        if not well_id:
            continue

        lat, lon = _parse_location(obj)
        if lat is None or lon is None:
            logger.debug("BRO well %s: could not parse location, skipping", well_id)
            continue

        wells.append({"well_id": well_id, "lat": lat, "lon": lon})

    return wells


def _parse_location(obj: dict[str, Any]) -> tuple[float | None, float | None]:
    """Extract (lat, lon) from a BRO GMW object dict.

    The BRO GMW API returns coordinates in several nested shapes.  We try the
    most common ones in order of preference:

    1. ``deliveredLocation.location.pos``  — space-separated "lat lon" string
    2. ``standardizedLocation.location.pos`` — same format
    3. ``deliveredLocation.coordinates`` — list [lon, lat] (GeoJSON order)
    4. Top-level ``lat`` / ``lon`` keys (hypothetical future change / test mocks)
    """
    # Shape 1 & 2: nested WKT-style "lat lon" string
    for loc_key in ("deliveredLocation", "standardizedLocation"):
        loc = obj.get(loc_key)
        if not isinstance(loc, dict):
            continue
        inner = loc.get("location", {})
        if not isinstance(inner, dict):
            continue
        pos = inner.get("pos", "")
        if pos:
            coords = _parse_pos(pos)
            if coords:
                return coords

    # Shape 3: GeoJSON [lon, lat] list under deliveredLocation.coordinates
    delivered = obj.get("deliveredLocation", {})
    if isinstance(delivered, dict):
        coords = delivered.get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            try:
                return float(coords[1]), float(coords[0])  # lat, lon from [lon, lat]
            except (ValueError, TypeError):
                pass

    # Shape 4: flat lat/lon keys (allows simple test fixtures)
    lat_raw = obj.get("lat")
    lon_raw = obj.get("lon")
    if lat_raw is not None and lon_raw is not None:
        try:
            return float(lat_raw), float(lon_raw)
        except (ValueError, TypeError):
            pass

    return None, None


def _parse_pos(pos: Any) -> tuple[float, float] | None:
    """Parse a WKT-style space-separated "lat lon" string."""
    if not isinstance(pos, str):
        return None
    parts = pos.strip().split()
    if len(parts) >= 2:
        try:
            return float(parts[0]), float(parts[1])
        except (ValueError, TypeError):
            pass
    return None


def build_entity_set(
    well_id: str,
    lat: float,
    lon: float,
) -> dict[str, Any]:
    """Build a CLIMATE_ADAPTATION_ENTITY_SETS-format dict for one BRO well.

    Thing name: "BRO {well_id}"
    Site key: "bro"
    """
    return {
        "site_key": "bro",
        "site_name": "BRO Groundwater",
        "thing": {
            "name": f"BRO {well_id}",
            "description": (
                f"BRO groundwater monitoring well {well_id} "
                f"(Basisregistratie Ondergrond)."
            ),
            "properties": {
                "site": "bro",
                "source": "bro_groundwater",
                "bro_well_id": well_id,
                "network": "BRO",
            },
        },
        "location": {
            "name": f"BRO {well_id} location",
            "description": f"BRO monitoring well {well_id} measurement point.",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {},
        },
        "sensors": [
            {
                "sensor_id": f"bro-{well_id}",
                "name": f"BRO {well_id} sensor",
                "description": (
                    f"Groundwater level sensor for BRO well {well_id} "
                    f"(GLD dossier from Basisregistratie Ondergrond)."
                ),
                "encodingType": "application/json",
                "metadata": "https://publiek.broservices.nl/gm/",
                "observed_properties": ["water_level"],
                "properties": {},
            }
        ],
        "observed_properties": dict(_DESCRIPTIONS),
    }


def parse_observations(
    gld_response: dict[str, Any],
    well_id: str,
) -> list[SensorReading]:
    """Parse a BRO GLD dossier response into SensorReadings.

    The GLD response can contain observations in several shapes depending on
    the API version / dossier type.  We handle the two most common:

    1. ``observations`` key — list of ``{"timestamp": ..., "value": ...}``
       or ``{"phenomenonTime": ..., "result": ...}`` dicts.
    2. ``measuredValues`` key (alternative field name seen in some dossiers).

    Args:
        gld_response: Parsed JSON body of a GET /gld/v1/objects/{gldBroId} call.
        well_id: The GMW BRO ID (e.g. "GMW000000012345") this dossier belongs to.

    Returns:
        List of SensorReadings with canonical WATER_LEVEL property and unit "m".
    """
    # Try both common field names for the observation array.
    obs_list: list[Any] = []
    for key in ("observations", "measuredValues", "observation", "results"):
        candidate = gld_response.get(key)
        if isinstance(candidate, list) and candidate:
            obs_list = candidate
            break

    readings: list[SensorReading] = []
    for obs in obs_list:
        if not isinstance(obs, dict):
            continue

        # Parse timestamp — try several field names.
        ts = _parse_observation_timestamp(obs)
        if ts is None:
            continue

        # Parse value — try several field names.
        value = _parse_observation_value(obs)
        if value is None:
            continue

        normalizer = LevellogNormalizer(water_level=value)
        entry_readings = normalizer.to_readings(
            sensor_id=f"bro-{well_id}",
            sensor_name=f"BRO {well_id} sensor",
            thing_name=f"BRO {well_id}",
            timestamp=ts,
            location="bro",
            quality="good",
        )
        readings.extend(entry_readings)

    return readings


def _parse_observation_timestamp(obs: dict[str, Any]) -> datetime | None:
    """Extract a datetime from an observation dict, trying multiple field names."""
    for key in ("timestamp", "phenomenonTime", "time", "DateTime", "dateTime", "date"):
        raw = obs.get(key)
        if raw is None:
            continue
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _parse_observation_value(obs: dict[str, Any]) -> float | None:
    """Extract a numeric water level value from an observation dict."""
    for key in ("value", "result", "level", "waterLevel", "water_level", "Value", "Result"):
        raw = obs.get(key)
        if raw is None:
            continue
        try:
            return float(raw)
        except (ValueError, TypeError):
            continue
    return None
