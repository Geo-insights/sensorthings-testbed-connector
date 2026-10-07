"""BRO groundwater level polling source (#16).

Polls the BRO (Basisregistratie Ondergrond) REST API for groundwater level
observations from GMW monitoring wells within a configured bounding box.

Multi-step fetch flow per cycle:
  1. If no wells discovered yet, POST /gmw/v1/objects/search to find wells.
  2. For each discovered well, GET /gld/v1/objects?gmwBroId=X to list its GLD
     dossiers.
  3. For each GLD dossier, GET /gld/v1/objects/{gldBroId} to fetch observations
     since the last poll.

HTTP 429 responses from the BRO API are logged as warnings; the underlying
AsyncAPIClient already handles transient errors with exponential-backoff retries.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.rest_polling_source import RestPollingSource
from app.sources.bro_groundwater import build_entity_set, discover_wells, parse_observations

logger = logging.getLogger("connector.bro_groundwater")

# How far back to fetch observations on the very first poll (before we have a
# last-seen timestamp).  One full poll interval is a safe default.
_INITIAL_LOOKBACK_SECONDS = 3600


class BROGroundwaterPollingSource(RestPollingSource):
    """Polls the BRO GMW/GLD API for groundwater level observations."""

    source_name = "bro_groundwater"
    uses_dynamic_discovery = True

    def __init__(self) -> None:
        super().__init__()
        self._client = AsyncAPIClient(
            base_url=settings.bro_api_url,
            headers={"User-Agent": "GeoInsights-Connector/1.0", "Accept": "application/json"},
        )
        # well_id -> {lat, lon}
        self._discovered_wells: dict[str, dict[str, float]] = {}
        # well_id -> list of GLD BRO IDs
        self._well_gld_map: dict[str, list[str]] = {}
        # Timestamp of the last successful observation fetch (UTC).
        self._last_fetch: datetime | None = None

    # ------------------------------------------------------------------
    # PollingSource interface
    # ------------------------------------------------------------------

    def is_enabled(self) -> bool:
        return settings.bro_enabled

    def poll_interval(self) -> int:
        return max(10, settings.bro_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        """Return entity sets for all discovered wells."""
        sets: list[dict[str, Any]] = []
        for well_id, coords in self._discovered_wells.items():
            sets.append(build_entity_set(well_id, coords["lat"], coords["lon"]))
        return sets

    # ------------------------------------------------------------------
    # RestPollingSource fetch implementation
    # ------------------------------------------------------------------

    async def _do_fetch_readings(self) -> list[SensorReading]:
        """Full multi-step BRO fetch cycle.

        1. Discover wells (once; cached in ``_discovered_wells``).
        2. Resolve GLD dossier IDs per well (cached in ``_well_gld_map``).
        3. Fetch observations for each GLD dossier since ``_last_fetch``.
        """
        # Step 1: well discovery (one-shot; wells rarely change)
        if not self._discovered_wells:
            await self._discover_wells()
            if not self._discovered_wells:
                logger.warning("BRO: no wells discovered — check BRO_BBOX")
                return []

        # Determine the observation window start
        begin_dt = self._last_fetch or (
            datetime.now(UTC) - timedelta(seconds=_INITIAL_LOOKBACK_SECONDS)
        )
        begin_str = begin_dt.strftime("%Y-%m-%dT%H:%M:%S")

        all_readings: list[SensorReading] = []

        for well_id in list(self._discovered_wells.keys()):
            # Step 2: resolve GLD IDs for this well (cached per well)
            if well_id not in self._well_gld_map:
                gld_ids = await self._fetch_gld_ids(well_id)
                self._well_gld_map[well_id] = gld_ids

            # Step 3: fetch observations from each GLD dossier
            for gld_id in self._well_gld_map.get(well_id, []):
                readings = await self._fetch_gld_observations(
                    gld_id, well_id, begin_str
                )
                all_readings.extend(readings)

        # Update the last-fetch timestamp to now so the next cycle only pulls
        # observations newer than this cycle.
        self._last_fetch = datetime.now(UTC)
        return all_readings

    # ------------------------------------------------------------------
    # Private API helpers
    # ------------------------------------------------------------------

    async def _discover_wells(self) -> None:
        """POST /gmw/v1/objects/search to find wells within the configured bbox."""
        bbox = settings.bro_bbox.strip()
        if not bbox:
            logger.warning(
                "BRO: BRO_BBOX is not set — well discovery skipped.  "
                "Set BRO_BBOX=lat1,lon1,lat2,lon2 to enable."
            )
            return

        try:
            lat1, lon1, lat2, lon2 = [float(p.strip()) for p in bbox.split(",")]
        except ValueError:
            logger.warning(
                "BRO: BRO_BBOX has invalid format %r — expected lat1,lon1,lat2,lon2",
                bbox,
            )
            return

        body = {
            "area": {
                "boundingBox": {
                    "lowerCorner": {"lat": min(lat1, lat2), "lon": min(lon1, lon2)},
                    "upperCorner": {"lat": max(lat1, lat2), "lon": max(lon1, lon2)},
                }
            }
        }

        try:
            response = await self._client.post(
                "/gmw/v1/objects/search",
                json_body=body,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning(
                    "BRO: GMW search received HTTP 429 (rate limited) — "
                    "well discovery skipped this cycle"
                )
            else:
                logger.exception("BRO: GMW search request failed (HTTP %d)", exc.response.status_code)
            return
        except Exception:
            logger.exception("BRO: GMW search request failed")
            return

        if not isinstance(response, dict):
            logger.warning(
                "BRO: GMW search returned unexpected type %s", type(response).__name__
            )
            return

        wells = discover_wells(response, max_wells=settings.bro_max_wells)
        for w in wells:
            well_id = w["well_id"]
            if well_id not in self._discovered_wells:
                logger.info(
                    "BRO: discovered well %s at lat=%.6f lon=%.6f",
                    well_id, w["lat"], w["lon"],
                )
            self._discovered_wells[well_id] = {"lat": w["lat"], "lon": w["lon"]}

        logger.info("BRO: %d wells in discovery cache", len(self._discovered_wells))

    async def _fetch_gld_ids(self, well_id: str) -> list[str]:
        """GET /gld/v1/objects?gmwBroId=X and return GLD dossier BRO IDs."""
        try:
            response = await self._client.get(
                "/gld/v1/objects",
                params={"gmwBroId": well_id},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning(
                    "BRO: GLD list for well %s received HTTP 429 (rate limited)",
                    well_id,
                )
            else:
                logger.warning(
                    "BRO: GLD list for well %s failed (HTTP %d)",
                    well_id, exc.response.status_code,
                )
            return []
        except Exception:
            logger.exception("BRO: failed to fetch GLD IDs for well %s", well_id)
            return []

        gld_ids: list[str] = []

        # Response may be a list of objects or a wrapper dict.
        objects: list[Any] = []
        if isinstance(response, list):
            objects = response
        elif isinstance(response, dict):
            for key in ("broObjects", "objects", "registrations", "results"):
                candidate = response.get(key)
                if isinstance(candidate, list):
                    objects = candidate
                    break

        for obj in objects:
            if not isinstance(obj, dict):
                continue
            gld_id = str(obj.get("broId", obj.get("id", ""))).strip()
            if gld_id:
                gld_ids.append(gld_id)

        logger.debug("BRO: well %s has %d GLD dossier(s)", well_id, len(gld_ids))
        return gld_ids

    async def _fetch_gld_observations(
        self,
        gld_id: str,
        well_id: str,
        begin_str: str,
    ) -> list[SensorReading]:
        """GET /gld/v1/objects/{gldId} and parse observations into SensorReadings."""
        try:
            response = await self._client.get(
                f"/gld/v1/objects/{gld_id}",
                params={"observationPeriod.beginDate": begin_str},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                logger.warning(
                    "BRO: observations for GLD %s received HTTP 429 (rate limited)",
                    gld_id,
                )
            else:
                logger.warning(
                    "BRO: observations for GLD %s failed (HTTP %d)",
                    gld_id, exc.response.status_code,
                )
            return []
        except Exception:
            logger.exception("BRO: failed to fetch observations for GLD %s", gld_id)
            return []

        if not isinstance(response, dict):
            logger.debug(
                "BRO: GLD %s returned unexpected type %s", gld_id, type(response).__name__
            )
            return []

        return parse_observations(response, well_id)
