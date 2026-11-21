#!/usr/bin/env python
"""Delete misplaced or duplicated observations from one FROST database, archiving every row first; restore them.

Owner decisions of 2026-11-20: misplaced TGV rows are deleted, not moved; every
deleted row is archived in full first and can be restored; the restart
duplicates are removed the same way.

Selections (one per run; each is a dry run unless --apply is given)
-------------------------------------------------------------------
--tgv-misplaced
    Rows labelled as TGV (The Green Village, Kafka lane) readings, in a datastream
    whose Thing is not a TGV Thing and which was not registered for that sensor_id.
    A TGV label: PARAMETERS sensor_id LIKE 'tgv-%' and location 'tgv' (the Kafka
    lane sets both), but not sensor_id LIKE 'tgv-levellog-%': Levellog readings
    also carry location 'tgv' and a 'tgv-' sensor_id, and are not TGV Kafka
    readings. The dry run counts the other 'tgv-' labels it leaves alone. Until
    the routing fix the connector's cache
    warm-up mapped bare stream keys ("humidity") to the first datastream listed
    with that streamKey, so TGV readings landed in Sensor.Community, KNMI, Ohnics
    and Samen Meten datastreams of the same property.
--duplicates
    Exact duplicates within one datastream: the same DATASTREAM_ID,
    PHENOMENON_TIME_START, PHENOMENON_TIME_END, RESULT_TYPE, RESULT_NUMBER,
    RESULT_STRING, RESULT_BOOLEAN and RESULT_JSON (NULL-safe) and the same
    PARAMETERS label (sensor_id, observed_property, sensor_name). The lowest ID is
    kept. sensor_name because co-located Sensor.Community sensors share a
    sensor_id, and so a datastream: only sensor_name tells their readings apart.
    Rows that differ in sensor_name are never duplicates (a missing sensor_name on
    both sides counts as equal).
    RESULT_BOOLEAN and RESULT_JSON are compared too, so a boolean or JSON result
    is never taken for a duplicate of a different one. Rows that share a
    datastream and phenomenonTime but differ in value or label are conflicts:
    they are reported, and no row of such a (datastream, phenomenonTime) group
    is deleted, not even an exact twin inside it (the dry run counts those).
    Before the seeding fix every connector restart re-posted each source's
    fetch window.
--misrouted-leftovers
    Rows that scripts/fix_sc_misrouted_observations.py plans to move ('move' or
    'move-blocked': the datastream matching their label exists) but leaves in
    place because that datastream already has an observation at their
    phenomenonTime, when that observation is the same reading: the same label
    (sensor_id, observed_property, sensor_name), phenomenon time and result. A different
    reading at that time is a conflict: reported, never deleted. The groups,
    labels and target datastreams come from that script's own catalogue, scan and
    plan (imported, not reimplemented). Run it after that script's --apply.
--ids-file FILE
    An explicit list, e.g. KNMI cross-station rows: one observation ID per line,
    or a CSV whose header names an observation_id (or id) column and optionally a
    datastream_id column. A row that is no longer in its listed datastream is left
    alone. Blank lines and lines starting with '#' are skipped.
    With --require-twin a listed row is deleted only while another row holds the
    same reading (the --duplicates definition: phenomenon start and end, every
    RESULT_* column and the label), checked by the dry run and again by each
    DELETE: in the row's own datastream (the listed one) with a lower ID, or in
    another datastream of the row's Thing or of the Thing its PARAMETERS
    thing_name names (--prefix + thing_name). For KNMI rows (sensor_id 'knmi-%')
    the station is PARAMETERS thing_name, not sensor_id: until 2026-11 their
    sensor_id was the station's place in the EDR response. Use it for the twins
    file of scripts/fix_knmi_observations.py: rows whose copy is in their own
    station's datastream; one whose copy is gone by then is left alone.

Never touched, by any selection or by --restore: rows of datastreams of TGV
Things. TGV Things carry properties.site = 'tgv' and properties.source = 'kafka'
(the contract with the TGV lane, which registers fresh TGV entities); either one
is enough here, so Levellog Things at the TGV site are protected too.
--tgv-misplaced also leaves a TGV-labelled row alone in a datastream registered
for its own sensor_id.

--datastream ID (repeatable) limits a selection, or --restore, to those
datastreams: a canary.

Safety
------
* Dry run by default: read-only sessions (default_transaction_read_only), and
  nothing is written, not even the archive.
* Archive first (--archive, required with --apply). Per page of at most
  --page-rows rows: read the full rows, every OBSERVATIONS column exactly as
  row_to_json prints it, with the md5 of that text, in a read-only session;
  append them to the archive and fsync it; only then send the DELETEs. Each
  DELETE takes at most --batch-rows rows of one datastream, is its own
  transaction, and deletes a listed row only while md5(row_to_json(row)) still
  equals the archived hash, its datastream is not a TGV one and the selection
  still holds (for duplicates and leftovers: the twin that stays still exists).
  So the archive always holds the exact content of every deleted row, also when
  a timeout, Ctrl-C, a dropped SSH connection, or a psql session that finishes
  on the server after the client left interrupts a run.
* --restore --archive FILE re-inserts archived rows with their original IDs and
  values (json_populate_recordset, INSERT ... OVERRIDING SYSTEM VALUE ... ON
  CONFLICT DO NOTHING) in batches of one datastream, then checks that every
  restorable row is back byte for byte: md5(row_to_json(row)) equals the
  archived hash. A row archived more than once (a re-run after an interruption)
  is restored from its last copy, the only one a DELETE can have matched. Rows
  whose datastream or FeatureOfInterest is gone, or whose datastream now belongs
  to a TGV Thing, are counted and skipped. Dry run unless --apply is given. The
  INSERT trigger may set the datastream's LAST_FOI_ID; nothing else changes.
* Datastream times: after deleting and after restoring, phenomenonTime and
  resultTime of every datastream touched are recomputed with the move script's
  refresh_sql. FROST 2.8 also has an AFTER DELETE trigger on OBSERVATIONS
  (datastreams_update_delete, read on production 2026-11-20) that recomputes
  them when a deleted row was at an end of the range; the refresh does not rely
  on it. The archive records which datastreams were refreshed, so a run
  interrupted before its refresh is caught up by the next --apply with the same
  archive.
* FeaturesOfInterest are never deleted. The dry run counts those that would be
  left without observations; --apply reports those that were, and how many a
  Location generated (FROST reuses those for the Location's next observation).
  FROST 2.8 has no foreign key onto OBSERVATIONS (checked on production
  2026-11-20); the script checks again and refuses to delete if one appears.
* Sessions: one psql session per read and one per page of DELETEs, each with
  statement_timeout 50 s, lock_timeout 10 s and idle_in_transaction_session_timeout
  60 s (the database stops statements after 60 s). Statements stay far below
  that: TGV scans per --range-ids observation IDs or per datastream; duplicate
  scans per datastream range of at most --chunk-rows rows, sized by a count
  over the DATASTREAM_ID index and reading OBS_DS_PHENTIME where it exists;
  reads of at most --page-rows rows. Writes refuse to start while a write
  session of a repair script (this one, fix_sc_pressure_units,
  fix_sc_misrouted_observations) is on the server, and each write session takes
  an advisory lock, so two of this script never overlap. SSH keepalives make a
  dead connection fail within about a minute.
* After --apply the selection is scanned again, read-only.

The archive
-----------
JSON lines, UTF-8, LF. The first line identifies the file; then per run a line
{"run": ...}, per archived row {"sel": ..., "run": ..., "ds": <datastream>,
"id": <observation>, "h": <md5>, ["target": <datastream>,] "row": <row_to_json
of the row, verbatim>}, and after each refresh {"refreshed": <run>,
"datastreams": [...]}. A cut-off last line (an interrupted append: no DELETE was
sent for its page) is dropped by the next --apply. --restore also reads a gzip
file (compress the archive once the cleanup is over); --apply appends only to
an uncompressed one. Keep the archive: it is the way back.

How it connects
---------------
Like the other repair scripts it pipes SQL into psql inside the FROST database
container: ssh to the FROST host, or docker exec for a local container. psql
there connects as the container's own $POSTGRES_USER to $POSTGRES_DB, so this
script never handles, needs or prints a credential.

Exit status
-----------
    0  dry run done; --apply or --restore --apply done and verified
    1  an error (psql, the archive, the IDs file, an unknown --datastream); see the message
    2  usage error
    3  refused to write: a write session of a repair script is on the server, a
       foreign key references OBSERVATIONS, or the archive's columns are not the table's
    4  verification: rows are still selected after --apply (rows that arrived or
       changed meanwhile: re-run), or rows are not back exactly after --restore --apply
  130  interrupted (Ctrl-C)

Runbook (production)
--------------------
0. Read-only first: dry runs of --tgv-misplaced and --duplicates, and free disk
   on the FROST host: the dry run estimates the WAL (one 8 kB full-page image
   per heap page touched, wal_compression is off; VACUUM writes as much again).
1. Deploy the connector fixes. From then on no TGV reading is routed to another
   source's datastream and no restart re-posts its window.
2. --tgv-misplaced: canary (--datastream ID), check that datastream in the
   FROST API, then everything, with one archive file. Before
   scripts/fix_sc_misrouted_observations.py --apply: once the TGV lane has
   registered TGV datastreams, that script would plan the TGV rows in
   Sensor.Community datastreams as moves into them.
3. scripts/fix_sc_pressure_units.py and scripts/fix_sc_misrouted_observations.py
   as their runbooks say.
4. --misrouted-leftovers, after the move script's --apply.
5. Once the connector's dead-letter queue has expired (48 h after the deploy):
   the dry runs again, and --apply again where they find rows.
6. --ids-file knmi_twins.csv --require-twin, the twins file of
   scripts/fix_knmi_observations.py: their conflict groups keep --duplicates
   away from those times.
7. --duplicates last: canary, then everything.
8. Consider VACUUM (ANALYZE) "OBSERVATIONS" in a session with statement_timeout = 0:
   the dead rows stay below autovacuum's threshold (20% of the table).
If a run is interrupted, run the same command again. To undo: --restore
--archive FILE (a dry run), then the same with --apply.

Usage
-----
    python scripts/cleanup_frost_observations.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner --tgv-misplaced
    python scripts/cleanup_frost_observations.py --ssh ... --tgv-misplaced --apply --archive tgv.jsonl --datastream 4258
    python scripts/cleanup_frost_observations.py --ssh ... --tgv-misplaced --apply --archive tgv.jsonl
    python scripts/cleanup_frost_observations.py --ssh ... --duplicates [--apply --archive duplicates.jsonl]
    python scripts/cleanup_frost_observations.py --ssh ... --misrouted-leftovers [--apply --archive leftovers.jsonl]
    python scripts/cleanup_frost_observations.py --ssh ... --ids-file knmi_twins.csv --require-twin [--apply --archive knmi.jsonl]
    python scripts/cleanup_frost_observations.py --ssh ... --restore --archive tgv.jsonl [--apply] [--datastream ID]
    python scripts/cleanup_frost_observations.py --container <local-db-container> --duplicates
"""

from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import hashlib
import io
import json
import os
import re
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, NamedTuple

# Run as `python scripts/cleanup_frost_observations.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import fix_sc_misrouted_observations as move
from scripts import fix_sc_pressure_units as pressure

RunSql = Callable[[str], list[str]]
PsqlError = pressure.PsqlError

APP_NAME = "cleanup_frost_observations"
ARCHIVE_VERSION = 1

TGV = "tgv-misplaced"
DUPLICATES = "duplicates"
LEFTOVERS = "misrouted-leftovers"
IDS = "ids-file"

EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_VERIFY, EXIT_INTERRUPTED = 0, 1, 3, 4, 130


