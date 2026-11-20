#!/usr/bin/env python
"""Plan (and optionally apply) moving misrouted Sensor.Community observations in FROST.

Until the routing fix in app/services/sensorthings_client.py and the discovery
fix in app/services/sensor_community_source.py, the connector wrote some
readings into datastreams of another observed property, location or source:

* A location's datastreams were registered from the first response entry seen
  for it (Sensor.Community returns one entry per physical sensor, e.g. an
  SDS011 for PM next to a BME280), so the other sensor's properties had no
  datastream. Their readings fell back to the bare sensor id, which the cache
  warm-up mapped to whichever of the location's datastreams FROST listed first.
  Example: datastream 1386 "GEO_SensorCommunity 93182 sensor - Air pressure"
  holds 93182's PM10 and PM2.5 readings.
* The entity cache kept the datastream ids of the previous primary FROST
  (sta.wbd-rd.nl). FROST numbers each server's datastreams from 1, so on the new
  server those ids name unrelated datastreams, and keys that were never
  registered again kept them. Example: 91154's PM10 key kept 1386, its PM2.5 key
  1387 (93182's Relative humidity).
* The warm-up mapped bare stream keys ("humidity") to the first datastream
  listed with that streamKey: TGV Kafka readings, which have no datastreams on
  the primary, landed in Sensor.Community, KNMI, Ohnics and Samen Meten
  datastreams of the same property.

The connector records each reading's own identity in the observation's
PARAMETERS (sensor_id, observed_property, location, thing_name). This script
finds every row whose labels don't match the Sensor.Community datastream it is
in, plus (by default) Sensor.Community readings in other sources' datastreams,
and plans where each group goes:

  move              the reading's own datastream exists: datastream of the same
                    sensor_id and (canonical) observed property, or for another
                    source's reading, of its Thing ('GEO_' + thing_name)
  move-blocked      that datastream exists, but its Thing has no single Location
                    with a FROST-generated FeatureOfInterest yet
  needs-datastream  a Sensor.Community reading whose datastream doesn't exist
                    yet; the fixed connector creates it once the location
                    reports that property again: re-run then
  no-datastream     another source's reading (e.g. TGV) with no Thing or
                    datastream on this server: proposed deletion, unless that
                    source gets registered here (then re-run moves them)
  ambiguous         several datastreams qualify; left for a decision
  unlabelled        no sensor_id/observed_property label: can't be placed, left

FEATURE_ID: FROST gives an observation posted without a FeatureOfInterest the
FoI it generated for its Thing's Location (LOCATIONS.GEN_FOI_ID). A row moved to
another Thing gets that Thing's FoI, as if it had been posted there; a row moved
between datastreams of one Thing keeps its FoI. A move to a Thing without a
single Location with a generated FoI is blocked until FROST has generated one
(the first observation the connector posts to that Thing does that).

DATASTREAMS aggregates: FROST's UPDATE trigger on OBSERVATIONS only reacts to
changed phenomenon/result times, not to a changed DATASTREAM_ID, so after moving
rows the script recomputes phenomenonTime and resultTime of every datastream it
touched. OBSERVED_AREA is unaffected: moved rows carry their Thing's FoI.

Duplicates: a row is not moved when its datastream-to-be already has an
observation at the same phenomenonTime; it is counted (proposed deletion).

Safety (the same pattern as scripts/fix_sc_pressure_units.py)
--------------------------------------------------------------
* Dry run by default: read-only sessions (default_transaction_read_only), the
  scan walks OBSERVATIONS once in ID ranges (one statement per range, far below
  the database's 60 s statement limit), and a plan is printed (and written as
  CSV with --plan-file).
* --apply moves only 'move' groups, in batches of at most --batch-rows rows. For
  each batch the rows are first selected read-only, then appended to --ids-file
  (JSON lines, flushed and fsynced) before the UPDATE runs; the UPDATE only
  touches the listed IDs while they are still in their source datastream, and
  each batch commits on its own. --datastream limits it to source datastreams
  (a canary).
* --rollback reads --ids-file and moves every listed row that is still where
  the script put it back to its datastream and FEATURE_ID, then refreshes the
  datastreams' times. Rows of a batch that never committed are untouched.
* Re-running is safe: moved rows match their datastream and are not selected
  again.

Order with the Pa -> hPa backfill (scripts/fix_sc_pressure_units.py on
fix/sc-pressure-hpa), which converts pressure-labelled rows inside 'Air
pressure' datastreams and skips rows labelled as another property:
1. Deploy the connector fixes (routing, discovery, pressure unit, registration
   of later-discovered entity sets) so no new rows are misrouted or in Pa.
2. Let Sensor.Community run a few polls: it registers the missing datastreams.
3. Dry run this script, then --apply (canary first): pressure rows that sat in
   other datastreams now sit in 'Air pressure' datastreams, still in Pa.
4. Run (or re-run) the pressure backfill: it converts them; rows it converted
   earlier are below its Pa band and untouched. Running the backfill before this
   script too is harmless.
5. Re-run this dry run after the connector's dead-letter queue has drained:
   replayed payloads carry the datastream id they were resolved to.

How it connects: like the pressure backfill it pipes SQL into psql inside the
FROST database container (ssh to the FROST host, or docker exec for a local
container); psql there uses the container's own $POSTGRES_USER/$POSTGRES_DB, so
the script never handles a credential.

Usage
-----
    python scripts/fix_sc_misrouted_observations.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner
    python scripts/fix_sc_misrouted_observations.py --container <db-container> --plan-file plan.csv
    python scripts/fix_sc_misrouted_observations.py --ssh ... --apply --ids-file moved.jsonl --datastream 1386
    python scripts/fix_sc_misrouted_observations.py --ssh ... --rollback --ids-file moved.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Run as `python scripts/fix_sc_misrouted_observations.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.sta.canonical import resolve

RunSql = Callable[[str], list[str]]

SC_SOURCE = "sensor_community"
SC_SENSOR_PREFIX = "sensor_community-"
SC_SENSOR_LIKE = r"sensor\_community-%"  # \_ : a literal underscore, not LIKE's wildcard

MOVE = "move"
MOVE_BLOCKED = "move-blocked"
NEEDS_DATASTREAM = "needs-datastream"
NO_DATASTREAM = "no-datastream"
AMBIGUOUS = "ambiguous"
UNLABELLED = "unlabelled"
ACTIONS = (MOVE, MOVE_BLOCKED, NEEDS_DATASTREAM, NO_DATASTREAM, AMBIGUOUS, UNLABELLED)

READ_ONLY_SESSION = (
    "SET default_transaction_read_only = on;\n"
    "SET statement_timeout = '55s';\n"
    "SET application_name = 'fix_sc_misrouted_observations (read-only)';\n"
)
WRITE_SESSION = (
    "SET statement_timeout = '55s';\n"
    "SET lock_timeout = '10s';\n"
    "SET application_name = 'fix_sc_misrouted_observations';\n"
)

# psql inside the database container: no psqlrc, quiet, unaligned tuples only,
# stop at the first error. Every result row is one JSON object per line.
CONTAINER_PSQL = 'psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Datastream:
    id: int
    name: str
    thing_id: int
    thing: str
    source: str
    sensor_id: str
    prop: str  # canonical key of properties.observed_property (or streamKey); raw lower-case if unknown


@dataclass(frozen=True)
class ThingFoi:
    locations: int
    foi: int | None  # the generated FoI of its Location, when it has exactly one


@dataclass(frozen=True)
class Catalogue:
    datastreams: dict[int, Datastream]
    fois: dict[int, ThingFoi]  # by Thing id
    prefix: str

    def by_sensor_prop(self) -> dict[tuple[str, str], list[Datastream]]:
        index: dict[tuple[str, str], list[Datastream]] = defaultdict(list)
        for ds in self.datastreams.values():
            if ds.sensor_id and ds.prop:
                index[(ds.sensor_id, ds.prop)].append(ds)
        return index

    def by_thing_prop(self) -> dict[tuple[str, str], list[Datastream]]:
        index: dict[tuple[str, str], list[Datastream]] = defaultdict(list)
        for ds in self.datastreams.values():
            if ds.thing and ds.prop:
                index[(ds.thing, ds.prop)].append(ds)
        return index

    def is_sc(self, ds_id: int) -> bool:
        ds = self.datastreams.get(ds_id)
        return ds is not None and ds.source == SC_SOURCE


@dataclass
class Group:
    """Observations of one source datastream sharing one label."""

    ds: int
    sid: str | None
    prop: str | None
    thing: str | None
    loc: str | None = None
    rows: int = 0
    min_id: int = 0
    max_id: int = 0
    first: str = ""
    last: str = ""

    @property
    def key(self) -> tuple[int, str | None, str | None, str | None]:
        return (self.ds, self.sid, self.prop, self.thing)

    def merge(self, other: Group) -> None:
        """Add the counts of the same label from another ID range."""
        self.rows += other.rows
        self.min_id = min(self.min_id, other.min_id)
        self.max_id = max(self.max_id, other.max_id)
        self.first = min((t for t in (self.first, other.first) if t), default="")  # ISO 8601, one offset
        self.last = max(self.last, other.last)
        self.loc = self.loc or other.loc


@dataclass
class PlannedGroup:
    group: Group
    action: str
    target: Datastream | None = None
    foi: int | None = None  # FEATURE_ID to set; None: keep each row's own
    note: str = ""


@dataclass
class Plan:
    groups: list[PlannedGroup] = field(default_factory=list)

    def by_action(self) -> dict[str, list[PlannedGroup]]:
        out: dict[str, list[PlannedGroup]] = {a: [] for a in ACTIONS}
        for planned in self.groups:
            out[planned.action].append(planned)
        return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def canonical(name: str | None) -> str:
    """Canonical key of an observed-property name; trimmed lower-case if it doesn't resolve."""
    raw = (name or "").strip()
    if not raw:
        return ""
    member = resolve(raw)
    return member.value if member is not None else raw.lower()


