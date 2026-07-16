"""Ohnics air quality polling source."""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.polling_source import PollingSource
from app.sources.ohnics import build_entity_set, parse_sensor_readings

logger = logging.getLogger("connector.ohnics")


class OhnicsPollingSource(PollingSource):
    """Polls the Ohnics 5min.json endpoint for air quality readings in Delft."""

    source_name = "ohnics"

    def __init__(self) -> None:
        self._client = AsyncAPIClient(
            headers={"User-Agent": "GeoInsights-Connector/1.0"},
            verify_ssl=False,  # ohnics.online has a TLS cert issue
        )
        self._prefix = settings.ohnics_sensor_prefix
        # Cache discovered sensor metadata for entity registration
        self._discovered_sensors: dict[str, dict[str, Any]] = {}

    def is_enabled(self) -> bool:
        return settings.ohnics_enabled

    def poll_interval(self) -> int:
        return max(10, settings.ohnics_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        """Return entity sets for all discovered Delft sensors."""
        sets: list[dict[str, Any]] = []
        for name, meta in self._discovered_sensors.items():
            lat = meta.get("lat", 52.01)
            lon = meta.get("lon", 4.36)
            props = meta.get("properties", ["pm25"])
            sets.append(build_entity_set(name, lat, lon, props))
        return sets

    async def fetch_readings(self) -> list[SensorReading]:
        """Fetch latest 5-min readings from Ohnics and return SensorReadings for Delft sensors."""
        url = settings.ohnics_api_url
        try:
            data = await self._client.get(url)
        except Exception:
            logger.exception("Failed to fetch Ohnics data from %s", url)
            return []

        if not isinstance(data, list):
            logger.warning("Ohnics response is not a list (got %s)", type(data).__name__)
            return []

        all_readings: list[SensorReading] = []
        for sensor_data in data:
            if not isinstance(sensor_data, dict):
                continue
            name = str(sensor_data.get("Name", sensor_data.get("name", "")))
            if not name.startswith(self._prefix):
                continue

            # Cache sensor metadata for entity registration
            if name not in self._discovered_sensors:
                lat = sensor_data.get("Lat", sensor_data.get("lat", 0.0))
                lon = sensor_data.get("Lon", sensor_data.get("lon", 0.0))
                self._discovered_sensors[name] = {"lat": lat, "lon": lon, "properties": ["pm25"]}
                logger.info("Discovered Ohnics sensor: %s (lat=%s, lon=%s)", name, lat, lon)

            readings = parse_sensor_readings(sensor_data)
            all_readings.extend(readings)

        if all_readings:
            logger.info("Ohnics: fetched %d readings from %d Delft sensors", len(all_readings), len(self._discovered_sensors))
        return all_readings
