"""Luchtmeetnet RIVM air quality polling source."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.rest_polling_source import RestPollingSource
from app.sources.luchtmeetnet import (
    build_entity_set,
    canonical_components,
    is_valid_wgs84,
    parse_measurements,
    parse_station_detail,
)

logger = logging.getLogger("connector.luchtmeetnet")

# GET /stations ignores ``per_page`` and serves 25 stations per page (103
# stations over 5 pages in Nov 2026). The cap only stops a runaway loop.
_STATION_LIST_MAX_PAGES = 20
# Upper bound on concurrent GET /stations/{number} requests during discovery.
# The API sits behind a rate limit (HTTP 429 after a few hundred requests in a
# few minutes), so discovery stays gentle.
_DETAIL_CONCURRENCY = 4
# While some station details are still missing (rate limit, network), readings
# are held back for at most this many cycles: the ingest loop registers entity
# sets once, on the first cycle that returns readings, so releasing readings
# early would leave the missing stations without Datastreams until a restart.
# Nothing is lost meanwhile: the look-back reaches back to an hour before the
# process started for the first three cycles.
_MAX_HOLD_BACK_CYCLES = 2
# Measurement look-back per poll cycle. Hourly values appear some time after
# the hour (many not yet at :17), so a one-hour window polled early in the hour
# misses most of them for good; three hours lets the next cycle pick them up.
# The overlap is harmless: the ingest loop skips readings at or before its
# high-water mark per sensor+property.
_MEASUREMENT_LOOKBACK = timedelta(hours=3)
# ...but never further back than this before the process started. After a
# restart the ingest loop seeds its high-water marks from FROST for only part
# of the Datastreams, so re-reading older hours could push them twice.
_STARTUP_LOOKBACK = timedelta(hours=1)


class LuchtmeetnetPollingSource(RestPollingSource):
    """Polls the Luchtmeetnet RIVM API for air quality readings.

    Discovery has two steps. The paged ``GET /stations`` list only carries each
    station's number and name, so its coordinates and measured components come
    from ``GET /stations/{number}``, fetched with bounded concurrency through
    :class:`AsyncAPIClient` (which retries). Details are cached for the life of
    the process: later cycles only fetch stations that are still missing, so a
    failed detail fetch is retried on the next cycle (readings are held back
    meanwhile, see ``_MAX_HOLD_BACK_CYCLES``). A station whose detail record
    has no usable coordinates, or that the detail endpoint does not know, is
    skipped with a warning; it is never registered at a made-up location.

    Each cycle then fetches the recent hourly measurements per station (see
    ``_MEASUREMENT_LOOKBACK``) and keeps only readings for the station's
    canonical components, i.e. exactly the Datastreams that
    :meth:`entity_sets` registers.
    """

    source_name = "luchtmeetnet"
    uses_dynamic_discovery = True

    def __init__(self) -> None:
        super().__init__()
        self._base_url = settings.luchtmeetnet_api_url
        # {station_number: {name, lat, lon, components}} for stations with a complete detail record
        self._discovered_stations: dict[str, dict[str, Any]] = {}
        # {station_number: name} from the paged station list
        self._station_names: dict[str, str] = {}
        self._station_list_complete = False
        # Stations skipped for the life of the process: no usable coordinates in
        # their detail record, or unknown to the detail endpoint (404)
        self._skipped_stations: set[str] = set()
        self._started_at = datetime.now(UTC)
        self._held_back_cycles = 0
        self._readings_released = False

    def is_enabled(self) -> bool:
        return settings.luchtmeetnet_enabled

    def poll_interval(self) -> int:
        return max(10, settings.luchtmeetnet_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        """Return entity sets for discovered stations.

        Stations without valid coordinates or without any supported component
        are left out: they would get a Thing at a wrong place, or a Thing
        without Datastreams.
        """
        sets: list[dict[str, Any]] = []
        for station_number, meta in self._discovered_stations.items():
            point = _station_point(meta)
            if point is None:
                logger.warning(
                    "Luchtmeetnet: station %s has no valid coordinates — not registered", station_number
                )
                continue
            components = _station_components(meta)
            if not canonical_components(components):
                continue
            lat, lon = point
            sets.append(
                build_entity_set(
                    station_number=station_number,
                    station_name=str(meta.get("name", station_number)),
                    lat=lat,
                    lon=lon,
                    components=components,
                )
            )
        return sets

    async def _do_fetch_readings(self) -> list[SensorReading]:
        """Discover stations (no HTTP once complete), then fetch measurements for each."""
        client = AsyncAPIClient(
            base_url=self._base_url,
            headers={"Accept": "application/json", "User-Agent": "GeoInsights-Connector/1.0"},
        )

        await self._discover_stations(client)

        if not self._discovered_stations:
            logger.warning("Luchtmeetnet: no stations discovered — skipping measurement fetch")
            return []

        if not self._readings_released:
            if self._discovery_incomplete() and self._held_back_cycles < _MAX_HOLD_BACK_CYCLES:
                self._held_back_cycles += 1
                logger.warning(
                    "Luchtmeetnet: station discovery incomplete (%d of %d stations resolved); holding "
                    "readings back so the first registration covers every station (cycle %d of at most %d)",
                    len(self._discovered_stations),
                    len(self._station_names),
                    self._held_back_cycles,
                    _MAX_HOLD_BACK_CYCLES,
                )
                return []
            self._readings_released = True

        all_readings: list[SensorReading] = []
        end = datetime.now(UTC)
        start = max(end - _MEASUREMENT_LOOKBACK, self._started_at - _STARTUP_LOOKBACK)
        start_str = start.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        end_str = end.strftime("%Y-%m-%dT%H:%M:%S+00:00")

        for station_number, meta in self._discovered_stations.items():
            allowed = canonical_components(_station_components(meta))
            if not allowed:
                continue
            station_name = str(meta.get("name", station_number))
            try:
                # No ``formula`` filter: the API honours only the first formula of
                # a comma-separated list, which limited every station to NO2.
                resp = await client.get(
                    "/measurements",
                    params={
                        "station_number": station_number,
                        "start": start_str,
                        "end": end_str,
                    },
                )
            except Exception as exc:
                if _is_rate_limited(exc):
                    # Still 429 after the client's retries: further requests would
                    # only prolong the block. The look-back covers the rest next cycle.
                    logger.warning(
                        "Luchtmeetnet: rate limited (HTTP 429) at station %s — stopping this cycle's "
                        "measurement requests",
                        station_number,
                    )
                    break
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

            readings = _registered_readings(
                parse_measurements(data, station_number, station_name), allowed, station_number
            )
            all_readings.extend(readings)
            logger.debug(
                "Luchtmeetnet: station %s yielded %d readings", station_number, len(readings)
            )

        # Oldest first: the ingest loop keeps the last reading it sees per
        # sensor+property as its de-duplication high-water mark.
        all_readings.sort(key=lambda reading: reading.timestamp)
        return all_readings

    async def _discover_stations(self, client: AsyncAPIClient) -> None:
        """Bring ``_discovered_stations`` up to date.

        Fetches the station list until it has been read completely once, then
        the detail record of every listed station that is not resolved yet.
        Makes no HTTP request once every station is resolved or skipped.
        """
        if not self._station_list_complete:
            await self._fetch_station_list(client)

        pending = self._pending_stations()
        if not pending:
            return

        await self._fetch_station_details(client, pending)

        logger.info(
            "Luchtmeetnet: %d of %d stations resolved (%d skipped, %d to retry next cycle)",
            len(self._discovered_stations),
            len(self._station_names),
            len(self._skipped_stations),
            len(self._pending_stations()),
        )

    def _pending_stations(self) -> list[str]:
        """Listed stations that are neither resolved nor skipped yet."""
        return [
            number
            for number in self._station_names
            if number not in self._discovered_stations and number not in self._skipped_stations
        ]

    def _discovery_incomplete(self) -> bool:
        return not self._station_list_complete or bool(self._pending_stations())

    async def _fetch_station_list(self, client: AsyncAPIClient) -> None:
        """Read the paged ``GET /stations`` list into ``_station_names``.

        Stops at the first failed page and leaves the list marked incomplete,
        so the next cycle reads it again; stations already read are kept.
        """
        logger.info("Luchtmeetnet: discovering stations from %s/stations", self._base_url)
        page = 1
        while page <= _STATION_LIST_MAX_PAGES:
            try:
                resp = await client.get("/stations", params={"page": str(page)})
            except Exception:
                logger.exception("Luchtmeetnet: failed to fetch station list (page %d)", page)
                return

            stations = _list_items(resp)
            for station in stations:
                if not isinstance(station, dict):
                    continue
                number = str(station.get("number") or "").strip()
                if not number:
                    continue
                # Same normalisation as before the detail fetch existed: Thing,
                # Location and Sensor names derive from it and must not change.
                name = str(station.get("location") or number).strip() or number
                self._station_names[number] = name

            last_page = _last_page(resp)
            if not stations or last_page is None or page >= last_page:
                break
            page += 1
        else:
            logger.warning(
                "Luchtmeetnet: station list has more than %d pages; reading stopped there",
                _STATION_LIST_MAX_PAGES,
            )

        self._station_list_complete = True

    async def _fetch_station_details(self, client: AsyncAPIClient, numbers: list[str]) -> None:
        """Fetch ``GET /stations/{number}`` for *numbers* with bounded concurrency."""
        semaphore = asyncio.Semaphore(_DETAIL_CONCURRENCY)
        rate_limited = False

        async def _fetch_one(number: str) -> tuple[str, Any, str]:
            nonlocal rate_limited
            async with semaphore:
                if rate_limited:  # don't prolong a block; retried next cycle
                    return number, None, "failed"
                try:
                    return number, await client.get(f"/stations/{quote(number, safe='')}"), "ok"
                except Exception as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                        return number, None, "unknown"
                    if _is_rate_limited(exc):
                        rate_limited = True
                    logger.warning(
                        "Luchtmeetnet: detail fetch failed for station %s (%s: %s) — retrying next cycle",
                        number,
                        type(exc).__name__,
                        exc,
                    )
                    return number, None, "failed"

        results = await asyncio.gather(*(_fetch_one(number) for number in numbers))

        for number, body, outcome in results:
            if outcome == "failed":
                continue
            name = self._station_names.get(number, number)
            if outcome == "unknown":
                self._skipped_stations.add(number)
                logger.warning(
                    "Luchtmeetnet: station %s (%s) is listed but its detail record is not found (404) — skipped",
                    number,
                    name,
                )
                continue
            detail = parse_station_detail(body)
            if detail is None:
                self._skipped_stations.add(number)
                logger.warning(
                    "Luchtmeetnet: station %s (%s) has no usable coordinates in its detail record — skipped",
                    number,
                    name,
                )
                continue
            self._discovered_stations[number] = {
                "name": name,
                "lat": detail.lat,
                "lon": detail.lon,
                "components": detail.components,
            }


def _is_rate_limited(exc: BaseException) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def _list_items(resp: Any) -> list[Any]:
    """Return the station items of a ``GET /stations`` page (list or ``data`` envelope)."""
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        items = resp.get("data")
        if items is None:
            items = resp.get("stations")
        if isinstance(items, list):
            return items
    return []


def _last_page(resp: Any) -> int | None:
    """Return ``pagination.last_page`` of a list response, or ``None`` when absent."""
    if not isinstance(resp, dict):
        return None
    pagination = resp.get("pagination")
    if not isinstance(pagination, dict):
        return None
    raw = pagination.get("last_page")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _station_point(meta: dict[str, Any]) -> tuple[float, float] | None:
    """Return ``(lat, lon)`` of a discovered station, or ``None`` when missing or invalid."""
    try:
        lat = float(meta["lat"])
        lon = float(meta["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not is_valid_wgs84(lat, lon):
        return None
    return lat, lon


def _station_components(meta: dict[str, Any]) -> list[str]:
    """Return a discovered station's component formulas as strings."""
    raw = meta.get("components") or []
    if not isinstance(raw, list):
        return []
    return [str(component) for component in raw]


def _registered_readings(
    readings: list[SensorReading],
    allowed: list[str],
    station_number: str,
) -> list[SensorReading]:
    """Keep the readings that map onto one of the station's registered Datastreams.

    Drops canonical properties the station's detail record does not list: they
    have no Datastream, and the push path's per-sensor fallback lookup could
    route them into a sibling Datastream. Also drops repeated
    property+timestamp pairs (e.g. ``PM25`` and ``PM2.5`` for the same hour).
    """
    allowed_set = set(allowed)
    kept: list[SensorReading] = []
    seen: set[tuple[str, datetime]] = set()
    for reading in readings:
        if reading.observed_property not in allowed_set:
            logger.debug(
                "Luchtmeetnet: station %s reported %s, which its station record does not list — skipped",
                station_number,
                reading.observed_property,
            )
            continue
        key = (reading.observed_property, reading.timestamp)
        if key in seen:
            continue
        seen.add(key)
        kept.append(reading)
    return kept
