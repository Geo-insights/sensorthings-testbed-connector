#!/usr/bin/env python
"""Move KNMI observations in FROST to their own station's datastream; retire the stale ground temperature ones.

Until fix/knmi-station-ids the KNMI parser named each station after the position
of its Coverage in the EDR /area response (sensor ids knmi-0 ... knmi-56). The
positions shift when stations start or stop reporting, so from 2026-11-12 on many
readings went into the datastreams of the station that held their position
before: on frost.geo-insights.nl about 200,000 KNMI rows sit in another station's
datastream, and only a handful of them have a copy in their own. They have to be
moved, not deleted. Every KNMI Thing also has a stale '<sensor> - Ground
temperature' datastream (ObservedProperty 'Ground temperature', from before the
canonical rename) next to the current '<sensor> - Ground surface temperature'
one; both carry properties.observed_property 'ground_temperature'.

The station of a reading is its PARAMETERS.thing_name ('KNMI KNMI-<lat>-<lon>',
built by the connector from the Coverage's own coordinates). PARAMETERS.sensor_id
is the shifting position and is never used.

Plan (dry run, the default)
---------------------------
Every row of a datastream of a Thing named '<prefix>KNMI %' (source knmi_weather)
is read once, one read-only statement per datastream. A row labelled with a KNMI
thing_name belongs in the datastream of the Thing '<prefix>' + thing_name whose
canonical observed property (properties.observed_property) is the row's
observed_property. Where a Thing has several such datastreams, the one whose
ObservedProperty carries the canonical display name wins ('Ground surface
temperature'); the others are stale. Categories:

  in place    already in that datastream
  move        in another station's datastream, or in a stale datastream (of its
              own Thing or another one)
  twin        a move whose target already holds the same reading: the same
              thing_name and observed_property labels, phenomenon time and result.
              Never moved or deleted here; --twins-file lists them in the format
              scripts/cleanup_frost_observations.py --ids-file reads
              (observation_id,datastream_id)
  no target   the reading's Thing or datastream is missing or ambiguous, or the
              target Thing has no single Location with a generated FoI: reported
Rows of other sources in KNMI datastreams (TGV, Sensor.Community) are counted and
left alone: scripts/cleanup_frost_observations.py --tgv-misplaced and
scripts/fix_sc_misrouted_observations.py deal with them.

A move is never skipped because the target holds another row at that time:
stations swapped readings, so moves form permutations.

The report gives the counts per category, per station (the reading's) and per
target datastream, and for each stale datastream whether the moves leave it empty.
--plan-file writes every group as CSV.

Apply (--apply --ids-file F)
----------------------------
Per session of --chunk source datastreams, the rows to move are read (read-only,
at most --batch-rows per datastream and round), appended to F with their datastream
and FEATURE_ID before and after, and F is fsynced; only then is the write session
sent. Each UPDATE (its own transaction) moves only listed rows that are still in
their source datastream with the recorded FEATURE_ID and whose target does not hold
their twin by then. A move to another Thing sets FEATURE_ID to that Thing's
Location's generated FoI (LOCATIONS.GEN_FOI_ID), as if the reading had been posted
there; a move within one Thing (stale to current ground temperature) keeps it.

FROST's UPDATE trigger ignores a changed DATASTREAM_ID, so afterwards (in a finally
block: also after an error or Ctrl-C) the script recomputes phenomenonTime,
resultTime, observedArea and LAST_FOI_ID of every datastream it touched. Every
--apply and --rollback also starts by recomputing them for every datastream named
in F, which repairs what an interrupted earlier run (killed before its finally)
left. A read-only verification ends the run; it exits 4 when planned moves remain.

F's first line is its header; a run refuses a file with another header, and drops
a cut-off last line (being appended when a run was interrupted: no UPDATE was sent
for it). Keep F: it is the way back.

Rollback (--rollback --ids-file F)
----------------------------------
A dry run unless --apply. Moves each recorded row that is still exactly where the
script put it (target datastream, target FEATURE_ID) back to its recorded datastream
and FEATURE_ID, honours --datastream (source or target), recomputes the datastreams
involved and verifies. A row goes back only while its recorded datastream and
FeatureOfInterest still exist (OBSERVATIONS has foreign keys to both, so one such
row would fail its whole batch): the report counts the others as "source gone",
and they stay where the move put them.

Retiring the stale datastreams (--retire-stale-ground-temperature)
------------------------------------------------------------------
A separate, explicit step, through the FROST HTTP API (never SQL), with the
target URL and credentials handling of scripts/migrate_knmi_sensor_ids.py
(SENSORTHINGS_BASE_URL(S) and SENSORTHINGS_AUTH_*; --target-url picks one; nothing
is printed). A dry run unless --apply. A stale datastream is '<Thing> sensor -
Ground temperature' with observed_property ground_temperature on a KNMI Thing that
also has a '... - Ground surface temperature' one. Deleting a Datastream in FROST
deletes its observations, so each is checked empty ($count) right before its
DELETE, and a datastream with observations is never deleted.

Retirement is the point of no return for rolling back the moves out of those
datastreams: a row can only go back to a datastream that exists. With --ids-file
(the record of --apply) an empty stale datastream that the file names as the
source of a move is left alone unless --force-retire; the dry run lists them.
Without --ids-file nothing is checked, so always give it.

Safety
------
Every session sets statement_timeout 50 s, lock_timeout 10 s and
idle_in_transaction_session_timeout 60 s (the database stops statements at 60 s).
Reads run in read-only sessions. A write refuses to start while a write session
of fix_sc_pressure_units, fix_sc_misrouted_observations,
cleanup_frost_observations or this script is on the server, and write sessions of
this script take an advisory lock. The transport is the one of
scripts/fix_sc_pressure_units.py: psql inside the FROST database container, over
ssh with keepalives (--ssh) or docker exec (--container); no credential is
handled or printed.

Runbook (production)
--------------------
1. scripts/migrate_knmi_sensor_ids.py --apply, then deploy the fixed connector:
   no new row lands in another station's datastream from then on. Check that the
   connector writes ground temperature to the 'Ground surface temperature'
   datastreams (the stale ones get no new rows).
2. Dry run: python scripts/fix_knmi_observations.py --ssh root@<frost-host>
   --ssh-key ~/.ssh/id_hetzner --plan-file knmi_plan.csv --twins-file knmi_twins.csv
3. Canary: --apply --ids-file knmi_moves.csv --datastream <ID>; check that
   datastream and the ones it exchanged rows with in the FROST API.
4. Everything: --apply --ids-file knmi_moves.csv (the same file).
5. scripts/cleanup_frost_observations.py --ids-file knmi_twins.csv (archives, then
   deletes the twins) and --tgv-misplaced (the TGV rows in KNMI datastreams).
6. Only once the moves will not be rolled back: --retire-stale-ground-temperature
   --ids-file knmi_moves.csv (dry run: the stale datastreams emptied by the moves
   are held back), then --apply --ids-file knmi_moves.csv --force-retire. This is
   the point of no return for the moves out of them; the other moves can still be
   rolled back (their rows are counted, not restored, as "source gone").
If a run is interrupted, run the same command again. To undo the moves (before
step 6): --rollback --ids-file knmi_moves.csv, then the same with --apply.

Usage
-----
    python scripts/fix_knmi_observations.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner [--twins-file T]
    python scripts/fix_knmi_observations.py --ssh ... --apply --ids-file knmi_moves.csv [--datastream ID]
    python scripts/fix_knmi_observations.py --ssh ... --rollback --ids-file knmi_moves.csv [--apply]
    python scripts/fix_knmi_observations.py --retire-stale-ground-temperature --ids-file knmi_moves.csv
        [--apply [--force-retire]] [--target-url URL]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import requests

# Run as `python scripts/fix_knmi_observations.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.sta.canonical import resolve
from scripts import fix_sc_misrouted_observations as misrouted
from scripts import fix_sc_pressure_units as pressure
from scripts import migrate_knmi_sensor_ids as migrate

RunSql = Callable[[str], list[str]]
PsqlError = pressure.PsqlError

APP_NAME = "fix_knmi_observations"
SOURCE = "knmi_weather"
LABEL_LIKE = "KNMI %"  # PARAMETERS.thing_name of a KNMI reading
STALE_SUFFIX = " - Ground temperature"
CURRENT_SUFFIX = " - Ground surface temperature"
GROUND_TEMPERATURE = "ground_temperature"

IN_PLACE = "in place"
MOVE = "move"
TWIN = "twin"
NO_TARGET = "no target"
CATEGORIES = (IN_PLACE, MOVE, TWIN, NO_TARGET)

EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_VERIFY, EXIT_INTERRUPTED = 0, 1, 3, 4, 130


def _app_name_of(session: str, default: str) -> str:
    match = re.search(r"application_name = '([^']+)'", session)
    return match[1] if match else default


# Write sessions of the repair scripts that change OBSERVATIONS through psql. The
# cleanup script lives on another branch; its name is pinned by a test.
CLEANUP_APP_NAME = "cleanup_frost_observations"
WRITERS = (
    APP_NAME,
    pressure.WRITE_APP_NAME,
    _app_name_of(misrouted.WRITE_SESSION, "fix_sc_misrouted_observations"),
    CLEANUP_APP_NAME,
)

_SETTINGS = (
    "".join(f"SET {name} = '{value}';\n" for name, value in pressure.SESSION_TIMEOUTS.items())
    + "SET extra_float_digits = 3;\n"
    + "SET TimeZone = 'UTC';\n"
    + "SET standard_conforming_strings = on;\n"
    + "SET client_encoding = 'UTF8';\n"
)
READ_ONLY_SESSION = (
    "SET default_transaction_read_only = on;\n" + f"SET application_name = '{APP_NAME} (read-only)';\n" + _SETTINGS
)
WRITE_SESSION = (
    f"SET application_name = '{APP_NAME}';\n"
    + _SETTINGS
    + "DO $lock$BEGIN\n"
    + f"  IF NOT pg_try_advisory_lock(hashtext('{APP_NAME}')) THEN\n"
    + f"    RAISE EXCEPTION 'another write session of {APP_NAME} is running';\n"
    + "  END IF;\n"
    + "END$lock$;\n"
)

lit = pressure.sql_literal


def like_prefix(text: str) -> str:
    """``text`` as a LIKE prefix: its %, _ and backslashes literal."""
    return re.sub(r"([\\%_])", r"\\\1", text)


def canonical(name: str | None) -> str:
    return misrouted.canonical(name)


def display_name(prop: str) -> str | None:
    member = resolve(prop) if prop else None
    return member.meta.display_name if member is not None else None


def _chunks[T](items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _progress(message: str) -> None:
    sys.stdout.flush()
    print(message, file=sys.stderr, flush=True)


def json_rows(lines: Iterable[str], tag: str, *, salvage: bool = False) -> list[dict[str, Any]]:
    """The JSON objects tagged ``tag``; a line cut off mid-way never parses."""
    rows = []
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            if salvage:
                continue
            raise
        if isinstance(obj, dict) and obj.get("t") == tag:
            rows.append(obj)
    return rows


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


# ---------------------------------------------------------------------------
# Catalogue and targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Datastream:
    id: int
    name: str
    thing_id: int
    thing: str
    source: str
    prop: str  # canonical observed property of properties.observed_property (or streamKey)
    op_name: str  # its ObservedProperty's name


@dataclass(frozen=True)
class Target:
    datastream: Datastream | None
    reason: str = ""  # why there is none


@dataclass
class Catalogue:
    prefix: str
    datastreams: dict[int, Datastream]  # every datastream of a '<prefix>KNMI %' Thing
    fois: dict[int, int | None]  # Thing id -> GEN_FOI_ID of its single Location
    targets: dict[tuple[str, str], Target] = field(default_factory=dict)  # (Thing name, prop)
    stale: set[int] = field(default_factory=set)
    things: dict[str, int] = field(default_factory=dict)  # name -> id, knmi_weather Things only

    @property
    def knmi(self) -> list[int]:
        """The datastreams to scan: those of knmi_weather Things."""
        return sorted(d.id for d in self.datastreams.values() if d.source == SOURCE)


def catalogue_sql(prefix: str) -> str:
    like = lit(like_prefix(f"{prefix}KNMI ") + "%")
    return f"""
