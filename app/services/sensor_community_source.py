"""Sensor.Community citizen air quality + noise polling source.

Several sensor types can share a Sensor.Community location (a BME280 and a
DHT22 both reporting temperature). Each (location, property) has one owner
type, whose readings go to the location datastream (``sensor_community-{loc}``);
the other types reporting that property get datastreams of their own
(``sensor_community-{loc}-{slug}``, see ``app.sources.sensor_community``). The
owner is chosen when the pair is first seen (best ``OWNER_RANK`` among the
batch's types reporting it), never changes, and is persisted in
``settings.sensor_community_owners_path`` so a restart cannot flip it::

    {"version": 1, "owners": {"6591": {"temperature": "BME280", "humidity": "BME280"}}}
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from app.config import settings
from app.models import SensorReading
from app.services.api_client import AsyncAPIClient
from app.services.rest_polling_source import RestPollingSource
from app.sources.sensor_community import build_entity_set, choose_owner, entry_sensor_type, parse_sensor_data
from app.sta.canonical import resolve

logger = logging.getLogger("connector.sensor_community")

OWNERS_FILE_VERSION = 1

# location id → {canonical observed property → owner sensor type}
Owners = dict[str, dict[str, str]]


def _entry_properties(entry: dict[str, Any]) -> list[str]:
    """Canonical observed properties one response entry (one physical sensor) reports."""
    props: list[str] = []
    for sdv in entry.get("sensordatavalues", []) or []:
        if not isinstance(sdv, dict):
            continue
        canonical = resolve(str(sdv.get("value_type", "")).strip())
        if canonical is not None and canonical.value not in props:
            props.append(canonical.value)
    return props


def _entry_coordinates(location: dict[str, Any]) -> tuple[float, float]:
    try:
        return float(location.get("latitude", 0.0)), float(location.get("longitude", 0.0))
    except (TypeError, ValueError):
        return 0.0, 0.0


def load_owners(path: str | Path) -> Owners:
    """The owner map stored at ``path``; empty, with a WARNING, if it is missing or unreadable.

    Entries that are not ``{location: {property: type}}`` strings are dropped
    (WARNING with their count); the rest is kept.
    """
    file = Path(path)
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("sensor_community: owners file %s not found; starting with no owners", file)
        return {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("sensor_community: owners file %s is unreadable (%s); starting with no owners", file, exc)
        return {}
    if (
        not isinstance(document, dict)
        or document.get("version") != OWNERS_FILE_VERSION
        or not isinstance(document.get("owners"), dict)
    ):
        logger.warning(
            "sensor_community: owners file %s is not a version %d owner map; starting with no owners",
            file,
            OWNERS_FILE_VERSION,
        )
        return {}
    owners: Owners = {}
    dropped = 0
    for loc, props in document["owners"].items():
        if not isinstance(props, dict):
            dropped += 1
            continue
        for prop, sensor_type in props.items():
            if isinstance(prop, str) and isinstance(sensor_type, str) and sensor_type:
                owners.setdefault(str(loc), {})[prop] = sensor_type
            else:
                dropped += 1
    if dropped:
        logger.warning("sensor_community: owners file %s: %d malformed entries ignored", file, dropped)
    return owners


def save_owners(path: str | Path, owners: Owners) -> None:
    """Write the owner map to ``path`` atomically: a temp file in the same directory, then ``os.replace``."""
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    document = {"version": OWNERS_FILE_VERSION, "owners": owners}
    fd, tmp_name = tempfile.mkstemp(prefix=f".{file.name}.", suffix=".tmp", dir=file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


class SensorCommunityPollingSource(RestPollingSource):
    """Polls the Sensor.Community NL-filter endpoint for citizen air quality + noise readings."""

    source_name = "sensor_community"
    uses_dynamic_discovery = True

    def __init__(self, owners_path: str | Path | None = None) -> None:
        super().__init__()
        self._client = AsyncAPIClient(
            headers={"User-Agent": "GeoInsights-Connector/1.0"},
        )
        # Cache discovered location metadata for entity registration.
        # Key: location_id (str) → {"lat": float, "lon": float, "properties": list[str],
        #                           "types": {sensor type: list[str]}}
        # "properties" is the union, in first-seen order, of the canonical observed
        # properties of every entry ever seen at the location: Sensor.Community
        # returns one entry per physical sensor (e.g. an SDS011 for PM and a BME280
        # for temperature/humidity/pressure at the same location.id), and a sensor
        # can be missing from one poll and present in the next. "types" is the same
        # union per sensor type.
        self._discovered_locations: dict[str, dict[str, Any]] = {}
        self._owners_path = Path(owners_path if owners_path is not None else settings.sensor_community_owners_path)
        self._owners: Owners = load_owners(self._owners_path)

    @property
    def owners(self) -> Owners:
        """The owner sensor type per (location, property); persisted, never changed once set."""
        return self._owners

    def is_enabled(self) -> bool:
        return settings.sensor_community_enabled

    def poll_interval(self) -> int:
        return max(10, settings.sensor_community_poll_seconds)

    def entity_sets(self) -> list[dict[str, Any]]:
        """Return entity sets for all discovered Sensor.Community locations.

        Each set lists every observed property seen at its location so far on
        the location sensor, so every reading of a property's owner type has a
        datastream of its own property; each type that reports a property it
        does not own gets a sensor of its own with those datastreams. Locations
        that have not reported a known property yet are left out rather than
        given placeholder datastreams.
        """
        sets: list[dict[str, Any]] = []
        for loc_id, meta in self._discovered_locations.items():
            props = list(meta.get("properties") or [])
            if not props:
                continue
            lat = meta.get("lat", 0.0) or 0.0
            lon = meta.get("lon", 0.0) or 0.0
            location_owners = self._owners.get(loc_id, {})
            secondary = {
                sensor_type: not_owned
                for sensor_type, type_props in (meta.get("types") or {}).items()
                if (not_owned := [prop for prop in type_props if location_owners.get(prop) != sensor_type])
            }
            sets.append(build_entity_set(loc_id, lat, lon, props, secondary=secondary or None))
        return sets

    def _discover(self, data: list[Any]) -> None:
        """Merge the locations, observed properties and sensor types of one response into the cache.

        Then give every (location, property) of the response without an owner
        the best-ranked type of the response's entries reporting it, and
        persist the owner map if that added any.
        """
        reported: dict[str, dict[str, set[str]]] = {}  # loc → property → types in this response
        for entry in data:
            if not isinstance(entry, dict):
                continue
            location = entry.get("location", {}) or {}
            if not isinstance(location, dict):
                continue
            loc_id = location.get("id")
            if loc_id is None:
                continue
            loc_str = str(loc_id)
            props = _entry_properties(entry)
            sensor_type = entry_sensor_type(entry)
            lat, lon = _entry_coordinates(location)
            for prop in props:
                reported.setdefault(loc_str, {}).setdefault(prop, set()).add(sensor_type)

            known = self._discovered_locations.get(loc_str)
            if known is None:
                types: dict[str, list[str]] = {sensor_type: list(props)} if props else {}
                self._discovered_locations[loc_str] = {"lat": lat, "lon": lon, "properties": props, "types": types}
                logger.info(
                    "sensor_community: discovered location %s (lat=%s, lon=%s, props=%s)",
                    loc_str,
                    lat,
                    lon,
                    props,
                )
                continue

            if not (known.get("lat") or known.get("lon")) and (lat or lon):
                known["lat"], known["lon"] = lat, lon
            merged: list[str] = known.setdefault("properties", [])
            added = [prop for prop in props if prop not in merged]
            if added:
                merged.extend(added)
                logger.info(
                    "sensor_community: location %s also reports %s (now %s)",
                    loc_str,
                    added,
                    merged,
                )
            if props:
                type_props: list[str] = known.setdefault("types", {}).setdefault(sensor_type, [])
                type_props.extend(prop for prop in props if prop not in type_props)

        self._assign_owners(reported)

    def _assign_owners(self, reported: dict[str, dict[str, set[str]]]) -> None:
        """Give each reported (location, property) without an owner its best-ranked type; persist if any."""
        assigned: list[str] = []
        for loc_str, by_property in reported.items():
            for prop, sensor_types in by_property.items():
                location_owners = self._owners.setdefault(loc_str, {})
                if prop in location_owners:
                    continue
                owner = choose_owner(prop, sensor_types)
                location_owners[prop] = owner
                if len(sensor_types) > 1:
                    logger.info(
                        "sensor_community: location %s %s owned by %s (also reported by %s)",
                        loc_str,
                        prop,
                        owner,
                        sorted(sensor_types - {owner}),
                    )
                assigned.append(f"{loc_str}::{prop}")
        if not assigned:
            return
        try:
            save_owners(self._owners_path, self._owners)
        except OSError:
            # The map stays in memory, so owners hold for this process; the next
            # assignment retries the write.
            logger.warning(
                "sensor_community: could not write owners file %s (%d new owners kept in memory)",
                self._owners_path,
                len(assigned),
                exc_info=True,
            )

    async def _do_fetch_readings(self) -> list[SensorReading]:
        """Fetch latest readings from the Sensor.Community NL-filter endpoint."""
        url = settings.sensor_community_api_url
        data = await self._client.get(url)

        if not isinstance(data, list):
            logger.warning(
                "sensor_community: response is not a list (got %s)", type(data).__name__
            )
            return []

        # Update the discovered-locations cache and the owner map before parsing,
        # so entity_sets() reflects every location, property and type seen, even
        # if parse yields 0 readings, and every reading's (location, property)
        # has an owner.
        self._discover(data)

        readings = parse_sensor_data(data, owners=self._owners)
        return readings
