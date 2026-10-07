"""KNMI weather station polling source.

Data is sourced from KNMI (Royal Netherlands Meteorological Institute) via the
Buienradar JSON feed (https://data.buienradar.nl/2.0/feed/json), which
republishes the 41 official KNMI station measurements under CC-BY-4.0.

The default feed URL can be overridden via ``KNMI_WEATHER_API_URL`` to point at
the KNMI Data Platform EDR API instead (requires a free API key set via
``KNMI_API_KEY``; the EDR endpoint returns CoverageJSON which would need a
dedicated parser).
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.rest_polling_source import RestPollingSource
from app.sources.knmi_weather import build_entity_set, parse_station_measurements

logger = logging.getLogger("connector.knmi_weather")


class KNMIWeatherPollingSource(RestPollingSource):
    """Polls the Buienradar/KNMI feed for weather readings from KNMI stations."""

    source_name = "knmi_weather"
    uses_dynamic_discovery = True

    def __init__(self) -> None:
        super().__init__()
        self._client = AsyncAPIClient(
            headers={"User-Agent": "GeoInsights-Connector/1.0"},
        )
        # Cache discovered station metadata for entity registration
        self._discovered_stations: dict[str, dict[str, Any]] = {}

    def is_enabled(self) -> bool:
        return settings.knmi_weather_enabled

    def poll_interval(self) -> int:
        return max(10, settings.knmi_weather_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        """Return entity sets for all discovered KNMI stations."""
        sets: list[dict[str, Any]] = []
        for sid, meta in self._discovered_stations.items():
            name = meta.get("name", sid)
            lat = float(meta.get("lat", 0.0))
            lon = float(meta.get("lon", 0.0))
            sets.append(build_entity_set(sid, str(name), lat, lon))
        return sets

    async def _do_fetch_readings(self) -> list[SensorReading]:
        """Fetch the latest KNMI station readings from the Buienradar feed.

        1. GET the feed URL (default: https://data.buienradar.nl/2.0/feed/json)
        2. Extract ``actual.stationmeasurements`` array
        3. Cache station metadata for ``entity_sets()``
        4. Parse all measurements into SensorReadings
        """
        url = settings.knmi_weather_api_url
        data = await self._client.get(url)

        if not isinstance(data, dict):
            logger.warning("KNMI weather feed response is not a dict (got %s)", type(data).__name__)
            return []

        actual = data.get("actual", {})
        if not isinstance(actual, dict):
            logger.warning("KNMI weather feed: 'actual' key missing or not a dict")
            return []

        measurements = actual.get("stationmeasurements", [])
        if not isinstance(measurements, list):
            logger.warning("KNMI weather feed: 'stationmeasurements' is not a list")
            return []

        # Cache station metadata for entity registration
        for station in measurements:
            if not isinstance(station, dict):
                continue
            sid = str(station.get("stationid", ""))
            if not sid:
                continue
            if sid not in self._discovered_stations:
                name = str(station.get("stationname", sid))
                lat = station.get("lat", 0.0)
                lon = station.get("lon", 0.0)
                self._discovered_stations[sid] = {"name": name, "lat": lat, "lon": lon}
                logger.info(
                    "KNMI weather: discovered station %s — %s (lat=%s, lon=%s)",
                    sid, name, lat, lon,
                )

        logger.info(
            "KNMI weather feed: %d stations in response", len(measurements)
        )

        return parse_station_measurements(measurements)