SELECT json_build_object('t', 'ds', 'id', d."ID", 'name', d."NAME", 'thing_id', t."ID", 'thing', t."NAME",
                         'source', t."PROPERTIES"->>'source', 'prop', d."PROPERTIES"->>'observed_property',
                         'stream_key', d."PROPERTIES"->>'streamKey', 'op_name', op."NAME")::text
  FROM "DATASTREAMS" d
  JOIN "THINGS" t ON t."ID" = d."THING_ID"
  JOIN "OBS_PROPERTIES" op ON op."ID" = d."OBS_PROPERTY_ID"
 WHERE t."NAME" LIKE {like}
 ORDER BY d."ID";
SELECT json_build_object('t', 'foi', 'thing_id', tl."THING_ID", 'locations', count(*),
                         'foi', min(l."GEN_FOI_ID"), 'fois', count(DISTINCT l."GEN_FOI_ID"))::text
  FROM "THINGS_LOCATIONS" tl
  JOIN "LOCATIONS" l ON l."ID" = tl."LOCATION_ID"
  JOIN "THINGS" t ON t."ID" = tl."THING_ID"
 WHERE t."NAME" LIKE {like}
 GROUP BY tl."THING_ID";
"""


def parse_catalogue(lines: Sequence[str], prefix: str) -> Catalogue:
    datastreams = {}
    for row in json_rows(lines, "ds"):
        datastreams[int(row["id"])] = Datastream(
            id=int(row["id"]),
            name=str(row.get("name") or ""),
            thing_id=int(row["thing_id"]),
            thing=str(row.get("thing") or ""),
            source=str(row.get("source") or ""),
            prop=canonical(row.get("prop") or row.get("stream_key")),
            op_name=str(row.get("op_name") or ""),
        )
    fois: dict[int, int | None] = {}
    for row in json_rows(lines, "foi"):
        single = int(row["locations"]) == 1 and int(row["fois"]) == 1 and row.get("foi") is not None
        fois[int(row["thing_id"])] = int(row["foi"]) if single else None
    catalogue = Catalogue(prefix, datastreams, fois)
    resolve_targets(catalogue)
    return catalogue


def resolve_targets(catalogue: Catalogue) -> None:
    """Fill the target per (Thing, property) and the set of stale datastreams."""
    by_key: dict[tuple[str, str], list[Datastream]] = defaultdict(list)
    for ds in catalogue.datastreams.values():
        if ds.source != SOURCE:
            continue
        catalogue.things[ds.thing] = ds.thing_id
        if ds.prop:
            by_key[(ds.thing, ds.prop)].append(ds)
    for key, candidates in by_key.items():
        if len(candidates) == 1:
            catalogue.targets[key] = Target(candidates[0])
            continue
        wanted = display_name(key[1])
        preferred = [d for d in candidates if wanted is not None and d.op_name == wanted]
        if len(preferred) == 1:
            catalogue.targets[key] = Target(preferred[0])
            catalogue.stale.update(d.id for d in candidates if d.id != preferred[0].id)
        else:
            ids = ",".join(str(d.id) for d in sorted(candidates, key=lambda d: d.id))
            catalogue.targets[key] = Target(None, f"several datastreams of {key[1]}: {ids}")


# ---------------------------------------------------------------------------
# Scan and plan
# ---------------------------------------------------------------------------


@dataclass
class Group:
    """Rows of one datastream with one label: a KNMI station and property, or another source."""

    ds: int
    thing: str | None  # PARAMETERS.thing_name of a KNMI reading, else None
    prop: str | None  # its PARAMETERS.observed_property
    other: str | None  # for rows that are not KNMI readings: their source (sensor_id prefix)
    rows: int = 0
    first: str = ""
    last: str = ""

    @property
    def key(self) -> tuple[int, str | None, str | None, str | None]:
        return (self.ds, self.thing, self.prop, self.other)


def scan_sql(ds_id: int) -> str:
    """Every row of one datastream, grouped by label (read-only, one statement)."""
    knmi = f"""o."PARAMETERS"->>'thing_name' LIKE {lit(LABEL_LIKE)}"""
    return f"""
