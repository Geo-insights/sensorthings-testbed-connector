"""Move the KNMI Datastreams, Sensors and Things in FROST to stable station ids.

Until fix/knmi-station-ids the KNMI parser named each station after the position
of its Coverage in the EDR /area response: sensor_id "knmi-0" ... "knmi-56",
Thing property knmi_station_id "0" ... "56". That position shifts whenever a
station appears in or drops out of a response, so each Datastream carries the
index its station had when the Datastream was created (on frost.geo-insights.nl
most KNMI Things mix two), and after every shift readings went into the
Datastreams of the station that held their index before. The fixed connector
identifies a station by its WIGOS id from the API ("eumetnet:locationId"):
sensor_id "knmi-0-20000-0-06260", knmi_station_id "0-20000-0-06260".

The connector keys its datastream cache warm-up, its restart seeding and the
push path's check that a cached datastream belongs to the reading's sensor on
the Datastreams' properties.sensor_id. A sensor id that changes in the
connector but not on the server makes the fixed connector refuse its cached
mappings (as an unPATCHed streamKey rename once dropped observations), so this
script renames them on the server. Registration finds Things, Locations, Sensors
and Datastreams by name and never updates properties, and the names stay
coordinate based ("<prefix>KNMI KNMI-52.36-3.34"), so the connector creates and
renames nothing: Thing, Location, Sensor and Datastream ids stay as they are.

For every Thing named "<prefix>KNMI KNMI-<lat>-<lon>" whose properties.source is
knmi_weather, the script reads its Location, checks that the Thing's name is the
one the connector derives from the Location's coordinates, and looks those
coordinates up in the KNMI EDR /locations station list (one station within
0.0005 degrees, as the connector does for a Coverage without its id; the API
serves the same coordinates on both endpoints). It then plans PATCHes of:

* each Datastream of the Thing: properties.sensor_id -> knmi-<WIGOS id>, and any
  other property whose value is the old sensor id or starts with
  "<old sensor id>::" (none on production: streamKey is the observed property);
* the Thing's Sensors whose properties carry a KNMI sensor_id (production
  Sensors have no properties, so normally none);
* the Thing: properties.knmi_station_id -> <WIGOS id>.

Each PATCH sends the entity's whole properties object with only those values
changed (FROST replaces the object). Nothing is created or deleted. A Thing
whose name does not match its Location, whose coordinates match no station or
several, or whose station another Thing also matches is reported and left
alone; so is a Datastream whose sensor_id is not a KNMI one. Re-running is safe:
entities already on their station's id are not touched. After --apply the
script reads everything again and reports what is left (nothing, normally).

Rollout order: migrate first, then deploy
-----------------------------------------
1. Dry run; check the plan (production, 2026-11-20: 57 Things, 627 Datastreams).
2. --apply --journal FILE while the old connector image still runs (see "The
   journal and the way back"). The old image keeps routing as before: its cache
   maps "knmi-<index>::<property>" to datastream ids, filled at startup, and it
   never reads properties.sensor_id again; its registration checks cached ids by
   name only.
3. Deploy the fixed connector, which must include the restart seeding of
   fix/seeding-followups (c9c1b6d). At startup its warm-up maps the keys its
   readings use ("knmi-<WIGOS id>::temperature") from the migrated properties
   and purges the persisted "knmi-<index>::..." entries (they now name another
   sensor's datastream); registration finds every entity by name. The
   Observations keep their old labels (sensor_id "knmi-<index>"), so no row is
   labelled with a new key: restart seeding marks a Datastream's new key from
   its newest Observation only when that row is the same station's under the
   older sensor id (same canonical observed property, parameters.thing_name the
   Thing's name without the entity name prefix). Where the newest row is
   another's (another station's misplaced reading, a TGV or Sensor.Community
   row), the new key gets no mark and the first cycle posts the station's
   latest readings again, now into its own Datastream; where the old image had
   put them into another station's Datastream, scripts/fix_knmi_observations.py
   reports those as twins (--twins-file) instead of moving them. Without
   c9c1b6d no new key is marked and the first cycle re-posts every KNMI
   Datastream's latest value.
4. Dry run again: nothing left. Stations that appeared between 2 and 3 (created
   by the old image under an index) show up here: --apply again, with the same
   journal.

The journal and the way back
----------------------------
Every PATCH replaces a whole properties object, so --apply needs --journal FILE:
before each PATCH it appends {entity_set, id, name, old properties, new
properties, target} as one JSON line to FILE and fsyncs it, and prints the same
line to stdout behind "JOURNAL ". A journal that cannot be written stops the run
before the next PATCH. Re-runs append to the same file.

--rollback --journal FILE (a dry run unless --apply) reads the current
properties of every journaled entity and PATCHes the old object back where the
entity is still as the migration left it (its properties equal the recorded
new object; several records of one entity are walked back from the newest).
An entity changed since is reported and left alone (exit 3), as is one deleted
since. It refuses a journal recorded on another FROST target. It does not need
the KNMI API. A rollback puts the old "knmi-<index>" ids back, so it only makes
sense while the old image runs (before step 3), or together with a redeploy of
the old image.

The script runs inside the connector container, so FILE is a path there. Use
the data volume (/app/data, kept when the container is recreated by the deploy)
and keep stdout as a second copy outside the container: tee it on the machine
the command is run from. --rollback reads either, the journal file or a saved
stdout (it takes the "JOURNAL " lines and skips the rest):

    docker compose exec -T connector python -u - --apply \
        --journal /app/data/knmi_sensor_ids_journal.jsonl \
        < scripts/migrate_knmi_sensor_ids.py | tee knmi_sensor_ids_apply.log
    docker compose cp connector:/app/data/knmi_sensor_ids_journal.jsonl .
    docker compose exec -T connector python -u - --rollback \
        --journal /app/data/knmi_sensor_ids_journal.jsonl [--apply] < scripts/migrate_knmi_sensor_ids.py

If the file inside the container is lost, copy the saved stdout in
(docker compose cp knmi_sensor_ids_apply.log connector:/app/data/) and pass
that as --journal.

Deploying first delivers most readings, but worse: every cached mapping is
refused (the datastream belongs to sensor knmi-<index>), so each (Thing,
property) is resolved by a live lookup on Thing name and streamKey; that lookup
takes FROST's first match, which for ground temperature is the stale '... -
Ground temperature' Datastream (every production KNMI Thing has one, with the
lower id). Restart seeding finds no high-water mark under the new keys, so the
first cycle posts the readings FROST already holds again (one duplicate per
datastream with a current reading). Migrating afterwards does not end this
until the connector restarts: its warm-up read the old properties. Both orders
are exercised in tests/integration/test_migrate_knmi_sensor_ids.py against
FROST 2.8 on PostGIS 16.

Stations with two Datastreams of one observed property (production: 57
'... - Ground temperature' Datastreams from before the canonical rename next to
'... - Ground surface temperature') end up sharing a key. The warm-up maps the
key to the lower id, the old one, until registration, which runs before the
first push of every cycle with new stations, maps it to the Datastream it
registers ('Ground surface temperature'). The plan lists these pairs.

SensorThings v1.1 and v2.0 endpoints both work (FROST serves both from one
database). Query options follow the target's declared ``version``; ids and next
links in responses are read in either convention, so a v2.0 URL declared as
v1.1 (the connector's default, and production's setting) works as well.

    python -m scripts.migrate_knmi_sensor_ids                               # dry run
    python -m scripts.migrate_knmi_sensor_ids --apply --journal FILE        # write
    python -m scripts.migrate_knmi_sensor_ids --rollback --journal FILE     # undo: dry run
    python -m scripts.migrate_knmi_sensor_ids --rollback --journal FILE --apply

The script only imports ``app.config``, ``app.exceptions`` and ``app.frost``,
which predate it, so it can be piped into a running connector container whose
image does not contain it; arguments go after ``-``, and ``-u`` keeps the output
unbuffered:

    docker compose exec -T connector python -u - < scripts/migrate_knmi_sensor_ids.py
    docker compose exec -T connector python -u - --apply --journal /app/data/knmi_sensor_ids_journal.jsonl \
        < scripts/migrate_knmi_sensor_ids.py | tee knmi_sensor_ids_apply.log

The FROST target and its credentials come from the connector's environment
(SENSORTHINGS_BASE_URL / SENSORTHINGS_BASE_URLS and the SENSORTHINGS_AUTH_*
variables), the KNMI API key from KNMI_API_KEY; none of them is printed.
``--target-url`` picks one of the configured targets; credentials are never
sent to a URL that is not configured. ``--locations-file`` reads a saved
/locations response instead of asking the API.

Exit status (the first that applies, in this order):
    1  a FROST request failed (reading the Things, or a PATCH), a PATCH did not
       stick, or the journal could not be written or read
    2  the KNMI station list could not be read; nothing was planned
    3  Things or Datastreams were left alone (see the report): re-running won't
       help until they are looked at; with --rollback: entities changed since
       the migration were left alone
    0  otherwise
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from app.config import settings
from app.exceptions import FrostConnectionError
from app.frost.http_client import FrostHTTPClient
from app.frost.odata_fields import FrostODataFields, odata_fields_for
from app.frost.target import FrostTarget

SOURCE = "knmi_weather"
SENSOR_ID_PREFIX = "knmi-"
# Copies of app/sources/knmi_weather.py (LOCATION_MATCH_TOLERANCE_DEG,
# station_name_for, sensor_id_for): the script runs in images without them.
# tests/test_migrate_knmi_sensor_ids.py checks that they agree.
LOCATION_MATCH_TOLERANCE_DEG = 0.0005
DATASTREAMS = "Datastreams"
SENSORS = "Sensors"
THINGS = "Things"
_PAGE_SIZE = 100
_EXPAND_TOP = 1000
_API_TIMEOUT = 30.0
_API_WAITS = (2.0, 5.0, 10.0)  # before the 2nd, 3rd and 4th attempt
_API_HEADERS = {"Accept": "application/geo+json, application/json", "User-Agent": "GeoInsights-Connector/1.0"}

EXIT_OK = 0
EXIT_FROST_FAILED = 1
EXIT_KNMI_FAILED = 2
EXIT_LEFT = 3

# The journal: one JSON object per PATCH, {"t": JOURNAL_TAG, "target", "entity_set", "id", "name", "old", "new",
# "at"}, appended and fsynced before the PATCH is sent, and printed to stdout behind JOURNAL_MARKER.
JOURNAL_TAG = "knmi-sensor-id-migration"
JOURNAL_MARKER = "JOURNAL "
ENTITY_SETS = (DATASTREAMS, SENSORS, THINGS)

# Why a Thing is left alone.
NOT_KNMI = "not_knmi"
NO_LOCATION = "no_location"
NAME_MISMATCH = "name_mismatch"
NO_STATION = "no_station"
AMBIGUOUS_STATION = "ambiguous_station"
SHARED_STATION = "shared_station"
REASONS = {
    NOT_KNMI: "properties.source is not knmi_weather",
    NO_LOCATION: "not exactly one Location with a Point",
    NAME_MISMATCH: "the name is not the one the connector derives from the Location",
    NO_STATION: "no KNMI station within 0.0005 degrees of the Location",
    AMBIGUOUS_STATION: "several KNMI stations within 0.0005 degrees of the Location",
    SHARED_STATION: "another KNMI Thing matches the same station",
}

Point = tuple[float, float]  # (lon, lat)


class KnmiApiError(Exception):
    """The KNMI station list could not be read."""


@dataclass(frozen=True)
class Station:
    """A KNMI station from the EDR /locations list."""

    station_id: str  # WIGOS id, e.g. "0-20000-0-06260"
    lon: float
    lat: float
    name: str = ""


@dataclass(frozen=True)
class Patch:
    entity_set: str  # DATASTREAMS, SENSORS or THINGS
    entity_id: str
    entity_name: str
    properties: dict[str, Any]  # the whole new properties object
    changes: tuple[tuple[str, Any, Any], ...]  # (key, old value, new value)
    old_properties: dict[str, Any] = field(default_factory=dict)  # the whole properties object as read


@dataclass
class StationPlan:
    station: Station
    thing_id: str
    thing_name: str
    sensor_id: str  # the new one
    datastreams: int = 0
    old_sensor_ids: Counter[str] = field(default_factory=Counter)  # of the Datastreams to PATCH
    patches: list[Patch] = field(default_factory=list)
    foreign: list[str] = field(default_factory=list)  # Datastreams left alone
    shared_keys: list[str] = field(default_factory=list)  # several Datastreams of one observed property

    def count(self, entity_set: str) -> int:
        return sum(1 for patch in self.patches if patch.entity_set == entity_set)


@dataclass(frozen=True)
class Left:
    thing_id: str | None
    thing_name: str
    reason: str
    detail: str = ""

    def describe(self) -> str:
        reason = REASONS.get(self.reason, self.reason)
        return f"{reason} ({self.detail})" if self.detail else reason


@dataclass
class Plan:
    things_seen: int = 0
    stations: list[StationPlan] = field(default_factory=list)
    left: list[Left] = field(default_factory=list)
    frost_errors: list[str] = field(default_factory=list)

    @property
    def patches(self) -> list[Patch]:
        return [patch for station in self.stations for patch in station.patches]

    def count(self, entity_set: str) -> int:
        return sum(station.count(entity_set) for station in self.stations)

    @property
    def foreign(self) -> list[str]:
        return [item for station in self.stations for item in station.foreign]


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def station_name_for(lat: float, lon: float) -> str:
    """The coordinate-based station name in the connector's Thing names."""
    return f"KNMI-{lat:.2f}-{lon:.2f}"