def lit(value: str | None) -> str:
    """SQL literal (standard_conforming_strings), or NULL."""
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def label_filter(alias: str, group: Group) -> str:
    """Rows of exactly this group's label (NULL-safe)."""
    o = alias
    return (
        f"""{o}."PARAMETERS"->>'sensor_id' IS NOT DISTINCT FROM {lit(group.sid)}"""
        f""" AND {o}."PARAMETERS"->>'observed_property' IS NOT DISTINCT FROM {lit(group.prop)}"""
        f""" AND {o}."PARAMETERS"->>'thing_name' IS NOT DISTINCT FROM {lit(group.thing)}"""
    )


def parse_json_lines(lines: Iterable[str], tag: str) -> list[dict[str, Any]]:
    rows = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        obj = json.loads(line)
        if obj.get("t") == tag:
            rows.append(obj)
    return rows


def _chunks(items: Sequence[Any], size: int) -> list[Sequence[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _progress(message: str) -> None:
    sys.stdout.flush()
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


def catalogue_sql() -> str:
    return """
SELECT json_build_object(
         't', 'ds', 'id', d."ID", 'name', d."NAME", 'thing_id', d."THING_ID", 'thing', t."NAME",
         'source', t."PROPERTIES"->>'source', 'sensor_id', d."PROPERTIES"->>'sensor_id',
         'prop', d."PROPERTIES"->>'observed_property', 'stream_key', d."PROPERTIES"->>'streamKey')::text
  FROM "DATASTREAMS" d JOIN "THINGS" t ON t."ID" = d."THING_ID"
 ORDER BY d."ID";
SELECT json_build_object('t', 'foi', 'thing_id', tl."THING_ID", 'locations', count(*),
                         'foi', min(l."GEN_FOI_ID"), 'fois', count(DISTINCT l."GEN_FOI_ID"))::text
  FROM "THINGS_LOCATIONS" tl JOIN "LOCATIONS" l ON l."ID" = tl."LOCATION_ID"
 GROUP BY tl."THING_ID";
SELECT json_build_object('t', 'bounds', 'min', min("ID"), 'max', max("ID"))::text FROM "OBSERVATIONS";
"""


# Rows that don't belong where they are: in a Sensor.Community datastream with
# another label, or (include_other) a Sensor.Community reading in another
# source's datastream. Python re-checks every group with canonical names.
def _misrouted_condition(include_other: bool) -> str:
    in_sc = (
        """(t."PROPERTIES"->>'source' = 'sensor_community' AND ("""
        """o."PARAMETERS"->>'sensor_id' IS DISTINCT FROM d."PROPERTIES"->>'sensor_id' OR """
        """o."PARAMETERS"->>'observed_property' IS DISTINCT FROM d."PROPERTIES"->>'observed_property'))"""
    )
    if not include_other:
        return in_sc
    elsewhere = (
        f"""(t."PROPERTIES"->>'source' IS DISTINCT FROM 'sensor_community' AND """
        f"""o."PARAMETERS"->>'sensor_id' LIKE '{SC_SENSOR_LIKE}')"""
    )
    return f"({in_sc} OR {elsewhere})"


def scan_sql(where: str, include_other: bool) -> str:
    """Misrouted groups among the rows selected by ``where`` (on alias o)."""
    return f"""
SELECT json_build_object('t', 'grp', 'ds', g.ds, 'sid', g.sid, 'prop', g.prop, 'thing', g.thing, 'loc', g.loc,
                         'n', g.n, 'min_id', g.min_id, 'max_id', g.max_id, 'first', g.first, 'last', g.last)::text
  FROM (SELECT o."DATASTREAM_ID" AS ds, o."PARAMETERS"->>'sensor_id' AS sid,
               o."PARAMETERS"->>'observed_property' AS prop, o."PARAMETERS"->>'thing_name' AS thing,
               max(o."PARAMETERS"->>'location') AS loc, count(*) AS n, min(o."ID") AS min_id, max(o."ID") AS max_id,
               min(o."PHENOMENON_TIME_START") AS first, max(o."PHENOMENON_TIME_START") AS last
          FROM "OBSERVATIONS" o
          JOIN "DATASTREAMS" d ON d."ID" = o."DATASTREAM_ID"
          JOIN "THINGS" t ON t."ID" = d."THING_ID"
         WHERE {where} AND {_misrouted_condition(include_other)}
         GROUP BY 1, 2, 3, 4) g;
"""


def range_scan_sql(first_id: int, last_id: int, include_other: bool) -> str:
    return scan_sql(f'o."ID" BETWEEN {int(first_id)} AND {int(last_id)}', include_other)


def datastream_scan_sql(ds_id: int, include_other: bool) -> str:
    return scan_sql(f'o."DATASTREAM_ID" = {int(ds_id)}', include_other)


def duplicates_sql(planned: PlannedGroup) -> str:
    """Rows of a move group whose phenomenonTime already exists in the target."""
    g, target = planned.group, planned.target
    assert target is not None
    return f"""
SELECT json_build_object('t', 'dup', 'ds', {int(g.ds)}, 'sid', {lit(g.sid)}, 'prop', {lit(g.prop)},
                         'thing', {lit(g.thing)}, 'n', count(*))::text
  FROM "OBSERVATIONS" o
 WHERE o."DATASTREAM_ID" = {int(g.ds)} AND o."ID" BETWEEN {int(g.min_id)} AND {int(g.max_id)}
   AND {label_filter("o", g)}
   AND EXISTS (SELECT 1 FROM "OBSERVATIONS" x
                WHERE x."DATASTREAM_ID" = {int(target.id)} AND x."PHENOMENON_TIME_START" = o."PHENOMENON_TIME_START");
"""


def batch_select_sql(planned: PlannedGroup, after_id: int, limit: int) -> str:
    """The next rows of a move group, read-only, with a duplicate flag."""
    g, target = planned.group, planned.target
    assert target is not None
    return f"""
SELECT json_build_object('t', 'row', 'id', o."ID", 'foi', o."FEATURE_ID",
                         'dup', EXISTS (SELECT 1 FROM "OBSERVATIONS" x
                                         WHERE x."DATASTREAM_ID" = {int(target.id)}
                                           AND x."PHENOMENON_TIME_START" = o."PHENOMENON_TIME_START"))::text
  FROM "OBSERVATIONS" o
 WHERE o."DATASTREAM_ID" = {int(g.ds)} AND o."ID" > {int(after_id)} AND o."ID" <= {int(g.max_id)}
   AND {label_filter("o", g)}
 ORDER BY o."ID"
 LIMIT {int(limit)};
"""


def move_sql(from_ds: int, to_ds: int, rows: Sequence[tuple[int, int | None]], foi: int | None) -> str:
    """Move the listed rows that are still in from_ds; foi None keeps each row's FEATURE_ID."""
    values = ", ".join(f"({int(i)}::bigint, {'NULL' if f is None else int(f)}::bigint)" for i, f in rows)
    feature = "o.\"FEATURE_ID\"" if foi is None else f"{int(foi)}"
    return f"""
WITH v(id, foi) AS (VALUES {values}),
moved AS (
    UPDATE "OBSERVATIONS" o
       SET "DATASTREAM_ID" = {int(to_ds)}, "FEATURE_ID" = {feature}
      FROM v
     WHERE o."ID" = v.id AND o."DATASTREAM_ID" = {int(from_ds)}
    RETURNING o."ID"
)
SELECT json_build_object('t', 'moved', 'n', count(*))::text FROM moved;
"""


def restore_sql(from_ds: int, to_ds: int, rows: Sequence[tuple[int, int | None]]) -> str:
    """Rollback: rows still in to_ds go back to from_ds with their recorded FEATURE_ID."""
    values = ", ".join(f"({int(i)}::bigint, {'NULL' if f is None else int(f)}::bigint)" for i, f in rows)
    return f"""
WITH v(id, foi) AS (VALUES {values}),
restored AS (
    UPDATE "OBSERVATIONS" o
       SET "DATASTREAM_ID" = {int(from_ds)}, "FEATURE_ID" = v.foi
      FROM v
     WHERE o."ID" = v.id AND o."DATASTREAM_ID" = {int(to_ds)}
    RETURNING o."ID"
)
SELECT json_build_object('t', 'restored', 'n', count(*))::text FROM restored;
"""


def refresh_sql(ds_id: int) -> str:
    """Recompute a datastream's phenomenon and result time extents from its rows."""
    return f"""
UPDATE "DATASTREAMS" d
   SET "PHENOMENON_TIME_START" = a.p0, "PHENOMENON_TIME_END" = a.p1,
       "RESULT_TIME_START" = a.r0, "RESULT_TIME_END" = a.r1
  FROM (SELECT min("PHENOMENON_TIME_START") AS p0,
               max(coalesce("PHENOMENON_TIME_END", "PHENOMENON_TIME_START")) AS p1,
               min("RESULT_TIME") AS r0, max("RESULT_TIME") AS r1
          FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" = {int(ds_id)}) a
 WHERE d."ID" = {int(ds_id)};
"""


# ---------------------------------------------------------------------------
# Catalogue, scan and plan
# ---------------------------------------------------------------------------


def parse_catalogue(lines: Sequence[str], prefix: str) -> tuple[Catalogue, tuple[int, int] | None]:
    datastreams = {}
    for row in parse_json_lines(lines, "ds"):
        prop = canonical(row.get("prop") or row.get("stream_key"))
        datastreams[int(row["id"])] = Datastream(
            id=int(row["id"]),
            name=str(row.get("name") or ""),
            thing_id=int(row["thing_id"]),
            thing=str(row.get("thing") or ""),
            source=str(row.get("source") or ""),
            sensor_id=str(row.get("sensor_id") or "").strip(),
            prop=prop,
        )
    fois = {}
    for row in parse_json_lines(lines, "foi"):
        single = int(row["locations"]) == 1 and int(row["fois"]) == 1
        fois[int(row["thing_id"])] = ThingFoi(int(row["locations"]), int(row["foi"]) if single else None)
    bounds = None
    for row in parse_json_lines(lines, "bounds"):
        if row.get("min") is not None:
            bounds = (int(row["min"]), int(row["max"]))
    return Catalogue(datastreams, fois, prefix), bounds


def parse_groups(lines: Sequence[str]) -> list[Group]:
    return [
        Group(
            ds=int(row["ds"]),
            sid=row.get("sid"),
            prop=row.get("prop"),
            thing=row.get("thing"),
            loc=row.get("loc"),
            rows=int(row["n"]),
            min_id=int(row["min_id"]),
            max_id=int(row["max_id"]),
            first=str(row.get("first") or ""),
            last=str(row.get("last") or ""),
        )
        for row in parse_json_lines(lines, "grp")
    ]


def merge_groups(groups: Iterable[Group]) -> list[Group]:
    merged: dict[tuple[int, str | None, str | None, str | None], Group] = {}
    for group in groups:
        known = merged.get(group.key)
        if known is None:
            merged[group.key] = Group(**vars(group))
        else:
            known.merge(group)
    return sorted(merged.values(), key=lambda g: (g.ds, -g.rows))


def scan_ranges(bounds: tuple[int, int], size: int) -> list[tuple[int, int]]:
    low, high = bounds
    return [(start, min(start + size - 1, high)) for start in range(low, high + 1, size)]


def plan_group(group: Group, catalogue: Catalogue, by_sensor: dict[tuple[str, str], list[Datastream]],
               by_thing: dict[tuple[str, str], list[Datastream]]) -> PlannedGroup | None:
    """Where one group goes; None when it already is where it belongs."""
    source = catalogue.datastreams.get(group.ds)
    sid = (group.sid or "").strip()
    prop = canonical(group.prop)
    if source is None:
        return PlannedGroup(group, AMBIGUOUS, note="source datastream not in catalogue")
    if not sid or not prop:
        return PlannedGroup(group, UNLABELLED)

    is_sc_reading = sid.startswith(SC_SENSOR_PREFIX)
    if is_sc_reading:
        candidates = by_sensor.get((sid, prop), [])
    else:
        thing = (group.thing or "").strip()
        thing_name = thing if thing.startswith(catalogue.prefix) else f"{catalogue.prefix}{thing}"
        candidates = by_thing.get((thing_name, prop), []) if thing else []
        if not candidates:
            candidates = by_sensor.get((sid, prop), [])
    if any(c.id == source.id for c in candidates):
        return None  # e.g. a datastream registered under an alias ("pm25" for pm2_5)
    if not candidates:
        if is_sc_reading:
            return PlannedGroup(group, NEEDS_DATASTREAM)
        return PlannedGroup(group, NO_DATASTREAM, note="proposed deletion")
    if len(candidates) > 1:
        return PlannedGroup(group, AMBIGUOUS, note="datastreams " + ",".join(str(c.id) for c in candidates))

    target = candidates[0]
    if target.thing_id == source.thing_id:
        return PlannedGroup(group, MOVE, target, foi=None)
    thing_foi = catalogue.fois.get(target.thing_id)
    if thing_foi is None or thing_foi.foi is None:
        return PlannedGroup(group, MOVE_BLOCKED, target, note="target Thing has no single Location with a generated FoI")
    return PlannedGroup(group, MOVE, target, foi=thing_foi.foi)


def build_plan(groups: Iterable[Group], catalogue: Catalogue) -> Plan:
    by_sensor, by_thing = catalogue.by_sensor_prop(), catalogue.by_thing_prop()
    plan = Plan()
    for group in groups:
        planned = plan_group(group, catalogue, by_sensor, by_thing)
        if planned is not None:
            plan.groups.append(planned)
    return plan


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _source_of(sid: str | None) -> str:
    sid = (sid or "").strip()
    if not sid:
        return "(no label)"
    if sid.startswith(SC_SENSOR_PREFIX):
        return SC_SOURCE
    return sid.split("-", 1)[0]


def print_report(plan: Plan, catalogue: Catalogue, duplicates: dict[tuple[Any, ...], int], top: int) -> None:
    by_action = plan.by_action()
    total = sum(p.group.rows for p in plan.groups)
    print("\nMisrouted rows by plan")
    print(f"  {'action':<18}{'groups':>8}{'rows':>12}")
    for action in ACTIONS:
        items = by_action[action]
        print(f"  {action:<18}{len(items):>8}{sum(p.group.rows for p in items):>12,}")
    print(f"  {'total':<18}{len(plan.groups):>8}{total:>12,}")
    if duplicates:
        print(f"  of the moves, rows whose target already has that phenomenonTime (not moved): "
              f"{sum(duplicates.values()):,}")

    where: Counter[tuple[str, str]] = Counter()
    origin: Counter[str] = Counter()
    cross_location = 0
    for planned in plan.groups:
        g = planned.group
        ds = catalogue.datastreams.get(g.ds)
        ds_kind = (ds.source or "?") if ds else "?"
        where[(f"{ds_kind}: {ds.prop if ds else '?'}", canonical(g.prop) or "(none)")] += g.rows
        origin[f"{_source_of(g.sid)} readings in {ds_kind} datastreams"] += g.rows
        if ds and ds.source == SC_SOURCE and (g.sid or "").startswith(SC_SENSOR_PREFIX) and g.sid != ds.sensor_id:
            cross_location += g.rows

    print("\nBy origin")
    for label, rows in origin.most_common():
        print(f"  {label:<58}{rows:>12,}")
    print(f"  {'  of those in SC datastreams: another location':<58}{cross_location:>12,}")

    print("\nWhere they are: datastream property <- reading property")
    for (ds_label, prop), rows in where.most_common(top):
        print(f"  {ds_label:<40} <- {prop:<20}{rows:>12,}")

    per_source: Counter[int] = Counter()
    for planned in plan.groups:
        per_source[planned.group.ds] += planned.group.rows
    print(f"\nLargest source datastreams (top {top})")
    for ds_id, rows in per_source.most_common(top):
        ds = catalogue.datastreams.get(ds_id)
        print(f"  {ds_id:>8} {rows:>10,}  {ds.name if ds else '?'}")

    moves: Counter[tuple[int, int]] = Counter()
    for planned in by_action[MOVE] + by_action[MOVE_BLOCKED]:
        if planned.target is not None:
            moves[(planned.group.ds, planned.target.id)] += planned.group.rows
    if moves:
        print(f"\nPlanned moves, source -> target datastream (top {top} of {len(moves)})")
        for (src, dst), rows in moves.most_common(top):
            print(f"  {src:>8} -> {dst:<8}{rows:>10,}  {catalogue.datastreams[dst].name}")


def write_plan_csv(path: Path, plan: Plan, catalogue: Catalogue, duplicates: dict[tuple[Any, ...], int]) -> None:
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        writer.writerow([
            "action", "source_datastream", "source_name", "label_sensor_id", "label_observed_property",
            "label_location", "label_thing_name", "rows", "first", "last", "min_id", "max_id",
            "target_datastream", "target_name", "feature_id", "duplicates", "note",
        ])
        for planned in sorted(plan.groups, key=lambda p: (ACTIONS.index(p.action), p.group.ds, -p.group.rows)):
            g, target = planned.group, planned.target
            source = catalogue.datastreams.get(g.ds)
            writer.writerow([
                planned.action, g.ds, source.name if source else "", g.sid or "", g.prop or "", g.loc or "",
                g.thing or "", g.rows, g.first, g.last, g.min_id, g.max_id,
                target.id if target else "", target.name if target else "",
                "" if planned.foi is None else planned.foi, duplicates.get(g.key, ""), planned.note,
            ])


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class PsqlError(RuntimeError):
    def __init__(self, message: str, lines: list[str] | None = None) -> None:
        super().__init__(message)
        self.lines = lines or []


def psql_argv(*, ssh: str | None = None, ssh_key: str | None = None, compose_dir: str = "/opt/frost",
              service: str = "frost-db", container: str | None = None) -> list[str]:
    """The command that runs psql inside the FROST database container."""
    in_container = ["sh", "-c", CONTAINER_PSQL]
    if container:
        return ["docker", "exec", "-i", container, *in_container]
    if not ssh:
        raise ValueError("either ssh or container is required")
    remote = (
        f"cd {shlex.quote(compose_dir)} && "
        f"docker compose exec -T {shlex.quote(service)} {shlex.join(in_container)}"
    )
    argv = ["ssh", "-o", "BatchMode=yes"]
    if ssh_key:
        argv += ["-i", os.path.expanduser(ssh_key)]
    return [*argv, ssh, remote]


def psql_runner(argv: list[str], timeout: float) -> RunSql:
    """Run one psql session per call: SQL on stdin, result lines back."""

    def run(sql: str) -> list[str]:
        try:
            proc = subprocess.run(argv, input=sql, capture_output=True, text=True, encoding="utf-8",
                                  timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise PsqlError(f"psql session did not finish within {timeout:.0f} s") from exc
        lines = proc.stdout.splitlines()
        if proc.returncode != 0:
            raise PsqlError(f"psql exited with {proc.returncode}: {proc.stderr.strip()}", lines)
        return lines

    return run


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def scan(run_sql: RunSql, bounds: tuple[int, int] | None, args: argparse.Namespace) -> list[Group]:
    """Collect misrouted groups: by ID range over all rows, or per datastream with --datastream."""
    if args.datastream:
        statements = [datastream_scan_sql(ds_id, not args.sc_datastreams_only) for ds_id in args.datastream]
    elif bounds is None:
        return []
    else:
        statements = [range_scan_sql(lo, hi, not args.sc_datastreams_only) for lo, hi in scan_ranges(bounds, args.range_ids)]
    groups: list[Group] = []
    done = 0
    for part in _chunks(statements, args.chunk):
        groups += parse_groups(run_sql(READ_ONLY_SESSION + "".join(part)))
        done += len(part)
        _progress(f"  scanned {done}/{len(statements)} statements, {len(groups)} group rows so far")
        if args.pause:
            time.sleep(args.pause)
    return merge_groups(groups)


def count_duplicates(run_sql: RunSql, plan: Plan, chunk: int) -> dict[tuple[Any, ...], int]:
    moves = plan.by_action()[MOVE]
    found: dict[tuple[Any, ...], int] = {}
    for part in _chunks(moves, chunk):
        for row in parse_json_lines(run_sql(READ_ONLY_SESSION + "".join(duplicates_sql(p) for p in part)), "dup"):
            if int(row["n"]):
                found[(int(row["ds"]), row.get("sid"), row.get("prop"), row.get("thing"))] = int(row["n"])
    return found


class IdsFile:
    """Append-only JSON lines record of every write, fsynced before the write runs."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps(record, separators=(",", ":")) + "\n")
            out.flush()
            os.fsync(out.fileno())

    def moves(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records = []
        with self.path.open(encoding="utf-8") as src:
            for line in src:
                if line.strip():
                    record = json.loads(line)
                    if record.get("op") == "move":
                        records.append(record)
        return records


def refresh(run_sql: RunSql, ds_ids: Iterable[int], chunk: int) -> int:
    ids = sorted(set(ds_ids))
    for part in _chunks(ids, chunk):
        run_sql(WRITE_SESSION + "".join(refresh_sql(i) for i in part))
    return len(ids)


def apply_moves(run_sql: RunSql, plan: Plan, ids_file: IdsFile, args: argparse.Namespace) -> tuple[int, int, set[int]]:
    """Move every 'move' group in batches. Returns (rows moved, duplicates skipped, datastreams touched)."""
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    moved = skipped = 0
    touched: set[int] = set()
    batch_no = 0
    for planned in plan.by_action()[MOVE]:
        g, target = planned.group, planned.target
        assert target is not None
        after = g.min_id - 1
        while True:
            rows = parse_json_lines(run_sql(READ_ONLY_SESSION + batch_select_sql(planned, after, args.batch_rows)), "row")
            if not rows:
                break
            after = int(rows[-1]["id"])
            to_move = [(int(r["id"]), None if r.get("foi") is None else int(r["foi"])) for r in rows if not r.get("dup")]
            skipped += len(rows) - len(to_move)
            if not to_move:
                continue
            batch_no += 1
            batch = f"{run_id}-{batch_no}"
            # Durable record first: what moves where, and each row's FEATURE_ID now.
            ids_file.append({
                "op": "move", "batch": batch, "from_ds": g.ds, "to_ds": target.id, "to_foi": planned.foi,
                "rows": [list(r) for r in to_move],
            })
            try:
                result = parse_json_lines(run_sql(WRITE_SESSION + move_sql(g.ds, target.id, to_move, planned.foi)), "moved")
            except PsqlError as exc:
                raise PsqlError(f"{exc} (batch {batch}; rows moved before it: {moved:,}; re-running is safe, "
                                f"--rollback restores what the IDs file lists)") from exc
            count = int(result[0]["n"]) if result else 0
            ids_file.append({"op": "committed", "batch": batch, "moved": count})
            moved += count
            touched.update((g.ds, target.id))
            _progress(f"  moved {moved:,} rows so far (datastream {g.ds} -> {target.id})")
    return moved, skipped, touched


def rollback(run_sql: RunSql, ids_file: IdsFile, chunk: int) -> tuple[int, set[int]]:
    restored = 0
    touched: set[int] = set()
    for record in ids_file.moves():
        rows = [(int(i), None if f is None else int(f)) for i, f in record["rows"]]
        from_ds, to_ds = int(record["from_ds"]), int(record["to_ds"])
        for part in _chunks(rows, 5000):
            result = parse_json_lines(run_sql(WRITE_SESSION + restore_sql(from_ds, to_ds, part)), "restored")
            count = int(result[0]["n"]) if result else 0
            ids_file.append({"op": "rolled_back", "batch": record["batch"], "restored": count})
            restored += count
        touched.update((from_ds, to_ds))
        _progress(f"  restored {restored:,} rows so far")
    refresh(run_sql, touched, chunk)
    return restored, touched


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan (dry run) or apply moving misrouted Sensor.Community observations in FROST.",
        epilog="Dry run unless --apply or --rollback is given.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--ssh", metavar="USER@HOST", help="SSH destination of the FROST host")
    target.add_argument("--container", metavar="NAME", help="local docker container running the FROST database")
    parser.add_argument("--ssh-key", metavar="PATH", help="identity file for --ssh")
    parser.add_argument("--compose-dir", default="/opt/frost", help="compose directory on the host (%(default)s)")
    parser.add_argument("--service", default="frost-db", help="database service in that compose project (%(default)s)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="move the 'move' groups (default: dry run)")
    mode.add_argument("--rollback", action="store_true", help="move back every row listed in --ids-file")
    parser.add_argument("--ids-file", type=Path, metavar="PATH",
                        help="required with --apply/--rollback: JSON lines record of every moved row")
    parser.add_argument("--plan-file", type=Path, metavar="PATH", help="write the full plan as CSV")
    parser.add_argument("--datastream", type=int, action="append", metavar="ID",
                        help="limit scan and moves to this source datastream (repeatable), e.g. a canary")
    parser.add_argument("--sc-datastreams-only", action="store_true",
                        help="don't look for Sensor.Community readings in other sources' datastreams")
    parser.add_argument("--prefix", default="GEO_", help="entity name prefix of the connector (%(default)s)")
    parser.add_argument("--range-ids", type=int, default=200_000, help="observation IDs per scan statement (%(default)s)")
    parser.add_argument("--batch-rows", type=int, default=5000, help="rows per UPDATE (%(default)s)")
    parser.add_argument("--chunk", type=int, default=10, help="statements per psql session (%(default)s)")
    parser.add_argument("--pause", type=float, default=0.0, help="seconds to wait between scan sessions (%(default)s)")
    parser.add_argument("--top", type=int, default=15, help="lines per report section (%(default)s)")
    parser.add_argument("--no-duplicate-check", action="store_true", help="skip counting duplicates in the dry run")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds per psql session (%(default)s)")
    args = parser.parse_args(argv)
    if min(args.range_ids, args.batch_rows, args.chunk, args.top) < 1:
        parser.error("--range-ids, --batch-rows, --chunk and --top must be positive")
    if (args.apply or args.rollback) and args.ids_file is None:
        parser.error("--apply and --rollback need --ids-file: the record of moved observations")
    return args


def run(args: argparse.Namespace, run_sql: RunSql) -> int:
    if args.rollback:
        print(f"ROLLBACK: moving the rows listed in {args.ids_file} back")
        ids_file = IdsFile(args.ids_file)
        if not ids_file.moves():
            print("Nothing recorded in the IDs file.")
            return 1
        restored, touched = rollback(run_sql, ids_file, args.chunk)
        print(f"Restored {restored:,} observations; refreshed times of {len(touched)} datastreams.")
        return 0

    mode = "APPLY: moves observations" if args.apply else "DRY RUN: read-only, nothing is written"
    print(f"Misrouted Sensor.Community observations ({mode})")
    catalogue, bounds = parse_catalogue(run_sql(READ_ONLY_SESSION + catalogue_sql()), args.prefix)
    sc_count = sum(1 for ds in catalogue.datastreams.values() if ds.source == SC_SOURCE)
    print(f"Catalogue: {len(catalogue.datastreams):,} datastreams ({sc_count:,} Sensor.Community), "
          f"{len(catalogue.fois):,} Things with Locations; observation IDs {bounds}")
    if args.datastream:
        unknown = sorted(set(args.datastream) - set(catalogue.datastreams))
        if unknown:
            print(f"Unknown datastreams: {unknown}")
            return 1

    scope = "Sensor.Community datastreams" if args.sc_datastreams_only else \
        "Sensor.Community datastreams, and Sensor.Community readings in any datastream"
    _progress(f"Scanning ({scope}) ...")
    groups = scan(run_sql, bounds, args)
    plan = build_plan(groups, catalogue)
    duplicates = {} if args.no_duplicate_check else count_duplicates(run_sql, plan, args.chunk)
    print_report(plan, catalogue, duplicates, args.top)
    if args.plan_file:
        write_plan_csv(args.plan_file, plan, catalogue, duplicates)
        print(f"\nPlan written to {args.plan_file}")

    moves = plan.by_action()[MOVE]
    print(f"\nPlan: move {sum(p.group.rows for p in moves):,} rows in {len(moves)} groups, "
          f"UPDATEs of at most {args.batch_rows:,} rows, each committed on its own; then refresh the "
          f"phenomenon/result times of the datastreams touched.")
    if moves:
        example = moves[0]
        assert example.target is not None
        print("First batch would be selected with:" + batch_select_sql(example, example.group.min_id - 1, args.batch_rows))
    if not args.apply:
        print("Dry run: nothing was written. Re-run with --apply --ids-file PATH to move.")
        return 0

    ids_file = IdsFile(args.ids_file)
    ids_file.append({"op": "run", "started": datetime.now(UTC).isoformat(), "datastreams": args.datastream or "all"})
    _progress(f"Applying; every batch is recorded in {args.ids_file} before it is written ...")
    moved, skipped, touched = apply_moves(run_sql, plan, ids_file, args)
    refreshed = refresh(run_sql, touched, args.chunk)
    print(f"Moved {moved:,} observations ({skipped:,} left in place: their target already has that "
          f"phenomenonTime); refreshed times of {refreshed} datastreams. Record: {args.ids_file}")

    _progress("Verifying, read-only ...")
    sources = sorted({p.group.ds for p in moves})
    if sources:
        recheck = argparse.Namespace(**{**vars(args), "datastream": sources})
        remaining = {g.key: g.rows for g in scan(run_sql, bounds, recheck)}
        left = sum(remaining.get(p.group.key, 0) for p in moves)
        print(f"Verification: {left:,} rows of the moved groups are still in their source datastreams "
              f"(duplicates kept, or rows written after the scan).")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run_sql = psql_runner(
        psql_argv(ssh=args.ssh, ssh_key=args.ssh_key, compose_dir=args.compose_dir, service=args.service,
                  container=args.container),
        args.timeout,
    )
    try:
        return run(args, run_sql)
    except PsqlError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