SELECT json_build_object('t', 'grp', 'ds', {int(ds_id)}, 'thing', g.thing, 'prop', g.prop, 'other', g.other,
                         'n', g.n, 'first', g.first, 'last', g.last)::text
  FROM (SELECT CASE WHEN {knmi} THEN o."PARAMETERS"->>'thing_name' END AS thing,
               CASE WHEN {knmi} THEN o."PARAMETERS"->>'observed_property' END AS prop,
               CASE WHEN {knmi} THEN NULL
                    ELSE coalesce(nullif(split_part(o."PARAMETERS"->>'sensor_id', '-', 1), ''), '(no label)') END AS other,
               count(*) AS n, min(o."PHENOMENON_TIME_START") AS first, max(o."PHENOMENON_TIME_START") AS last
          FROM "OBSERVATIONS" o
         WHERE o."DATASTREAM_ID" = {int(ds_id)}
         GROUP BY 1, 2, 3) g;
SELECT json_build_object('t', 'scanned', 'ds', {int(ds_id)})::text;
"""


def parse_groups(lines: Sequence[str]) -> tuple[list[Group], set[int]]:
    groups = [
        Group(
            ds=int(row["ds"]),
            thing=row.get("thing"),
            prop=row.get("prop"),
            other=row.get("other"),
            rows=int(row["n"]),
            first=str(row.get("first") or ""),
            last=str(row.get("last") or ""),
        )
        for row in json_rows(lines, "grp")
    ]
    return groups, {int(row["ds"]) for row in json_rows(lines, "scanned")}


@dataclass
class Planned:
    group: Group
    category: str  # IN_PLACE, MOVE or NO_TARGET; twins are counted per row in `twins`
    target: Datastream | None = None
    foi: int | None = None  # FEATURE_ID to set; None keeps the row's own (a move within one Thing)
    reason: str = ""
    twins: int = 0

    @property
    def moves(self) -> int:
        return self.group.rows - self.twins if self.category == MOVE else 0


@dataclass
class Plan:
    groups: list[Planned] = field(default_factory=list)  # KNMI readings
    others: list[Group] = field(default_factory=list)  # rows of other sources, left alone
    twin_ids: dict[int, int] = field(default_factory=dict)  # observation -> its datastream

    def rows(self, category: str) -> int:
        if category == TWIN:
            return sum(p.twins for p in self.groups if p.category == MOVE)
        if category == MOVE:
            return sum(p.moves for p in self.groups)
        return sum(p.group.rows for p in self.groups if p.category == category)

    def move_groups(self) -> list[Planned]:
        return [p for p in self.groups if p.category == MOVE]

    def sources(self) -> list[int]:
        return sorted({p.group.ds for p in self.move_groups()})


def plan_group(group: Group, catalogue: Catalogue) -> Planned:
    thing = f"{catalogue.prefix}{group.thing}"
    prop = canonical(group.prop)
    source = catalogue.datastreams[group.ds]
    if not prop:
        return Planned(group, NO_TARGET, reason="no observed_property label")
    if thing not in catalogue.things:
        return Planned(group, NO_TARGET, reason=f"no KNMI Thing {thing!r}")
    choice = catalogue.targets.get((thing, prop))
    if choice is None:
        return Planned(group, NO_TARGET, reason=f"Thing {thing!r} has no datastream of {prop}")
    target = choice.datastream
    if target is None:
        return Planned(group, NO_TARGET, reason=choice.reason)
    if target.id == group.ds:
        return Planned(group, IN_PLACE, target)
    if target.thing_id == source.thing_id:
        return Planned(group, MOVE, target, foi=None)
    foi = catalogue.fois.get(target.thing_id)
    if foi is None:
        return Planned(group, NO_TARGET, target, reason="target Thing has no single Location with a generated FoI")
    return Planned(group, MOVE, target, foi=foi)


def build_plan(groups: Iterable[Group], catalogue: Catalogue, only: set[int] | None = None) -> Plan:
    """The plan; with ``only``, limited to groups whose source or target datastream is in it."""
    plan = Plan()
    for group in groups:
        if group.thing is None:
            if not only or group.ds in only:
                plan.others.append(group)
            continue
        planned = plan_group(group, catalogue)
        target = planned.target.id if planned.target else None
        if only and group.ds not in only and target not in only:
            continue
        plan.groups.append(planned)
    return plan


def _move_values(groups: Sequence[Planned], with_foi: bool) -> str:
    """The (thing, prop, to_ds[, foi]) relation of a source datastream's move groups."""
    rows = []
    for p in groups:
        assert p.target is not None and p.group.thing is not None and p.group.prop is not None
        row = f"({lit(p.group.thing)}::text, {lit(p.group.prop)}::text, {int(p.target.id)}::bigint"
        if with_foi:
            row += ", " + ("NULL" if p.foi is None else str(int(p.foi))) + "::bigint"
        rows.append(row + ")")
    return ", ".join(rows)


def twin_exists(row: str, to_ds: str) -> str:
    """SQL: the target ``to_ds`` holds the same reading as ``row`` (another row of it)."""
    same = " AND ".join(
        f'x."{col}" IS NOT DISTINCT FROM {row}."{col}"'
        for col in ("RESULT_TYPE", "RESULT_NUMBER", "RESULT_STRING", "RESULT_BOOLEAN", "RESULT_JSON")
    )
    return f"""EXISTS (SELECT 1 FROM "OBSERVATIONS" x
                WHERE x."DATASTREAM_ID" = {to_ds}
                  AND x."PHENOMENON_TIME_START" = {row}."PHENOMENON_TIME_START"
                  AND x."ID" <> {row}."ID"
                  AND x."PARAMETERS"->>'thing_name' = {row}."PARAMETERS"->>'thing_name'
                  AND x."PARAMETERS"->>'observed_property' = {row}."PARAMETERS"->>'observed_property'
                  AND {same})"""


_LABEL_JOIN = """o."PARAMETERS"->>'thing_name' = m.thing AND o."PARAMETERS"->>'observed_property' = m.prop"""


def twins_sql(ds_id: int, groups: Sequence[Planned]) -> str:
    """The rows of one source datastream's move groups whose target holds their twin (read-only)."""
    return f"""
WITH m(thing, prop, to_ds) AS (VALUES {_move_values(groups, False)})
SELECT json_build_object('t', 'twin', 'id', o."ID", 'ds', o."DATASTREAM_ID", 'thing', m.thing, 'prop', m.prop)::text
  FROM "OBSERVATIONS" o JOIN m ON {_LABEL_JOIN}
 WHERE o."DATASTREAM_ID" = {int(ds_id)} AND {twin_exists("o", "m.to_ds")};
SELECT json_build_object('t', 'checked', 'ds', {int(ds_id)})::text;
"""


def scan(run_sql: RunSql, ds_ids: Sequence[int], chunk: int) -> list[Group]:
    groups: list[Group] = []
    done: set[int] = set()
    for part in _chunks(ds_ids, chunk):
        found, scanned = parse_groups(run_sql(READ_ONLY_SESSION + "".join(scan_sql(i) for i in part)))
        groups += found
        done |= scanned
        _progress(f"  scanned {len(done)}/{len(ds_ids)} datastreams")
    missing = set(ds_ids) - done
    if missing:
        raise PsqlError(f"no scan result for datastreams {sorted(missing)[:20]}")
    return groups


