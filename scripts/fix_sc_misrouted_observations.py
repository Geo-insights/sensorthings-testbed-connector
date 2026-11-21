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
changed phenomenon/result times, not to a changed DATASTREAM_ID, so the script
recomputes phenomenonTime and resultTime of the source and target datastreams
of every group once its batches are done (and, best effort, when a batch
fails), and every --apply, --rollback --apply and --refresh-only first
refreshes every datastream the IDs file names: a run that stopped half-way
never leaves stale times behind for good. OBSERVED_AREA and LAST_FOI_ID: a
moved row carries its target Thing's FoI (or keeps its own within one Thing),
which a datastream that has rows already covers; a datastream without an
observed area yet (registered, never written to) gets the convex hull of its
rows' FoI geometries and the FoI of its newest row, as FROST 2.8's insert
trigger (datastreams_update_insert) would have set them. Like FROST on a
delete, the script never shrinks an observed area: a datastream emptied by a
move or a rollback keeps it.

Duplicates: a row is not moved when its datastream-to-be already holds the
same reading: the same label (sensor_id, observed_property), the same
phenomenonTime (start and end) and the same result (type, number, string,
boolean, JSON), all NULL-safe: what scripts/cleanup_frost_observations.py
--duplicates (feat/frost-observation-cleanup) calls an exact duplicate. It is
counted (proposed deletion).
Another row at that phenomenonTime alone does not keep a row in place: it can
be another misrouted row, which moves away itself, or another reading. The
check runs per batch, so a reading misrouted twice moves once and its second
copy then counts as a duplicate. A pressure row the backfill converted in its
target already (b below) no longer has the result of its copy still in Pa: the
copy moves, the backfill re-run (e) converts it, and the two are then equal
(left to that script's --duplicates).

Safety (the same pattern as scripts/fix_sc_pressure_units.py)
--------------------------------------------------------------
* Dry run by default: read-only sessions (default_transaction_read_only), the
  scan walks OBSERVATIONS once in ID ranges (one statement per range, far below
  the database's 60 s statement limit), and a plan is printed (and written as
  CSV with --plan-file).
* --apply moves only 'move' groups, in batches of at most --batch-rows rows. For
  each batch the rows are first selected read-only, then appended to --ids-file
  (JSON lines, flushed and fsynced) with their FEATURE_ID and RESULT_NUMBER
  before the UPDATE runs; the UPDATE only touches the listed IDs while they are
  still in their source datastream, and each batch commits on its own.
  --datastream limits it to source datastreams (a canary).
* The IDs file starts with a header line naming this script; any other file
  (e.g. the pressure backfill's CSV) is refused. A last line cut off by an
  interrupted run is dropped before anything is appended (its UPDATE never
  ran) and skipped when the file is read.
* --rollback is a dry run: per recorded batch, how many rows are still where
  the script put them, already back, elsewhere or deleted, and how many hold
  another RESULT_NUMBER than when they were moved (a row listed by several
  batches, e.g. a failed batch and the re-run's, counts once, by the newest
  batch: the one that moves it back first). --rollback --apply moves
  every listed row that is still where the script put it back to its datastream
  and FEATURE_ID, newest batch first, refreshes the datastreams' times and
  checks that none is left. --datastream limits it to batches from those source
  datastreams. Rows of a batch that never committed are untouched.
* Writes refuse to start while another write session of this script is still
  active on the server (e.g. one left behind by an interrupted run).
* Re-running is safe: moved rows match their datastream and are not selected
  again.

Disk: a moved row gets a new row version in OBSERVATIONS and new entries in
every index on it (DATASTREAM_ID changes, so no HOT update). Measured on FROST
2.8 / PostgreSQL 16: 0.64 to 1.4 KB of WAL per moved row (the high end right
after checkpoints, when the first change to a page writes the whole page), so
1.5 to 3 GB of WAL for the ~2.3 million rows to move on production (dry run,
2026-11-20), plus the dead row versions in the table and its indexes until
(auto)vacuum reclaims them. Check free disk space on the FROST host before
--apply, as for the pressure backfill, and move in parts (--datastream) if it
is tight.

Order with the Pa -> hPa backfill (scripts/fix_sc_pressure_units.py on
fix/sc-pressure-hpa), which converts pressure-labelled rows inside 'Air
pressure' datastreams and skips rows labelled as another property:
1. Deploy the connector fixes (routing, discovery, pressure unit, registration
   of later-discovered entity sets) so no new rows are misrouted or in Pa.
2. Let Sensor.Community run a few polls: it registers the missing datastreams.
3. Dry run this script, then --apply (canary first): pressure rows that sat in
   other datastreams now sit in 'Air pressure' datastreams, still in Pa.
4. Run (or re-run) the pressure backfill with its same IDs file: it converts
   them; rows it converted earlier are below its Pa band and untouched. Running
   the backfill before this script too is harmless.
5. After the connector's dead-letter queue has drained or expired: a pre-fix
   payload replayed into a misrouted datastream carries the datastream id it
   was resolved to, and a move re-run puts it in an 'Air pressure' datastream,
   still in Pa. So, in this order: this dry run and --apply again (same IDs
   file), then the pressure backfill --apply again (same file), then the
   pressure backfill's dry run, which must find 0 pressure readings to convert.

Undo: this --rollback (dry run, then with --apply) moves rows back whatever
their value; rows the pressure backfill converted after their move go back in
hPa, and the dry run counts them ("value changed"). The pressure backfill's
--rollback restores every row it converted wherever the row is now. Run both,
in either order, to get back to the state before both scripts; run only this
one to undo only the moves.

How it connects: like the pressure backfill it pipes SQL into psql inside the
FROST database container (ssh to the FROST host with keepalives, so a dead
connection fails within about a minute, or docker exec for a local
container); psql there uses the container's own $POSTGRES_USER/$POSTGRES_DB, so
the script never handles a credential.

Usage
-----
    python scripts/fix_sc_misrouted_observations.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner
    python scripts/fix_sc_misrouted_observations.py --container <db-container> --plan-file plan.csv
    python scripts/fix_sc_misrouted_observations.py --ssh ... --apply --ids-file moved.jsonl --datastream 1386
    python scripts/fix_sc_misrouted_observations.py --ssh ... --rollback --ids-file moved.jsonl [--apply]
    python scripts/fix_sc_misrouted_observations.py --ssh ... --refresh-only --ids-file moved.jsonl
"""

from __future__ import annotations

import argparse
import csv
import json
import math
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

WRITE_APP_NAME = "fix_sc_misrouted_observations"

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

# The first line of every IDs file: refuses another script's file (e.g. the pressure
# backfill's CSV) and a file of an older format of this one.
IDS_HEADER: dict[str, Any] = {"op": "ids-file", "script": "fix_sc_misrouted_observations", "version": 2}


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


# What makes a row x in the target the same reading as row o: its label, its phenomenonTime and its
# result, as scripts/cleanup_frost_observations.py --duplicates defines an exact duplicate. A row at
# that time with another label (e.g. another misrouted row, which moves away itself) or another
# result is another reading: it does not keep o where it is.
_SAME_READING_COLUMNS = ('"PHENOMENON_TIME_END"', '"RESULT_TYPE"', '"RESULT_NUMBER"', '"RESULT_STRING"',
                         '"RESULT_BOOLEAN"', '"RESULT_JSON"')
_LABEL_KEYS = ("sensor_id", "observed_property")


def same_reading_sql(target_ds: int, alias: str = "o") -> str:
    """EXISTS: datastream target_ds already holds the reading of row ``alias`` (same label, time and result)."""
    o = alias
    same = [f'x."PHENOMENON_TIME_START" = {o}."PHENOMENON_TIME_START"']
    same += [f'x."PARAMETERS"->>{lit(k)} IS NOT DISTINCT FROM {o}."PARAMETERS"->>{lit(k)}' for k in _LABEL_KEYS]
    same += [f"x.{c} IS NOT DISTINCT FROM {o}.{c}" for c in _SAME_READING_COLUMNS]
    conditions = "\n                  AND ".join(same)
    return f"""EXISTS (SELECT 1 FROM "OBSERVATIONS" x
                WHERE x."DATASTREAM_ID" = {int(target_ds)}
                  AND {conditions})"""


def duplicates_sql(planned: PlannedGroup) -> str:
    """Rows of a move group whose reading (label, phenomenonTime and result) the target already holds."""
    g, target = planned.group, planned.target
    assert target is not None
    return f"""
SELECT json_build_object('t', 'dup', 'ds', {int(g.ds)}, 'sid', {lit(g.sid)}, 'prop', {lit(g.prop)},
                         'thing', {lit(g.thing)}, 'n', count(*))::text
  FROM "OBSERVATIONS" o
 WHERE o."DATASTREAM_ID" = {int(g.ds)} AND o."ID" BETWEEN {int(g.min_id)} AND {int(g.max_id)}
   AND {label_filter("o", g)}
   AND {same_reading_sql(target.id)};
"""


def batch_select_sql(planned: PlannedGroup, after_id: int, limit: int) -> str:
    """The next rows of a move group, read-only, with their FEATURE_ID and RESULT_NUMBER and a duplicate flag.

    dup: the target already holds the same reading (label, phenomenonTime and result).
    """
    g, target = planned.group, planned.target
    assert target is not None
    return f"""
SELECT json_build_object('t', 'row', 'id', o."ID", 'foi', o."FEATURE_ID", 'num', o."RESULT_NUMBER",
                         'dup', {same_reading_sql(target.id)})::text
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
    """Recompute a datastream's phenomenon and result time extents from its rows.

    A datastream without an observed area yet (registered, then only written to by
    moves) also gets one: the convex hull of its rows' FoI geometries, with the FoI
    of its newest row as LAST_FOI_ID. That is what FROST 2.8's insert trigger
    (datastreams_update_insert) arrives at, one FoI at a time. An existing area is
    kept: FROST never shrinks it either, also not on a delete. The area's subqueries
    only run for a datastream without one.
    """
    ds = int(ds_id)
    return f"""
UPDATE "DATASTREAMS" d
   SET "PHENOMENON_TIME_START" = a.p0, "PHENOMENON_TIME_END" = a.p1,
       "RESULT_TIME_START" = a.r0, "RESULT_TIME_END" = a.r1,
       "OBSERVED_AREA" = CASE WHEN d."OBSERVED_AREA" IS NOT NULL THEN d."OBSERVED_AREA" ELSE
           (SELECT ST_ConvexHull(ST_Collect(f."GEOM")) FROM "FEATURES" f
             WHERE f."ID" IN (SELECT o."FEATURE_ID" FROM "OBSERVATIONS" o WHERE o."DATASTREAM_ID" = {ds})) END,
       "LAST_FOI_ID" = CASE WHEN d."OBSERVED_AREA" IS NOT NULL THEN d."LAST_FOI_ID" ELSE
           (SELECT o."FEATURE_ID" FROM "OBSERVATIONS" o WHERE o."DATASTREAM_ID" = {ds} ORDER BY o."ID" DESC LIMIT 1) END
  FROM (SELECT min("PHENOMENON_TIME_START") AS p0,
               max(coalesce("PHENOMENON_TIME_END", "PHENOMENON_TIME_START")) AS p1,
               min("RESULT_TIME") AS r0, max("RESULT_TIME") AS r1
          FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" = {ds}) a
 WHERE d."ID" = {ds};
"""


def busy_sql() -> str:
    """Other write sessions of this script that may still change rows (read-only)."""
    return f"""
SELECT json_build_object('t', 'busy', 'pid', pid, 'state', state,
                         'running', coalesce(to_char(now() - query_start, 'HH24:MI:SS'), ''))::text
  FROM pg_stat_activity
 WHERE application_name = {lit(WRITE_APP_NAME)} AND pid <> pg_backend_pid()
   AND state IN ('active', 'idle in transaction', 'idle in transaction (aborted)');
"""


def _number_literal(value: Any) -> str | None:
    """A recorded RESULT_NUMBER (as json_build_object printed it) as an SQL literal; None: not comparable."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
        if math.isnan(number):
            return "'NaN'"
        if math.isinf(number):
            return "'Infinity'" if number > 0 else "'-Infinity'"
        return repr(number)
    if value in ("NaN", "Infinity", "-Infinity"):  # json_build_object prints these as strings
        return f"'{value}'"
    return None


def move_state_sql(batch: str, from_ds: int, to_ds: int, rows: Sequence[Sequence[Any]]) -> str:
    """Where the rows of one recorded batch are now (read-only): rows are [id, foi] or [id, foi, result_number].

    in_to: still in to_ds (what --rollback --apply moves back); in_from: back already;
    changed: of in_to, rows whose RESULT_NUMBER is not the one recorded when they were moved.
    """
    values = []
    for row in rows:
        number = _number_literal(row[2]) if len(row) > 2 else None
        values.append(f"({int(row[0])}::bigint, {number or 'NULL'}::double precision, {'true' if number else 'false'})")
    return f"""
WITH v(id, num, known) AS (VALUES {", ".join(values)})
SELECT json_build_object('t', 'rbstate', 'batch', {lit(batch)}, 'listed', count(*),
                         'in_to', count(*) FILTER (WHERE o."DATASTREAM_ID" = {int(to_ds)}),
                         'in_from', count(*) FILTER (WHERE o."DATASTREAM_ID" = {int(from_ds)}),
                         'missing', count(*) FILTER (WHERE o."ID" IS NULL),
                         'changed', count(*) FILTER (WHERE o."DATASTREAM_ID" = {int(to_ds)} AND v.known
                                                      AND o."RESULT_NUMBER" IS DISTINCT FROM v.num))::text
  FROM v LEFT JOIN "OBSERVATIONS" o ON o."ID" = v.id;
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
        print(f"  of the moves, rows whose target already holds the same reading (not moved): "
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
    # Keepalives: a silently dropped connection fails within about a minute instead of hanging until --timeout.
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    if ssh_key:
        argv += ["-i", os.path.expanduser(ssh_key)]
    return [*argv, ssh, remote]


def _complete_lines(output: str | bytes | None) -> list[str]:
    """The complete lines of what psql printed; a last line without its line end was cut off."""
    if not output:
        return []
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
    lines = text.splitlines()
    if lines and not text.endswith("\n"):
        lines.pop()
    return lines


def psql_runner(argv: list[str], timeout: float) -> RunSql:
    """Run one psql session per call: SQL on stdin, result lines back.

    On failure, PsqlError.lines holds the complete lines printed before it: after a
    non-zero exit, after the timeout, or empty after Ctrl-C.
    """

    def run(sql: str) -> list[str]:
        try:
            proc = subprocess.run(argv, input=sql, capture_output=True, text=True, encoding="utf-8",
                                  timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise PsqlError(f"psql session did not finish within {timeout:.0f} s", _complete_lines(exc.stdout)) from exc
        except KeyboardInterrupt as exc:
            raise PsqlError("interrupted (Ctrl-C)") from exc
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


class IdsFileError(RuntimeError):
    pass


class IdsFile:
    """Append-only JSON lines record of every write, fsynced before the write runs.

    The first line is IDS_HEADER. Records: "run" and "rollback" (a run started),
    "move" (a batch about to be moved: from_ds, to_ds, to_foi and its rows as
    [id, FEATURE_ID, RESULT_NUMBER]), "committed" and "rolled_back" (a batch done).
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _write_header(self) -> None:
        with self.path.open("w", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps(IDS_HEADER, separators=(",", ":")) + "\n")
            out.flush()
            os.fsync(out.fileno())

    def _check_header(self, line: str) -> None:
        try:
            header = json.loads(line)
        except json.JSONDecodeError:
            header = None
        if header != IDS_HEADER:
            raise IdsFileError(
                f"{self.path} is not an IDs file of this script (its first line is not "
                f"{json.dumps(IDS_HEADER, separators=(',', ':'))}): refusing to use it. Pass this script's own file, "
                "or a new one."
            )

    def prepare(self) -> None:
        """Before writing: create the file with its header, or check the header and drop a cut-off last line.

        A last line without its line end was being appended when a run was interrupted; the
        write it announced is only sent once the whole line is on disk, so it never ran.
        """
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._write_header()
            return
        with self.path.open("rb+") as handle:
            first = handle.readline()
            self._check_header(first.decode("utf-8", "replace").rstrip("\r\n"))
            body = handle.tell()
            size = handle.seek(0, os.SEEK_END)
            if size == body:
                if not first.endswith(b"\n"):  # the header only, without its line end
                    handle.write(b"\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                return
            handle.seek(size - 1)
            if handle.read(1) == b"\n":
                return
            cut_at, end = body, size  # after the last line end, searched backwards a block at a time
            while end > body:
                start = max(body, end - 1_048_576)
                handle.seek(start)
                found = handle.read(end - start).rfind(b"\n")
                if found >= 0:
                    cut_at = start + found + 1
                    break
                end = start
            handle.seek(cut_at)
            _progress(f"  WARNING: {self.path}: dropping the cut-off last line {handle.read(80)!r}... "
                      "(written by an interrupted run; the write it announced was never sent)")
            handle.truncate(cut_at)
            handle.flush()
            os.fsync(handle.fileno())

    def append(self, record: dict[str, Any]) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._write_header()
        with self.path.open("a", encoding="utf-8", newline="\n") as out:
            out.write(json.dumps(record, separators=(",", ":")) + "\n")
            out.flush()
            os.fsync(out.fileno())

    def records(self) -> list[dict[str, Any]]:
        """Every record after the header; an unreadable last line (cut off by an interrupted run) is skipped."""
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8", errors="replace", newline="") as src:
            lines = src.read().split("\n")
        if lines == [""]:
            return []
        self._check_header(lines[0].rstrip("\r"))
        body = lines[1:]
        if body and body[-1] == "":
            body.pop()  # the file ends with a line end, as it should
        records = []
        for number, line in enumerate(body, start=2):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("not a JSON object")
            except ValueError as exc:
                if number == len(body) + 1:
                    _progress(f"  WARNING: {self.path}, line {number}: unreadable last line skipped "
                              f"({exc}; cut off by an interrupted run?)")
                    break
                raise IdsFileError(f"{self.path}, line {number}: not a record ({exc}): {line[:120]!r}") from exc
            records.append(record)
        return records

    def moves(self) -> list[dict[str, Any]]:
        """The "move" records, in the order they were written."""
        return [r for r in self.records() if r.get("op") == "move"]

    def datastreams(self) -> set[int]:
        """Every source and target datastream of the recorded moves."""
        return {int(r[key]) for r in self.moves() for key in ("from_ds", "to_ds")}


def recorded_rows(record: dict[str, Any]) -> list[tuple[int, int | None]]:
    """(observation ID, FEATURE_ID before the move) of the rows of a "move" record."""
    return [(int(row[0]), None if row[1] is None else int(row[1])) for row in record["rows"]]


def refresh(run_sql: RunSql, ds_ids: Iterable[int], chunk: int) -> int:
    """Recompute the times (and a missing observed area) of these datastreams; returns how many."""
    ids = sorted(set(ds_ids))
    for part in _chunks(ids, chunk):
        run_sql(WRITE_SESSION + "".join(refresh_sql(i) for i in part))
    return len(ids)


def busy(run_sql: RunSql) -> list[dict[str, Any]]:
    """Other write sessions of this script active on the server (read-only)."""
    return parse_json_lines(run_sql(READ_ONLY_SESSION + busy_sql()), "busy")


def refuse_if_busy(run_sql: RunSql) -> bool:
    """True, after saying so, when another write session of this script is active on the server."""
    sessions = busy(run_sql)
    if not sessions:
        return False
    print(f"Another write session of this script is still active on the server ({len(sessions)}):")
    for s in sessions:
        print(f"  pid {s.get('pid')}: {s.get('state')}, statement running for {s.get('running') or '?'}")
    print("It may be left from an interrupted run. Wait for it to end (each statement stops within the statement "
          "timeout), or end it with SELECT pg_terminate_backend(<pid>), then re-run.")
    return True


def _refresh_after_failure(run_sql: RunSql, ds_ids: set[int], chunk: int) -> str:
    """Best effort after a failed write: refresh what it may have changed. Returns a note for the error."""
    if not ds_ids:
        return ""
    names = sorted(ds_ids)
    try:
        if busy(run_sql):
            return (f" Not refreshed yet: datastreams {names} (a write session of this script is still active "
                    "on the server). Once it has ended, re-run or run --refresh-only: both refresh every "
                    "datastream the IDs file names first.")
        refresh(run_sql, ds_ids, chunk)
    except PsqlError as exc:
        return (f" Refreshing datastreams {names} failed too ({str(exc).splitlines()[0]}); re-run or run "
                "--refresh-only: both refresh every datastream the IDs file names first.")
    return f" Datastreams {names} were refreshed."


def apply_moves(run_sql: RunSql, plan: Plan, ids_file: IdsFile, args: argparse.Namespace) -> tuple[int, int, set[int]]:
    """Move every 'move' group in batches. Returns (rows moved, duplicates skipped, datastreams touched).

    The source and target datastream of a group are refreshed once its batches are done;
    when a batch fails, they are refreshed (best effort) before the error is raised.
    """
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    moved = skipped = 0
    touched: set[int] = set()
    batch_no = 0
    for planned in plan.by_action()[MOVE]:
        g, target = planned.group, planned.target
        assert target is not None
        pair = {g.ds, target.id}
        written = False
        after = g.min_id - 1
        while True:
            rows = parse_json_lines(run_sql(READ_ONLY_SESSION + batch_select_sql(planned, after, args.batch_rows)), "row")
            if not rows:
                break
            after = int(rows[-1]["id"])
            to_move = [r for r in rows if not r.get("dup")]
            skipped += len(rows) - len(to_move)
            if not to_move:
                continue
            ids = [(int(r["id"]), None if r.get("foi") is None else int(r["foi"])) for r in to_move]
            batch_no += 1
            batch = f"{run_id}-{batch_no}"
            # Durable record first: what moves where, and each row's FEATURE_ID and RESULT_NUMBER now.
            ids_file.append({
                "op": "move", "batch": batch, "from_ds": g.ds, "to_ds": target.id, "to_foi": planned.foi,
                "rows": [[i, f, r.get("num")] for (i, f), r in zip(ids, to_move, strict=True)],
            })
            written = True
            try:
                result = parse_json_lines(run_sql(WRITE_SESSION + move_sql(g.ds, target.id, ids, planned.foi)), "moved")
            except PsqlError as exc:
                note = _refresh_after_failure(run_sql, pair, args.chunk)
                raise PsqlError(f"{exc} (batch {batch}; rows moved before it: {moved:,}; re-running is safe, "
                                f"--rollback restores what the IDs file lists).{note}", exc.lines) from exc
            count = int(result[0]["n"]) if result else 0
            ids_file.append({"op": "committed", "batch": batch, "moved": count})
            moved += count
            _progress(f"  moved {moved:,} rows so far (datastream {g.ds} -> {target.id})")
        if written:
            try:
                refresh(run_sql, pair, args.chunk)
            except PsqlError as exc:
                raise PsqlError(f"{exc} (refreshing datastreams {sorted(pair)} after moving rows between them; "
                                "re-run or run --refresh-only: both refresh every datastream the IDs file names "
                                "first)", exc.lines) from exc
            touched |= pair
    return moved, skipped, touched


def selected_moves(records: Iterable[dict[str, Any]], datastreams: Iterable[int] | None = None) -> list[dict[str, Any]]:
    """The "move" records from these source datastreams (all when None or empty), in file order."""
    wanted = set(datastreams or ())
    return [r for r in records if r.get("op") == "move" and (not wanted or int(r["from_ds"]) in wanted)]


def rollback(run_sql: RunSql, ids_file: IdsFile, chunk: int, datastreams: Iterable[int] | None = None,
             batch_rows: int = 5000) -> tuple[int, set[int]]:
    """Move the recorded rows still in their target back, newest batch first. Returns (rows restored, datastreams).

    ``datastreams`` limits it to batches from those source datastreams. The datastreams of
    each source/target pair are refreshed once its batches are done, and (best effort) when
    a batch fails.
    """
    restored = 0
    touched: set[int] = set()
    pending: set[int] = set()

    def refresh_pending() -> None:
        try:
            refresh(run_sql, pending, chunk)
        except PsqlError as exc:
            raise PsqlError(f"{exc} (refreshing datastreams {sorted(pending)} after moving rows back; re-running the "
                            "rollback is safe and refreshes them)", exc.lines) from exc

    for record in reversed(selected_moves(ids_file.moves(), datastreams)):
        from_ds, to_ds = int(record["from_ds"]), int(record["to_ds"])
        if pending and pending != {from_ds, to_ds}:
            refresh_pending()
            pending = set()
        pending |= {from_ds, to_ds}
        for part in _chunks(recorded_rows(record), batch_rows):
            try:
                result = parse_json_lines(run_sql(WRITE_SESSION + restore_sql(from_ds, to_ds, part)), "restored")
            except PsqlError as exc:
                note = _refresh_after_failure(run_sql, pending, chunk)
                raise PsqlError(f"{exc} (moving batch {record['batch']} back; rows moved back before it: "
                                f"{restored:,}; re-running the rollback is safe).{note}", exc.lines) from exc
            count = int(result[0]["n"]) if result else 0
            ids_file.append({"op": "rolled_back", "batch": record["batch"], "restored": count})
            restored += count
        touched |= {from_ds, to_ds}
        _progress(f"  moved back {restored:,} rows so far (datastream {to_ds} -> {from_ds})")
    if pending:
        refresh_pending()
    return restored, touched


@dataclass(frozen=True)
class MoveState:
    """Where the rows of one recorded batch are now.

    A row listed by several batches (a batch that failed, then the re-run's batch; or a row
    moved twice) is counted by the newest of them only, the one the rollback moves it back
    with first; the older ones count it as relisted.
    """

    batch: str
    from_ds: int
    to_ds: int
    listed: int  # rows in the batch
    in_to: int  # still where the script put them: moved back by --rollback --apply
    in_from: int  # back in their source datastream already (or never moved: a batch that failed)
    missing: int  # deleted since
    changed: int  # of in_to: RESULT_NUMBER is not the one recorded when they were moved
    relisted: int = 0  # listed by a later batch too: counted there

    @property
    def elsewhere(self) -> int:
        """Rows in neither datastream (moved again since): left alone."""
        return self.listed - self.relisted - self.in_to - self.in_from - self.missing


def move_states(run_sql: RunSql, records: Sequence[dict[str, Any]], chunk: int,
                batch_rows: int = 5000) -> list[MoveState]:
    """The state of every recorded batch, read-only, in the order of ``records``; a row by its newest batch only."""
    newest = {int(row[0]): index for index, record in enumerate(records) for row in record["rows"]}
    own = [[row for row in record["rows"] if newest[int(row[0])] == index] for index, record in enumerate(records)]
    statements = [(index, part) for index, rows in enumerate(own) for part in _chunks(rows, batch_rows)]
    totals: dict[int, Counter[str]] = {}
    for session in _chunks(statements, chunk):
        sql = READ_ONLY_SESSION + "".join(
            move_state_sql(str(index), int(records[index]["from_ds"]), int(records[index]["to_ds"]), part)
            for index, part in session
        )
        for row in parse_json_lines(run_sql(sql), "rbstate"):
            totals.setdefault(int(row["batch"]), Counter()).update(
                {key: int(row[key]) for key in ("in_to", "in_from", "missing", "changed")}
            )
    states = []
    for index, record in enumerate(records):
        found = totals.get(index, Counter())
        states.append(MoveState(str(record["batch"]), int(record["from_ds"]), int(record["to_ds"]), len(record["rows"]),
                                found["in_to"], found["in_from"], found["missing"], found["changed"],
                                len(record["rows"]) - len(own[index])))
    return states


def print_move_states(states: Sequence[MoveState]) -> None:
    print("\nRecorded batches, newest first (the order --rollback --apply moves them back)")
    print(f"  {'batch':<22}{'from':>8} <- {'to':<8}{'rows':>8}{'later':>8}{'in to':>8}{'back':>8}{'else':>8}"
          f"{'gone':>8}{'changed':>9}")
    for s in reversed(states):
        print(f"  {s.batch:<22}{s.from_ds:>8} <- {s.to_ds:<8}{s.listed:>8}{s.relisted:>8}{s.in_to:>8}{s.in_from:>8}"
              f"{s.elsewhere:>8}{s.missing:>8}{s.changed:>9}")

    def line(label: str, count: int) -> None:
        print(f"  {label:<60}{count:>12,}")

    print("\nTotals")
    line("batches", len(states))
    line("rows recorded", sum(s.listed for s in states))
    line("  listed again by a later batch (counted there)", sum(s.relisted for s in states))
    line("  still where the script put them: to move back", sum(s.in_to for s in states))
    line("    of those with another RESULT_NUMBER than when moved", sum(s.changed for s in states))
    line("  in their source datastream (moved back, or never moved)", sum(s.in_from for s in states))
    line("  in another datastream (moved again since): left alone", sum(s.elsewhere for s in states))
    line("  not found (deleted)", sum(s.missing for s in states))
    if any(s.changed for s in states):
        print("  NOTE: rows with another RESULT_NUMBER than when they were moved (converted to hPa by "
              "scripts/fix_sc_pressure_units.py since?) move back with their current value. That script's "
              "--rollback restores their values, wherever they are, before or after this rollback.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan (dry run) or apply moving misrouted Sensor.Community observations in FROST, or roll the "
                    "moves back.",
        epilog="Dry run unless --apply is given, also with --rollback. The module docstring has the runbook.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--ssh", metavar="USER@HOST", help="SSH destination of the FROST host")
    target.add_argument("--container", metavar="NAME", help="local docker container running the FROST database")
    parser.add_argument("--ssh-key", metavar="PATH", help="identity file for --ssh")
    parser.add_argument("--compose-dir", default="/opt/frost", help="compose directory on the host (%(default)s)")
    parser.add_argument("--service", default="frost-db", help="database service in that compose project (%(default)s)")
    parser.add_argument("--apply", action="store_true",
                        help="write: move the 'move' groups, or with --rollback move the rows back (default: dry run)")
    parser.add_argument("--rollback", action="store_true",
                        help="move the rows listed in --ids-file back: a dry run that reports per batch, unless "
                             "--apply is also given")
    parser.add_argument("--refresh-only", action="store_true",
                        help="only recompute the times (and a missing observed area) of every datastream the IDs "
                             "file names, e.g. after an interrupted run")
    parser.add_argument("--ids-file", type=Path, metavar="PATH",
                        help="required with --apply, --rollback and --refresh-only: JSON lines record of every moved row")
    parser.add_argument("--plan-file", type=Path, metavar="PATH", help="write the full plan as CSV")
    parser.add_argument("--datastream", type=int, action="append", metavar="ID",
                        help="limit scan and moves, or a rollback, to this source datastream (repeatable), e.g. a "
                             "canary")
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
    if args.refresh_only and (args.apply or args.rollback):
        parser.error("--refresh-only writes nothing but the datastreams' times: no --apply or --rollback")
    if (args.apply or args.rollback or args.refresh_only) and args.ids_file is None:
        parser.error("--apply, --rollback and --refresh-only need --ids-file: the record of moved observations")
    if (args.rollback or args.refresh_only) and not args.ids_file.is_file():
        parser.error(f"--ids-file {args.ids_file} does not exist")
    return args


def refresh_recorded(run_sql: RunSql, ids_file: IdsFile, chunk: int, skip: Iterable[int] = ()) -> int:
    """Refresh every datastream the IDs file names but ``skip``: what an interrupted run may have left stale."""
    ds_ids = ids_file.datastreams() - set(skip)
    if ds_ids:
        _progress(f"Refreshing the times of the {len(ds_ids)} datastreams the IDs file names ...")
    return refresh(run_sql, ds_ids, chunk)


def run_refresh_only(args: argparse.Namespace, run_sql: RunSql) -> int:
    print(f"Refresh of the datastreams named in {args.ids_file} (writes their times, and a missing observed area)")
    ids_file = IdsFile(args.ids_file)
    ids_file.records()  # a wrong file fails before anything else
    if refuse_if_busy(run_sql):
        return 1
    print(f"Refreshed {refresh_recorded(run_sql, ids_file, args.chunk)} datastreams.")
    return 0


def run_rollback(args: argparse.Namespace, run_sql: RunSql) -> int:
    mode = "APPLY: moves observations back" if args.apply else "DRY RUN: read-only, nothing is written"
    print(f"Rollback of the moves recorded in {args.ids_file} ({mode})")
    ids_file = IdsFile(args.ids_file)
    if args.apply:
        ids_file.prepare()  # a wrong file fails before anything else; a cut-off last line goes
    moves = ids_file.moves()
    if not moves:
        print("Nothing recorded in the IDs file.")
        return 1
    selected = selected_moves(moves, args.datastream)
    scope = f" from source datastreams {sorted(args.datastream)}" if args.datastream else ""
    print(f"{len(moves):,} batches recorded; {len(selected):,} of them{scope}, "
          f"{sum(len(r['rows']) for r in selected):,} rows")
    if not selected:
        print("Nothing to move back.")
        return 1
    _progress("Checking the recorded rows, read-only ...")
    before = move_states(run_sql, selected, args.chunk, args.batch_rows)
    print_move_states(before)
    planned = sum(s.in_to for s in before)
    if not args.apply:
        print(f"\nDry run: nothing was written. Re-run with --apply to move {planned:,} rows back.")
        return 0

    if refuse_if_busy(run_sql):
        return 1
    pairs = {int(r[key]) for r in selected for key in ("from_ds", "to_ds")}
    refresh_recorded(run_sql, ids_file, args.chunk, skip=pairs)  # the rollback refreshes these itself
    ids_file.append({"op": "rollback", "started": datetime.now(UTC).isoformat(), "datastreams": args.datastream or "all"})
    _progress("Moving rows back, newest batch first ...")
    restored, touched = rollback(run_sql, ids_file, args.chunk, args.datastream, args.batch_rows)
    print(f"\nMoved back {restored:,} observations (planned {planned:,}); refreshed times of {len(touched)} "
          "datastreams.")
    _progress("Verifying, read-only ...")
    left = sum(s.in_to for s in move_states(run_sql, selected, args.chunk, args.batch_rows))
    if left:
        print(f"Verification: {left:,} recorded rows are still where the script put them. Re-run to move them back.")
        return 1
    print("Verification: no recorded row is left where the script put it.")
    return 0


def run(args: argparse.Namespace, run_sql: RunSql) -> int:
    if args.refresh_only:
        return run_refresh_only(args, run_sql)
    if args.rollback:
        return run_rollback(args, run_sql)

    mode = "APPLY: moves observations" if args.apply else "DRY RUN: read-only, nothing is written"
    print(f"Misrouted Sensor.Community observations ({mode})")
    ids_file = IdsFile(args.ids_file) if args.apply else None
    if ids_file is not None:
        ids_file.prepare()  # a wrong file fails before anything else; a cut-off last line goes
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
          f"UPDATEs of at most {args.batch_rows:,} rows, each committed on its own; the phenomenon/result times "
          f"of each group's source and target datastream are refreshed once its batches are done.")
    if moves:
        example = moves[0]
        assert example.target is not None
        print("First batch would be selected with:" + batch_select_sql(example, example.group.min_id - 1, args.batch_rows))
    if ids_file is None:
        print("Dry run: nothing was written. Re-run with --apply --ids-file PATH to move.")
        return 0

    if refuse_if_busy(run_sql):
        return 1
    refresh_recorded(run_sql, ids_file, args.chunk)  # what an earlier, interrupted run may have left stale
    ids_file.append({"op": "run", "started": datetime.now(UTC).isoformat(), "datastreams": args.datastream or "all"})
    _progress(f"Applying; every batch is recorded in {args.ids_file} before it is written ...")
    moved, skipped, touched = apply_moves(run_sql, plan, ids_file, args)
    print(f"Moved {moved:,} observations ({skipped:,} left in place: their target already holds "
          f"the same reading); refreshed times of {len(touched)} datastreams. Record: {args.ids_file}")

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
    except (PsqlError, IdsFileError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        note = (" Every batch sent is in the IDs file. Re-run, or run --refresh-only: both first refresh the times of "
                "every datastream the IDs file names.") if (args.apply or args.rollback) else ""
        print(f"Interrupted.{note}", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