def sensor_id_for(station_id: str) -> str:
    """The fixed connector's sensor id for a KNMI station (WIGOS id)."""
    return f"{SENSOR_ID_PREFIX}{station_id}"


def thing_prefix(entity_prefix: str) -> str:
    return f"{entity_prefix}KNMI "


def parse_point(value: Any) -> Point | None:
    """``(lon, lat)`` of a GeoJSON Point (bare or wrapped in a Feature)."""
    if not isinstance(value, dict):
        return None
    if value.get("type") == "Feature":
        return parse_point(value.get("geometry"))
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list | tuple) or len(coordinates) < 2:
        return None
    try:
        lon = float(coordinates[0])
        lat = float(coordinates[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    return lon, lat


def format_point(point: Point) -> str:
    return f"POINT({point[0]:g} {point[1]:g})"


def parse_stations(body: Any) -> list[Station]:
    """Stations of an EDR /locations FeatureCollection (Feature id = WIGOS id, Point geometry)."""
    features = body.get("features") if isinstance(body, dict) else None
    stations: list[Station] = []
    for feature in features if isinstance(features, list) else []:
        if not isinstance(feature, dict):
            continue
        raw_id = feature.get("id")
        station_id = str(raw_id).strip() if isinstance(raw_id, str | int) and not isinstance(raw_id, bool) else ""
        point = parse_point(feature.get("geometry"))
        if not station_id or point is None:
            continue
        properties = feature.get("properties")
        name = str(properties.get("name") or "") if isinstance(properties, dict) else ""
        stations.append(Station(station_id, point[0], point[1], name))
    return stations


def match_station(point: Point, stations: Sequence[Station]) -> tuple[Station | None, int]:
    """The one station within the tolerance of ``point``, and how many stations are that close."""
    lon, lat = point
    close = [
        station for station in stations
        if abs(station.lon - lon) <= LOCATION_MATCH_TOLERANCE_DEG and abs(station.lat - lat) <= LOCATION_MATCH_TOLERANCE_DEG
    ]
    ids = {station.station_id for station in close}
    return (close[0] if len(ids) == 1 else None), len(ids)


def rewrite_properties(
    properties: dict[str, Any], old_sensor_id: str, new_sensor_id: str
) -> tuple[dict[str, Any], tuple[tuple[str, Any, Any], ...]]:
    """``properties`` with ``sensor_id`` set to ``new_sensor_id``, and every other
    value that is ``old_sensor_id`` or starts with ``"<old_sensor_id>::"`` moved to
    the new id; plus the changes made."""
    updated = dict(properties)
    changes: list[tuple[str, Any, Any]] = []
    if updated.get("sensor_id") != new_sensor_id:
        changes.append(("sensor_id", updated.get("sensor_id"), new_sensor_id))
        updated["sensor_id"] = new_sensor_id
    if old_sensor_id and old_sensor_id != new_sensor_id:
        for key, value in properties.items():
            if key == "sensor_id" or not isinstance(value, str):
                continue
            if value == old_sensor_id:
                new_value = new_sensor_id
            elif value.startswith(f"{old_sensor_id}::"):
                new_value = new_sensor_id + value[len(old_sensor_id):]
            else:
                continue
            changes.append((key, value, new_value))
            updated[key] = new_value
    return updated, tuple(changes)


_extract_id = FrostHTTPClient.extract_iot_id_from_body


def entity_id(entity: dict[str, Any], fields: FrostODataFields) -> str | None:
    """An entity's id in the target's convention, falling back to the other one."""
    return _extract_id(entity, fields)


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _properties(entity: dict[str, Any]) -> dict[str, Any]:
    properties = entity.get("properties")
    return properties if isinstance(properties, dict) else {}


def _id_order(value: str | None) -> tuple[int, str]:
    text = str(value or "")
    return (int(text), "") if text.isdigit() else (sys.maxsize, text)


def _is_knmi_sensor_id(value: str) -> bool:
    return value.startswith(SENSOR_ID_PREFIX)


def plan_station(
    thing: dict[str, Any], thing_id: str, thing_name: str, station: Station, fields: FrostODataFields
) -> StationPlan:
    """The PATCHes that put one Thing's Datastreams, Sensors and Thing on ``station``'s id."""
    new_sensor_id = sensor_id_for(station.station_id)
    plan = StationPlan(station=station, thing_id=thing_id, thing_name=thing_name, sensor_id=new_sensor_id)
    sensors: dict[str, dict[str, Any]] = {}
    by_property: dict[str, list[str]] = defaultdict(list)
    datastreams = sorted(_dicts(thing.get("Datastreams")), key=lambda ds: _id_order(entity_id(ds, fields)))
    for datastream in datastreams:
        ds_id = entity_id(datastream, fields)
        ds_name = str(datastream.get("name") or "")
        if ds_id is None:
            plan.foreign.append(f"Datastream {ds_name!r}: FROST returned it without an id")
            continue
        plan.datastreams += 1
        properties = _properties(datastream)
        old = str(properties.get("sensor_id") or "").strip()
        by_property[str(properties.get("observed_property") or "")].append(f"{ds_id} {ds_name!r}")
        if old and not _is_knmi_sensor_id(old):
            plan.foreign.append(f"Datastream {ds_id} {ds_name!r}: sensor_id {old!r} is not a KNMI one")
            continue
        new_properties, changes = rewrite_properties(properties, old, new_sensor_id)
        if changes:
            plan.old_sensor_ids[old or "no sensor_id"] += 1
            plan.patches.append(Patch(DATASTREAMS, ds_id, ds_name, new_properties, changes, dict(properties)))
        sensor = datastream.get("Sensor")
        sensor_entity_id = entity_id(sensor, fields) if isinstance(sensor, dict) else None
        if isinstance(sensor, dict) and sensor_entity_id is not None:
            sensors.setdefault(sensor_entity_id, sensor)

    for sensor_entity_id, sensor in sorted(sensors.items(), key=lambda item: _id_order(item[0])):
        properties = _properties(sensor)
        old = str(properties.get("sensor_id") or "").strip()
        if not old or not _is_knmi_sensor_id(old):
            continue  # the connector sets no Sensor properties
        new_properties, changes = rewrite_properties(properties, old, new_sensor_id)
        if changes:
            plan.patches.append(
                Patch(SENSORS, sensor_entity_id, str(sensor.get("name") or ""), new_properties, changes, dict(properties))
            )

    thing_properties = _properties(thing)
    if str(thing_properties.get("knmi_station_id") or "") != station.station_id:
        new_properties = {**thing_properties, "knmi_station_id": station.station_id}
        change = ("knmi_station_id", thing_properties.get("knmi_station_id"), station.station_id)
        plan.patches.append(Patch(THINGS, thing_id, thing_name, new_properties, (change,), dict(thing_properties)))

    for observed_property, entries in sorted(by_property.items()):
        if len(entries) > 1:
            plan.shared_keys.append(f"{observed_property or '(no observed_property)'}: {', '.join(entries)}")
    return plan


def build_plan(
    things: list[dict[str, Any]], stations: Sequence[Station], entity_prefix: str, fields: FrostODataFields
) -> Plan:
    """Match every KNMI Thing to its station and plan the PATCHes."""
    plan = Plan(things_seen=len(things))
    prefix = thing_prefix(entity_prefix)
    matched: list[tuple[dict[str, Any], str, str, Point, Station]] = []
    for thing in sorted(things, key=lambda t: _id_order(entity_id(t, fields))):
        name = str(thing.get("name") or "")
        thing_id = entity_id(thing, fields)
        if thing_id is None:
            plan.frost_errors.append(f"Thing {name!r}: FROST returned it without an id")
            continue
        if _properties(thing).get("source") != SOURCE:
            plan.left.append(Left(thing_id, name, NOT_KNMI, f"source {_properties(thing).get('source')!r}"))
            continue
        locations = _dicts(thing.get("Locations"))
        points = [point for point in (parse_point(loc.get("location")) for loc in locations) if point is not None]
        if len(locations) != 1 or len(points) != 1:
            plan.left.append(Left(thing_id, name, NO_LOCATION, f"{len(locations)} Location(s)"))
            continue
        point = points[0]
        expected = f"{prefix}{station_name_for(point[1], point[0])}"
        if name != expected:
            plan.left.append(Left(thing_id, name, NAME_MISMATCH, f"Location {format_point(point)} names {expected!r}"))
            continue
        station, close = match_station(point, stations)
        if station is None:
            reason = AMBIGUOUS_STATION if close > 1 else NO_STATION
            plan.left.append(Left(thing_id, name, reason, f"Location {format_point(point)}"))
            continue
        matched.append((thing, thing_id, name, point, station))

    claims = Counter(station.station_id for *_, station in matched)
    for thing, thing_id, name, _point, station in matched:
        if claims[station.station_id] > 1:
            plan.left.append(Left(thing_id, name, SHARED_STATION, f"station {station.station_id}"))
            continue
        plan.stations.append(plan_station(thing, thing_id, name, station, fields))
    return plan


def patch_body(patch: Patch) -> dict[str, Any]:
    """PATCH payload; the same on SensorThings v1.1 and v2.0."""
    return {"properties": patch.properties}


def exit_status(plan: Plan, frost_failures: int) -> int:
    if frost_failures or plan.frost_errors:
        return EXIT_FROST_FAILED
    if plan.left or plan.foreign:
        return EXIT_LEFT
    return EXIT_OK


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def select_target(targets: Sequence[FrostTarget], target_url: str | None) -> FrostTarget:
    if not targets:
        raise SystemExit("No FROST target configured (SENSORTHINGS_BASE_URL / SENSORTHINGS_BASE_URLS).")
    if target_url:
        wanted = target_url.rstrip("/")
        for target in targets:
            if target.url.rstrip("/") == wanted:
                return target
        raise SystemExit(f"{wanted} is not a configured FROST target; refusing to send credentials to it.")
    return targets[0]


def url_version(url: str) -> str | None:
    """The SensorThings version in a service root URL's last path segment (``v1.1``, ``v2.0``)."""
    segment = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return segment if re.fullmatch(r"v\d+\.\d+", segment) else None


def edr_base_url(url: str) -> str:
    """The EDR collection URL, without the ``/locations`` the connector's setting may end with."""
    base = url.strip().rstrip("/")
    return base[: -len("/locations")] if base.endswith("/locations") else base


def _frost_failure(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    message = body.get("message") if isinstance(body, dict) else None
    return f"HTTP {response.status_code}" + (f": {str(message)[:200]}" if message else "")


def thing_expand() -> str:
    """``$expand`` for the Things query: the Location and every Datastream with its Sensor."""
    return f"Locations,Datastreams($top={_EXPAND_TOP};$expand=Sensor)"


def fetch_knmi_things(
    http: FrostHTTPClient, things_path: str, entity_prefix: str, fields: FrostODataFields
) -> list[dict[str, Any]]:
    """All Things whose name starts with ``<prefix>KNMI ``, with Locations, Datastreams and Sensors.

    Pages with ``$skip`` in id order; next links from the server only tell
    whether there is another page and are never followed.
    """
    endpoint = http.endpoint(things_path)
    if not endpoint:
        raise SystemExit("No FROST endpoint configured.")
    literal = thing_prefix(entity_prefix).replace("'", "''")
    things: list[dict[str, Any]] = []
    skip = 0
    while True:
        try:
            response = http.get(
                endpoint,
                params={
                    "$filter": f"startswith(name,'{literal}')",
                    "$expand": thing_expand(),
                    "$orderby": f"{fields.id} asc",
                    "$top": str(_PAGE_SIZE),
                    "$skip": str(skip),
                },
            )
        except FrostConnectionError as exc:
            raise SystemExit(f"GET {endpoint} failed: {type(exc.cause or exc).__name__}.") from None
        if not response.ok:
            raise SystemExit(f"GET {endpoint} failed with {_frost_failure(response)}.")
        try:
            body = response.json()
        except ValueError:
            raise SystemExit(f"GET {endpoint} returned a body that is not JSON.") from None
        page = body.get("value") if isinstance(body, dict) else None
        if not isinstance(page, list):
            raise SystemExit(f"GET {endpoint} returned no 'value' list.")
        things.extend(item for item in page if isinstance(item, dict))
        more = body.get(fields.next_link) or body.get("@iot.nextLink") or body.get("@nextLink")
        if not page or not more:
            return things
        skip += len(page)


def fetch_station_list(
    api_url: str,
    api_key: str,
    *,
    http_get: Callable[..., requests.Response] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """GET ``<api_url>/locations`` and return the parsed body.

    Retries HTTP 429, 5xx, timeouts and bodies that are not JSON (after 2, 5,
    10 s); other HTTP errors (401/403: the API key) fail at once. Raises
    :class:`KnmiApiError`, whose message never contains the key.
    """
    send = http_get or requests.get
    pause = sleep or _sleep
    url = f"{edr_base_url(api_url)}/locations"
    headers = dict(_API_HEADERS)
    if api_key:
        headers["Authorization"] = api_key
    error = "no attempt made"
    for attempt in range(len(_API_WAITS) + 1):
        try:
            response = send(url, headers=headers, timeout=_API_TIMEOUT)
        except requests.RequestException as exc:
            error = type(exc).__name__
        else:
            status = response.status_code
            if status == 429 or status >= 500:
                error = f"HTTP {status}"
            elif status >= 400:
                raise KnmiApiError(f"GET {url} failed with HTTP {status}" + ("" if api_key else " (no KNMI_API_KEY set)"))
            else:
                try:
                    return response.json()
                except ValueError:
                    error = "body is not JSON"
        if attempt < len(_API_WAITS):
            pause(_API_WAITS[attempt])
    raise KnmiApiError(f"GET {url} failed after {len(_API_WAITS) + 1} attempts: {error}")


class JournalError(Exception):
    """The journal could not be written or read."""


class Journal:
    """Append-only record of every PATCH: written and fsynced before the PATCH is sent, and echoed to stdout.

    The file is the record ``--rollback`` reads. The stdout copy (each line behind :data:`JOURNAL_MARKER`) is
    for the operator's ``| tee``: the script usually runs inside the connector container, so the file lives
    there (on the data volume); the tee keeps a copy outside. ``--rollback`` reads either.
    """

    def __init__(self, path: Path, target_url: str) -> None:
        self.path = path
        self.target_url = target_url.rstrip("/")

    def record(self, patch: Patch) -> None:
        entry = {
            "t": JOURNAL_TAG,
            "target": self.target_url,
            "entity_set": patch.entity_set,
            "id": patch.entity_id,
            "name": patch.entity_name,
            "old": patch.old_properties,
            "new": patch.properties,
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        line = json.dumps(entry, sort_keys=True)
        try:
            with self.path.open("a", encoding="utf-8", newline="\n") as out:
                out.write(line + "\n")
                out.flush()
                os.fsync(out.fileno())
        except OSError as exc:
            raise JournalError(f"cannot append to the journal {self.path}: {exc}") from exc
        print(JOURNAL_MARKER + line, flush=True)


def apply_plan(http: FrostHTTPClient, plan: Plan, paths: dict[str, str], journal: Journal) -> tuple[int, int]:
    """PATCH every planned entity: Datastreams first, then Sensors, then Things.

    Each PATCH is in the journal before it is sent; when the journal cannot be written, nothing more is sent.
    """
    applied = failed = 0
    order = {DATASTREAMS: 0, SENSORS: 1, THINGS: 2}
    for station in plan.stations:
        for patch in sorted(station.patches, key=lambda p: order[p.entity_set]):
            label = f"{station.station.station_id} {patch.entity_set}({patch.entity_id})"
            endpoint = http.endpoint(f"{paths[patch.entity_set]}({patch.entity_id})")
            if not endpoint:
                failed += 1
                continue
            try:
                journal.record(patch)
            except JournalError as exc:
                print(f"  STOPPED before {label}: {exc}; nothing more was PATCHed")
                return applied, failed + 1
            try:
                response = http.patch(endpoint, patch_body(patch))
            except FrostConnectionError as exc:
                print(f"  FAILED {label}: {type(exc.cause or exc).__name__}")
                failed += 1
                continue
            if response.ok:
                applied += 1
            else:
                failed += 1
                print(f"  FAILED {label}: {_frost_failure(response)}")
    return applied, failed


def _natural(text: str) -> list[Any]:
    """Sort key that orders "knmi-3" before "knmi-10"."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def _describe_ids(counter: Counter[str]) -> str:
    return ", ".join(f"{old} x{count}" for old, count in sorted(counter.items(), key=lambda item: _natural(item[0])))


def print_plan(plan: Plan) -> None:
    for station in plan.stations:
        st = station.station
        header = f"{st.station_id:<17} {st.name[:22]:<22} Thing {station.thing_id:>6} {station.thing_name!r}"
        if not station.patches:
            print(f"{header}: ok, {station.datastreams} Datastreams on {station.sensor_id}")
        else:
            print(header)
            datastreams = station.count(DATASTREAMS)
            if datastreams:
                print(
                    f"    Datastreams: {datastreams} of {station.datastreams} to PATCH "
                    f"({_describe_ids(station.old_sensor_ids)}) -> {station.sensor_id}"
                )
            for patch in station.patches:
                if patch.entity_set == SENSORS:
                    print(f"    Sensor {patch.entity_id} {patch.entity_name!r}: {_describe_changes(patch)}")
                elif patch.entity_set == THINGS:
                    print(f"    Thing: {_describe_changes(patch)}")
                elif any(key != "sensor_id" for key, _old, _new in patch.changes):
                    print(f"    Datastream {patch.entity_id}: {_describe_changes(patch)}")
        for note in station.shared_keys:
            print(f"    note: Datastreams of one observed property share the key {station.sensor_id}::{note}")
        for item in station.foreign:
            print(f"    left alone: {item}")
    for item in plan.left:
        print(f"  left alone: Thing {item.thing_id} {item.thing_name!r}: {item.describe()}")
    for error in plan.frost_errors:
        print(f"  FROST error: {error}")


def _describe_changes(patch: Patch) -> str:
    return ", ".join(f"{key} {old!r} -> {new!r}" for key, old, new in patch.changes)


def print_summary(plan: Plan) -> None:
    to_migrate = sum(1 for station in plan.stations if station.patches)
    print(
        f"\n{plan.things_seen} KNMI Things: {to_migrate} to migrate ({plan.count(DATASTREAMS)} Datastream, "
        f"{plan.count(SENSORS)} Sensor and {plan.count(THINGS)} Thing PATCHes), "
        f"{len(plan.stations) - to_migrate} already on their station id, {len(plan.left)} left alone."
    )
    if plan.left:
        print("Left alone, by reason:")
        for reason, count in sorted(Counter(item.reason for item in plan.left).items()):
            print(f"  {count:>4}  {REASONS.get(reason, reason)}")
    if plan.foreign:
        print(f"{len(plan.foreign)} Datastreams with a sensor id that is not a KNMI one left alone.")


def _iter_patch_ids(plan: Plan) -> Iterator[tuple[str, str]]:
    for patch in plan.patches:
        yield patch.entity_set, patch.entity_id


def run(
    http: FrostHTTPClient,
    stations_body: Any,
    *,
    fields: FrostODataFields,
    entity_prefix: str,
    paths: dict[str, str],
    apply: bool,
    journal: Journal | None = None,
) -> int:
    if apply and journal is None:
        raise ValueError("--apply needs a journal: the record that makes --rollback possible")
    stations = parse_stations(stations_body)
    if not stations:
        print("The KNMI station list has no stations with an id and a Point; nothing was planned.")
        return EXIT_KNMI_FAILED
    print(f"KNMI stations in the station list: {len(stations)}")
    things = fetch_knmi_things(http, paths[THINGS], entity_prefix, fields)
    plan = build_plan(things, stations, entity_prefix, fields)
    print_plan(plan)
    print_summary(plan)

    if not apply:
        print("Dry run: nothing was written." + (" Re-run with --apply to PATCH them." if plan.patches else ""))
        return exit_status(plan, 0)

    assert journal is not None
    if plan.patches:
        print(f"Every PATCH is appended to the journal {journal.path} (and printed after {JOURNAL_MARKER.strip()!r}) "
              "before it is sent.")
    applied, failed = apply_plan(http, plan, paths, journal)
    print(f"Applied {applied} PATCHes, {failed} failed.")
    if applied:
        after = build_plan(fetch_knmi_things(http, paths[THINGS], entity_prefix, fields), stations, entity_prefix, fields)
        pending = set(_iter_patch_ids(after))
        if pending:
            print(f"Read back: {len(pending)} PATCHes still pending; re-run the dry run.")
            failed += len(pending)
        else:
            print("Read back: every planned entity is on its station id.")
    return exit_status(plan, failed)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JournalEntry:
    target: str
    entity_set: str
    entity_id: str
    name: str
    old: dict[str, Any]
    new: dict[str, Any]


def _journal_entry(obj: Any) -> JournalEntry | None:
    if not isinstance(obj, dict) or obj.get("t") != JOURNAL_TAG:
        return None
    entity_set, entity_id, old, new = obj.get("entity_set"), obj.get("id"), obj.get("old"), obj.get("new")
    if entity_set not in ENTITY_SETS or not isinstance(entity_id, str) or not entity_id \
            or not isinstance(old, dict) or not isinstance(new, dict):
        raise ValueError("a journal record without entity_set, id, old and new")
    return JournalEntry(str(obj.get("target") or ""), entity_set, entity_id, str(obj.get("name") or ""), old, new)


def read_journal(path: Path) -> list[JournalEntry]:
    """The records of a journal file, or of a saved stdout (lines behind JOURNAL_MARKER), in order.

    Other lines of a saved stdout are skipped. A cut-off last line (a run killed while appending it: its PATCH
    was never sent) is skipped with a warning; an unreadable line elsewhere is an error.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise JournalError(f"cannot read the journal {path}: {exc}") from exc
    while lines and not lines[-1].strip():
        lines.pop()
    entries: list[JournalEntry] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        marked = line.startswith(JOURNAL_MARKER)
        if marked:
            line = line[len(JOURNAL_MARKER):].strip()
        if not marked and not line.startswith("{"):
            continue
        try:
            entry = _journal_entry(json.loads(line))
        except ValueError as exc:
            if number == len(lines):
                print(f"  WARNING: {path}, line {number}: cut-off last record skipped (its PATCH was never sent)")
                break
            raise JournalError(f"{path}, line {number}: {exc}") from exc
        if entry is not None:
            entries.append(entry)
    return entries


@dataclass
class Restore:
    entity_set: str
    entity_id: str
    name: str
    properties: dict[str, Any]  # to PATCH


@dataclass
class RollbackPlan:
    restore: list[Restore] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)  # already as before the migration
    changed: list[str] = field(default_factory=list)  # changed since: left alone
    missing: list[str] = field(default_factory=list)  # deleted since
    errors: list[str] = field(default_factory=list)


def fetch_properties(http: FrostHTTPClient, path: str, entity_id: str) -> dict[str, Any] | None:
    """An entity's current properties ({} when it has none), or None when FROST no longer has it."""
    endpoint = http.endpoint(f"{path}({entity_id})")
    if not endpoint:
        raise JournalError("No FROST endpoint configured.")
    try:
        response = http.get(endpoint)
    except FrostConnectionError as exc:
        raise RuntimeError(f"GET failed: {type(exc.cause or exc).__name__}") from None
    if response.status_code == 404:
        return None
    if not response.ok:
        raise RuntimeError(f"GET failed with {_frost_failure(response)}")
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError("GET returned a body that is not JSON") from None
    if not isinstance(body, dict):
        raise RuntimeError("GET returned no entity")
    properties = body.get("properties")
    return dict(properties) if isinstance(properties, dict) else {}


def rollback_target(current: dict[str, Any], entries: Sequence[JournalEntry]) -> dict[str, Any] | None:
    """The properties before the first recorded PATCH, walking the entity's records back from ``current``;
    None when the entity was changed since (``current`` is neither a record's new nor its old object)."""
    state = current
    for entry in reversed(entries):
        if state == entry.new:
            state = entry.old
        elif state != entry.old:  # equal to old: that PATCH never took effect
            return None
    return state


def plan_rollback(http: FrostHTTPClient, entries: Sequence[JournalEntry], paths: dict[str, str]) -> RollbackPlan:
    by_entity: dict[tuple[str, str], list[JournalEntry]] = defaultdict(list)
    for entry in entries:
        by_entity[(entry.entity_set, entry.entity_id)].append(entry)
    plan = RollbackPlan()
    for (entity_set, entity_id), records in by_entity.items():
        label = f"{entity_set}({entity_id}) {records[-1].name!r}"
        try:
            current = fetch_properties(http, paths[entity_set], entity_id)
        except RuntimeError as exc:
            plan.errors.append(f"{label}: {exc}")
            continue
        if current is None:
            plan.missing.append(label)
            continue
        target = rollback_target(current, records)
        if target is None:
            plan.changed.append(label)
        elif target == current:
            plan.unchanged.append(label)
        else:
            plan.restore.append(Restore(entity_set, entity_id, records[-1].name, target))
    return plan


def print_rollback_plan(plan: RollbackPlan) -> None:
    counts = Counter(item.entity_set for item in plan.restore)
    print(f"To restore: {len(plan.restore)} ({', '.join(f'{counts[s]} {s}' for s in ENTITY_SETS)}); "
          f"already as before: {len(plan.unchanged)}; changed since (left alone): {len(plan.changed)}; "
          f"deleted since: {len(plan.missing)}; failed to read: {len(plan.errors)}")
    for label in plan.changed:
        print(f"  changed since the migration, left alone: {label}")
    for label in plan.missing:
        print(f"  deleted since the migration: {label}")
    for error in plan.errors:
        print(f"  FROST error: {error}")


def run_rollback(
    http: FrostHTTPClient, journal_path: Path, *, target_url: str, paths: dict[str, str], apply: bool
) -> int:
    """PATCH every journaled entity back to its properties before the migration, where it is still as the
    migration left it. A dry run unless ``apply``."""
    entries = read_journal(journal_path)
    print(f"Journal {journal_path}: {len(entries)} records of "
          f"{len({(e.entity_set, e.entity_id) for e in entries})} entities")
    others = sorted({e.target for e in entries if e.target.rstrip('/') != target_url.rstrip('/')})
    if others:
        print(f"The journal records PATCHes on another FROST target ({', '.join(others)}); entity ids are per "
              "server. Pick that target with --target-url. Nothing was done.")
        return EXIT_FROST_FAILED
    plan = plan_rollback(http, entries, paths)
    print_rollback_plan(plan)
    if not apply:
        print("Dry run: nothing was written." + (" Re-run with --apply to restore them." if plan.restore else ""))
        return EXIT_FROST_FAILED if plan.errors else (EXIT_LEFT if plan.changed else EXIT_OK)

    restored = failed = 0
    for item in plan.restore:
        label = f"{item.entity_set}({item.entity_id})"
        endpoint = http.endpoint(f"{paths[item.entity_set]}({item.entity_id})")
        try:
            response = http.patch(endpoint, {"properties": item.properties}) if endpoint else None
        except FrostConnectionError as exc:
            print(f"  FAILED {label}: {type(exc.cause or exc).__name__}")
            failed += 1
            continue
        if response is not None and response.ok:
            restored += 1
        else:
            failed += 1
            print(f"  FAILED {label}: {_frost_failure(response) if response is not None else 'no endpoint'}")
    print(f"Restored {restored} entities, {failed} failed.")
    pending = 0
    for item in plan.restore:
        try:
            if fetch_properties(http, paths[item.entity_set], item.entity_id) != item.properties:
                pending += 1
        except RuntimeError:
            pending += 1
    print(f"Read back: {pending} entities not as before the migration." if pending
          else "Read back: every restored entity is as before the migration.")
    if failed or pending or plan.errors:
        return EXIT_FROST_FAILED
    return EXIT_LEFT if plan.changed else EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--apply", action="store_true", help="PATCH the entities (default: dry run)")
    parser.add_argument("--journal", type=Path, metavar="FILE",
                        help="with --apply: append every PATCH (old and new properties) here before sending it; "
                             "with --rollback: the journal to undo")
    parser.add_argument("--rollback", action="store_true",
                        help="restore the properties recorded in --journal (a dry run unless --apply)")
    parser.add_argument("--target-url", help="one of the configured FROST targets (default: the primary target)")
    parser.add_argument(
        "--api-url",
        default=settings.knmi_weather_api_url,
        help="KNMI EDR collection URL (default: KNMI_WEATHER_API_URL; a trailing /locations is dropped)",
    )
    parser.add_argument("--locations-file", type=Path, help="a saved EDR /locations response to use instead of the API")
    parser.add_argument("--prefix", default=settings.entity_name_prefix, help="entity name prefix (default from env)")
    args = parser.parse_args(argv)
    if args.rollback and (args.journal is None or not args.journal.is_file()):
        parser.error("--rollback needs --journal FILE: the journal an --apply wrote")
    if args.apply and args.journal is None:
        parser.error("--apply needs --journal FILE: the record of the old properties, for --rollback")

    target = select_target(settings.frost_targets, args.target_url)
    fields = odata_fields_for(target.version)
    http = FrostHTTPClient(
        base_urls=[target.url],
        auth_username=target.auth_username,
        auth_password=target.auth_password,
        auth_token=target.auth_token,
        timeout=settings.request_timeout_seconds,
    )
    has_auth = bool(target.auth_username or target.auth_token)
    print(
        f"FROST target: {target.url} (declared SensorThings {target.version}; "
        f"credentials from environment: {'yes' if has_auth else 'none'})"
    )
    endpoint_version = url_version(target.url)
    if endpoint_version and odata_fields_for(endpoint_version) != fields:
        print(f"  note: the URL is a {endpoint_version} endpoint; ids and next links are read in both conventions")
    paths = {THINGS: settings.things_path, DATASTREAMS: settings.datastreams_path, SENSORS: settings.sensors_path}
    if args.rollback:
        print(f"Mode: ROLLBACK {'APPLY' if args.apply else 'dry run'}\n")
        try:
            return run_rollback(http, args.journal, target_url=target.url, paths=paths, apply=args.apply)
        except JournalError as exc:
            print(f"{exc}; nothing was done.")
            return EXIT_FROST_FAILED
    print(f"Thing name prefix: {thing_prefix(args.prefix)!r}")
    print(f"Mode: {'APPLY' if args.apply else 'dry run'}\n")

    if args.locations_file:
        print(f"KNMI stations from {args.locations_file}")
        try:
            stations_body = json.loads(args.locations_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"Could not read {args.locations_file}: {type(exc).__name__}")
            return EXIT_KNMI_FAILED
    else:
        print(f"KNMI stations from {edr_base_url(args.api_url)}/locations "
              f"(API key from environment: {'yes' if settings.knmi_api_key else 'none'})")
        try:
            stations_body = fetch_station_list(args.api_url, settings.knmi_api_key)
        except KnmiApiError as exc:
            print(f"{exc}; nothing was planned.")
            return EXIT_KNMI_FAILED

    return run(
        http,
        stations_body,
        fields=fields,
        entity_prefix=args.prefix,
        paths=paths,
        apply=args.apply,
        journal=Journal(args.journal, target.url) if args.apply else None,
    )


if __name__ == "__main__":
    sys.exit(main())