def find_twins(run_sql: RunSql, plan: Plan, chunk: int) -> None:
    """Count the twins of every move group and collect their ids (read-only)."""
    by_ds: dict[int, list[Planned]] = defaultdict(list)
    for p in plan.move_groups():
        by_ds[p.group.ds].append(p)
    index = {(p.group.ds, p.group.thing, p.group.prop): p for p in plan.move_groups()}
    sources = sorted(by_ds)
    checked: set[int] = set()
    for part in _chunks(sources, chunk):
        lines = run_sql(READ_ONLY_SESSION + "".join(twins_sql(ds, by_ds[ds]) for ds in part))
        checked |= {int(row["ds"]) for row in json_rows(lines, "checked")}
        for row in json_rows(lines, "twin"):
            planned = index[(int(row["ds"]), row["thing"], row["prop"])]
            planned.twins += 1
            plan.twin_ids[int(row["id"])] = int(row["ds"])
        _progress(f"  checked {len(checked)}/{len(sources)} source datastreams for twins")
    if set(sources) - checked:
        raise PsqlError(f"no twin check result for datastreams {sorted(set(sources) - checked)[:20]}")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _line(label: str, count: int, width: int = 52) -> None:
    print(f"  {label:<{width}}{count:>12,}")


def print_report(plan: Plan, catalogue: Catalogue, top: int) -> None:
    print("\nKNMI readings in KNMI datastreams, by category")
    for category in CATEGORIES:
        _line(category, plan.rows(category))
    _line("total", sum(p.group.rows for p in plan.groups))
    cross = sum(p.moves for p in plan.groups if p.category == MOVE and p.foi is not None)
    same = plan.rows(MOVE) - cross
    _line("  moves to another Thing (FEATURE_ID set)", cross)
    _line("  moves within one Thing (FEATURE_ID kept)", same)
    _line("  moves out of stale datastreams", sum(p.moves for p in plan.groups if p.group.ds in catalogue.stale))
    moving = [p for p in plan.groups if p.moves]
    if moving:
        print(f"    phenomenon times of the groups to move: {min(p.group.first for p in moving)} .. "
              f"{max(p.group.last for p in moving)}")
    reasons = Counter[str]()
    for p in plan.groups:
        if p.category == NO_TARGET:
            reasons[p.reason] += p.group.rows
    for reason, rows in reasons.most_common():
        print(f"    no target: {reason}: {rows:,}")

    others = Counter[str]()
    for g in plan.others:
        others[g.other or "?"] += g.rows
    print("\nRows of other sources in KNMI datastreams (left alone)")
    for source, rows in others.most_common():
        _line(source, rows)
    _line("total", sum(others.values()))

    stations: dict[str, Counter[str]] = defaultdict(Counter)
    for p in plan.groups:
        counts = stations[p.group.thing or "?"]
        counts[p.category] += p.group.rows - p.twins
        counts[TWIN] += p.twins
    print("\nPer station (the reading's thing_name)")
    print(f"  {'station':<28}" + "".join(f"{c:>11}" for c in CATEGORIES))
    for station in sorted(stations):
        counts = stations[station]
        print(f"  {station:<28}" + "".join(f"{counts[c]:>11,}" for c in CATEGORIES))

    targets = Counter[int]()
    sources = Counter[int]()
    for p in plan.groups:
        if p.moves and p.target is not None:
            targets[p.target.id] += p.moves
            sources[p.group.ds] += p.moves
    print(f"\nMoves per target datastream: {len(targets)} targets from {len(sources)} source datastreams"
          f" (top {top}; --plan-file lists all)")
    for ds_id, rows in targets.most_common(top):
        print(f"  {ds_id:>8} {rows:>10,}  {catalogue.datastreams[ds_id].name}")


def stale_forecast(plan: Plan, catalogue: Catalogue) -> list[tuple[int, int, int]]:
    """(stale datastream, rows now, rows left after the moves) per stale datastream."""
    now: Counter[int] = Counter()
    left: Counter[int] = Counter()
    for p in plan.groups:
        now[p.group.ds] += p.group.rows
        left[p.group.ds] += p.group.rows - p.moves
    for g in plan.others:
        now[g.ds] += g.rows
        left[g.ds] += g.rows
    return [(ds_id, now[ds_id], left[ds_id]) for ds_id in sorted(catalogue.stale)]


def print_stale(plan: Plan, catalogue: Catalogue) -> None:
    forecast = stale_forecast(plan, catalogue)
    empty = [f for f in forecast if f[2] == 0]
    print(f"\nStale datastreams: {len(forecast)}; empty after the moves: {len(empty)} "
          f"(already empty: {sum(1 for f in forecast if f[1] == 0)})")
    for ds_id, rows, left in forecast:
        if left:
            print(f"  {ds_id:>8} keeps {left:,} of {rows:,} rows  {catalogue.datastreams[ds_id].name}")


def write_plan_csv(path: Path, plan: Plan, catalogue: Catalogue) -> None:
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(["category", "source_datastream", "source_name", "label_thing_name",
                         "label_observed_property", "other_source", "rows", "moves", "twins", "first", "last",
                         "target_datastream", "target_name", "feature_id", "note"])
        for p in sorted(plan.groups, key=lambda p: (CATEGORIES.index(p.category), p.group.ds, p.group.thing or "")):
            g, t = p.group, p.target
            writer.writerow([p.category, g.ds, catalogue.datastreams[g.ds].name, g.thing, g.prop, "", g.rows,
                             p.moves, p.twins, g.first, g.last, t.id if t else "", t.name if t else "",
                             "" if p.foi is None else p.foi, p.reason])
        for g in plan.others:
            writer.writerow(["other source", g.ds, catalogue.datastreams[g.ds].name, "", "", g.other, g.rows,
                             0, 0, g.first, g.last, "", "", "", "left alone"])


TWINS_HEADER = "observation_id,datastream_id"


def write_twins_file(path: Path, twins: dict[int, int]) -> None:
    """observation_id,datastream_id per twin: what cleanup_frost_observations.py --ids-file reads."""
    with path.open("w", encoding="utf-8", newline="\n") as out:
        out.write(TWINS_HEADER + "\n")
        out.writelines(f"{obs_id},{ds_id}\n" for obs_id, ds_id in sorted(twins.items()))
        out.flush()
        os.fsync(out.fileno())


# ---------------------------------------------------------------------------
# The IDs file
# ---------------------------------------------------------------------------

# Deliberately no 'observation_id' column: cleanup_frost_observations.py --ids-file refuses this file.
IDS_HEADER = "moved_observation_id,from_datastream_id,to_datastream_id,from_feature_id,to_feature_id"


class IdsFileError(RuntimeError):
    pass


class Move(NamedTuple):
    observation_id: int
    from_ds: int
    to_ds: int
    from_foi: int
    to_foi: int


_MOVE_LINE = re.compile(r"([0-9]+),([0-9]+),([0-9]+),([0-9]+),([0-9]+)")


def format_move(move: Move) -> str:
    return ",".join(str(int(v)) for v in move) + "\n"


def parse_move(line: str) -> Move:
    match = _MOVE_LINE.fullmatch(line)
    if not match:
        raise ValueError(f"not a record: {line[:120]!r}")
    return Move(*(int(v) for v in match.groups()))


def prepare_ids_file(path: Path) -> None:
    """Create the IDs file with its header, or check an existing one and drop a cut-off last line."""
    if not path.exists() or path.stat().st_size == 0:
        with path.open("w", encoding="utf-8", newline="\n") as out:
            out.write(IDS_HEADER + "\n")
            out.flush()
            os.fsync(out.fileno())
        return
    with path.open("rb+") as handle:
        header = handle.readline().decode("utf-8", "replace").rstrip("\r\n")
        if header != IDS_HEADER:
            raise IdsFileError(f"{path} does not start with {IDS_HEADER!r}: not an IDs file of this script. "
                               "Use a new file.")
        body = handle.tell()
        size = handle.seek(0, os.SEEK_END)
        if body == size:
            if size == len(IDS_HEADER):  # the header without its line end
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            return
        start = max(body, size - 4096)
        handle.seek(start)
        tail = handle.read()
        if tail.endswith(b"\n"):
            return
        cut = tail.rfind(b"\n")
        if cut < 0 and start > body:
            raise IdsFileError(f"{path}: its last line is cut off and longer than 4 KiB; repair it by hand")
        _progress(f"  WARNING: {path}: dropping the cut-off last line {tail[cut + 1 :][:80]!r} "
                  "(written by an interrupted run; no UPDATE was sent for it)")
        handle.truncate(start + cut + 1)
        handle.flush()
        os.fsync(handle.fileno())


