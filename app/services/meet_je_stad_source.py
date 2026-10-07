"""Meet je Stad urban climate + soil moisture polling source."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.rest_polling_source import RestPollingSource
from app.sources.meet_je_stad import build_entity_set, parse_data_readings

logger = logging.getLogger("connector.meet_je_stad")

# Batch size for ?ids= query parameter (URL length safety)
_BATCH_SIZE = 50


class MeetJeStadPollingSource(RestPollingSource):
    """Polls the Meet je Stad API for urban climate and soil moisture readings."""

    source_name = "meet_je_stad"
    uses_dynamic_discovery = True

    def __init__(self) -> None:
        super().__init__()
        self._client = AsyncAPIClient(
            base_url=settings.meet_je_stad_api_url,
            headers={"User-Agent": "GeoInsights-Connector/1.0"},
        )
        # Cache discovered sensor metadata: {sensor_id: {city, lat, lon}}
        self._discovered_sensors: dict[str, dict[str, Any]] = {}

    def is_enabled(self) -> bool:
        return settings.meet_je_stad_enabled

    def poll_interval(self) -> int:
        return max(10, settings.meet_je_stad_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        """Return entity sets for all discovered sensors."""
        sets: list[dict[str, Any]] = []
        for sid, meta in self._discovered_sensors.items():
            city = meta.get("city", "unknown")
            lat = meta.get("lat", 0.0)
            lon = meta.get("lon", 0.0)
            sets.append(build_entity_set(str(sid), city, float(lat), float(lon)))
        return sets

    async def _do_fetch_readings(self) -> list[SensorReading]:
        """Fetch current readings from the Meet je Stad API.

        Step 1: if no sensors discovered yet, GET ?type=sensors to discover them.
        Step 2: batch sensor IDs and GET ?type=data&ids=X,Y,Z&begin=T-15m&end=T.
        Step 3: parse all data entries via MeetJeStadNormalizer.
        """
        # Step 1: sensor discovery
        if not self._discovered_sensors:
            await self._discover_sensors()
            if not self._discovered_sensors:
                logger.warning("MeetJeStad: no sensors discovered")
                return []

        # Step 2: fetch data in batches
        sensor_ids = list(self._discovered_sensors.keys())
        now = datetime.now(UTC)
        begin = now - timedelta(minutes=15)
        begin_str = begin.strftime("%Y-%m-%dT%H:%M:%S")
        end_str = now.strftime("%Y-%m-%dT%H:%M:%S")

        all_readings: list[SensorReading] = []
        for batch_start in range(0, len(sensor_ids), _BATCH_SIZE):
            batch = sensor_ids[batch_start : batch_start + _BATCH_SIZE]
            ids_str = ",".join(str(sid) for sid in batch)
            try:
                data = await self._client.get(
                    "",
                    params={
                        "type": "data",
                        "format": "json",
                        "ids": ids_str,
                        "begin": begin_str,
                        "end": end_str,
                    },
                )
            except Exception:
                logger.exception("MeetJeStad: failed to fetch data for ids batch %s", ids_str[:80])
                continue

            if not isinstance(data, list):
                logger.warning(
                    "MeetJeStad: data response is not a list (got %s)", type(data).__name__
                )
                continue

            # Group entries by sensor id and parse
            entries_by_sensor: dict[str, list[dict[str, Any]]] = {}
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                sid = str(entry.get("id", ""))
                if not sid:
                    continue
                entries_by_sensor.setdefault(sid, []).append(entry)

            for sid, entries in entries_by_sensor.items():
                meta = self._discovered_sensors.get(sid, {})
                city = meta.get("city", "unknown")
                sensor_name = f"{city} {sid}"
                readings = parse_data_readings(entries, sid, sensor_name)
                all_readings.extend(readings)

        return all_readings

    async def _discover_sensors(self) -> None:
        """Fetch sensor list from ?type=sensors and populate _discovered_sensors."""
        try:
            data = await self._client.get(
                "",
                params={"type": "sensors", "format": "json"},
            )
        except Exception:
            logger.exception("MeetJeStad: sensor discovery failed")
            return

        if not isinstance(data, list):
            logger.warning(
                "MeetJeStad: sensor list response is not a list (got %s)", type(data).__name__
            )
            return

        for sensor in data:
            if not isinstance(sensor, dict):
                continue
            sid = str(sensor.get("id", "")).strip()
            if not sid:
                continue
            city = str(sensor.get("city", "")).strip() or "unknown"
            lat = _coerce_float(sensor.get("latitude", sensor.get("lat"))) or 0.0
            lon = _coerce_float(sensor.get("longitude", sensor.get("lon"))) or 0.0

            if sid not in self._discovered_sensors:
                logger.info(
                    "MeetJeStad: discovered sensor %s in %s (lat=%s, lon=%s)",
                    sid, city, lat, lon,
                )
            self._discovered_sensors[sid] = {"city": city, "lat": lat, "lon": lon}

        logger.info("MeetJeStad: %d sensors discovered", len(self._discovered_sensors))


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
