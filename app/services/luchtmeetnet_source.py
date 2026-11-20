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
# Until station discovery is complete (station list read to its last page,
# every listed station resolved or skipped), readings are held back for at
# most this many cycles, so that the ingest loop's first registration of the
# entity sets covers every station: a guard for stations whose Datastreams are
# not registered yet. Holding back delays readings but does not drop them: the
# first cycle that fetches measurements reads back to where the first
# held-back cycle's window would have started (bounded by
# _MAX_CATCH_UP_LOOKBACK).
_MAX_HOLD_BACK_CYCLES = 2
# Measurement look-back. Hourly values appear some time after the hour (many
# not yet at :17), so a window ending at the poll misses the latest hours; each
# station's window therefore starts this long before the end of its last
# successful fetch, and the next cycle picks those hours up. A station without
# a successful fetch in this process (the first cycle after a restart
# included) starts this long before the end of its first window, which after a
# restart fills the gap since the previous process's last cycle. The overlap
# is harmless: the ingest loop drops readings at or before its high-water mark
# per sensor+property, and after a restart it seeds those marks from FROST for
# every Datastream of the source. A restart does not read back further: the
# seed is best effort (a partial seed re-posts what it did not reach), so a
# longer first window would only re-post more.
_MEASUREMENT_LOOKBACK = timedelta(hours=3)
# Bound on every window: after failed fetches (exceptions, 5xx, malformed
# bodies, a rate limit that stopped the cycle) and after cycles that fetched no
# measurements (discovery not ready). GET /measurements returns at most 1000
# values per request without paging (a 7-day window returned exactly 1000,
# last_page=1); 24 h is about 250 values for a station with every component.
# An outage or hold-back longer than 24 h minus the look-back loses the hours
# before the window.
_MAX_CATCH_UP_LOOKBACK = timedelta(hours=24)