def append_moves(path: Path, moves: Sequence[Move]) -> None:
    """Append and fsync: on disk before the UPDATE that may move these rows is sent."""
    with path.open("a", encoding="utf-8", newline="\n") as out:
        out.writelines(format_move(m) for m in moves)
        out.flush()
        os.fsync(out.fileno())


def read_ids_file(path: Path) -> list[Move]:
    moves = []
    with path.open(encoding="utf-8", newline="") as handle:
        if handle.readline().rstrip("\r\n") != IDS_HEADER:
            raise IdsFileError(f"{path} does not start with {IDS_HEADER!r}: not an IDs file of this script")
        for number, line in enumerate(handle, start=2):
            if not line.endswith("\n"):
                _progress(f"  WARNING: {path}, line {number}: cut-off last line skipped "
                          "(written by an interrupted run; no UPDATE was sent for it)")
                break
            body = line.rstrip("\n").removesuffix("\r")
            if not body:
                continue
            try:
                moves.append(parse_move(body))
            except ValueError as exc:
                raise IdsFileError(f"{path}, line {number}: {exc}") from exc
    return moves


def distinct_moves(moves: Iterable[Move], only: set[int] | None = None) -> list[Move]:
    """One record per observation (a re-run records a row again, identically), sorted by ID."""
    seen: dict[int, Move] = {}
    conflicts = set()
    for move in moves:
        if only and move.from_ds not in only and move.to_ds not in only:
            continue
        if seen.setdefault(move.observation_id, move) != move:
            conflicts.add(move.observation_id)
    if conflicts:
        raise IdsFileError(f"{len(conflicts):,} observations are recorded with different moves, e.g. "
                           f"{sorted(conflicts)[:10]}; resolve that by hand first")
    return [seen[k] for k in sorted(seen)]


def datastreams_in(moves: Iterable[Move]) -> set[int]:
    return {ds for m in moves for ds in (m.from_ds, m.to_ds)}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def busy_sql() -> str:
    """Write sessions of the repair scripts on the server, other than the asking one (read-only)."""
    names = ", ".join(lit(n) for n in WRITERS)
    return f"""
SELECT json_build_object('t', 'busy', 'pid', pid, 'app', application_name, 'state', state,
                         'running', coalesce(to_char(now() - query_start, 'HH24:MI:SS'), ''))::text
  FROM pg_stat_activity
 WHERE application_name IN ({names}) AND pid <> pg_backend_pid();
"""


def refuse_if_busy(run_sql: RunSql) -> bool:
    busy = json_rows(run_sql(READ_ONLY_SESSION + busy_sql()), "busy")
    if not busy:
        return False
    print(f"Refusing to write: {len(busy)} write session(s) of a repair script on the server:")
    for row in busy:
        print(f"  pid {row['pid']}: {row['app']}, {row['state']}, statement running for {row['running'] or '?'}")
    print("Wait for them to end (each statement stops within the statement timeout), or end one left by an "
          "interrupted run with SELECT pg_terminate_backend(<pid>), then re-run.")
    return True


def area_sql(ds_id: int) -> str:
    """Recompute a datastream's observedArea and LAST_FOI_ID from its rows, as FROST's insert trigger builds them:
    the convex hull of its FeaturesOfInterest, and the FoI of its last row."""
    return f"""
UPDATE "DATASTREAMS" d
   SET "OBSERVED_AREA" = (SELECT ST_ConvexHull(ST_Collect(f."GEOM")) FROM "FEATURES" f
                           WHERE f."ID" IN (SELECT DISTINCT "FEATURE_ID" FROM "OBSERVATIONS"
                                             WHERE "DATASTREAM_ID" = {int(ds_id)})),
       "LAST_FOI_ID" = (SELECT "FEATURE_ID" FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" = {int(ds_id)}
                         ORDER BY "ID" DESC LIMIT 1)
 WHERE d."ID" = {int(ds_id)};
"""


def refresh(run_sql: RunSql, ds_ids: Iterable[int], chunk: int) -> int:
    """Recompute times, observedArea and LAST_FOI_ID of these datastreams; one statement each."""
    ids = sorted(set(ds_ids))
    for part in _chunks(ids, chunk):
        sql = WRITE_SESSION + "".join(
            misrouted.refresh_sql(i) + area_sql(i) + f"SELECT json_build_object('t', 'refreshed', 'ds', {int(i)})::text;\n"
            for i in part
        )
        lines = run_sql(sql)
        missing = set(part) - {int(r["ds"]) for r in json_rows(lines, "refreshed")}
        if missing:
            raise PsqlError(f"refresh did not report datastreams {sorted(missing)}")
    if ids:
        _progress(f"  refreshed {len(ids)} datastreams")
    return len(ids)


def select_sql(ds_id: int, groups: Sequence[Planned], after_id: int, limit: int) -> str:
    """The next rows to move of one source datastream, with their twin flag (read-only)."""
    return f"""
WITH m(thing, prop, to_ds, foi) AS (VALUES {_move_values(groups, True)})
SELECT json_build_object('t', 'row', 'ds', {int(ds_id)}, 'id', o."ID", 'to', m.to_ds, 'from_foi', o."FEATURE_ID",
                         'to_foi', coalesce(m.foi, o."FEATURE_ID"), 'twin', {twin_exists("o", "m.to_ds")})::text
  FROM "OBSERVATIONS" o JOIN m ON {_LABEL_JOIN}
 WHERE o."DATASTREAM_ID" = {int(ds_id)} AND o."ID" > {int(after_id)}
 ORDER BY o."ID"
 LIMIT {int(limit)};
SELECT json_build_object('t', 'selected', 'ds', {int(ds_id)})::text;
"""


def move_sql(ds_id: int, moves: Sequence[Move]) -> str:
    """Move the listed rows still in ``ds_id`` with their recorded FEATURE_ID, unless their target holds their twin."""
    values = ", ".join(f"({m.observation_id}::bigint, {m.to_ds}::bigint, {m.from_foi}::bigint, {m.to_foi}::bigint)"
                       for m in moves)
    return f"""
WITH v(id, to_ds, from_foi, to_foi) AS (VALUES {values}),
moved AS (
    UPDATE "OBSERVATIONS" o
       SET "DATASTREAM_ID" = v.to_ds, "FEATURE_ID" = v.to_foi
      FROM v
     WHERE o."ID" = v.id AND o."DATASTREAM_ID" = {int(ds_id)} AND o."FEATURE_ID" = v.from_foi
       AND NOT {twin_exists("o", "v.to_ds")}
    RETURNING o."ID"
)
SELECT json_build_object('t', 'moved', 'ds', {int(ds_id)}, 'listed', {len(moves)}, 'n', count(*))::text FROM moved;
"""


def _counted(lines: Sequence[str], tag: str, *, salvage: bool = False) -> int:
    total = 0
    for row in json_rows(lines, tag, salvage=salvage):
        if int(row["n"]) > int(row["listed"]):
            raise ValueError(f"{row['n']} rows {tag} of {row['listed']} listed")
        total += int(row["n"])
    return total


