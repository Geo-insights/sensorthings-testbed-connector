"""BRO groundwater level polling source (#16; rewritten 2026-11 against the live API).

Polls the BRO GM API (app.sources.bro_groundwater documents its endpoints) for
the groundwater levels of the monitoring wells in BRO_BBOX. Owners deliver
levels to BRO in batches, often months after measuring, so this is a daily
catch-up, not a live feed. Per cycle:

1. Wells: the GMW characteristics search of the bounding box (once per process;
   at most BRO_MAX_WELLS, by BRO id).
2. Per well, once: its monitoring tubes with a groundwater level dossier (GLD)
   from gmw-relations. Wells without one get no Thing.
3. Per tube and dossier: the observationsSummary, then for each regular
   observation that reaches past what we have, only its points after that:
   ``startTVPTime``/``endTVPTime``. "What we have" is the tube's newest point
   fetched by this process, else the polling loop's high-water mark (the newest
   observation in FROST, ``use_marks``), else, on first sight, the last
   BRO_INITIAL_DAYS of the dossier's own data.

Overlapping observations of a dossier are merged per time (a fully assessed
value wins over a provisional one); rejected points are left out. The readings
go to FROST in batches of a few wells (``fetch_reading_batches``), and a batch
registers only its own wells (``entity_sets_for``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.rest_polling_source import RestPollingSource
from app.sources.bro_groundwater import (
    REGULAR,
    WATER_LEVEL,
    Tube,
    Well,
    build_entity_set,
    merge_points,
    parse_points,
    parse_relations,
    parse_summary,
    parse_wells,
    readings_for,
    sensor_id_for,
)

logger = logging.getLogger("connector.bro_groundwater")

# Requests to BRO at once, and wells per batch of readings.
_CONCURRENCY = 4
_WELLS_PER_BATCH = 25
# startTVPTime is Dutch local time without a zone: start this much earlier and let
# the polling loop's dedup drop what was pushed before.
_TVP_MARGIN = timedelta(days=1)


def _tvp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


class BROGroundwaterPollingSource(RestPollingSource):
    """Polls the BRO GM API for groundwater levels of the wells in BRO_BBOX."""

    source_name = "bro_groundwater"
    uses_dynamic_discovery = True

    def __init__(self) -> None:
        super().__init__()
        self._client = AsyncAPIClient(
            base_url=settings.bro_api_url,
            headers={"User-Agent": "GeoInsights-Connector/1.0", "Accept": "application/json"},
            timeout=60.0,
        )
        self._wells: dict[str, Well] = {}
        # Tubes with a GLD per well whose gmw-relations were read (an empty list: none).
        self._tubes: dict[str, list[Tube]] = {}
        # Newest point fetched per sensor_id::water_level.
        self._last: dict[str, datetime] = {}
        self._marks: Mapping[str, datetime] = {}

    # ------------------------------------------------------------------
    # PollingSource interface
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        return settings.bro_enabled

    def poll_interval(self) -> int:
        return max(10, settings.bro_poll_seconds)

    def use_marks(self, marks: Mapping[str, datetime]) -> None:
        """Start a tube without a fetched point at the polling loop's mark for it (its newest row in FROST)."""
        self._marks = marks

    def entity_sets(self) -> list[dict[str, Any]]:
        """One entity set per well with a GLD, among the wells whose relations were read."""
        return [self._entity_set(well_id) for well_id in sorted(self._tubes) if self._tubes[well_id]]

    def entity_sets_for(self, readings: Iterable[SensorReading]) -> list[dict[str, Any]]:
        """The entity sets of the wells these readings come from (a batch registers only those)."""
        sensor_ids = {r.sensor_id for r in readings}
        return [
            self._entity_set(well_id)
            for well_id in sorted(self._tubes)
            if any(sensor_id_for(well_id, t.number) in sensor_ids for t in self._tubes[well_id])
        ]

    def _entity_set(self, well_id: str) -> dict[str, Any]:
        return build_entity_set(self._wells[well_id], [t.number for t in self._tubes[well_id]])

    # ------------------------------------------------------------------
    # Fetching
    # ------------------------------------------------------------------

    async def _do_fetch_readings(self) -> list[SensorReading]:
        """A whole cycle's readings at once: every batch of :meth:`_do_fetch_reading_batches`."""
        return [reading async for batch in self._do_fetch_reading_batches() for reading in batch]

    async def _do_fetch_reading_batches(self) -> AsyncGenerator[list[SensorReading], None]:
        if not self._wells:
            await self._discover_wells()
            if not self._wells:
                logger.warning("BRO: no wells discovered — check BRO_BBOX")
                return
        sem = asyncio.Semaphore(_CONCURRENCY)
        now = datetime.now(UTC)
        well_ids = sorted(self._wells)
        for start in range(0, len(well_ids), _WELLS_PER_BATCH):
            group = well_ids[start : start + _WELLS_PER_BATCH]
            results = await asyncio.gather(*(self._well_readings(w, sem, now) for w in group))
            batch = [reading for readings in results for reading in readings]
            if batch:
                yield batch

    async def _discover_wells(self) -> None:
        bbox = settings.bro_bbox.strip()
        if not bbox:
            logger.warning("BRO: BRO_BBOX is not set — well discovery skipped. Set BRO_BBOX=lat1,lon1,lat2,lon2.")
            return
        try:
            lat1, lon1, lat2, lon2 = (float(p.strip()) for p in bbox.split(","))
        except ValueError:
            logger.warning("BRO: BRO_BBOX has an invalid format — expected lat1,lon1,lat2,lon2")
            return
        body = {"area": {"boundingBox": {"lowerCorner": {"lat": min(lat1, lat2), "lon": min(lon1, lon2)},
                                         "upperCorner": {"lat": max(lat1, lat2), "lon": max(lon1, lon2)}}}}
        try:
            xml_text = await self._client.post_text("/gmw/v1/characteristics/searches", json_body=body)
            wells = parse_wells(xml_text, settings.bro_max_wells)
        except httpx.HTTPStatusError as exc:
            logger.warning("BRO: well search failed (HTTP %d)%s", exc.response.status_code,
                           " — the bounding box may be too large" if exc.response.status_code == 400 else "")
            return
        except Exception:
            logger.exception("BRO: well search failed")
            return
        self._wells = {w.bro_id: w for w in wells}
        logger.info("BRO: %d wells in the bounding box", len(self._wells))

    async def _well_readings(self, well_id: str, sem: asyncio.Semaphore, now: datetime) -> list[SensorReading]:
        """One well's new readings; a failure is logged and gives none (the next cycle tries again)."""
        try:
            if well_id not in self._tubes:
                async with sem:
                    relations = await self._client.get(f"/v1/gmw-relations/{well_id}")
                self._tubes[well_id] = parse_relations(relations)
            readings: list[SensorReading] = []
            for tube in self._tubes[well_id]:
                readings += await self._tube_readings(well_id, tube, sem, now)
            return readings
        except Exception as exc:
            logger.warning("BRO: well %s skipped this cycle: %s", well_id, exc)
            return []

    async def _tube_readings(self, well_id: str, tube: Tube, sem: asyncio.Semaphore,
                             now: datetime) -> list[SensorReading]:
        key = f"{sensor_id_for(well_id, tube.number)}::{WATER_LEVEL}"
        since = self._last.get(key) or self._marks.get(key)
        series: list[tuple[str, Sequence[Any]]] = []
        for gld_id in tube.gld_ids:
            async with sem:
                summary = parse_summary(await self._client.get(f"/gld/v1/objects/{gld_id}/observationsSummary"))
            regular = [o for o in summary if o.observation_type == REGULAR]
            if not regular:
                continue
            start = since
            if start is None:
                ends = [o.end for o in regular if o.end is not None]
                newest = max(ends) if ends else now.date()
                start = datetime(newest.year, newest.month, newest.day, tzinfo=UTC) - timedelta(
                    days=max(1, settings.bro_initial_days))
            for obs in regular:
                if obs.end is not None and obs.end < (start - _TVP_MARGIN).date():
                    continue  # ends before what we have
                async with sem:
                    xml_text = await self._client.get_text(
                        f"/gld/v1/objects/{gld_id}/observations/{obs.observation_id}",
                        params={"startTVPTime": _tvp(start - _TVP_MARGIN), "endTVPTime": _tvp(now + _TVP_MARGIN)},
                    )
                series.append((obs.status, [p for p in parse_points(xml_text) if p.time > start]))
        points = merge_points(series)
        if points:
            self._last[key] = max(p.time for p in points)
        return readings_for(points, well_id, tube.number)
