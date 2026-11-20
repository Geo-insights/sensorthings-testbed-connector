"""Sensor.Community citizen air quality + noise polling source."""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.rest_polling_source import RestPollingSource
from app.sources.sensor_community import build_entity_set, parse_sensor_data
from app.sta.canonical import resolve

logger = logging.getLogger("connector.sensor_community")


def _entry_properties(entry: dict[str, Any]) -> list[str]:
    """Canonical observed properties one response entry (one physical sensor) reports."""
    props: list[str] = []
    for sdv in entry.get("sensordatavalues", []) or []:
        if not isinstance(sdv, dict):
            continue
        canonical = resolve(str(sdv.get("value_type", "")).strip())
        if canonical is not None and canonical.value not in props:
            props.append(canonical.value)
    return props


def _entry_coordinates(location: dict[str, Any]) -> tuple[float, float]:
    try:
        return float(location.get("latitude", 0.0)), float(location.get("longitude", 0.0))
    except (TypeError, ValueError):
        return 0.0, 0.0


class SensorCommunityPollingSource(RestPollingSource):
    """Polls the Sensor.Community NL-filter endpoint for citizen air quality + noise readings."""

    source_name = "sensor_community"
    uses_dynamic_discovery = True

    def __init__(self) -> None:
        super().__init__()
        self._client = AsyncAPIClient(
            headers={"User-Agent": "GeoInsights-Connector/1.0"},
        )
        # Cache discovered location metadata for entity registration.
        # Key: location_id (str) → {"lat": float, "lon": float, "properties": list[str]}
        # "properties" is the union, in first-seen order, of the canonical observed
        # properties of every entry ever seen at the location: Sensor.Community
        # returns one entry per physical sensor (e.g. an SDS011 for PM and a BME280
        # for temperature/humidity/pressure at the same location.id), and a sensor
        # can be missing from one poll and present in the next.
        self._discovered_locations: dict[str, dict[str, Any]] = {}

    def is_enabled(self) -> bool:
        return settings.sensor_community_enabled

    def poll_interval(self) -> int:
        return max(10, settings.sensor_community_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        """Return entity sets for all discovered Sensor.Community locations.

        Each set lists every observed property seen at its location so far, so
        every reading the location produces has a datastream of its own
        property. Locations that have not reported a known property yet are
        left out rather than given placeholder datastreams.
        """
        sets: list[dict[str, Any]] = []
        for loc_id, meta in self._discovered_locations.items():
            props = list(meta.get("properties") or [])
            if not props:
                continue
            lat = meta.get("lat", 0.0) or 0.0
            lon = meta.get("lon", 0.0) or 0.0
            sets.append(build_entity_set(loc_id, lat, lon, props))
        return sets

    def _discover(self, data: list[Any]) -> None:
        """Merge the locations and observed properties of one response into the cache."""
        for entry in data:
            if not isinstance(entry, dict):
                continue
            location = entry.get("location", {}) or {}
            if not isinstance(location, dict):
                continue
            loc_id = location.get("id")
            if loc_id is None:
                continue
            loc_str = str(loc_id)
            props = _entry_properties(entry)
            lat, lon = _entry_coordinates(location)

            known = self._discovered_locations.get(loc_str)
            if known is None:
                self._discovered_locations[loc_str] = {"lat": lat, "lon": lon, "properties": props}
                logger.info(
                    "sensor_community: discovered location %s (lat=%s, lon=%s, props=%s)",
                    loc_str,
                    lat,
                    lon,
                    props,
                )
                continue

            if not (known.get("lat") or known.get("lon")) and (lat or lon):
                known["lat"], known["lon"] = lat, lon
            merged: list[str] = known.setdefault("properties", [])
            added = [prop for prop in props if prop not in merged]
            if added:
                merged.extend(added)
                logger.info(
                    "sensor_community: location %s also reports %s (now %s)",
                    loc_str,
                    added,
                    merged,
                )

    async def _do_fetch_readings(self) -> list[SensorReading]:
        """Fetch latest readings from the Sensor.Community NL-filter endpoint."""
        url = settings.sensor_community_api_url
        data = await self._client.get(url)

        if not isinstance(data, list):
            logger.warning(
                "sensor_community: response is not a list (got %s)", type(data).__name__
            )
            return []

        # Update the discovered-locations cache before parsing so entity_sets()
        # reflects every location and property seen, even if parse yields 0 readings.
        self._discover(data)

        readings = parse_sensor_data(data)
        return readings
