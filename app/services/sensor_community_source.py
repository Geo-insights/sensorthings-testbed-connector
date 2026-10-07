"""Sensor.Community citizen air quality + noise polling source."""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.rest_polling_source import RestPollingSource
from app.sources.sensor_community import build_entity_set, parse_sensor_data

logger = logging.getLogger("connector.sensor_community")


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
        self._discovered_locations: dict[str, dict[str, Any]] = {}

    def is_enabled(self) -> bool:
        return settings.sensor_community_enabled

    def poll_interval(self) -> int:
        return max(10, settings.sensor_community_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        """Return entity sets for all discovered Sensor.Community locations."""
        sets: list[dict[str, Any]] = []
        for loc_id, meta in self._discovered_locations.items():
            lat = meta.get("lat", 0.0) or 0.0
            lon = meta.get("lon", 0.0) or 0.0
            props = meta.get("properties", ["pm10", "pm2_5"])
            sets.append(build_entity_set(loc_id, lat, lon, props))
        return sets

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
        # reflects any newly-seen locations even if parse yields 0 readings.
        for entry in data:
            if not isinstance(entry, dict):
                continue
            location = entry.get("location", {}) or {}
            loc_id = location.get("id")
            if loc_id is None:
                continue
            loc_str = str(loc_id)
            if loc_str not in self._discovered_locations:
                try:
                    lat = float(location.get("latitude", 0.0))
                    lon = float(location.get("longitude", 0.0))
                except (TypeError, ValueError):
                    lat, lon = 0.0, 0.0
                # Collect the observed property names from the sensordatavalues
                from app.sta.canonical import resolve

                props: list[str] = []
                for sdv in entry.get("sensordatavalues", []):
                    if not isinstance(sdv, dict):
                        continue
                    vt = str(sdv.get("value_type", "")).strip()
                    canonical = resolve(vt)
                    if canonical is not None and canonical.value not in props:
                        props.append(canonical.value)

                self._discovered_locations[loc_str] = {
                    "lat": lat,
                    "lon": lon,
                    "properties": props or ["pm10", "pm2_5"],
                }
                logger.info(
                    "sensor_community: discovered location %s (lat=%s, lon=%s, props=%s)",
                    loc_str,
                    lat,
                    lon,
                    props,
                )

        readings = parse_sensor_data(data)
        return readings
