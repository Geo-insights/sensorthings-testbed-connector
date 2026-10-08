"""Luchtmeetnet RIVM air quality polling source."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.rest_polling_source import RestPollingSource
from app.sources.luchtmeetnet import SUPPORTED_FORMULAS, build_entity_set, parse_measurements

logger = logging.getLogger("connector.luchtmeetnet")


class LuchtmeetnetPollingSource(RestPollingSource):
    """Polls the Luchtmeetnet RIVM API for air quality readings.

    Stations are discovered dynamically on the first poll cycle via
    ``GET /stations``. Subsequent cycles fetch measurements for all discovered
    stations with a 1-hour look-back window.
    """

    source_name = "luchtmeetnet"
    uses_dynamic_discovery = True

    def __init__(self) -> None:
        super().__init__()
        self._base_url = settings.luchtmeetnet_api_url
        # {station_number: {name, lat, lon, components}} — populated on first fetch
        self._discovered_stations: dict[str, dict[str, Any]] = {}

    def is_enabled(self) -> bool:
        return settings.luchtmeetnet_enabled

    def poll_interval(self) -> int:
        return max(10, settings.luchtmeetnet_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        """Return entity sets for all discovered Luchtmeetnet stations."""
        sets: list[dict[str, Any]] = []
        for station_number, meta in self._discovered_stations.items():
            sets.append(
                build_entity_set(
                    station_number=station_number,
                    station_name=str(meta.get("name", station_number)),
                    lat=float(str(meta.get("lat", 52.0))),
                    lon=float(str(meta.get("lon", 5.0))),
                    components=list(meta.get("components", [])),
                )
            )
        return sets

    async def _do_fetch_readings(self) -> list[SensorReading]:
        """Discover stations (if needed) then fetch measurements for each."""
        client = AsyncAPIClient(
            base_url=self._base_url,
            headers={"Accept": "application/json", "User-Agent": "GeoInsights-Connector/1.0"},
        )

        if not self._discovered_stations:
            await self._discover_stations(client)

        if not self._discovered_stations:
            logger.warning("Luchtmeetnet: no stations discovered — skipping measurement fetch")
            return []

        all_readings: list[SensorReading] = []
        end = datetime.now(UTC)
        start = end - timedelta(hours=1)
        start_str = start.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        formulas_param = ",".join(SUPPORTED_FORMULAS)

        for station_number, meta in self._discovered_stations.items():
            station_name = str(meta.get("name", station_number))
            try:
                resp = await client.get(
                    "/measurements",
                    params={
                        "station_number": station_number,
                        "formula": formulas_param,
                        "start": start_str,
                        "end": end_str,
                    },
                )
            except Exception:
                logger.exception(
                    "Luchtmeetnet: failed to fetch measurements for station %s", station_number
                )
                continue

            if not isinstance(resp, dict):
                logger.warning(
                    "Luchtmeetnet: unexpected response type %s for station %s",
                    type(resp).__name__,
                    station_number,
                )
                continue

            data = resp.get("data", [])
            if not isinstance(data, list):
                logger.warning(
                    "Luchtmeetnet: 'data' field is not a list for station %s", station_number
                )
                continue

            readings = parse_measurements(data, station_number, station_name)
            all_readings.extend(readings)
            logger.debug(
                "Luchtmeetnet: station %s yielded %d readings", station_number, len(readings)
            )

        return all_readings

    async def _discover_stations(self, client: AsyncAPIClient) -> None:
        """Populate ``_discovered_stations`` from the /stations endpoint.

        The API paginates at 25 stations per page. We follow all pages.
        """
        logger.info("Luchtmeetnet: discovering stations from %s/stations", self._base_url)
        page = 1
        max_pages = 20  # safety cap

        while page <= max_pages:
            try:
                resp = await client.get("/stations", params={"page": str(page), "per_page": "100"})
            except Exception:
                logger.exception("Luchtmeetnet: failed to fetch station list (page %d)", page)
                break

            stations: list[Any] = []
            if isinstance(resp, list):
                stations = resp
            elif isinstance(resp, dict):
                fallback: list[Any] = resp.get("stations", []) or []
                stations = resp.get("data", fallback) or []

            if not stations:
                break

            for station in stations:
                if not isinstance(station, dict):
                    continue
                number = str(station.get("number", "")).strip()
                if not number:
                    continue

                name = str(station.get("location", number)).strip() or number
                lat_raw = station.get("latitude")
                lon_raw = station.get("longitude")
                components_raw = station.get("components", "")

                try:
                    lat = float(lat_raw) if lat_raw is not None else 52.0
                    lon = float(lon_raw) if lon_raw is not None else 5.0
                except (ValueError, TypeError):
                    lat, lon = 52.0, 5.0

                if isinstance(components_raw, str):
                    components = [c.strip() for c in components_raw.split(",") if c.strip()]
                elif isinstance(components_raw, list):
                    components = [str(c).strip() for c in components_raw if str(c).strip()]
                else:
                    components = []

                self._discovered_stations[number] = {
                    "name": name,
                    "lat": lat,
                    "lon": lon,
                    "components": components,
                }

            # Check if there are more pages
            if isinstance(resp, dict):
                pagination = resp.get("pagination", {})
                last_page = pagination.get("last_page", 1)
                if page >= last_page:
                    break
            else:
                break

            page += 1

        logger.info(
            "Luchtmeetnet: discovered %d stations", len(self._discovered_stations)
        )