def apply_moves(run_sql: RunSql, plan: Plan, ids_file: Path, args: argparse.Namespace,
                touched: set[int]) -> tuple[int, int]:
    """Move the plan's rows. Returns (rows recorded, rows moved); ``touched`` collects the datastreams written to."""
    by_ds: dict[int, list[Planned]] = defaultdict(list)
    for p in plan.move_groups():
        by_ds[p.group.ds].append(p)
    cursor = dict.fromkeys(sorted(by_ds), 0)  # source datastream -> last ID read
    recorded = moved = 0
    while cursor:
        part = list(cursor)[: args.chunk]
        lines = run_sql(READ_ONLY_SESSION + "".join(
            select_sql(ds, by_ds[ds], cursor[ds], args.batch_rows) for ds in part))
        selected = {int(r["ds"]) for r in json_rows(lines, "selected")}
        if selected != set(part):
            raise PsqlError(f"no selection result for datastreams {sorted(set(part) - selected)}")
        rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in json_rows(lines, "row"):
            rows[int(row["ds"])].append(row)
        batch: dict[int, list[Move]] = {}
        for ds in part:
            found = rows.get(ds, [])
            if len(found) < args.batch_rows:
                del cursor[ds]
            else:
                cursor[ds] = max(int(r["id"]) for r in found)
            for r in found:
                if r["twin"]:
                    plan.twin_ids[int(r["id"])] = ds
            todo = [Move(int(r["id"]), ds, int(r["to"]), int(r["from_foi"]), int(r["to_foi"]))
                    for r in found if not r["twin"]]
            if todo:
                batch[ds] = todo
        if not batch:
            continue
        records = [m for todo in batch.values() for m in todo]
        try:
            append_moves(ids_file, records)
        except OSError as exc:
            raise IdsFileError(f"cannot append to {ids_file}: {exc} (nothing of this round was moved; "
                               f"rows moved before it: {moved:,})") from exc
        recorded += len(records)
        touched.update(datastreams_in(records))  # before the write: it may commit even if the client fails
        try:
            result = run_sql(WRITE_SESSION + "".join(move_sql(ds, todo) for ds, todo in batch.items()))
        except PsqlError as exc:
            raise PsqlError(
                f"{exc}\n  Rows moved before the error, as far as psql reported: "
                f"{moved + _counted(exc.lines, 'moved', salvage=True):,}. Every row this session can move is in "
                f"{ids_file}; re-running is safe (use a smaller --batch-rows if a statement timed out)."
            ) from exc
        moved += _counted(result, "moved")
        _progress(f"  moved {moved:,} rows so far, {recorded:,} recorded; {len(cursor)} source datastreams to go")
    return recorded, moved


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def _move_relation(moves: Sequence[Move]) -> str:
    values = ", ".join(f"({m.observation_id}::bigint, {m.from_ds}::bigint, {m.to_ds}::bigint, "
                       f"{m.from_foi}::bigint, {m.to_foi}::bigint)" for m in moves)
    return f"(VALUES {values}) AS v(id, from_ds, to_ds, from_foi, to_foi)"


# A recorded row can go back only while its source datastream and FeatureOfInterest exist: OBSERVATIONS has
# foreign keys to both, so one row pointing at a deleted one fails the whole UPDATE (its batch and every re-run).
# --retire-stale-ground-temperature deletes emptied stale datastreams, the sources of the moves out of them.
_SOURCE_EXISTS = ('EXISTS (SELECT 1 FROM "DATASTREAMS" sd WHERE sd."ID" = v.from_ds) '
                  'AND EXISTS (SELECT 1 FROM "FEATURES" sf WHERE sf."ID" = v.from_foi)')
_AT_TARGET = 'o."DATASTREAM_ID" = v.to_ds AND o."FEATURE_ID" = v.to_foi'


def rollback_state_sql(moves: Sequence[Move]) -> str:
    """How many recorded rows are where the move put them (with their source there or deleted since), back where
    they were, elsewhere, or gone (read-only)."""
    return f"""
SELECT json_build_object(
         't', 'rbstate', 'listed', count(*),
         'moved', count(*) FILTER (WHERE {_AT_TARGET} AND {_SOURCE_EXISTS}),
         'source_gone', count(*) FILTER (WHERE {_AT_TARGET} AND NOT ({_SOURCE_EXISTS})),
         'back', count(*) FILTER (WHERE o."DATASTREAM_ID" = v.from_ds AND o."FEATURE_ID" = v.from_foi),
         'gone', count(*) FILTER (WHERE o."ID" IS NULL))::text
  FROM {_move_relation(moves)}
  LEFT JOIN "OBSERVATIONS" o ON o."ID" = v.id;
"""


def restore_sql(moves: Sequence[Move]) -> str:
    """Move the recorded rows still where the move put them back, those whose source datastream and FoI exist."""
    return f"""
WITH restored AS (
    UPDATE "OBSERVATIONS" o
       SET "DATASTREAM_ID" = v.from_ds, "FEATURE_ID" = v.from_foi
      FROM {_move_relation(moves)}
     WHERE o."ID" = v.id AND {_AT_TARGET}
       AND {_SOURCE_EXISTS}
    RETURNING o."ID"
)
SELECT json_build_object('t', 'restored', 'listed', {len(moves)}, 'n', count(*))::text FROM restored;
"""


@dataclass
class RollbackState:
    listed: int = 0
    moved: int = 0  # where the move put them: restored by --rollback --apply
    source_gone: int = 0  # where the move put them, but their source datastream or FoI was deleted: they stay
    back: int = 0  # where they were: nothing to do
    gone: int = 0

    @property
    def elsewhere(self) -> int:
        return self.listed - self.moved - self.source_gone - self.back - self.gone


def rollback_scan(run_sql: RunSql, moves: Sequence[Move], batch_rows: int, chunk: int) -> RollbackState:
    state = RollbackState()
    for session in _chunks(_chunks(moves, batch_rows), chunk):
        lines = run_sql(READ_ONLY_SESSION + "".join(rollback_state_sql(batch) for batch in session))
        rows = json_rows(lines, "rbstate")
        if len(rows) != len(session):
            raise PsqlError("rollback state: a statement returned no result")
        for row in rows:
            state.listed += int(row["listed"])
            state.moved += int(row["moved"])
            state.source_gone += int(row["source_gone"])
            state.back += int(row["back"])
            state.gone += int(row["gone"])
    return state


def print_rollback_state(state: RollbackState) -> None:
    print("\nRecorded moves")
    _line("observations recorded", state.listed)
    _line("  where the move put them: to restore", state.moved)
    _line("  where the move put them, source gone: they stay", state.source_gone)
    _line("  back where they were: nothing to do", state.back)
    _line("  elsewhere (changed since): left alone", state.elsewhere)
    _line("  deleted since", state.gone)
    if state.source_gone:
        print(f"  {state.source_gone:,} rows cannot go back: the datastream or FeatureOfInterest they were moved out "
              "of was deleted since (--retire-stale-ground-temperature deletes the emptied stale datastreams). "
              "They stay in their station's current datastream.")


def run_rollback(args: argparse.Namespace, run_sql: RunSql) -> int:
    mode = "APPLY: moves rows back" if args.apply else "DRY RUN: read-only, nothing is written"
    print(f"Rollback of the KNMI moves recorded in {args.ids_file} ({mode})")
    only = set(args.datastream or ())
    every = distinct_moves(read_ids_file(args.ids_file))
    moves = distinct_moves(every, only or None)
    print(f"{len(every):,} distinct observations recorded" +
          (f"; {len(moves):,} from or to datastreams {sorted(only)}" if only else ""))
    if not moves:
        return EXIT_OK
    _progress("Checking the recorded rows, read-only ...")
    before = rollback_scan(run_sql, moves, args.batch_rows, args.chunk)
    print_rollback_state(before)
    if not args.apply:
        print("Dry run: nothing was written. Re-run with --apply to move them back.")
        return EXIT_OK
    if refuse_if_busy(run_sql):
        return EXIT_REFUSED
    _progress("Refreshing the datastreams named in the IDs file (left by an interrupted run?) ...")
    refresh(run_sql, datastreams_in(every), args.chunk)
    touched: set[int] = set()
    restored = 0
    try:
        for session in _chunks(_chunks(moves, args.batch_rows), args.chunk):
            touched.update(datastreams_in(m for batch in session for m in batch))
            try:
                lines = run_sql(WRITE_SESSION + "".join(restore_sql(batch) for batch in session))
            except PsqlError as exc:
                raise PsqlError(f"{exc}\n  Rows restored before the error, as far as psql reported: "
                                f"{restored + _counted(exc.lines, 'restored', salvage=True):,}. "
                                "Re-running the rollback is safe.") from exc
            restored += _counted(lines, "restored")
            _progress(f"  restored {restored:,} rows so far")
    finally:
        _progress("Refreshing the datastreams touched ...")
        refresh(run_sql, touched, args.chunk)
    print(f"Moved back {restored:,} observations (planned {before.moved:,}); refreshed {len(touched)} datastreams.")
    _progress("Verifying, read-only ...")
    after = rollback_scan(run_sql, moves, args.batch_rows, args.chunk)
    if after.moved:
        print(f"Verification: {after.moved:,} recorded rows whose source exists are still where the move put them. "
              "Re-run.")
        return EXIT_VERIFY
    print("Verification: no recorded row is where the move put it any more"
          + (f", except {after.source_gone:,} whose source is gone." if after.source_gone else "."))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Plan and apply
