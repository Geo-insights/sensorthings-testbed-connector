"""Samen Meten RIVM citizen science STA federation polling source.

Polls the Samen Meten STA API (https://api-samenmeten.rivm.nl/v1.0), discovers
Things and their Datastreams, fetches new Observations incrementally, and
publishes them to our FROST targets via the standard polling loop.

Pattern: STA-to-STA federation — we consume an OGC SensorThings API and
re-publish to our own FROST servers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.rest_polling_source import RestPollingSource
from app.sources.samen_meten import (
    build_entity_set,
    parse_observations,
    parse_things_response,
    samen_meten_sensor_id,
)

logger = logging.getLogger("connector.samen_meten")

# Default look-back window when no previous timestamp is known for a Datastream.
_DEFAULT_LOOKBACK_HOURS = 1

# Seconds between batches when SAMEN_METEN_BATCH_SECONDS is not positive.
_DEFAULT_BATCH_SECONDS = 300


class SamenMetenPollingSource(RestPollingSource):
    """Polls the Samen Meten RIVM STA API for air quality readings."""

    source_name = "samen_meten"
    uses_dynamic_discovery = True

    def __init__(self) -> None:
        super().__init__()
        self._client = AsyncAPIClient(
            base_url=settings.samen_meten_api_url,
            headers={"User-Agent": "GeoInsights-Connector/1.0"},
        )
        # Discovered Things: {thing_id: {"name": str, "lat": float, "lon": float,
        #                                "datastreams": [{"ds_id": int, "observed_property": str}]}}
        self._discovered_things: dict[int, dict[str, Any]] = {}
        # Last observation timestamp per Datastream id (for incremental fetch)
        self._last_ts: dict[int, datetime] = {}
        # Datastreams that returned errors — skipped until retry (1h cooldown)
        self._dead_ds: dict[int, datetime] = {}

    def is_enabled(self) -> bool:
        return settings.samen_meten_enabled

    def poll_interval(self) -> int:
        return max(10, settings.samen_meten_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        """Return entity sets for all discovered Things."""
        return [self._entity_set(thing_id, meta) for thing_id, meta in self._discovered_things.items()]

    def entity_sets_for(self, readings: Iterable[SensorReading]) -> list[dict[str, Any]]:
        """The entity sets of the Things ``readings`` come from.

        The polling loop registers these before it pushes a batch; it registers
        the rest of :meth:`entity_sets` after the cycle's last batch.
        """
        sensor_ids = {reading.sensor_id for reading in readings}
        return [
            self._entity_set(thing_id, meta)
            for thing_id, meta in self._discovered_things.items()
            if samen_meten_sensor_id(thing_id) in sensor_ids
        ]

    @staticmethod
    def _entity_set(thing_id: int, meta: dict[str, Any]) -> dict[str, Any]:
        props = list({ds["observed_property"] for ds in meta.get("datastreams", [])})
        return build_entity_set(
            thing_name=meta["name"],
            lat=meta["lat"],
            lon=meta["lon"],
            observed_properties=props,
            thing_id=thing_id,
        )

    async def _do_fetch_readings(self) -> list[SensorReading]:
        """A whole cycle's readings at once: every batch of :meth:`_do_fetch_reading_batches`."""
        return [reading async for batch in self._do_fetch_reading_batches() for reading in batch]

    async def _do_fetch_reading_batches(self) -> AsyncGenerator[list[SensorReading], None]:
        """Fetch new observations from the Samen Meten STA API, in batches.

        Step 1: if no Things discovered yet, paginate through /Things until all
                Things are loaded (respects samen_meten_max_things cap).
        Step 2: for each discovered Datastream, GET
                /Datastreams({id})/Observations filtered to new data.
                Uses concurrent fetches (20 at a time) and skips datastreams
                that previously returned errors (retried once per hour).
        Step 3: parse observations into SensorReadings, and yield those fetched
                so far every ``samen_meten_batch_seconds``. A cycle reads 11,000+
                datastreams and can take two hours; the fetches go on while the
                polling loop pushes a batch.
        """
        # Step 1: discover Things
        if not self._discovered_things:
            await self._discover_things()
            if not self._discovered_things:
                logger.warning("samen_meten: no Things discovered")
                return

        # Step 2 & 3: fetch observations per Datastream (concurrent)
        sem = asyncio.Semaphore(20)
        now = datetime.now(UTC)

        # Retry blacklisted datastreams once per hour
        retry_cutoff = now - timedelta(hours=1)

        async def _fetch_one(thing_id: int, meta: dict, ds_info: dict) -> list[SensorReading]:
            ds_id: int = ds_info["ds_id"]
            observed_property: str = ds_info["observed_property"]

            # Skip blacklisted datastreams (retry after 1 hour)
            blacklisted_at = self._dead_ds.get(ds_id)
            if blacklisted_at is not None and blacklisted_at > retry_cutoff:
                return []

            last = self._last_ts.get(ds_id)
            if last is None:
                last = now - timedelta(hours=_DEFAULT_LOOKBACK_HOURS)
            filter_ts = last.strftime("%Y-%m-%dT%H:%M:%SZ")

            async with sem:
                try:
                    obs_data = await self._client.get(
                        f"/Datastreams({ds_id})/Observations",
                        params={
                            "$filter": f"phenomenonTime gt {filter_ts}",
                            "$orderby": "phenomenonTime asc",
                            "$top": "100",
                        },
                    )
                except Exception:
                    # Blacklist this datastream to skip on future cycles
                    self._dead_ds[ds_id] = now
                    return []

            if not isinstance(obs_data, dict):
                return []

            observations = obs_data.get("value", [])
            if not isinstance(observations, list) or not observations:
                return []

            # Clear from blacklist on success
            self._dead_ds.pop(ds_id, None)

            thing_name = meta["name"]
            sensor_id = samen_meten_sensor_id(thing_id)
            sensor_name = f"SamenMeten {thing_name} sensor"
            full_thing_name = f"SamenMeten {thing_name}"

            readings = parse_observations(
                observations=observations,
                sensor_id=sensor_id,
                sensor_name=sensor_name,
                observed_property=observed_property,
                thing_name=full_thing_name,
            )
            if readings:
                self._last_ts[ds_id] = max(r.timestamp for r in readings)
            return readings

        # One task per Datastream; the semaphore bounds how many run at once.
        pending = {
            asyncio.create_task(_fetch_one(thing_id, meta, ds_info))
            for thing_id, meta in self._discovered_things.items()
            for ds_info in meta.get("datastreams", [])
        }
        batch_seconds = settings.samen_meten_batch_seconds
        if batch_seconds <= 0:
            batch_seconds = _DEFAULT_BATCH_SECONDS
        try:
            while pending:
                done, pending = await asyncio.wait(pending, timeout=batch_seconds)
                # A Datastream whose fetch raised gives nothing this cycle, as before.
                batch = [
                    reading
                    for task in done
                    if not task.cancelled() and task.exception() is None
                    for reading in task.result()
                ]
                if batch:
                    yield batch
        finally:
            # The polling loop stopped reading batches (a push raised): stop fetching.
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        skipped = sum(1 for ts in self._dead_ds.values() if ts > retry_cutoff)
        if skipped:
            logger.info("samen_meten: skipped %d blacklisted datastreams", skipped)

    async def _discover_things(self) -> None:
        """Paginate through /Things until all are loaded (or optional cap is hit)."""
        max_things = settings.samen_meten_max_things  # 0 = no cap

        # First page URL — relative path; client will prepend base_url
        next_path: str | None = (
            "/Things"
            "?$expand=Locations,Datastreams($expand=ObservedProperty)"
            "&$top=100"
        )

        while next_path:
            try:
                data = await self._client.get(next_path)
            except Exception:
                logger.exception("samen_meten: Things discovery page failed (%s)", next_path)
                break

            if not isinstance(data, dict):
                logger.warning(
                    "samen_meten: Things response is not a dict (got %s)", type(data).__name__
                )
                break

            things = parse_things_response(data)
            for thing in things:
                thing_id = thing["thing_id"]
                if thing_id not in self._discovered_things:
                    logger.debug(
                        "samen_meten: discovered Thing %d %r (%d datastreams)",
                        thing_id,
                        thing["name"],
                        len(thing["datastreams"]),
                    )
                self._discovered_things[thing_id] = {
                    "name": thing["name"],
                    "lat": thing["lat"],
                    "lon": thing["lon"],
                    "datastreams": thing["datastreams"],
                }

            if max_things and len(self._discovered_things) >= max_things:
                logger.info(
                    "samen_meten: reached max_things cap (%d) — stopping discovery",
                    max_things,
                )
                break

            # Follow @iot.nextLink for the next page
            next_link = data.get("@iot.nextLink")
            if next_link:
                base = settings.samen_meten_api_url
                if str(next_link).startswith(base):
                    next_path = str(next_link)[len(base):]
                else:
                    next_path = str(next_link)
            else:
                next_path = None

        logger.info("samen_meten: %d Things discovered", len(self._discovered_things))
