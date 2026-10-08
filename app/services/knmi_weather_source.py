"""KNMI weather station polling source.

Uses the KNMI Data Platform EDR API (CC-BY-4.0) by default.  Requires a free
API key from https://developer.dataplatform.knmi.nl — set ``KNMI_API_KEY``.

Also supports the legacy Buienradar JSON feed for backward compatibility
(auto-detected from the URL).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.rest_polling_source import RestPollingSource
from app.sources.knmi_weather import (
    build_entity_set,
    parse_edr_locations,
    parse_station_measurements,
)

logger = logging.getLogger("connector.knmi_weather")


class KNMIWeatherPollingSource(RestPollingSource):
    """Polls KNMI weather stations for 10-minute meteorological observations."""

    source_name = "knmi_weather"
    uses_dynamic_discovery = True

    def __init__(self) -> None:
        super().__init__()
        headers: dict[str, str] = {"User-Agent": "GeoInsights-Connector/1.0"}
        if settings.knmi_api_key:
            headers["Authorization"] = settings.knmi_api_key
        self._client = AsyncAPIClient(headers=headers)
        self._discovered_stations: dict[str, dict[str, Any]] = {}
        self._station_names: dict[str, str] = {}
        self._is_buienradar = "buienradar" in settings.knmi_weather_api_url.lower()

    def is_enabled(self) -> bool:
        if not settings.knmi_weather_enabled:
            return False
        if not self._is_buienradar and not settings.knmi_api_key:
            logger.warning(
                "KNMI weather source enabled but KNMI_API_KEY is not set. "
                "Get a free key at https://developer.dataplatform.knmi.nl"
            )
            return False
        return True

    def poll_interval(self) -> int:
        return max(10, settings.knmi_weather_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        sets: list[dict[str, Any]] = []
        for sid, meta in self._discovered_stations.items():
            name = meta.get("name", sid)
            lat = float(meta.get("lat", 0.0))
            lon = float(meta.get("lon", 0.0))
            sets.append(build_entity_set(sid, str(name), lat, lon))
        return sets

    async def _do_fetch_readings(self) -> list[SensorReading]:
        """Fetch latest KNMI station readings.

        Auto-detects the response format:
        - KNMI EDR API → CoverageJSON via ``/area`` query
        - Buienradar feed → ``actual.stationmeasurements`` array (legacy)
        """
        if self._is_buienradar:
            data = await self._client.get(settings.knmi_weather_api_url)
            if not isinstance(data, dict):
                logger.warning("KNMI weather response is not a dict (got %s)", type(data).__name__)
                return []
            return self._parse_buienradar(data)

        # KNMI EDR API: use /area query to get all NL stations in one request
        return await self._fetch_edr()

    def _parse_buienradar(self, data: dict[str, Any]) -> list[SensorReading]:
        """Parse Buienradar JSON feed (legacy path)."""
        actual = data.get("actual", {})
        if not isinstance(actual, dict):
            return []
        measurements = actual.get("stationmeasurements", [])
        if not isinstance(measurements, list):
            return []

        for station in measurements:
            if not isinstance(station, dict):
                continue
            sid = str(station.get("stationid", ""))
            if sid and sid not in self._discovered_stations:
                self._discovered_stations[sid] = {
                    "name": str(station.get("stationname", sid)),
                    "lat": station.get("lat", 0.0),
                    "lon": station.get("lon", 0.0),
                }

        return parse_station_measurements(measurements)

    async def _fetch_edr(self) -> list[SensorReading]:
        """Fetch all NL stations via KNMI EDR /area query (CoverageJSON)."""
        from datetime import timedelta

        now = datetime.now(UTC)
        start = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Netherlands bounding box polygon
        base_url = settings.knmi_weather_api_url.rstrip("/")
        # Strip /locations if present (config may still have the old path)
        if base_url.endswith("/locations"):
            base_url = base_url[: -len("/locations")]

        # The KNMI EDR API requires literal unencoded WKT in the coords
        # parameter (spaces, parentheses, commas must NOT be percent-encoded).
        # httpx always re-encodes query strings, so use requests in a thread.
        import asyncio

        import requests as req

        url = (
            f"{base_url}/area"
            f"?coords=POLYGON((3.3 50.7,7.2 50.7,7.2 53.5,3.3 53.5,3.3 50.7))"
            f"&parameter-name=ta,tg,ff,dd,fxx,pp,rh,vv,rg,r1h"
            f"&datetime={start}/{end}"
        )

        headers = {"User-Agent": "GeoInsights-Connector/1.0"}
        if settings.knmi_api_key:
            headers["Authorization"] = settings.knmi_api_key

        def _do_request() -> dict:
            resp = req.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()

        data = await asyncio.get_event_loop().run_in_executor(None, _do_request)

        if not isinstance(data, dict):
            logger.warning("KNMI EDR response is not a dict (got %s)", type(data).__name__)
            return []

        # Build station name map from /locations (cached after first call)
        if not self._station_names:
            await self._load_station_names()

        stations, readings = parse_edr_locations(data, station_names=self._station_names)

        for st in stations:
            sid = st["id"]
            if sid not in self._discovered_stations:
                self._discovered_stations[sid] = st
                logger.info(
                    "KNMI weather: discovered station %s — %s (lat=%s, lon=%s)",
                    sid, st["name"], st["lat"], st["lon"],
                )

        return readings

    async def _load_station_names(self) -> None:
        """Fetch station metadata from /locations for name mapping."""
        base_url = settings.knmi_weather_api_url.rstrip("/")
        if base_url.endswith("/locations"):
            base_url = base_url[: -len("/locations")]
        try:
            data = await self._client.get(f"{base_url}/locations")
            if isinstance(data, dict):
                for feature in data.get("features", []):
                    fid = feature.get("id", "")
                    name = feature.get("properties", {}).get("name", str(fid))
                    self._station_names[str(fid)] = str(name)
                logger.info("KNMI: loaded %d station names", len(self._station_names))
        except Exception:
            logger.warning("KNMI: could not load station names, using coordinates")