# ---------------------------------------------------------------------------


def make_plan(run_sql: RunSql, catalogue: Catalogue, ds_ids: Sequence[int], args: argparse.Namespace) -> Plan:
    groups = scan(run_sql, ds_ids, args.chunk)
    plan = build_plan(groups, catalogue, set(args.datastream or ()) or None)
    find_twins(run_sql, plan, args.chunk)
    return plan


def run(args: argparse.Namespace, run_sql: RunSql) -> int:
    if args.rollback:
        return run_rollback(args, run_sql)
    mode = "APPLY: moves observations" if args.apply else "DRY RUN: read-only, nothing is written"
    print(f"KNMI observations in another station's or a stale datastream ({mode})")
    if args.apply:
        prepare_ids_file(args.ids_file)  # a wrong file fails before anything else

    catalogue = parse_catalogue(run_sql(READ_ONLY_SESSION + catalogue_sql(args.prefix)), args.prefix)
    knmi = catalogue.knmi
    ambiguous = [k for k, t in catalogue.targets.items() if t.datastream is None]
    print(f"Catalogue: {len(catalogue.things)} KNMI Things (source {SOURCE}), {len(knmi)} datastreams, "
          f"{len(catalogue.stale)} stale, {len(ambiguous)} ambiguous (Thing, property) pairs; "
          f"{len(catalogue.datastreams) - len(knmi)} datastreams of '{args.prefix}KNMI' Things of another source "
          "not scanned")
    for key in ambiguous:
        print(f"  ambiguous: {key[0]} {key[1]}: {catalogue.targets[key].reason}")
    if args.datastream:
        unknown = sorted(set(args.datastream) - set(knmi))
        if unknown:
            print(f"Not KNMI datastreams: {unknown}")
            return EXIT_ERROR
        print(f"Limited to moves from or to datastreams {sorted(args.datastream)} (all KNMI datastreams are "
              "scanned: rows come from anywhere)")
    if not knmi:
        return EXIT_OK

    if args.apply:
        if refuse_if_busy(run_sql):
            return EXIT_REFUSED
        named = datastreams_in(read_ids_file(args.ids_file))
        if named:
            _progress("Refreshing the datastreams named in the IDs file (left by an interrupted run?) ...")
            refresh(run_sql, named, args.chunk)

    _progress("Scanning the KNMI datastreams, one read-only statement each ...")
    plan = make_plan(run_sql, catalogue, knmi, args)
    print_report(plan, catalogue, args.top)
    print_stale(plan, catalogue)
    if args.plan_file:
        write_plan_csv(args.plan_file, plan, catalogue)
        print(f"\nPlan written to {args.plan_file}")
    planned = plan.rows(MOVE)
    print(f"\nPlan: move {planned:,} rows out of {len(plan.sources())} source datastreams; per session of "
          f"{args.chunk} of them, the rows (at most {args.batch_rows:,} per datastream) are read, appended to the "
          "IDs file, then moved, one UPDATE per datastream. Then the times, observedArea and LAST_FOI_ID of every "
          "datastream touched are recomputed.")

    if not args.apply:
        if args.twins_file:
            write_twins_file(args.twins_file, plan.twin_ids)
            print(f"Twins ({len(plan.twin_ids):,}) written to {args.twins_file}")
        print("Dry run: nothing was written. Re-run with --apply --ids-file PATH to move.")
        return EXIT_OK

    _progress(f"Applying; every row is appended to {args.ids_file} before it is moved ...")
    touched: set[int] = set()
    try:
        recorded, moved = apply_moves(run_sql, plan, args.ids_file, args, touched)
    finally:
        _progress("Refreshing the datastreams touched ...")
        refresh(run_sql, touched, args.chunk)
    print(f"Moved {moved:,} observations (planned {planned:,}); {recorded:,} recorded in {args.ids_file}; "
          f"refreshed {len(touched)} datastreams.")

    _progress("Verifying, read-only ...")
    sources = sorted(set(plan.sources()) | (touched & set(knmi)))
    check = make_plan(run_sql, catalogue, sources, args) if sources else Plan()
    twins = {**plan.twin_ids, **check.twin_ids}
    if args.twins_file:
        write_twins_file(args.twins_file, twins)
        print(f"Twins ({len(twins):,}) written to {args.twins_file}")
    left = check.rows(MOVE)
    if left:
        print(f"Verification: {left:,} rows to move remain. Re-run the same command.")
        return EXIT_VERIFY
    print(f"Verification: no rows to move remain in the {len(sources)} datastreams moved from or to; "
          f"{check.rows(TWIN):,} twins stay for cleanup_frost_observations.py --ids-file.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Retiring the stale ground temperature datastreams (FROST HTTP API)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaleDatastream:
    id: str
    name: str
    thing: str


def stale_ground_temperature(things: Sequence[dict[str, Any]], fields: Any) -> list[StaleDatastream]:
    """The '... - Ground temperature' datastreams of KNMI Things that also have a 'Ground surface temperature' one."""
    found = []
    for thing in things:
        if migrate._properties(thing).get("source") != SOURCE:
            continue
        datastreams = migrate._dicts(thing.get("Datastreams"))

        def ground(ds: dict[str, Any]) -> bool:
            return canonical(str(migrate._properties(ds).get("observed_property") or "")) == GROUND_TEMPERATURE

        if not any(ground(ds) and str(ds.get("name") or "").endswith(CURRENT_SUFFIX) for ds in datastreams):
            continue
        for ds in datastreams:
            ds_id = migrate.entity_id(ds, fields)
            if ds_id is not None and ground(ds) and str(ds.get("name") or "").endswith(STALE_SUFFIX):
                found.append(StaleDatastream(ds_id, str(ds.get("name") or ""), str(thing.get("name") or "")))
    return sorted(found, key=lambda s: migrate._id_order(s.id))


def observation_count(http: Any, datastreams_path: str, ds_id: str, fields: Any) -> int:
    """How many observations a datastream has, as FROST counts them; raises on any doubt."""
    endpoint = http.endpoint(f"{datastreams_path}({ds_id})/Observations")
    response = http.get(endpoint, params={"$top": "1", "$count": "true"})  # $select names differ per version
    if not response.ok:
        raise RuntimeError(f"GET {endpoint} failed with HTTP {response.status_code}")
    body = response.json()
    count = body.get(fields.count, body.get("@iot.count", body.get("@count"))) if isinstance(body, dict) else None
    values = body.get("value") if isinstance(body, dict) else None
    if not isinstance(count, int) or not isinstance(values, list):
        raise RuntimeError(f"GET {endpoint} returned no count")
    return max(count, len(values))


def delete_datastream(http: Any, endpoint: str, timeout: float) -> requests.Response:
    return requests.delete(endpoint, headers=http.headers(), timeout=timeout)


def recorded_sources(moves: Iterable[Move]) -> Counter[str]:
    """Recorded moves per source datastream (as FROST ids are printed): the rows only that datastream takes back."""
    return Counter(str(m.from_ds) for m in distinct_moves(moves))


def retire(http: Any, things_path: str, datastreams_path: str, prefix: str, fields: Any, *, apply: bool,
           only: set[str] | None = None, timeout: float = 30.0, recorded: Mapping[str, int] | None = None,
           force: bool = False) -> int:
    """Delete the empty stale datastreams. One that ``recorded`` (an IDs file's moves per source datastream) names
    as a source is kept unless ``force``: deleting it ends the rollback of the rows moved out of it."""
    things = migrate.fetch_knmi_things(http, things_path, prefix, fields)
    stale = [s for s in stale_ground_temperature(things, fields) if not only or s.id in only]
    print(f"{len(things)} KNMI Things; {len(stale)} stale ground temperature datastreams"
          + (f" (limited to {sorted(only)})" if only else ""))
    deleted = kept = held = failed = 0
    for s in stale:
        try:
            count = observation_count(http, datastreams_path, s.id, fields)
        except (RuntimeError, ValueError, requests.RequestException) as exc:
            print(f"  {s.id:>8} {s.name!r}: cannot count its observations ({exc}); left alone")
            failed += 1
            continue
        if count:
            print(f"  {s.id:>8} {s.name!r}: {count:,} observations; never deleted while it has any")
            kept += 1
            continue
        sources = (recorded or {}).get(s.id, 0)
        if sources and not force:
            print(f"  {s.id:>8} {s.name!r}: empty, but the source of {sources:,} moves in the IDs file; deleting it "
                  "ends their rollback: left alone (--force-retire deletes it)")
            held += 1
            continue
        note = f" (source of {sources:,} recorded moves: they can no longer be rolled back)" if sources else ""
        if not apply:
            print(f"  {s.id:>8} {s.name!r}: empty, would be deleted{note}")
            continue
        endpoint = http.endpoint(f"{datastreams_path}({s.id})")
        try:
            response = delete_datastream(http, endpoint, timeout)
        except requests.RequestException as exc:
            print(f"  {s.id:>8} {s.name!r}: DELETE failed: {type(exc).__name__}")
            failed += 1
            continue
        if response.ok:
            print(f"  {s.id:>8} {s.name!r}: deleted{note}")
            deleted += 1
        else:
            print(f"  {s.id:>8} {s.name!r}: DELETE failed with HTTP {response.status_code}")
            failed += 1
    empty = len(stale) - kept - failed
    print(f"\n{empty} empty" + (f", {deleted} deleted" if apply else " (dry run: nothing deleted)")
          + f"; {kept} with observations left alone; {held} empty but named as a source in the IDs file left alone; "
          f"{failed} failed.")
    if failed:
        return EXIT_ERROR
    return EXIT_REFUSED if kept or held else EXIT_OK


def run_retire(args: argparse.Namespace) -> int:
    from app.config import settings
    from app.frost.http_client import FrostHTTPClient
    from app.frost.odata_fields import odata_fields_for

    target = migrate.select_target(settings.frost_targets, args.target_url)
    fields = odata_fields_for(target.version)
    http = FrostHTTPClient(base_urls=[target.url], auth_username=target.auth_username,
                           auth_password=target.auth_password, auth_token=target.auth_token,
                           timeout=settings.request_timeout_seconds)
    has_auth = bool(target.auth_username or target.auth_token)
    print(f"FROST target: {target.url} (declared SensorThings {target.version}; credentials from environment: "
          f"{'yes' if has_auth else 'none'})")
    print(f"Mode: {'APPLY: deletes empty stale datastreams' if args.apply else 'dry run'}\n")
    only = {str(i) for i in args.datastream} if args.datastream else None
    recorded = None
    if args.ids_file:
        recorded = recorded_sources(read_ids_file(args.ids_file))
        print(f"IDs file {args.ids_file}: {sum(recorded.values()):,} recorded moves out of {len(recorded)} "
              "datastreams; a stale datastream named as their source is "
              + ("deleted anyway (--force-retire): the point of no return for those moves\n" if args.force_retire
                 else "left alone (--force-retire deletes it)\n"))
    else:
        print("No --ids-file: not checked whether recorded moves came out of these datastreams. Deleting one ends "
              "the rollback of every row moved out of it.\n")
    return retire(http, settings.things_path, settings.datastreams_path, args.prefix, fields, apply=args.apply,
                  only=only, timeout=settings.request_timeout_seconds, recorded=recorded, force=args.force_retire)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move KNMI observations in FROST to their own station's datastream (dry run by default), roll "
                    "that back, or retire the empty stale ground temperature datastreams.",
        epilog="The module docstring has the runbook.",
    )
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--ssh", metavar="USER@HOST", help="SSH destination of the FROST host")
    where.add_argument("--container", metavar="NAME", help="local docker container running the FROST database")
    parser.add_argument("--ssh-key", metavar="PATH", help="identity file for --ssh")
    parser.add_argument("--compose-dir", default="/opt/frost", help="compose directory on the host (%(default)s)")
    parser.add_argument("--service", default="frost-db", help="database service in that compose project (%(default)s)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rollback", action="store_true", help="move the rows recorded in --ids-file back")
    mode.add_argument("--retire-stale-ground-temperature", action="store_true",
                      help="delete the empty stale 'Ground temperature' datastreams through the FROST API")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--ids-file", type=Path, metavar="PATH",
                        help="CSV every moved row is appended to before it is moved; --rollback reads it; "
                             "--retire-stale-ground-temperature keeps the datastreams it names as a source")
    parser.add_argument("--force-retire", action="store_true",
                        help="with --retire-stale-ground-temperature --ids-file: delete empty stale datastreams even "
                             "when moves out of them are recorded (they can no longer be rolled back)")
    parser.add_argument("--twins-file", type=Path, metavar="PATH",
                        help="write the twins as observation_id,datastream_id (for cleanup_frost_observations.py)")
    parser.add_argument("--plan-file", type=Path, metavar="PATH", help="write the plan per group as CSV")
    parser.add_argument("--datastream", type=int, action="append", metavar="ID",
                        help="limit to moves from or to this datastream (repeatable): a canary")
    parser.add_argument("--prefix", default="GEO_", help="entity name prefix of the connector (%(default)s)")
    parser.add_argument("--target-url", help="with --retire-stale-ground-temperature: one of the configured targets")
    parser.add_argument("--batch-rows", type=int, default=2000, help="max rows per datastream and UPDATE (%(default)s)")
    parser.add_argument("--chunk", type=int, default=20, help="datastreams per psql session (%(default)s)")
    parser.add_argument("--top", type=int, default=20, help="lines per report list (%(default)s)")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds per psql session (%(default)s)")
    args = parser.parse_args(argv)
    if args.retire_stale_ground_temperature:
        if args.ssh or args.container:
            parser.error("--retire-stale-ground-temperature uses the FROST API: no --ssh/--container")
        if args.ids_file is not None and not args.ids_file.is_file():
            parser.error("--ids-file: no such file (the record written by --apply)")
        if args.force_retire and args.ids_file is None:
            parser.error("--force-retire overrides the check against --ids-file: give the IDs file")
        return args
    if args.force_retire:
        parser.error("--force-retire goes with --retire-stale-ground-temperature")
    if not (args.ssh or args.container):
        parser.error("one of --ssh or --container is required")
    if min(args.batch_rows, args.chunk, args.top) < 1:
        parser.error("--batch-rows, --chunk and --top must be positive")
    if args.rollback and (args.ids_file is None or not args.ids_file.is_file()):
        parser.error("--rollback needs --ids-file: an existing record written by --apply")
    if args.apply and args.ids_file is None:
        parser.error("--apply needs --ids-file: the record of every moved row, for audit and rollback")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.retire_stale_ground_temperature:
            return run_retire(args)
        run_sql = pressure.psql_runner(
            pressure.psql_argv(ssh=args.ssh, ssh_key=args.ssh_key, compose_dir=args.compose_dir,
                               service=args.service, container=args.container),
            args.timeout,
        )
        return run(args, run_sql)
    except (PsqlError, IdsFileError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("Interrupted. Every row moved is in the IDs file; run the same command again.", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