def _app_name_of(session: str, default: str) -> str:
    match = re.search(r"application_name = '([^']+)'", session)
    return match[1] if match else default


# Write sessions of the repair scripts that change OBSERVATIONS through psql.
# fix_knmi_observations (fix/knmi-station-ids) names its write sessions by this
# application_name; it already refuses while this script writes.
KNMI_APP_NAME = "fix_knmi_observations"
WRITERS = (APP_NAME, pressure.WRITE_APP_NAME, _app_name_of(move.WRITE_SESSION, "fix_sc_misrouted_observations"), KNMI_APP_NAME)

# What two rows of one datastream and phenomenonTime must share to hold the same reading.
READING_COLUMNS = ("PHENOMENON_TIME_END", "RESULT_TYPE", "RESULT_NUMBER", "RESULT_STRING", "RESULT_BOOLEAN", "RESULT_JSON")
# sensor_name: co-located Sensor.Community sensors share a sensor_id, and so a datastream; only it tells them apart.
LABEL_KEYS = ("sensor_id", "observed_property", "sensor_name")

# Every session: the database stops statements after 60 s; fixed output formats, because
# md5(row_to_json(row)) must come out the same in the read and in the DELETE.
_SETTINGS = (
    "".join(f"SET {name} = '{value}';\n" for name, value in pressure.SESSION_TIMEOUTS.items())
    + "SET extra_float_digits = 3;\n"  # double precision printed so that it reads back exactly
    + "SET TimeZone = 'UTC';\n"  # timestamps in row_to_json
    + "SET DateStyle = 'ISO, MDY';\n"
    + "SET standard_conforming_strings = on;\n"  # backslashes in string literals are literal
    + "SET client_encoding = 'UTF8';\n"
    + "SET max_parallel_workers_per_gather = 0;\n"  # one process per statement on a shared database
    + "SET jit = off;\n"  # short statements: compiling costs more than it saves
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

# Numeric result lines end in this field, so a line cut off mid-way never reads as a complete one.
END = "end"

_MD5 = re.compile(r"[0-9a-f]{32}")
_INT = re.compile(r"[0-9]+")


class ArchiveError(RuntimeError):
    pass


class IdsFileError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# SQL building blocks
# ---------------------------------------------------------------------------


def protected_thing(t: str) -> str:
    """The Thing (alias t) is a TGV Thing: rows of its datastreams are never touched."""
    return f"""coalesce({t}."PROPERTIES"->>'site' = 'tgv' OR {t}."PROPERTIES"->>'source' = 'kafka', false)"""


def protected_ds(ds: str) -> str:
    """The datastream with ID ``ds`` (an SQL expression) belongs to a TGV Thing."""
    return (
        f'EXISTS (SELECT 1 FROM "DATASTREAMS" pd JOIN "THINGS" pt ON pt."ID" = pd."THING_ID" '
        f'WHERE pd."ID" = {ds} AND {protected_thing("pt")})'
    )


def label(o: str, key: str) -> str:
    return f"""({o}."PARAMETERS"->>'{key}')"""


def station(o: str) -> str:
    """The station of row o's label: thing_name for a KNMI row (sensor_id 'knmi-%': until 2026-11 that was the
    station's place in the EDR response, which shifted), else sensor_id."""
    sid = label(o, "sensor_id")
    return f"(CASE WHEN {sid} LIKE 'knmi-%' THEN coalesce('knmi:' || {label(o, 'thing_name')}, {sid}) ELSE {sid} END)"


def same_reading(a: str, b: str, *, by_station: bool = False) -> str:
    """Rows a and b hold the same reading: phenomenon end, every result column and the label; NULL-safe.
    With by_station, a KNMI row's sensor_id is compared as its station (see station())."""
    parts = [f'{a}."{c}" IS NOT DISTINCT FROM {b}."{c}"' for c in READING_COLUMNS]
    for k in LABEL_KEYS:
        if by_station and k == "sensor_id":
            parts.append(f"{station(a)} IS NOT DISTINCT FROM {station(b)}")
        else:
            parts.append(f"{label(a, k)} IS NOT DISTINCT FROM {label(b, k)}")
    return " AND ".join(parts)


def reading_key(x: str) -> str:
    """The same columns as a PARTITION BY list (NULLs share a partition, as with IS NOT DISTINCT FROM)."""
    return ", ".join([*(f'{x}."{c}"' for c in READING_COLUMNS), *(label(x, k) for k in LABEL_KEYS)])


def tgv_prefix(o: str) -> str:
    return f"{label(o, 'sensor_id')} LIKE 'tgv-%'"


def tgv_label(o: str) -> str:
    """Row o is labelled as a TGV Kafka-lane reading: a 'tgv-' sensor_id and location 'tgv' (the lane sets both),
    and not a Levellog sensor ('tgv-levellog-', also at location 'tgv')."""
    return (f"({tgv_prefix(o)} AND {label(o, 'sensor_id')} NOT LIKE 'tgv-levellog-%' "
            f"AND coalesce({label(o, 'location')} = 'tgv', false))")


def own_label(o: str, d: str) -> str:
    """Row o is labelled with the sensor_id its datastream d was registered for."""
    return f"""coalesce({label(o, 'sensor_id')} = ({d}."PROPERTIES"->>'sensor_id'), false)"""


def own_label_sub(o: str) -> str:
    """own_label as a subquery, for a DELETE."""
    return (
        f"""coalesce({label(o, 'sensor_id')} = (SELECT od."PROPERTIES"->>'sensor_id' FROM "DATASTREAMS" od """
        f"""WHERE od."ID" = {o}."DATASTREAM_ID"), false)"""
    )


def int_array(values: Iterable[int | None]) -> str:
    return "'{" + ",".join("NULL" if v is None else str(int(v)) for v in values) + "}'::bigint[]"


def md5_array(values: Iterable[str]) -> str:
    items = list(values)
    if not all(_MD5.fullmatch(v) for v in items):
        raise ValueError("not an md5 hex digest")
    return "'{" + ",".join(items) + "}'::text[]"


def _chunks[T](items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _progress(message: str) -> None:
    sys.stdout.flush()  # keep the report and the progress lines in order on a terminal
    print(message, file=sys.stderr, flush=True)


def _json_lines(lines: Iterable[str], tag: str) -> list[dict[str, Any]]:
    return move.parse_json_lines(lines, tag)


def _tagged(lines: Iterable[str], tag: str, width: int) -> list[list[str]]:
    """The fields of the result lines 'tag|<width fields>|end'."""
    rows = []
    for line in lines:
        fields = line.split("|")
        if fields[0] != tag:
            continue
        if len(fields) != width + 2 or fields[-1] != END:
            raise ValueError(f"unexpected psql output line: {line[:200]!r}")
        rows.append(fields[1:-1])
    return rows


def parse_counts(lines: Iterable[str], tag: str) -> list[tuple[int, int, int]]:
    """(datastream, rows listed, rows changed) per 'deleted' or 'restored' statement."""
    counts = []
    for ds, listed, count in _tagged(lines, tag, 3):
        if int(count) > int(listed):
            raise ValueError(f"datastream {ds}: {count} rows {tag} of {listed} listed")
        counts.append((int(ds), int(listed), int(count)))
    return counts


def _salvage(lines: Sequence[str], tag: str) -> int:
    """Rows changed according to what a failed session printed before it stopped."""
    done = 0
    for line in lines:
        try:
            done += sum(n for _, _, n in parse_counts([line], tag))
        except ValueError:
            _progress(f"  WARNING: unreadable psql output skipped: {line[:80]!r}")
    return done


def run_reads(run_sql: RunSql, statements: Sequence[str], chunk: int, what: str) -> list[str]:
    """Read-only sessions of `chunk` statements each."""
    lines: list[str] = []
    for n, part in enumerate(_chunks(statements, chunk), start=1):
        lines += run_sql(READ_ONLY_SESSION + "".join(part))
        _progress(f"  {what}: {min(n * chunk, len(statements))}/{len(statements)} statements")
    return lines


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Datastream:
    id: int
    name: str
    thing: str
    source: str
    sensor_id: str
    protected: bool


@dataclass
class Catalogue:
    datastreams: dict[int, Datastream]
    bounds: tuple[int, int] | None  # lowest and highest observation ID
    foreign_keys: list[str]  # constraints referencing OBSERVATIONS
    columns: list[str]  # of OBSERVATIONS

    def source(self, ds_id: int) -> str:
        ds = self.datastreams.get(ds_id)
        return (ds.source or "(none)") if ds else "(gone)"

    def name(self, ds_id: int) -> str:
        ds = self.datastreams.get(ds_id)
        return ds.name if ds else "?"

    def protected(self) -> set[int]:
        return {ds.id for ds in self.datastreams.values() if ds.protected}


def catalogue_sql() -> str:
    return f"""
SELECT json_build_object('t', 'ds', 'id', d."ID", 'name', d."NAME", 'thing', th."NAME",
                         'source', th."PROPERTIES"->>'source', 'sensor_id', d."PROPERTIES"->>'sensor_id',
                         'protected', {protected_thing("th")})::text
  FROM "DATASTREAMS" d JOIN "THINGS" th ON th."ID" = d."THING_ID"
 ORDER BY d."ID";
SELECT json_build_object('t', 'bounds', 'min', min("ID"), 'max', max("ID"))::text FROM "OBSERVATIONS";
SELECT json_build_object('t', 'fk', 'name', conrelid::regclass::text || '.' || conname)::text
  FROM pg_constraint WHERE contype = 'f' AND confrelid = '"OBSERVATIONS"'::regclass;
SELECT json_build_object('t', 'col', 'name', attname)::text
  FROM pg_attribute WHERE attrelid = '"OBSERVATIONS"'::regclass AND attnum > 0 AND NOT attisdropped
 ORDER BY attnum;
"""


def parse_catalogue(lines: Sequence[str]) -> Catalogue:
    datastreams = {}
    for r in _json_lines(lines, "ds"):
        ds = Datastream(int(r["id"]), str(r.get("name") or ""), str(r.get("thing") or ""), str(r.get("source") or ""),
                        str(r.get("sensor_id") or "").strip(), bool(r.get("protected")))
        datastreams[ds.id] = ds
    bounds = None
    for r in _json_lines(lines, "bounds"):
        if r.get("min") is not None:
            bounds = (int(r["min"]), int(r["max"]))
    return Catalogue(
        datastreams,
        bounds,
        [str(r["name"]) for r in _json_lines(lines, "fk")],
        [str(r["name"]) for r in _json_lines(lines, "col")],
    )


def load_catalogue(run_sql: RunSql) -> Catalogue:
    return parse_catalogue(run_sql(READ_ONLY_SESSION + catalogue_sql()))


def busy_sql() -> str:
    """Write sessions of the repair scripts other than the asking one (read-only)."""
    names = ", ".join(pressure.sql_literal(n) for n in WRITERS)
    return f"""
SELECT 'busy', pid, application_name, state, coalesce(to_char(now() - query_start, 'HH24:MI:SS'), ''), '{END}'
  FROM pg_stat_activity
 WHERE application_name IN ({names}) AND pid <> pg_backend_pid();
"""


def refuse_if_busy(run_sql: RunSql) -> bool:
    """True, after saying so, when a write session of a repair script is on the server."""
    busy = _tagged(run_sql(READ_ONLY_SESSION + busy_sql()), "busy", 4)
    if not busy:
        return False
    print(f"Refusing to write: {len(busy)} write session(s) of a repair script on the server:")
    for pid, app, state, running in busy:
        print(f"  pid {pid}: {app}, {state}, statement running for {running or '?'}")
    print("One left by an interrupted run of this script can only delete rows that are in its archive. Wait for it "
          "to end (each statement stops within the statement timeout), or end it with "
          "SELECT pg_terminate_backend(<pid>), then re-run.")
    return True


# ---------------------------------------------------------------------------
# Rows read in full, and the archive
# ---------------------------------------------------------------------------


class Candidate(NamedTuple):
    """A row to delete, read in full: one line of the archive."""

    ds: int
    id: int
    md5: str  # md5 of row, as computed by PostgreSQL
    target: int | None  # misrouted leftovers: the datastream holding the same reading
    row: str  # row_to_json of the row, exactly as PostgreSQL printed it
    feature: int  # its FEATURE_ID


def check_row(ds: str | int, obs_id: str | int, digest: str, row: str) -> dict[str, Any]:
    """The row's JSON, after checking it against its md5, ID and datastream."""
    if not _MD5.fullmatch(digest) or hashlib.md5(row.encode("utf-8"), usedforsecurity=False).hexdigest() != digest:
        raise ValueError(f"observation {obs_id}: its text does not match its md5")
    try:
        obj = json.loads(row)
    except json.JSONDecodeError as exc:
        raise ValueError(f"observation {obs_id}: its text is not JSON") from exc
    if not isinstance(obj, dict) or obj.get("ID") != int(obs_id) or obj.get("DATASTREAM_ID") != int(ds):
        raise ValueError(f"observation {obs_id}: the row is not observation {obs_id} of datastream {ds}")
    return obj


def make_candidate(ds: str, obs_id: str, digest: str, target: str, row: str) -> Candidate:
    if not (_INT.fullmatch(ds) and _INT.fullmatch(obs_id) and (not target or _INT.fullmatch(target))):
        raise ValueError(f"unexpected IDs: {ds!r}, {obs_id!r}, {target!r}")
    obj = check_row(ds, obs_id, digest, row)
    feature = obj.get("FEATURE_ID")
    return Candidate(int(ds), int(obs_id), digest, int(target) if target else None, row,
                     int(feature) if isinstance(feature, int) else 0)


def parse_candidates(lines: Sequence[str]) -> list[Candidate]:
    """The 'cand|ds|id|md5|target|row' lines of a read."""
    cands = []
    for line in lines:
        if not line.startswith("cand|"):
            continue
        fields = line.split("|", 5)  # the JSON is last and may contain '|'
        if len(fields) != 6:
            raise ValueError(f"unexpected psql output line: {line[:200]!r}")
        cands.append(make_candidate(*fields[1:]))
    return cands


def candidates_sql(inner: str, target: str = "''") -> str:
    """Rows to delete, in full: ``inner`` selects ds, id and j = row_to_json(o)::text, ordered by id."""
    return f"SELECT 'cand', c.ds, c.id, md5(c.j), {target}, c.j FROM ({inner}) c ORDER BY c.id;\n"


ARCHIVE_HEADER = json.dumps({"archive": APP_NAME, "version": ARCHIVE_VERSION}, separators=(",", ":"))
_ROW_LINE = re.compile(
    r'\{"sel":"([a-z-]+)","run":"([0-9A-Za-z-]+)","ds":([0-9]+),"id":([0-9]+),"h":"([0-9a-f]{32})"'
    r'(?:,"target":([0-9]+))?,"row":'
)


def format_row_line(run_id: str, sel: str, c: Candidate) -> str:
    target = "" if c.target is None else f',"target":{c.target}'
    return f'{{"sel":"{sel}","run":"{run_id}","ds":{c.ds},"id":{c.id},"h":"{c.md5}"{target},"row":{c.row}}}\n'


class ArchivedRow(NamedTuple):
    sel: str
    run: str
    ds: int
    id: int
    md5: str
    target: int | None
    row: str  # verbatim row_to_json text
    feature: int
    columns: tuple[str, ...]


def parse_row_line(line: str) -> ArchivedRow:
    """One archived row, checked against its md5, ID and datastream."""
    match = _ROW_LINE.match(line)
    if match is None or not line.endswith("}"):
        raise ValueError("not an archived row")
    sel, run, ds, obs_id, digest, target = match.groups()
    row = line[match.end() : -1]
    obj = check_row(ds, obs_id, digest, row)
    feature = obj.get("FEATURE_ID")
    return ArchivedRow(sel, run, int(ds), int(obs_id), digest, int(target) if target else None, row,
                       int(feature) if isinstance(feature, int) else 0, tuple(obj))


def _is_gzip(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(2) == b"\x1f\x8b"


def _open_text(path: Path) -> IO[str]:
    if _is_gzip(path):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", newline="")
    return path.open(encoding="utf-8", newline="")


def iter_archive(path: Path) -> Iterator[tuple[int, str]]:
    """(line number, text) of every complete line after the header, which is checked."""
    try:
        with _open_text(path) as handle:
            if handle.readline().rstrip("\r\n") != ARCHIVE_HEADER:
                raise ArchiveError(f"{path} does not start with {ARCHIVE_HEADER}: not an archive of this script")
            for number, line in enumerate(handle, start=2):
                if not line.endswith("\n"):
                    _progress(f"  WARNING: {path}, line {number}: cut-off last line skipped (an interrupted "
                              "append: no DELETE was sent for its page)")
                    return
                yield number, line.rstrip("\n").removesuffix("\r")
    except (EOFError, gzip.BadGzipFile, UnicodeDecodeError) as exc:
        raise ArchiveError(f"{path}: cannot read it: {exc}") from exc


def _append(path: Path, text: str) -> None:
    """Append and fsync: on disk before any DELETE that may remove these rows is sent."""
    with path.open("a", encoding="utf-8", newline="\n") as out:  # LF, also on Windows
        out.write(text)
        out.flush()
        os.fsync(out.fileno())


def _drop_cut_off_line(path: Path) -> None:
    with path.open("rb+") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(size - 1)
        if handle.read(1) == b"\n":
            return
        start = max(0, size - (1 << 22))
        handle.seek(start)
        tail = handle.read()
        cut = tail.rfind(b"\n")
        if cut < 0 and start == 0 and tail == ARCHIVE_HEADER.encode():
            handle.write(b"\n")  # the header without its line end
        elif cut < 0:
            raise ArchiveError(f"{path}: not an archive of this script, or its last line is longer than 4 MiB")
        else:
            _progress(f"  WARNING: {path}: dropping the cut-off last line {tail[cut + 1 :][:80]!r} (an interrupted "
                      "append: no DELETE was sent for its page)")
            handle.truncate(start + cut + 1)
        handle.flush()
        os.fsync(handle.fileno())


def prepare_archive(path: Path) -> set[int]:
    """Create the archive, or check an existing one and drop a cut-off last line.

    Returns the datastreams of rows archived after its last refresh: those of an
    interrupted run, to refresh now.
    """
    if not path.exists() or path.stat().st_size == 0:
        _append(path, ARCHIVE_HEADER + "\n")
        return set()
    if _is_gzip(path):
        raise ArchiveError(f"{path} is compressed: --apply only appends to an uncompressed archive; use a new file")
    _drop_cut_off_line(path)
    pending: set[int] = set()
    for number, line in iter_archive(path):
        if line.startswith('{"sel":'):
            match = _ROW_LINE.match(line)
            if match is None:
                raise ArchiveError(f"{path}, line {number}: unreadable archived row")
            pending.add(int(match[3]))
        elif line.startswith('{"refreshed":'):
            pending.clear()
        elif not line.startswith('{"run":'):
            raise ArchiveError(f"{path}, line {number}: not a line of this archive")
    return pending


def append_rows(path: Path, run_id: str, sel: str, cands: Sequence[Candidate]) -> None:
    _append(path, "".join(format_row_line(run_id, sel, c) for c in cands))


def append_run(path: Path, run_id: str, sel: str, datastreams: Sequence[int] | None) -> None:
    record = {"run": run_id, "selection": sel, "started": datetime.now(UTC).isoformat(),
              "datastreams": sorted(datastreams) if datastreams else "all"}
    _append(path, json.dumps(record, separators=(",", ":")) + "\n")


def append_refreshed(path: Path, run_id: str, datastreams: Iterable[int]) -> None:
    _append(path, json.dumps({"refreshed": run_id, "datastreams": sorted(datastreams)}, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------
# Selections
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    """What a scan found to delete."""

    rows: int = 0
    bytes: int = 0  # of their row_to_json text: the archive
    pages: int = 0  # heap pages holding them: the WAL of the DELETEs
    by_ds: Counter[int] = field(default_factory=Counter)
    fois: Counter[int] = field(default_factory=Counter)  # FEATURE_ID -> rows

    def add(self, ds: int, foi: int | None, rows: int, size: int) -> None:
        if rows:
            self.rows += rows
            self.bytes += size
            self.by_ds[ds] += rows
            if foi is not None:
                self.fois[foi] += rows


@dataclass
class Page:
    """A read of rows to delete: sql(after_id, limit). A paged read continues by ID while it comes back full."""

    sql: Callable[[int, int], str]
    after: int = 0
    paged: bool = True


class Selection:
    name = ""
    summary = ""

    def __init__(self, catalogue: Catalogue, args: argparse.Namespace) -> None:
        self.catalogue = catalogue
        self.args = args
        self.only: set[int] = set(args.datastream or ())
        self.plan = Plan()

    def scan(self, run_sql: RunSql) -> None:
        raise NotImplementedError

    def report(self) -> None:
        raise NotImplementedError

    def pages(self) -> list[Page]:
        raise NotImplementedError

    def recheck(self) -> str:
        """What must still hold for a listed row o (and v.target) when its DELETE runs."""
        raise NotImplementedError

    def _by_source(self, rows: Counter[int]) -> list[tuple[str, int, int]]:
        """(source, datastreams, rows), most rows first."""
        per: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for ds, n in rows.items():
            if n:
                entry = per[self.catalogue.source(ds)]
                entry[0] += 1
                entry[1] += n
        return sorted(((s, d, n) for s, (d, n) in per.items()), key=lambda x: (-x[2], x[0]))

    def _print_datastreams(self, rows: Counter[int], title: str) -> None:
        top = self.args.top
        print(f"\n{title} (top {min(top, len(rows))} of {len(rows)})")
        for ds, n in rows.most_common(top):
            print(f"  {ds:>8} {n:>10,}  {self.catalogue.source(ds):<18} {self.catalogue.name(ds)}")


def _merge_windows(windows: Sequence[tuple[int, int, int]], limit: int, span: int) -> list[tuple[int, int]]:
    """Consecutive ID windows (lo, hi, rows) merged while their rows stay within limit and at most span windows."""
    merged: list[tuple[int, int]] = []
    lo = hi = rows = count = 0
    for w_lo, w_hi, w_rows in windows:
        if count and (rows + w_rows > limit or count >= span or w_lo != hi + 1):
            merged.append((lo, hi))
            count = 0
        if not count:
            lo, rows = w_lo, 0
        hi, rows, count = w_hi, rows + w_rows, count + 1
    if count:
        merged.append((lo, hi))
    return merged


# --tgv-misplaced ------------------------------------------------------------


def tgv_scan_sql(where: str, key: int) -> str:
    """'tgv-' labelled rows selected by ``where`` (on o): per datastream, label, FoI and class, and heap pages to touch."""
    return f"""
WITH c AS (
    SELECT o."ID" AS id, o."DATASTREAM_ID" AS ds, o."FEATURE_ID" AS foi, o."PHENOMENON_TIME_START" AS t,
           {label("o", "sensor_id")} AS sid, {label("o", "observed_property")} AS prop,
           CASE WHEN {protected_thing("th")} THEN 'protected' WHEN NOT {tgv_label("o")} THEN 'other'
                WHEN {own_label("o", "d")} THEN 'own' ELSE 'delete' END AS class,
           length(row_to_json(o)::text) AS len, (o.ctid::text::point)[0] AS page
      FROM "OBSERVATIONS" o
      JOIN "DATASTREAMS" d ON d."ID" = o."DATASTREAM_ID"
      JOIN "THINGS" th ON th."ID" = d."THING_ID"
     WHERE {where} AND {tgv_prefix("o")}
)
SELECT json_build_object('t', 'tgv', 'w', {int(key)}, 'ds', ds, 'sid', sid, 'prop', prop, 'foi', foi, 'class', class,
                         'n', count(*), 'min_id', min(id), 'max_id', max(id), 'first', min(t), 'last', max(t),
                         'bytes', sum(len))::text
  FROM c GROUP BY ds, sid, prop, foi, class
UNION ALL
SELECT json_build_object('t', 'pages', 'w', {int(key)}, 'pages', count(DISTINCT page))::text
  FROM c WHERE class = 'delete';
"""


def tgv_read_sql(where: str, after: int, limit: int) -> str:
    return candidates_sql(f"""
SELECT o."DATASTREAM_ID" AS ds, o."ID" AS id, row_to_json(o)::text AS j
  FROM "OBSERVATIONS" o
  JOIN "DATASTREAMS" d ON d."ID" = o."DATASTREAM_ID"
  JOIN "THINGS" th ON th."ID" = d."THING_ID"
 WHERE {where} AND o."ID" > {int(after)} AND {tgv_label("o")}
   AND NOT {protected_thing("th")} AND NOT {own_label("o", "d")}
 ORDER BY o."ID" LIMIT {int(limit)}""")


@dataclass(frozen=True)
class TgvGroup:
    w: int
    ds: int
    sid: str
    prop: str
    foi: int | None
    cls: str
    n: int
    min_id: int
    max_id: int
    first: str
    last: str
    bytes: int


class TgvSelection(Selection):
    name = TGV
    summary = ("TGV-labelled rows (PARAMETERS sensor_id LIKE 'tgv-%' and location 'tgv', not 'tgv-levellog-%') in "
               "datastreams of Things that are not TGV Things and were not registered for that sensor_id")

    def __init__(self, catalogue: Catalogue, args: argparse.Namespace) -> None:
        super().__init__(catalogue, args)
        self.groups: list[TgvGroup] = []
        self.windows: list[tuple[int, int]] = []

    def scan(self, run_sql: RunSql) -> None:
        if self.only:
            statements = [tgv_scan_sql(f'o."DATASTREAM_ID" = {ds}', ds) for ds in sorted(self.only)]
        elif self.catalogue.bounds is not None:
            self.windows = move.scan_ranges(self.catalogue.bounds, self.args.range_ids)
            statements = [tgv_scan_sql(f'o."ID" BETWEEN {lo} AND {hi}', lo) for lo, hi in self.windows]
        else:
            statements = []
        lines = run_reads(run_sql, statements, self.args.chunk, "scanning")
        self.groups = [
            TgvGroup(int(r["w"]), int(r["ds"]), str(r.get("sid") or ""), str(r.get("prop") or ""),
                     None if r.get("foi") is None else int(r["foi"]), str(r["class"]), int(r["n"]), int(r["min_id"]),
                     int(r["max_id"]), str(r.get("first") or ""), str(r.get("last") or ""), int(r.get("bytes") or 0))
            for r in _json_lines(lines, "tgv")
        ]
        for g in self.groups:
            if g.cls == "delete":
                self.plan.add(g.ds, g.foi, g.n, g.bytes)
        self.plan.pages = sum(int(r["pages"]) for r in _json_lines(lines, "pages"))

    def report(self) -> None:
        by_class: Counter[str] = Counter()
        for g in self.groups:
            by_class[g.cls] += g.n
        delete = [g for g in self.groups if g.cls == "delete"]
        print(f"\nRows with a 'tgv-' sensor_id found: {sum(by_class.values()):,}")
        print(f"  to delete: {by_class['delete']:,} in {len(self.plan.by_ds):,} datastreams")
        print(f"  left alone, in datastreams of TGV Things: {by_class['protected']:,}")
        print(f"  left alone, in a datastream registered for their own sensor_id: {by_class['own']:,}")
        print(f"  left alone, not a TGV Kafka label (a Levellog sensor, or location not 'tgv'): {by_class['other']:,}")
        if not delete:
            return
        span: dict[str, list[str]] = defaultdict(list)
        for g in delete:
            span[self.catalogue.source(g.ds)] += [g.first, g.last]
        print("\nTo delete, by source of the datastream's Thing")
        print(f"  {'source':<18}{'datastreams':>12}{'rows':>12}  phenomenonTime")
        for source, n_ds, rows in self._by_source(self.plan.by_ds):
            times = sorted(t for t in span[source] if t)
            print(f"  {source:<18}{n_ds:>12,}{rows:>12,}  {times[0] if times else '-'} .. {times[-1] if times else '-'}")
        labels: Counter[tuple[str, str]] = Counter()
        for g in delete:
            labels[(g.sid, g.prop)] += g.n
        print("\nTo delete, by label (sensor_id, observed_property)")
        for (sid, prop), rows in sorted(labels.items()):
            print(f"  {sid:<44}{prop:<22}{rows:>12,}")
        self._print_datastreams(self.plan.by_ds, "To delete, by datastream")

    def pages(self) -> list[Page]:
        args = self.args
        if self.only:
            spans: dict[int, list[int]] = {}
            for g in self.groups:
                if g.cls == "delete":
                    lo, hi = spans.setdefault(g.ds, [g.min_id, g.max_id])
                    spans[g.ds] = [min(lo, g.min_id), max(hi, g.max_id)]
            return [Page(_tgv_page(f'o."DATASTREAM_ID" = {ds} AND o."ID" <= {hi}'), lo - 1)
                    for ds, (lo, hi) in sorted(spans.items())]
        rows: Counter[int] = Counter()
        for g in self.groups:
            if g.cls == "delete":
                rows[g.w] += g.n
        windows = [(lo, hi, rows[lo]) for lo, hi in self.windows if rows[lo]]
        return [Page(_tgv_page(f'o."ID" <= {hi}'), lo - 1) for lo, hi in _merge_windows(windows, args.page_rows, 5)]

    def recheck(self) -> str:
        return f"{tgv_label('o')} AND NOT {own_label_sub('o')}"


def _tgv_page(where: str) -> Callable[[int, int], str]:
    return lambda after, limit: tgv_read_sql(where, after, limit)


# --duplicates ---------------------------------------------------------------


def count_sql(lo: int, hi: int) -> str:
    """Rows per datastream (over the DATASTREAM_ID index)."""
    return f"""
SELECT json_build_object('t', 'n', 'ds', "DATASTREAM_ID", 'n', count(*))::text
  FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" BETWEEN {int(lo)} AND {int(hi)} GROUP BY "DATASTREAM_ID";
"""


def dup_chunks(counts: dict[int, int], protected: set[int], max_rows: int, *, contiguous: bool = True) -> list[tuple[int, int]]:
    """Datastream ID ranges over the datastreams with rows, of at most max_rows rows each (a datastream is never
    split), none of them containing a TGV datastream. Without contiguous, one range per datastream."""
    guards = sorted(protected)
    chunks: list[tuple[int, int]] = []
    lo = hi = rows = 0
    for ds in sorted(counts):
        n = counts[ds]
        if not n or ds in protected:
            continue
        if rows:
            between = bisect.bisect_right(guards, hi)
            crosses = between < len(guards) and guards[between] < ds
            if not contiguous or crosses or rows + n > max_rows:
                chunks.append((lo, hi))
                rows = 0
        if not rows:
            lo = ds
        hi, rows = ds, rows + n
    if rows:
        chunks.append((lo, hi))
    return chunks


def dup_cte(lo: int, hi: int, *, scan: bool) -> str:
    """Rows of datastreams lo..hi that share their datastream and phenomenonTime with another row, with
    k: their rank among the copies of their reading (k > 1: a duplicate of a row with a lower ID), and
    n_keys: the number of different readings at that time (> 1: a conflict group)."""
    cols = ", ".join(f'y."{c}"' for c in ("ID", "DATASTREAM_ID", "PHENOMENON_TIME_START", "FEATURE_ID",
                                          *READING_COLUMNS, "PARAMETERS"))
    extra = ", length(row_to_json(y)::text) AS len, (y.ctid::text::point)[0] AS page" if scan else ""
    return f"""
WITH g AS MATERIALIZED (
    SELECT "DATASTREAM_ID" AS ds, "PHENOMENON_TIME_START" AS t
      FROM "OBSERVATIONS"
     WHERE "DATASTREAM_ID" BETWEEN {int(lo)} AND {int(hi)}
     GROUP BY 1, 2
    HAVING count(*) > 1
), r AS (
    SELECT x.*, row_number() OVER (PARTITION BY x."DATASTREAM_ID", x."PHENOMENON_TIME_START", {reading_key("x")}
                                   ORDER BY x."ID") AS k
      FROM g CROSS JOIN LATERAL (SELECT {cols}{extra} FROM "OBSERVATIONS" y
                                  WHERE y."DATASTREAM_ID" = g.ds AND y."PHENOMENON_TIME_START" = g.t) x
), s AS (
    SELECT r.*, count(*) FILTER (WHERE r.k = 1) OVER (PARTITION BY r."DATASTREAM_ID", r."PHENOMENON_TIME_START")
               AS n_keys
      FROM r
)"""


DELETABLE_DUP = "s.k > 1 AND s.n_keys = 1"


def dup_scan_sql(lo: int, hi: int) -> str:
    return dup_cte(lo, hi, scan=True) + f"""
SELECT json_build_object('t', 'dup', 'ds', a.ds, 'day', a.day, 'rows', a.n, 'dups', a.dups, 'bytes', a.bytes,
                         'conflict_groups', a.cg, 'conflict_rows', a.cr, 'conflict_twins', a.ct)::text
  FROM (SELECT s."DATASTREAM_ID" AS ds, to_char(s."PHENOMENON_TIME_START" AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day,
               count(*) AS n,
               count(*) FILTER (WHERE {DELETABLE_DUP}) AS dups,
               coalesce(sum(s.len) FILTER (WHERE {DELETABLE_DUP}), 0) AS bytes,
               count(DISTINCT s."PHENOMENON_TIME_START") FILTER (WHERE s.n_keys > 1) AS cg,
               count(*) FILTER (WHERE s.n_keys > 1) AS cr,
               count(*) FILTER (WHERE s.k > 1 AND s.n_keys > 1) AS ct
          FROM s GROUP BY 1, 2) a
UNION ALL
SELECT json_build_object('t', 'dupfoi', 'foi', s."FEATURE_ID", 'n', count(*))::text
  FROM s WHERE {DELETABLE_DUP} GROUP BY s."FEATURE_ID"
UNION ALL
SELECT json_build_object('t', 'duppages', 'pages', count(DISTINCT s.page))::text
  FROM s WHERE {DELETABLE_DUP};
"""


def dup_read_sql(lo: int, hi: int, after: int, limit: int) -> str:
    return dup_cte(lo, hi, scan=False) + candidates_sql(f"""
SELECT o."DATASTREAM_ID" AS ds, o."ID" AS id, row_to_json(o)::text AS j
  FROM s JOIN "OBSERVATIONS" o ON o."ID" = s."ID"
 WHERE {DELETABLE_DUP} AND s."ID" > {int(after)} AND NOT {protected_ds('o."DATASTREAM_ID"')}
 ORDER BY o."ID" LIMIT {int(limit)}""")


def dup_recheck() -> str:
    """A copy with a lower ID still exists, and no other reading is at that time."""
    same_time = ('k."DATASTREAM_ID" = o."DATASTREAM_ID" AND k."PHENOMENON_TIME_START" = o."PHENOMENON_TIME_START"')
    return (
        f'EXISTS (SELECT 1 FROM "OBSERVATIONS" k WHERE {same_time} AND k."ID" < o."ID" AND {same_reading("k", "o")})'
        f' AND NOT EXISTS (SELECT 1 FROM "OBSERVATIONS" k WHERE {same_time} AND NOT ({same_reading("k", "o")}))'
    )


class DuplicateSelection(Selection):
    name = DUPLICATES
    summary = ("exact duplicates within a datastream: same DATASTREAM_ID, PHENOMENON_TIME_START/END, "
               "RESULT_TYPE/NUMBER/STRING/BOOLEAN/JSON and label (sensor_id, observed_property, sensor_name); "
               "the lowest ID stays")

    def __init__(self, catalogue: Catalogue, args: argparse.Namespace) -> None:
        super().__init__(catalogue, args)
        self.counts: dict[int, int] = {}
        self.chunks: list[tuple[int, int]] = []
        self.chunk_dups: Counter[int] = Counter()  # by index into chunks
        self.lines: list[dict[str, Any]] = []

    def scan(self, run_sql: RunSql) -> None:
        args, catalogue = self.args, self.catalogue
        if self.only:
            windows = [(ds, ds) for ds in sorted(self.only)]
        else:
            first, last = min(catalogue.datastreams, default=1), max(catalogue.datastreams, default=0)
            step = args.count_datastreams
            windows = [(lo, min(lo + step - 1, last)) for lo in range(first, last + 1, step)]
        counted = run_reads(run_sql, [count_sql(lo, hi) for lo, hi in windows], args.chunk, "counting rows per datastream")
        self.counts = {int(r["ds"]): int(r["n"]) for r in _json_lines(counted, "n")}
        self.chunks = dup_chunks(self.counts, catalogue.protected(), args.chunk_rows, contiguous=not self.only)
        starts = [lo for lo, _ in self.chunks]
        lines = run_reads(run_sql, [dup_scan_sql(lo, hi) for lo, hi in self.chunks], args.chunk,
                          "scanning datastream ranges for duplicates")
        for r in _json_lines(lines, "dup"):
            self.lines.append(r)
            ds = int(r["ds"])
            self.plan.add(ds, None, int(r["dups"]), int(r["bytes"]))
            self.chunk_dups[bisect.bisect_right(starts, ds) - 1] += int(r["dups"])
        for r in _json_lines(lines, "dupfoi"):
            self.plan.fois[int(r["foi"])] += int(r["n"])
        self.plan.pages = sum(int(r["pages"]) for r in _json_lines(lines, "duppages"))

    def report(self) -> None:
        catalogue = self.catalogue
        skipped = sorted(ds for ds in self.counts if ds in catalogue.protected() and self.counts[ds])
        groups = sum(int(r["rows"]) for r in self.lines)
        cg = sum(int(r["conflict_groups"]) for r in self.lines)
        cr = sum(int(r["conflict_rows"]) for r in self.lines)
        ct = sum(int(r["conflict_twins"]) for r in self.lines)
        print(f"\nScanned {sum(self.counts.values()):,} rows in {len(self.counts):,} datastreams "
              f"({len(self.chunks):,} ranges); left alone: {len(skipped):,} datastreams of TGV Things "
              f"({sum(self.counts[ds] for ds in skipped):,} rows)")
        print(f"  rows sharing their datastream and phenomenonTime with another row: {groups:,}")
        print(f"  exact duplicates to delete: {self.plan.rows:,} in {len(self.plan.by_ds):,} datastreams")
        print(f"  conflicts (same datastream and phenomenonTime, another value or label): {cg:,} times, {cr:,} rows; "
              "never deleted")
        print(f"    of those rows, exact twins of another row of their group (left as well): {ct:,}")
        per: dict[str, Counter[str]] = defaultdict(Counter)
        with_dups: dict[str, set[int]] = defaultdict(set)
        days: dict[str, Counter[str]] = defaultdict(Counter)
        conflict_days: dict[str, Counter[str]] = defaultdict(Counter)
        for r in self.lines:
            source = catalogue.source(int(r["ds"]))
            for key in ("dups", "conflict_groups", "conflict_rows", "conflict_twins"):
                per[source][key] += int(r[key])
            if int(r["dups"]):
                with_dups[source].add(int(r["ds"]))
            days[str(r["day"])][source] += int(r["dups"])
            conflict_days[str(r["day"])][source] += int(r["conflict_rows"])
        sources = sorted(per, key=lambda s: (-per[s]["dups"], s))
        print("\nBy source")
        print(f"  {'source':<18}{'datastreams':>12}{'duplicates':>12}{'conflict times':>16}{'conflict rows':>15}"
              f"{'twins in them':>15}")
        for source in sources:
            c = per[source]
            print(f"  {source:<18}{len(with_dups[source]):>12,}{c['dups']:>12,}{c['conflict_groups']:>16,}"
                  f"{c['conflict_rows']:>15,}{c['conflict_twins']:>15,}")
        for title, table in (("Duplicates to delete", days), ("Conflict rows (left)", conflict_days)):
            print(f"\n{title}, by day of phenomenonTime (UTC) and source")
            print("  " + f"{'day':<12}" + "".join(f"{s[:16]:>18}" for s in sources) + f"{'total':>12}")
            for day in sorted(table):
                row = table[day]
                if sum(row.values()):
                    print("  " + f"{day:<12}" + "".join(f"{row[s]:>18,}" for s in sources) + f"{sum(row.values()):>12,}")
        self._print_datastreams(self.plan.by_ds, "Duplicates to delete, by datastream")

    def pages(self) -> list[Page]:
        return [Page(_dup_page(lo, hi)) for i, (lo, hi) in enumerate(self.chunks) if self.chunk_dups[i]]

    def recheck(self) -> str:
        return dup_recheck()


def _dup_page(lo: int, hi: int) -> Callable[[int, int], str]:
    return lambda after, limit: dup_read_sql(lo, hi, after, limit)


# --misrouted-leftovers ------------------------------------------------------

LEFTOVER_ACTIONS = (move.MOVE, move.MOVE_BLOCKED)


def _twin_in(target: str, o: str = "o") -> str:
    return (f'EXISTS (SELECT 1 FROM "OBSERVATIONS" x WHERE x."DATASTREAM_ID" = {target} '
            f'AND x."PHENOMENON_TIME_START" = {o}."PHENOMENON_TIME_START" AND {same_reading("x", o)})')


def _group_rows(g: move.Group) -> str:
    return (f'o."DATASTREAM_ID" = {int(g.ds)} AND o."ID" BETWEEN {int(g.min_id)} AND {int(g.max_id)} '
            f"AND {move.label_filter('o', g)} AND NOT {protected_ds(str(int(g.ds)))}")


def leftover_scan_sql(i: int, planned: move.PlannedGroup) -> str:
    """Rows of one planned group whose target already has their phenomenonTime: the same reading or another."""
    target = planned.target
    assert target is not None
    return f"""
SELECT json_build_object('t', 'lo', 'i', {int(i)}, 'foi', a.foi, 'twins', a.twins, 'conflicts', a.conflicts,
                         'bytes', a.bytes, 'pages', a.pages)::text
  FROM (SELECT o."FEATURE_ID" AS foi,
               count(*) FILTER (WHERE m.twin) AS twins,
               count(*) FILTER (WHERE m.clash AND NOT m.twin) AS conflicts,
               coalesce(sum(length(row_to_json(o)::text)) FILTER (WHERE m.twin), 0) AS bytes,
               count(DISTINCT (o.ctid::text::point)[0]) FILTER (WHERE m.twin) AS pages
          FROM "OBSERVATIONS" o
          CROSS JOIN LATERAL (
              SELECT {_twin_in(str(int(target.id)))} AS twin,
                     EXISTS (SELECT 1 FROM "OBSERVATIONS" x WHERE x."DATASTREAM_ID" = {int(target.id)}
                                AND x."PHENOMENON_TIME_START" = o."PHENOMENON_TIME_START") AS clash) m
         WHERE {_group_rows(planned.group)}
         GROUP BY o."FEATURE_ID") a
 WHERE a.twins > 0 OR a.conflicts > 0;
"""


def leftover_read_sql(planned: move.PlannedGroup, after: int, limit: int) -> str:
    target = planned.target
    assert target is not None
    return candidates_sql(f"""
SELECT o."DATASTREAM_ID" AS ds, o."ID" AS id, row_to_json(o)::text AS j
  FROM "OBSERVATIONS" o
 WHERE {_group_rows(planned.group)} AND o."ID" > {int(after)} AND {_twin_in(str(int(target.id)))}
 ORDER BY o."ID" LIMIT {int(limit)}""", target=str(int(target.id)))


class LeftoverSelection(Selection):
    name = LEFTOVERS
    summary = ("rows the move script leaves in place because the datastream matching their label already has the "
               "same reading (label, phenomenon time and result)")

    def __init__(self, catalogue: Catalogue, args: argparse.Namespace) -> None:
        super().__init__(catalogue, args)
        self.move_plan = move.Plan()
        self.targets: list[move.PlannedGroup] = []
        self.found: dict[int, Counter[str]] = defaultdict(Counter)  # by index into targets

    def scan(self, run_sql: RunSql) -> None:
        args = self.args
        mcat, bounds = move.parse_catalogue(run_sql(READ_ONLY_SESSION + move.catalogue_sql()), args.prefix)
        if self.only:
            statements = [move.datastream_scan_sql(ds, True) for ds in sorted(self.only)]
        elif bounds is not None:
            statements = [move.range_scan_sql(lo, hi, True) for lo, hi in move.scan_ranges(bounds, args.range_ids)]
        else:
            statements = []
        lines = run_reads(run_sql, statements, args.chunk, "scanning with the move script's conditions")
        self.move_plan = move.build_plan(move.merge_groups(move.parse_groups(lines)), mcat)
        protected = self.catalogue.protected()
        self.targets = [p for p in self.move_plan.groups
                        if p.action in LEFTOVER_ACTIONS and p.target is not None and p.group.ds not in protected]
        statements = [leftover_scan_sql(i, p) for i, p in enumerate(self.targets)]
        for r in _json_lines(run_reads(run_sql, statements, args.chunk, "checking their targets"), "lo"):
            i, foi = int(r["i"]), None if r.get("foi") is None else int(r["foi"])
            for key in ("twins", "conflicts"):
                self.found[i][key] += int(r[key])
            self.plan.add(self.targets[i].group.ds, foi, int(r["twins"]), int(r["bytes"]))
            self.plan.pages += int(r["pages"])

    def report(self) -> None:
        by_action = self.move_plan.by_action()
        print("\nThe move script's plan (scripts/fix_sc_misrouted_observations.py), misrouted rows")
        print(f"  {'action':<18}{'groups':>8}{'rows':>12}")
        for action in move.ACTIONS:
            items = by_action[action]
            print(f"  {action:<18}{len(items):>8}{sum(p.group.rows for p in items):>12,}")
        twins = sum(c["twins"] for c in self.found.values())
        conflicts = sum(c["conflicts"] for c in self.found.values())
        print(f"\nOf the {len(self.targets):,} groups with a target datastream, rows whose target already has their "
              f"phenomenonTime (the move script leaves them in place): {twins + conflicts:,}")
        print(f"  the same reading (label, phenomenon time and result) is in the target: {twins:,}: to delete")
        print(f"  another reading at that time in the target (conflict): {conflicts:,}: left")
        rows = [(i, c) for i, c in sorted(self.found.items()) if c["twins"] or c["conflicts"]]
        if rows:
            print(f"\nBy group (top {min(self.args.top, len(rows))} of {len(rows)}): source -> target, label, "
                  "to delete, conflicts")
            for i, c in sorted(rows, key=lambda x: -(x[1]["twins"] + x[1]["conflicts"]))[: self.args.top]:
                p = self.targets[i]
                assert p.target is not None
                print(f"  {p.group.ds:>8} -> {p.target.id:<8} {p.action:<13} {p.group.sid or ''} / "
                      f"{p.group.prop or ''}  {c['twins']:>8,} {c['conflicts']:>8,}")

    def pages(self) -> list[Page]:
        return [Page(_leftover_page(p), p.group.min_id - 1) for i, p in enumerate(self.targets)
                if self.found[i]["twins"]]

    def recheck(self) -> str:
        return _twin_in("v.target")


def _leftover_page(planned: move.PlannedGroup) -> Callable[[int, int], str]:
    return lambda after, limit: leftover_read_sql(planned, after, limit)


# --ids-file -----------------------------------------------------------------


def read_ids_file(path: Path) -> list[tuple[int, int | None]]:
    """(observation ID, listed datastream or None), sorted and distinct."""
    with path.open(encoding="utf-8-sig", newline="") as handle:
        lines = [line for line in handle if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        return []
    listed: list[tuple[int, int | None]] = []
    if _INT.fullmatch(lines[0].strip()):
        for number, line in enumerate(lines, start=1):
            if not _INT.fullmatch(line.strip()):
                raise IdsFileError(f"{path}: {line.strip()[:80]!r} (content line {number}) is not an observation ID")
            listed.append((int(line.strip()), None))
    else:
        reader = csv.DictReader(lines)
        names = [n.strip() for n in reader.fieldnames or []]
        reader.fieldnames = names
        id_col = next((n for n in ("observation_id", "id", "@iot.id") if n in names), None)
        if id_col is None:
            raise IdsFileError(f"{path}: the header {names} has no observation_id (or id) column")
        ds_col = "datastream_id" if "datastream_id" in names else None
        for number, row in enumerate(reader, start=2):
            obs, ds = (row.get(id_col) or "").strip(), (row.get(ds_col) or "").strip() if ds_col else ""
            if not _INT.fullmatch(obs) or (ds and not _INT.fullmatch(ds)):
                raise IdsFileError(f"{path}, content line {number}: not an observation and datastream ID: {row}")
            listed.append((int(obs), int(ds) if ds else None))
    distinct: dict[int, int | None] = {}
    for obs_id, ds_id in listed:
        if distinct.setdefault(obs_id, ds_id) != ds_id:
            raise IdsFileError(f"{path}: observation {obs_id} is listed with different datastreams")
    return sorted(distinct.items())


def _ids_rows(batch: Sequence[tuple[int, int | None]]) -> str:
    return (f'unnest({int_array(i for i, _ in batch)}, {int_array(d for _, d in batch)}) AS v(id, ds) '
            'JOIN "OBSERVATIONS" o ON o."ID" = v.id')


def twin_sql(o: str, prefix: str) -> str:
    """--require-twin: another row holds the same reading as row o (the --duplicates definition, a KNMI row's
    station by thing_name): in o's datastream with a lower ID, or in another datastream of o's Thing or of the Thing
    o's thing_name label names. The lower ID keeps one copy of rows listed together in one DELETE."""
    return f"""EXISTS (SELECT 1 FROM "OBSERVATIONS" x
         WHERE x."DATASTREAM_ID" IN (
                   SELECT td."ID" FROM "DATASTREAMS" td
                    WHERE td."THING_ID" = (SELECT sd."THING_ID" FROM "DATASTREAMS" sd WHERE sd."ID" = {o}."DATASTREAM_ID")
                       OR td."THING_ID" IN (SELECT tt."ID" FROM "THINGS" tt
                                             WHERE tt."NAME" = {pressure.sql_literal(prefix)} || {label(o, 'thing_name')}))
           AND x."PHENOMENON_TIME_START" = {o}."PHENOMENON_TIME_START"
           AND (x."DATASTREAM_ID" <> {o}."DATASTREAM_ID" OR x."ID" < {o}."ID")
           AND {same_reading("x", o, by_station=True)})"""


def ids_class(only: set[int], twin: str | None = None) -> str:
    outside = f'o."DATASTREAM_ID" <> ALL({int_array(sorted(only))})' if only else "false"
    no_twin = f"WHEN NOT {twin} THEN 'no-twin' " if twin else ""
    return (f"""CASE WHEN v.ds IS NOT NULL AND v.ds <> o."DATASTREAM_ID" THEN 'elsewhere' """
            f"""WHEN {outside} THEN 'outside' WHEN {protected_ds('o."DATASTREAM_ID"')} THEN 'protected' """
            f"""{no_twin}ELSE 'delete' END""")


def ids_scan_sql(batch: Sequence[tuple[int, int | None]], only: set[int], twin: str | None = None) -> str:
    return f"""
SELECT json_build_object('t', 'ids', 'ds', a.ds, 'class', a.class, 'foi', a.foi, 'n', a.n, 'bytes', a.bytes,
                         'pages', a.pages)::text
  FROM (SELECT o."DATASTREAM_ID" AS ds, {ids_class(only, twin)} AS class, o."FEATURE_ID" AS foi, count(*) AS n,
               sum(length(row_to_json(o)::text)) AS bytes, count(DISTINCT (o.ctid::text::point)[0]) AS pages
          FROM {_ids_rows(batch)}
         GROUP BY 1, 2, 3) a;
"""


def ids_read_sql(batch: Sequence[tuple[int, int | None]], only: set[int], twin: str | None = None) -> str:
    return candidates_sql(f"""
SELECT o."DATASTREAM_ID" AS ds, o."ID" AS id, row_to_json(o)::text AS j
  FROM {_ids_rows(batch)}
 WHERE {ids_class(only, twin)} = 'delete'""")


class IdsSelection(Selection):
    name = IDS
    summary = "the observations listed in the IDs file"

    def __init__(self, catalogue: Catalogue, args: argparse.Namespace) -> None:
        super().__init__(catalogue, args)
        self.listed = read_ids_file(args.ids_file)
        self.batches = _chunks(self.listed, args.page_rows)
        self.by_class: Counter[str] = Counter()
        self.twin = twin_sql("o", args.prefix) if args.require_twin else None
        if self.twin:
            self.summary += (", each only while another row holds the same reading: in its datastream, or in another "
                             "datastream of its Thing or of the Thing its thing_name names")

    def scan(self, run_sql: RunSql) -> None:
        statements = [ids_scan_sql(batch, self.only, self.twin) for batch in self.batches]
        for r in _json_lines(run_reads(run_sql, statements, self.args.chunk, "checking the listed rows"), "ids"):
            self.by_class[str(r["class"])] += int(r["n"])
            if r["class"] == "delete":
                foi = None if r.get("foi") is None else int(r["foi"])
                self.plan.add(int(r["ds"]), foi, int(r["n"]), int(r["bytes"]))
                self.plan.pages += int(r["pages"])

    def report(self) -> None:
        found = sum(self.by_class.values())
        with_ds = sum(1 for _, ds in self.listed if ds is not None)
        print(f"\nIDs file {self.args.ids_file}: {len(self.listed):,} distinct observations "
              f"({with_ds:,} with a datastream)")
        print(f"  found: {found:,}; not found (deleted already?): {len(self.listed) - found:,}")
        print(f"  to delete: {self.by_class['delete']:,}")
        print(f"  left alone, in datastreams of TGV Things: {self.by_class['protected']:,}")
        print(f"  left alone, no longer in their listed datastream: {self.by_class['elsewhere']:,}")
        if self.twin:
            print(f"  left alone, no other row holds the same reading (--require-twin): {self.by_class['no-twin']:,}")
        if self.only:
            print(f"  left alone, outside --datastream: {self.by_class['outside']:,}")
        if self.plan.rows:
            print("\nTo delete, by source")
            for source, n_ds, rows in self._by_source(self.plan.by_ds):
                print(f"  {source:<18}{n_ds:>8,} datastreams{rows:>12,} rows")
            self._print_datastreams(self.plan.by_ds, "To delete, by datastream")

    def pages(self) -> list[Page]:
        return ([Page(_ids_page(batch, self.only, self.twin), paged=False) for batch in self.batches]
                if self.plan.rows else [])

    def recheck(self) -> str:
        return self.twin or "true"


def _ids_page(batch: Sequence[tuple[int, int | None]], only: set[int], twin: str | None) -> Callable[[int, int], str]:
    return lambda after, limit: ids_read_sql(batch, only, twin)


SELECTIONS: dict[str, type[Selection]] = {
    TGV: TgvSelection,
    DUPLICATES: DuplicateSelection,
    LEFTOVERS: LeftoverSelection,
    IDS: IdsSelection,
}


def selection_name(args: argparse.Namespace) -> str:
    if args.tgv_misplaced:
        return TGV
    if args.duplicates:
        return DUPLICATES
    if args.misrouted_leftovers:
        return LEFTOVERS
    return IDS


def make_selection(args: argparse.Namespace, catalogue: Catalogue) -> Selection:
    return SELECTIONS[selection_name(args)](catalogue, args)


# ---------------------------------------------------------------------------
# Delete, refresh, FeaturesOfInterest
# ---------------------------------------------------------------------------


def delete_sql(ds: int, cands: Sequence[Candidate], recheck: str) -> str:
    """One batch: delete the listed rows of one datastream that are still exactly as archived and still selected."""
    return f"""
WITH del AS (
    DELETE FROM "OBSERVATIONS" o
     USING unnest({int_array(c.id for c in cands)}, {md5_array(c.md5 for c in cands)},
                  {int_array(c.target for c in cands)}) AS v(id, h, target)
     WHERE o."ID" = v.id AND o."DATASTREAM_ID" = {int(ds)}
       AND md5(row_to_json(o)::text) = v.h
       AND NOT {protected_ds('o."DATASTREAM_ID"')}
       AND {recheck}
    RETURNING o."ID"
)
SELECT 'deleted', {int(ds)}, {len(cands)}, count(*), '{END}' FROM del;
"""


def refresh_session(ds_ids: Sequence[int]) -> str:
    return WRITE_SESSION + "".join(
        move.refresh_sql(ds) + f"SELECT 'refreshed', {int(ds)}, '{END}';\n" for ds in ds_ids
    )


def refresh(run_sql: RunSql, ds_ids: Iterable[int], chunk: int) -> int:
    """Recompute phenomenonTime and resultTime of these datastreams from their rows, one UPDATE each."""
    ids = sorted(set(ds_ids))
    for n, part in enumerate(_chunks(ids, chunk), start=1):
        done = {int(f[0]) for f in _tagged(run_sql(refresh_session(part)), "refreshed", 1)}
        if done != set(part):
            raise PsqlError(f"refresh: no confirmation for datastreams {sorted(set(part) - done)}")
        _progress(f"  refreshed {min(n * chunk, len(ids))}/{len(ids)} datastreams")
    return len(ids)


def foi_sql(counts: Sequence[tuple[int, int]]) -> str:
    """Per FoI: how many rows reference it, counted up to n + 1, and whether a Location generated it."""
    return f"""
SELECT json_build_object('t', 'foi', 'foi', v.f, 'n', v.n,
       'total', (SELECT count(*) FROM (SELECT 1 FROM "OBSERVATIONS" x WHERE x."FEATURE_ID" = v.f LIMIT v.n + 1) c),
       'generated', EXISTS (SELECT 1 FROM "LOCATIONS" l WHERE l."GEN_FOI_ID" = v.f))::text
  FROM unnest({int_array(f for f, _ in counts)}, {int_array(n for _, n in counts)}) AS v(f, n);
"""


def orphaned_fois(run_sql: RunSql, counts: dict[int, int], chunk: int) -> tuple[list[int], int]:
    """FoIs all of whose rows are among the counted ones: (their IDs, how many of them a Location generated)."""
    statements, part, rows = [], [], 0
    for foi, n in sorted(counts.items()):
        if part and rows + n > 1_000_000:  # rows to count per statement
            statements.append(foi_sql(part))
            part, rows = [], 0
        part.append((foi, n))
        rows += n
    if part:
        statements.append(foi_sql(part))
    found = _json_lines(run_reads(run_sql, statements, chunk, "checking FeaturesOfInterest"), "foi")
    orphans = [r for r in found if int(r["total"]) <= int(r["n"])]
    return sorted(int(r["foi"]) for r in orphans), sum(1 for r in orphans if r["generated"])


def _ids_text(ids: Sequence[int], limit: int = 20) -> str:
    return "" if not ids else f" {list(ids)}" if len(ids) <= limit else f" (first {limit}: {list(ids[:limit])})"


@dataclass
class Applied:
    archived: int = 0
    deleted: int = 0
    kept: int = 0  # listed, but changed or no longer selected when their DELETE ran
    touched: set[int] = field(default_factory=set)
    fois: set[int] = field(default_factory=set)


def apply_pages(run_sql: RunSql, sel: Selection, archive: Path, run_id: str) -> Applied:
    """Per page: read the rows (read-only), archive them (fsync), then delete them in batches (one write session)."""
    args = sel.args
    done = Applied()
    recheck = sel.recheck()
    for page in sel.pages():
        after = page.after
        while True:
            try:
                cands = parse_candidates(run_sql(READ_ONLY_SESSION + page.sql(after, args.page_rows)))
            except PsqlError as exc:
                raise PsqlError(f"{exc}\n  (while reading rows to delete: nothing of this page was deleted; rows "
                                f"deleted before it: {done.deleted:,}; re-running is safe)") from exc
            if not cands:
                break
            try:
                append_rows(archive, run_id, sel.name, cands)
            except OSError as exc:
                raise ArchiveError(f"cannot append to {archive}: {exc} (nothing of this page was deleted; rows "
                                   f"deleted before it: {done.deleted:,})") from exc
            done.archived += len(cands)
            done.fois.update(c.feature for c in cands)
            by_ds: dict[int, list[Candidate]] = defaultdict(list)
            for c in cands:
                by_ds[c.ds].append(c)
            statements = [delete_sql(ds, batch, recheck)
                          for ds, rows in sorted(by_ds.items()) for batch in _chunks(rows, args.batch_rows)]
            try:
                lines = run_sql(WRITE_SESSION + "".join(statements))
            except PsqlError as exc:
                salvaged = done.deleted + _salvage(exc.lines, "deleted")
                raise PsqlError(
                    f"{exc}\n  Rows deleted before the error, as far as psql reported: {salvaged:,}. Every row this "
                    f"session can delete, also on the server after the client gave up, is already in {archive}. "
                    "Re-running is safe (use a smaller --batch-rows if a statement timed out); it refuses to start "
                    "while a write session of a repair script is still on the server."
                ) from exc
            counts = parse_counts(lines, "deleted")
            if len(counts) != len(statements):
                raise PsqlError(f"{len(statements)} DELETE statements sent, {len(counts)} confirmed")
            for ds, listed, count in counts:
                done.deleted += count
                done.kept += listed - count
                if count:
                    done.touched.add(ds)
            _progress(f"  deleted {done.deleted:,} rows so far, {done.archived:,} archived")
            if not page.paged or len(cands) < args.page_rows:
                break
            after = max(c.id for c in cands)
    return done


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class _IdSet:
    """Observation IDs: a bitmap below 2**28 (FROST numbers them from 1), a set above."""

    LIMIT = 1 << 28

    def __init__(self) -> None:
        self.bits = bytearray()
        self.big: set[int] = set()

    def add(self, value: int) -> bool:
        """Add; False when it was there already."""
        if 0 <= value < self.LIMIT:
            byte, bit = value >> 3, 1 << (value & 7)
            if byte >= len(self.bits):
                self.bits.extend(bytes(byte + 1 - len(self.bits) + (1 << 16)))
            if self.bits[byte] & bit:
                return False
            self.bits[byte] |= bit
            return True
        if value in self.big:
            return False
        self.big.add(value)
        return True


@dataclass
class ArchiveIndex:
    rows: int = 0  # archived row lines
    repeated: dict[int, int] = field(default_factory=dict)  # ID archived more than once -> line of its last copy
    copies: int = 0  # extra copies
    columns: tuple[str, ...] = ()
    selections: Counter[str] = field(default_factory=Counter)
    runs: int = 0

    @property
    def distinct(self) -> int:
        return self.rows - self.copies


def index_archive(path: Path) -> ArchiveIndex:
    """Check every line of the archive and find the last copy of each observation."""
    idx = ArchiveIndex()
    seen = _IdSet()
    for number, line in iter_archive(path):
        if line.startswith('{"run":'):
            idx.runs += 1
            continue
        if line.startswith('{"refreshed":'):
            continue
        try:
            row = parse_row_line(line)
        except ValueError as exc:
            raise ArchiveError(f"{path}, line {number}: {exc}") from exc
        idx.rows += 1
        idx.selections[row.sel] += 1
        if not idx.columns:
            idx.columns = row.columns
        elif row.columns != idx.columns:
            raise ArchiveError(f"{path}, line {number}: other columns than the rows before it")
        if not seen.add(row.id):
            idx.copies += 1
            idx.repeated[row.id] = number
    return idx


def restore_batches(path: Path, idx: ArchiveIndex, batch_rows: int, only: set[int]) -> Iterator[list[ArchivedRow]]:
    """The last copy of every archived row (in --datastream), in batches of one datastream."""
    buffers: dict[int, list[ArchivedRow]] = {}
    held = 0
    for number, line in iter_archive(path):
        if not line.startswith('{"sel":'):
            continue
        row = parse_row_line(line)
        if idx.repeated.get(row.id, number) != number or (only and row.ds not in only):
            continue
        buffer = buffers.setdefault(row.ds, [])
        buffer.append(row)
        held += 1
        if len(buffer) >= batch_rows:
            held -= len(buffer)
            yield buffers.pop(row.ds)
        elif held >= 10 * batch_rows:
            for ds in sorted(buffers):
                yield buffers[ds]
            buffers.clear()
            held = 0
    for ds in sorted(buffers):
        yield buffers[ds]


def restore_sql(rows: Sequence[ArchivedRow]) -> str:
    """Re-insert the rows (one datastream) with their IDs and values; existing IDs are left alone."""
    ds = rows[0].ds
    doc = "[" + ",".join(r.row for r in rows) + "]"
    return f"""
WITH v AS (
    SELECT * FROM json_populate_recordset(NULL::"OBSERVATIONS", {pressure.sql_literal(doc)}::json)
), ins AS (
    INSERT INTO "OBSERVATIONS" OVERRIDING SYSTEM VALUE
    SELECT v.* FROM v
     WHERE v."DATASTREAM_ID" = {int(ds)}
       AND EXISTS (SELECT 1 FROM "DATASTREAMS" d WHERE d."ID" = v."DATASTREAM_ID")
       AND EXISTS (SELECT 1 FROM "FEATURES" f WHERE f."ID" = v."FEATURE_ID")
       AND NOT {protected_ds('v."DATASTREAM_ID"')}
    ON CONFLICT DO NOTHING
    RETURNING "ID"
)
SELECT 'restored', {int(ds)}, {len(rows)}, count(*), '{END}' FROM ins;
"""


def restore_state_sql(rows: Sequence[ArchivedRow]) -> str:
    """For the archived rows of one datastream: present, present and identical, and why absent ones can't come back."""
    ds = rows[0].ds
    return f"""
SELECT 'rstate', {int(ds)}, count(*), count(o."ID"),
       count(*) FILTER (WHERE o."ID" IS NOT NULL AND md5(row_to_json(o)::text) = v.h),
       count(*) FILTER (WHERE o."ID" IS NULL AND m.no_ds),
       count(*) FILTER (WHERE o."ID" IS NULL AND NOT m.no_ds AND m.no_foi),
       count(*) FILTER (WHERE o."ID" IS NULL AND NOT m.no_ds AND NOT m.no_foi AND m.prot),
       '{END}'
  FROM unnest({int_array(r.id for r in rows)}, {md5_array(r.md5 for r in rows)},
              {int_array(r.feature for r in rows)}) AS v(id, h, foi)
  LEFT JOIN "OBSERVATIONS" o ON o."ID" = v.id
  CROSS JOIN LATERAL (
      SELECT NOT EXISTS (SELECT 1 FROM "DATASTREAMS" d WHERE d."ID" = {int(ds)}) AS no_ds,
             NOT EXISTS (SELECT 1 FROM "FEATURES" f WHERE f."ID" = v.foi) AS no_foi,
             {protected_ds(str(int(ds)))} AS prot) m;
"""


@dataclass
class RestoreState:
    listed: int = 0
    present: int = 0
    identical: int = 0
    no_datastream: int = 0
    no_feature: int = 0
    protected: int = 0
    datastreams: set[int] = field(default_factory=set)

    @property
    def changed(self) -> int:
        """Present with other values: changed since, or the ID is taken; left alone."""
        return self.present - self.identical

    @property
    def blocked(self) -> int:
        return self.no_datastream + self.no_feature + self.protected

    @property
    def to_restore(self) -> int:
        return self.listed - self.present - self.blocked


def restore_state(run_sql: RunSql, batches: Iterable[list[ArchivedRow]], chunk: int) -> RestoreState:
    state = RestoreState()
    pending: list[str] = []

    def flush() -> None:
        for f in _tagged(run_sql(READ_ONLY_SESSION + "".join(pending)), "rstate", 7):
            ds, listed, present, identical, no_ds, no_foi, prot = map(int, f)
            state.listed += listed
            state.present += present
            state.identical += identical
            state.no_datastream += no_ds
            state.no_feature += no_foi
            state.protected += prot
            state.datastreams.add(ds)
        pending.clear()
        _progress(f"  checked {state.listed:,} archived rows")

    for batch in batches:
        pending.append(restore_state_sql(batch))
        if len(pending) >= chunk:
            flush()
    if pending:
        flush()
    return state


def restore_apply(run_sql: RunSql, batches: Iterable[list[ArchivedRow]], chunk: int) -> int:
    restored = 0
    pending: list[str] = []

    def flush() -> None:
        nonlocal restored
        try:
            lines = run_sql(WRITE_SESSION + "".join(pending))
        except PsqlError as exc:
            salvaged = restored + _salvage(exc.lines, "restored")
            raise PsqlError(f"{exc}\n  Rows restored before the error, as far as psql reported: {salvaged:,}. "
                            "Re-running the restore is safe: rows already back are left alone.") from exc
        counts = parse_counts(lines, "restored")
        if len(counts) != len(pending):
            raise PsqlError(f"{len(pending)} INSERT statements sent, {len(counts)} confirmed")
        restored += sum(n for _, _, n in counts)
        pending.clear()
        _progress(f"  restored {restored:,} rows so far")

    for batch in batches:
        pending.append(restore_sql(batch))
        if len(pending) >= chunk:
            flush()
    if pending:
        flush()
    return restored


def print_restore_state(state: RestoreState) -> None:
    def line(text: str, count: int) -> None:
        print(f"  {text:<62}{count:>12,}")

    line("archived observations (last copies)", state.listed)
    line("  present and identical: nothing to do", state.identical)
    line("  present with other values (changed since): left alone", state.changed)
    line("  absent: to restore", state.to_restore)
    line("  absent, cannot be restored: datastream gone", state.no_datastream)
    line("  absent, cannot be restored: FeatureOfInterest gone", state.no_feature)
    line("  absent, left out: datastream of a TGV Thing", state.protected)


def run_restore(args: argparse.Namespace, run_sql: RunSql) -> int:
    mode = "APPLY: inserts observations" if args.apply else "DRY RUN: read-only, nothing is written"
    print(f"Restore of the observations archived in {args.archive} ({mode})")
    _progress("Checking the archive ...")
    idx = index_archive(args.archive)
    only = set(args.datastream or ())
    selections = ", ".join(f"{sel} {n:,}" for sel, n in sorted(idx.selections.items()))
    print(f"{idx.rows:,} archived rows ({selections or 'none'}) from {idx.runs:,} runs: {idx.distinct:,} distinct "
          "observations" + (f"; {idx.copies:,} archived again by a later run, restored from their last copy"
                            if idx.copies else "") + (f"; limited to datastreams {sorted(only)}" if only else ""))
    if not idx.rows:
        return EXIT_OK
    catalogue = load_catalogue(run_sql)
    if set(idx.columns) != set(catalogue.columns):
        print(f"Refusing to restore: the archived rows have the columns {list(idx.columns)}, OBSERVATIONS has "
              f"{catalogue.columns}.")
        return EXIT_REFUSED

    def batches() -> Iterator[list[ArchivedRow]]:
        return restore_batches(args.archive, idx, args.batch_rows, only)

    _progress("Checking the archived rows against the database, read-only ...")
    before = restore_state(run_sql, batches(), args.chunk)
    print()
    print_restore_state(before)
    print(f"\nPlan: INSERT ... OVERRIDING SYSTEM VALUE ... ON CONFLICT DO NOTHING of at most {args.batch_rows:,} rows "
          "of one datastream each, from the archived JSON; then refresh the phenomenon/result times of "
          f"{len(before.datastreams):,} datastreams and check every row against its archived md5.")
    if not args.apply:
        print("Dry run: nothing was written. Re-run with --apply to restore.")
        return EXIT_OK
    if refuse_if_busy(run_sql):
        return EXIT_REFUSED
    _progress("Restoring ...")
    restored = restore_apply(run_sql, batches(), args.chunk)
    print(f"Restored {restored:,} observations (planned {before.to_restore:,}).")
    _progress("Refreshing datastream times ...")
    refresh(run_sql, before.datastreams, args.chunk)
    _progress("Verifying, read-only ...")
    after = restore_state(run_sql, batches(), args.chunk)
    print("\nVerification")
    print_restore_state(after)
    if after.to_restore or after.changed > before.changed:
        print(f"Verification: {after.to_restore + after.changed - before.changed:,} archived rows are not back "
              "exactly. Re-run to restore them.")
        return EXIT_VERIFY
    print(f"Verification: every restorable row is back, byte for byte ({after.identical:,} identical to the archive).")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def print_plan(run_sql: RunSql, sel: Selection, args: argparse.Namespace) -> None:
    plan = sel.plan
    print(f"\nRows to delete: {plan.rows:,} in {len(plan.by_ds):,} datastreams")
    if not plan.rows:
        return
    orphans, generated = orphaned_fois(run_sql, dict(plan.fois), args.chunk)
    print(f"  FeaturesOfInterest of those rows: {len(plan.fois):,}; left without observations afterwards: "
          f"{len(orphans):,}{_ids_text(orphans)} ({generated:,} of them generated for a Location); none is deleted")
    wal = plan.pages * 8192 + plan.rows * 60
    print(f"  heap pages holding them: {plan.pages:,}: about {wal / 2**20:,.0f} MB of WAL for the DELETEs (an 8 kB "
          "full-page image per page, wal_compression off, plus ~60 B per row); VACUUM writes about as much again")
    print(f"  archive: about {(plan.bytes + plan.rows * 110) / 2**20:,.0f} MB")
    print(f"\nPlan: per page of at most {args.page_rows:,} rows, read them in full (read-only), append them to the "
          f"archive and fsync it, then DELETE them in statements of at most {args.batch_rows:,} rows of one datastream, "
          "each its own transaction, rechecking each row against its archived md5 and the selection; then refresh "
          "phenomenonTime/resultTime of the datastreams touched, and scan again.")


def run_cleanup(args: argparse.Namespace, run_sql: RunSql) -> int:
    name = selection_name(args)
    mode = "APPLY: deletes observations, archiving each first" if args.apply else "DRY RUN: read-only, nothing is written"
    print(f"FROST observation cleanup: {name} ({mode})")
    pending: set[int] = set()
    if args.apply:
        pending = prepare_archive(args.archive)  # a wrong file fails before anything else
    catalogue = load_catalogue(run_sql)
    protected = catalogue.protected()
    print(f"Catalogue: {len(catalogue.datastreams):,} datastreams ({len(protected):,} of TGV Things, never touched); "
          f"observation IDs {catalogue.bounds}; foreign keys onto OBSERVATIONS: {catalogue.foreign_keys or 'none'}")
    if args.datastream:
        unknown = sorted(set(args.datastream) - set(catalogue.datastreams))
        if unknown:
            print(f"Unknown datastreams: {unknown}")
            return EXIT_ERROR
        if set(args.datastream) & protected:
            print(f"Datastreams of TGV Things are never touched: {sorted(set(args.datastream) & protected)}")
    sel = make_selection(args, catalogue)
    print(f"Selection: {sel.summary}")
    _progress("Scanning, read-only ...")
    sel.scan(run_sql)
    sel.report()
    print_plan(run_sql, sel, args)
    if not args.apply:
        print("\nDry run: nothing was written. Re-run with --apply --archive FILE to delete.")
        return EXIT_OK
    if catalogue.foreign_keys:
        print(f"Refusing to delete: foreign keys reference OBSERVATIONS: {catalogue.foreign_keys}")
        return EXIT_REFUSED
    if refuse_if_busy(run_sql):
        return EXIT_REFUSED
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    append_run(args.archive, run_id, name, args.datastream)
    _progress(f"Applying; every row is appended to {args.archive} before it is deleted ...")
    done = apply_pages(run_sql, sel, args.archive, run_id)
    print(f"\nDeleted {done.deleted:,} observations (planned {sel.plan.rows:,}); {done.archived:,} rows archived in "
          f"{args.archive} first; {done.kept:,} of them left (changed, or no longer selected, when their DELETE ran).")
    to_refresh = pending | done.touched
    if to_refresh:
        _progress("Refreshing datastream times ...")
        refresh(run_sql, to_refresh, args.chunk)
    append_refreshed(args.archive, run_id, to_refresh)
    print(f"Refreshed phenomenonTime/resultTime of {len(to_refresh):,} datastreams"
          + (f" ({len(pending - done.touched):,} left by an interrupted run)" if pending - done.touched else "") + ".")
    if done.fois:
        orphans, generated = orphaned_fois(run_sql, dict.fromkeys(done.fois, 0), args.chunk)
        print(f"FeaturesOfInterest left without observations: {len(orphans):,}{_ids_text(orphans)} ({generated:,} "
              "generated for a Location); none was deleted.")
    _progress("Verifying, read-only ...")
    again = make_selection(args, load_catalogue(run_sql))
    again.scan(run_sql)
    if again.plan.rows:
        print(f"Verification: {again.plan.rows:,} rows are still selected (rows that arrived, or changed, during the "
              "run?). Re-run with the same archive to delete them.")
        return EXIT_VERIFY
    print("Verification: no rows left in the selection, as of now.")
    return EXIT_OK


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete misplaced or duplicated observations from a FROST database, archiving every row in full "
                    "first, or restore them from that archive.",
        epilog="Dry run unless --apply is given. The module docstring has the runbook.",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--ssh", metavar="USER@HOST", help="SSH destination of the FROST host")
    target.add_argument("--container", metavar="NAME", help="local docker container running the FROST database")
    parser.add_argument("--ssh-key", metavar="PATH", help="identity file for --ssh")
    parser.add_argument("--compose-dir", default="/opt/frost", help="compose directory on the host (%(default)s)")
    parser.add_argument("--service", default="frost-db", help="database service in that compose project (%(default)s)")
    what = parser.add_mutually_exclusive_group(required=True)
    what.add_argument("--tgv-misplaced", action="store_true",
                      help="TGV-labelled rows in datastreams of other Things")
    what.add_argument("--duplicates", action="store_true", help="exact duplicates within a datastream")
    what.add_argument("--misrouted-leftovers", action="store_true",
                      help="rows the move script leaves in place because their own datastream has the same reading")
    what.add_argument("--ids-file", type=Path, metavar="FILE",
                      help="observation IDs, one per line, or a CSV with observation_id[,datastream_id]")
    what.add_argument("--restore", action="store_true", help="re-insert the rows of --archive")
    parser.add_argument("--archive", type=Path, metavar="FILE",
                        help="with --apply: JSON lines file every row is appended to, in full, before it is deleted; "
                             "with --restore: that file")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--datastream", type=int, action="append", metavar="ID",
                        help="limit to this datastream (repeatable), e.g. for a canary")
    parser.add_argument("--require-twin", action="store_true",
                        help="with --ids-file: delete a listed row only while another row holds the same reading")
    parser.add_argument("--prefix", default="GEO_", help="entity name prefix, for the move script's plan and the Thing "
                                                         "a thing_name label names (%(default)s)")
    parser.add_argument("--range-ids", type=int, default=200_000,
                        help="observation IDs per scan statement (%(default)s)")
    parser.add_argument("--count-datastreams", type=int, default=100,
                        help="datastreams per row-count statement of --duplicates (%(default)s)")
    parser.add_argument("--chunk-rows", type=int, default=300_000,
                        help="rows per duplicate-scan statement (%(default)s)")
    parser.add_argument("--page-rows", type=int, default=50_000,
                        help="rows read, archived and deleted per page (%(default)s)")
    parser.add_argument("--batch-rows", type=int, default=5000, help="rows per DELETE or INSERT (%(default)s)")
    parser.add_argument("--chunk", type=int, default=20, help="statements per read, refresh or restore session "
                                                              "(%(default)s)")
    parser.add_argument("--top", type=int, default=20, help="lines per report section (%(default)s)")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds per psql session (%(default)s)")
    args = parser.parse_args(argv)
    if not (args.ssh or args.container):
        parser.error("one of --ssh or --container is required")
    sizes = (args.range_ids, args.count_datastreams, args.chunk_rows, args.page_rows, args.batch_rows, args.chunk,
             args.top)
    if min(sizes) < 1:
        parser.error("--range-ids, --count-datastreams, --chunk-rows, --page-rows, --batch-rows, --chunk and --top "
                     "must be positive")
    if (args.apply or args.restore) and args.archive is None:
        parser.error("--apply needs --archive (every row is appended to it, in full, before it is deleted); "
                     "--restore needs the --archive to restore from")
    if args.restore and not args.archive.is_file():
        parser.error(f"--archive {args.archive} does not exist")
    if args.require_twin and args.ids_file is None:
        parser.error("--require-twin goes with --ids-file")
    if args.ids_file is not None and not args.ids_file.is_file():
        parser.error(f"--ids-file {args.ids_file} does not exist")
    return args


def _interrupted(exc: BaseException) -> bool:
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, KeyboardInterrupt):
            return True
        cause = cause.__cause__
    return False


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run_sql = pressure.psql_runner(
        pressure.psql_argv(ssh=args.ssh, ssh_key=args.ssh_key, compose_dir=args.compose_dir, service=args.service,
                           container=args.container),
        args.timeout,
    )
    note = (" Every row deleted, or still deletable by a session left on the server, is in the archive; run the "
            "same command again to continue.") if args.apply and not args.restore else ""
    try:
        if args.restore:
            return run_restore(args, run_sql)
        return run_cleanup(args, run_sql)
    except PsqlError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if _interrupted(exc):
            print(f"Interrupted.{note}", file=sys.stderr)
            return EXIT_INTERRUPTED
        return EXIT_ERROR
    except (ArchiveError, IdsFileError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(f"Interrupted.{note}", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