class LuchtmeetnetPollingSource(RestPollingSource):
    """Polls the Luchtmeetnet RIVM API for air quality readings.

    Discovery has two steps. The paged ``GET /stations`` list only carries each
    station's number and name, so its coordinates and measured components come
    from ``GET /stations/{number}``, fetched with bounded concurrency through
    :class:`AsyncAPIClient` (which retries). The list is read again every cycle
    until one read has reached its last page; a failed request or an empty or
    malformed page does not count as the end of the list. Details are cached for
    the life of the process: later cycles only fetch stations that are still
    missing, so a failed detail fetch is retried on the next cycle (readings are
    held back meanwhile, see ``_MAX_HOLD_BACK_CYCLES``). A station whose detail
    record has no usable coordinates, or that the detail endpoint does not know,
    is skipped with a warning; it is never registered at a made-up location.

    Each cycle then fetches the recent hourly measurements per station and
    keeps only readings for the station's canonical components, i.e. exactly
    the Datastreams that :meth:`entity_sets` registers. Each station's window
    starts the look-back before the end of its last successful fetch, so the
    hours of cycles whose request for that station failed are read on its next
    successful fetch (see ``_MEASUREMENT_LOOKBACK`` and
    ``_MAX_CATCH_UP_LOOKBACK``).
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
        self._held_back_cycles = 0
        self._readings_released = False
        # Start of the window the first cycle without a measurement fetch would
        # have read; the next fetch reads back to it (see _MAX_HOLD_BACK_CYCLES).
        self._unfetched_since: datetime | None = None
        # {station_number: end of its last successful measurement fetch}
        self._fetched_until: dict[str, datetime] = {}
        # {station_number: start of the first window it was due}, for stations
        # without a successful measurement fetch yet
        self._due_since: dict[str, datetime] = {}

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

        end = datetime.now(UTC)
        if not self._discovered_stations:
            logger.warning("Luchtmeetnet: no stations discovered — skipping measurement fetch")
            self._note_unfetched(end)
            return []

        if not self._readings_released:
            if self._discovery_incomplete() and self._held_back_cycles < _MAX_HOLD_BACK_CYCLES:
                self._held_back_cycles += 1
                logger.warning(
                    "Luchtmeetnet: station discovery incomplete (%d of %d listed stations resolved, list %s); "
                    "holding readings back so the first registration covers every station (cycle %d of at most %d)",
                    len(self._discovered_stations),
                    len(self._station_names),
                    "complete" if self._station_list_complete else "incomplete",
                    self._held_back_cycles,
                    _MAX_HOLD_BACK_CYCLES,
                )
                self._note_unfetched(end)
                return []
            self._readings_released = True

        all_readings: list[SensorReading] = []
        # Where a station without a successful fetch starts: the plain look-back,
        # or, catching up on cycles without a measurement fetch, where the first
        # of them would have started.
        first_start = end - _MEASUREMENT_LOOKBACK
        if self._unfetched_since is not None:
            first_start = min(first_start, self._unfetched_since)
            self._unfetched_since = None
        due = [
            (station_number, meta, allowed)
            for station_number, meta in self._discovered_stations.items()
            if (allowed := canonical_components(_station_components(meta)))
        ]
        for station_number, _meta, _allowed in due:
            if station_number not in self._fetched_until:
                self._due_since.setdefault(station_number, first_start)
        end_str = _api_time(end)

        for station_number, meta, allowed in due:
            station_name = str(meta.get("name", station_number))
            try:
                # No ``formula`` filter: the API honours only the first formula of
                # a comma-separated list, which limited every station to NO2.
                resp = await client.get(
                    "/measurements",
                    params={
                        "station_number": station_number,
                        "start": _api_time(self._window_start(station_number, end)),
                        "end": end_str,
                    },
                )
            except Exception as exc:
                if _is_rate_limited(exc):
                    # Still 429 after the client's retries: further requests would
                    # only prolong the block. The stations not fetched read back
                    # over this cycle on their next successful fetch.
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

            # Only a well-formed answer moves the station's window on.
            self._fetched_until[station_number] = end
            self._due_since.pop(station_number, None)
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

    def _note_unfetched(self, now: datetime) -> None:
        """Remember where the window of a cycle that fetched no measurements would have started."""
        if self._unfetched_since is None:
            self._unfetched_since = now - _MEASUREMENT_LOOKBACK

    def _window_start(self, station_number: str, end: datetime) -> datetime:
        """Start of a station's measurement window for a cycle ending at *end*.

        The look-back before the end of the station's last successful fetch
        (never later than the look-back before *end*), so the hours of failed
        fetches in between are read too; for a station without a successful
        fetch, the start of the first window it was due. Never more than
        ``_MAX_CATCH_UP_LOOKBACK`` before *end*.
        """
        fetched_until = self._fetched_until.get(station_number)
        if fetched_until is not None:
            start = min(end - _MEASUREMENT_LOOKBACK, fetched_until - _MEASUREMENT_LOOKBACK)
        else:
            start = self._due_since.get(station_number, end - _MEASUREMENT_LOOKBACK)
        return max(start, end - _MAX_CATCH_UP_LOOKBACK)

    async def _fetch_station_list(self, client: AsyncAPIClient) -> None:
        """Read the paged ``GET /stations`` list into ``_station_names``.

        The list counts as complete only once a read has reached
        ``pagination.last_page``, every page on the way returning stations. A
        failed request, or a 200 page without stations or without
        ``pagination.last_page`` (an empty or malformed body), is a failed read:
        the list stays incomplete and the next cycle reads it again. Stations
        already read are kept.
        """
        logger.info("Luchtmeetnet: discovering stations from %s/stations", self._base_url)
        page = 1
        while page <= _STATION_LIST_MAX_PAGES:
            try:
                resp = await client.get("/stations", params={"page": str(page)})
            except Exception:
                logger.exception("Luchtmeetnet: failed to fetch station list (page %d)", page)
                return

            names = _station_names_of(resp)
            last_page = _last_page(resp)
            if not names or last_page is None:
                logger.warning(
                    "Luchtmeetnet: station list page %d came back without %s — reading the list again next cycle",
                    page,
                    "stations" if not names else "pagination.last_page",
                )
                return
            self._station_names.update(names)
            if page >= last_page:
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


def _api_time(moment: datetime) -> str:
    """A UTC time as the start/end measurement parameters take it."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S+00:00")


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


def _station_names_of(resp: Any) -> dict[str, str]:
    """``{station number: name}`` for the usable items of a ``GET /stations`` page."""
    names: dict[str, str] = {}
    for station in _list_items(resp):
        if not isinstance(station, dict):
            continue
        number = str(station.get("number") or "").strip()
        if not number:
            continue
        # Same normalisation as before the detail fetch existed: Thing,
        # Location and Sensor names derive from it and must not change.
        names[number] = str(station.get("location") or number).strip() or number
    return names


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
