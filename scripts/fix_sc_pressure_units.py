#!/usr/bin/env python
"""Backfill: convert Sensor.Community "Air pressure" observations in FROST from Pa to hPa.

Until the fix in app/sources/sensor_community.py, the connector pushed the
Sensor.Community ``pressure`` value as reported, in pascal, to the canonical
Air pressure datastreams, whose unit is hPa: FROST holds results like
100391.86 labelled hPa. The mapper now converts; this script repairs the
history already stored in one FROST database, can undo that repair, and can
check a copy of the connector's dead-letter queue for Pa readings still to come.

What it changes
---------------
OBSERVATIONS rows of the datastreams of Things named 'GEO_SensorCommunity %'
(PROPERTIES.source = 'sensor_community') with ObservedProperty 'Air pressure'
and unit hPa, where RESULT_TYPE = 0 (a number), RESULT_NUMBER lies strictly
inside the Pa band the mapper uses (``PA_BAND`` in app/sources/sensor_community.py,
2000 < value < 200000), and the row is not tagged as another observed property
in PARAMETERS (these datastreams also hold PM10/PM2.5/humidity readings routed
there by a separate connector bug; those are counted, never converted). For
those rows:

    RESULT_NUMBER = RESULT_NUMBER / 100   (as double precision)
    RESULT_STRING = the same number as text

computed in exact decimal arithmetic (numeric), so 99954.35 becomes 999.5435
rather than 999.5435000000001. Both columns are written because FROST serves a
numeric result from RESULT_STRING, while its $filter and our SQL readers use
RESULT_NUMBER. Nothing else in FROST changes: its UPDATE trigger on OBSERVATIONS
(datastreams_update_update, read on production 2026-11-20) only touches
DATASTREAMS when phenomenon or result times change.

A converted value is below 2000, so it is never selected again, and values
outside the band are never touched: re-running is safe.

The record (--ids-file)
-----------------------
For each session of UPDATE batches, the rows they will convert are read
first (read-only) and appended to the IDs file with their values as stored,
and the file is fsync'ed. Only then are the UPDATEs sent, and each converts
only listed rows that are still in the Pa band. So the file is always a
superset of the rows converted: a timeout, Ctrl-C, a dropped SSH connection,
or psql finishing its statements on the server after the client is gone can
never convert a row that is missing from it. Rows recorded but not converted
(an interrupted run) or recorded twice (a re-run) do no harm: --rollback
only restores rows that still hold exactly the value --apply wrote. Columns:

    datastream_id,observation_id,result_number,result_string

result_number is RESULT_NUMBER as PostgreSQL prints it (it reads back as the
same double precision); result_string is the original RESULT_STRING in
double quotes, and an empty field is NULL: PostgreSQL's CSV convention, so
the file also loads with COPY ... CSV HEADER. A line cut off by an
interrupted run is dropped by the next run (no UPDATE was sent for it).
Keep the file: it is the way back.

Rollback (--rollback)
---------------------
Restores RESULT_NUMBER and RESULT_STRING exactly as recorded in the IDs
file, for the listed rows whose current values are exactly what --apply wrote
from the recorded ones. Rows never converted, or changed since, are left alone
and counted. Dry run unless --apply is given; batched like the conversion,
then verified.

Sessions, batches and timeouts
------------------------------
The FROST database stops statements after 60 s, so every UPDATE covers at
most --batch-rows rows of one datastream and is its own transaction (psql
autocommit); a psql session covers --chunk datastreams and stops at its first
error. Every session sets statement_timeout, lock_timeout and
idle_in_transaction_session_timeout (see SESSION_TIMEOUTS). Reads (scans,
the rows to convert, verification, dry runs) run in READ ONLY sessions.
Writes refuse to start while another write session of this script is still
active on the server, e.g. one left behind by an interrupted run.

How it connects
---------------
It pipes SQL into psql inside the FROST database container, the same path as a
manual ``ssh <host> 'cd /opt/frost && docker compose exec -T frost-db psql ...'``.
psql there connects as the container's own $POSTGRES_USER to $POSTGRES_DB, so
this script never handles, needs or prints a credential. Use --ssh for the
FROST host (with keepalives, so a dead connection fails within about a
minute), or --container for a local database container.

Runbook (production)
--------------------
The connector's dead-letter queue (DLQ) holds finished payloads: pre-fix
Sensor.Community pressure readings in it are replayed unchanged, in Pa,
bypassing the mapper, until they are delivered or FAILED_DLQ_MAX_AGE_HOURS
(48 h) after they were written. --check-dlq reads a copy of it:

    ssh -i ~/.ssh/id_hetzner root@<connector-host> \\
        'cd /opt/geo-insights && docker compose exec -T connector cat /app/data/failed_observations.jsonl' \\
        > dlq_copy.jsonl
    python scripts/fix_sc_pressure_units.py --check-dlq dlq_copy.jsonl

1. Before the deploy, check a copy of the DLQ: it counts such readings per
   FROST host, so you know whether this database can still receive Pa rows.
2. Deploy the connector with the mapper fix. From then on no new Pa reading
   enters the DLQ.
3. Dry run against the FROST database. Check free disk space on the FROST host
   first: every converted row is a new row version (about +10% for
   OBSERVATIONS and its indexes, plus WAL).
4. Canary: --apply --ids-file sc_pressure_ids.csv --datastream <ID>; check that
   datastream's observations in the FROST API.
5. Everything: --apply --ids-file sc_pressure_ids.csv (the same file).
6. Check a fresh copy of the DLQ, taken after the deploy. If it finds no Pa
   readings, you are done. Otherwise, once the expiry time it prints has passed
   (48 h after the deploy at the latest), run the dry run again and, if it
   finds rows (replayed from the DLQ), --apply again with the same file.

If a run is interrupted, run the same command again. If it reports another
write session as active, wait for it (each statement ends within the
statement timeout) or end it with SELECT pg_terminate_backend(<pid>).
To undo: --rollback --ids-file sc_pressure_ids.csv (a dry run), then the same
with --apply.

Not covered: only this FROST database is repaired. Other FROST targets the
connector pushed to keep their Pa values, and so do copies made from FROST
(monitoring.readings_hourly: no sensor linked these datastreams on 2026-11-20).

Usage
-----
    # Dry run (default): per-datastream and total counts, value ranges,
    # before/after samples and the planned UPDATE batches. Writes nothing.
    python scripts/fix_sc_pressure_units.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner

    # Canary, then everything (owner-approved): record, convert, re-scan to verify.
    python scripts/fix_sc_pressure_units.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner \\
        --apply --ids-file sc_pressure_ids.csv --datastream 16
    python scripts/fix_sc_pressure_units.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner \\
        --apply --ids-file sc_pressure_ids.csv

    # Rollback: dry run, then restore.
    python scripts/fix_sc_pressure_units.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner \\
        --rollback --ids-file sc_pressure_ids.csv [--apply]

    # Dead-letter queue check on a local copy: no database needed.
    python scripts/fix_sc_pressure_units.py --check-dlq dlq_copy.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

# Run as `python scripts/fix_sc_pressure_units.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.sources.sensor_community import PA_BAND, PA_TO_HPA

RunSql = Callable[[str], list[str]]

THING_NAME_LIKE = r"GEO\_SensorCommunity %"  # \_ : a literal underscore, not LIKE's wildcard
THING_SOURCE = "sensor_community"
OBS_PROPERTY_NAME = "Air pressure"
UNIT_SYMBOL = "hPa"

LOW, HIGH = PA_BAND

# The connector's FAILED_DLQ_MAX_AGE_HOURS default (app/config.py).
DLQ_MAX_AGE_HOURS = 48.0

# The same open band as the mapper; RESULT_TYPE 0 = number.
IN_BAND = f'"RESULT_TYPE" = 0 AND "RESULT_NUMBER" > {LOW!r} AND "RESULT_NUMBER" < {HIGH!r}'

# The connector records each reading's own observed property in PARAMETERS. These
# datastreams also hold readings of other properties (PM10, PM2.5, humidity) routed
# there by a separate connector bug; never convert a row tagged as something else.
IS_PRESSURE = """coalesce("PARAMETERS"->>'observed_property', 'pressure') = 'pressure'"""

# Rows to convert.
TO_CONVERT = f"{IN_BAND} AND {IS_PRESSURE}"


def converted(number: str) -> str:
    """The converted value of the Pa number ``number`` (an SQL expression) as an exact decimal.

    PostgreSQL casts double precision to numeric with 15 significant digits, more
    than any stored Pa value carries; trim_scale drops the zeros the shift can leave
    (100000 -> 1000, not 1000.00). The rollback recomputes it from the recorded value
    with this same expression.
    """
    return f"trim_scale({number}::numeric * {PA_TO_HPA})"


NEW_VALUE = converted('"RESULT_NUMBER"')

# RESULT_STRING should be RESULT_NUMBER as text; the scan counts rows where it is not.
_PLAIN_NUMBER = "'^[-+]?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$'"
TEXT_MATCHES_NUMBER = (
    f'coalesce(CASE WHEN "RESULT_STRING" ~ {_PLAIN_NUMBER} '
    'THEN "RESULT_STRING"::numeric::double precision = "RESULT_NUMBER" END, false)'
)

WRITE_APP_NAME = "fix_sc_pressure_units"

# Per session. A statement stops before the database's own 60 s limit; a lock wait
# fails fast; a session left idle inside a transaction (a stalled client) is ended
# by the server, which rolls the transaction back and releases its locks.
SESSION_TIMEOUTS = {
    "statement_timeout": "50s",
    "lock_timeout": "10s",
    "idle_in_transaction_session_timeout": "60s",
}
_SESSION_SETTINGS = "".join(f"SET {name} = '{value}';\n" for name, value in SESSION_TIMEOUTS.items()) + (
    # double precision printed so that it reads back exactly (shortest such form on PostgreSQL 12+)
    "SET extra_float_digits = 3;\n"
    # backslashes in string literals are literal: the rollback's JSON relies on it
    "SET standard_conforming_strings = on;\n"
    "SET client_encoding = 'UTF8';\n"
)
READ_ONLY_SESSION = (
    "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;\n"
    f"SET application_name = '{WRITE_APP_NAME} (read-only)';\n" + _SESSION_SETTINGS
)
WRITE_SESSION = f"SET application_name = '{WRITE_APP_NAME}';\n" + _SESSION_SETTINGS

# psql inside the database container: no psqlrc, quiet (no command tags), unaligned
# '|'-separated tuples only, stop at the first error. The container's shell expands
# $POSTGRES_USER and $POSTGRES_DB from the container's own environment.
CONTAINER_PSQL = 'psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

# Output lines that are salvaged after a failure end in this field, so a line cut
# off mid-way can never be read as a complete one.
END = "end"


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


def sql_literal(text: str) -> str:
    """A string literal (standard_conforming_strings is on in every session)."""
    return "'" + text.replace("'", "''") + "'"


def datastreams_sql() -> str:
    """Every Sensor.Community Air pressure datastream, with its unit symbol."""
    return f"""
SELECT 'ds', d."ID", d."UNIT_SYMBOL", d."NAME"
  FROM "DATASTREAMS" d
  JOIN "THINGS" t ON t."ID" = d."THING_ID"
  JOIN "OBS_PROPERTIES" op ON op."ID" = d."OBS_PROPERTY_ID"
 WHERE t."NAME" LIKE '{THING_NAME_LIKE}'
   AND t."PROPERTIES"->>'source' = '{THING_SOURCE}'
   AND op."NAME" = '{OBS_PROPERTY_NAME}'
 ORDER BY d."ID";
"""


def stats_sql(ds_id: int) -> str:
    """Counts for one datastream; one statement each keeps it under the timeout."""
    return f"""
SELECT 'stat', {int(ds_id)}, count(*),
       count(*) FILTER (WHERE {TO_CONVERT}),
       count(*) FILTER (WHERE NOT {IS_PRESSURE}),
       count(*) FILTER (WHERE {IS_PRESSURE} AND "RESULT_TYPE" = 0 AND "RESULT_NUMBER" <= {LOW!r}),
       count(*) FILTER (WHERE {IS_PRESSURE} AND "RESULT_TYPE" = 0 AND "RESULT_NUMBER" >= {HIGH!r}),
       count(*) FILTER (WHERE {IN_BAND} AND NOT {IS_PRESSURE}),
       count(*) FILTER (WHERE {TO_CONVERT} AND NOT {TEXT_MATCHES_NUMBER}),
       min("RESULT_NUMBER") FILTER (WHERE {TO_CONVERT}),
       max("RESULT_NUMBER") FILTER (WHERE {TO_CONVERT})
  FROM "OBSERVATIONS"
 WHERE "DATASTREAM_ID" = {int(ds_id)};
"""


def sample_sql(ds_id: int, order_by: str, limit: int) -> str:
    """Rows to convert, before and after, using the UPDATE's own expression."""
    return f"""
SELECT 'sample', "ID", "DATASTREAM_ID", "PHENOMENON_TIME_START", "RESULT_NUMBER", "RESULT_STRING",
       {NEW_VALUE}::double precision, {NEW_VALUE}::text
  FROM "OBSERVATIONS"
 WHERE "DATASTREAM_ID" = {int(ds_id)} AND {TO_CONVERT}
 ORDER BY {order_by}
 LIMIT {int(limit)};
"""


def targets_sql(ds_id: int) -> str:
    """The rows of one datastream to convert, with their values as stored (read-only).

    RESULT_STRING comes as JSON (a string, or null for NULL): one line per row, exact,
    and a line cut off mid-way does not parse.
    """
    return f"""
SELECT 'target', "DATASTREAM_ID", "ID", "RESULT_NUMBER", coalesce(to_json("RESULT_STRING")::text, 'null')
  FROM "OBSERVATIONS"
 WHERE "DATASTREAM_ID" = {int(ds_id)} AND {TO_CONVERT}
 ORDER BY "ID";
"""


def _update_sql(ds_id: int, ids: str, listed: int) -> str:
    return f"""
WITH changed AS (
    UPDATE "OBSERVATIONS"
       SET "RESULT_NUMBER" = {NEW_VALUE}::double precision,
           "RESULT_STRING" = {NEW_VALUE}::text
     WHERE "DATASTREAM_ID" = {int(ds_id)} AND "ID" = ANY({ids})
       AND {TO_CONVERT}
    RETURNING "ID"
)
SELECT 'updated', {int(ds_id)}, {int(listed)}, count(*), '{END}' FROM changed;
"""


def update_sql(ds_id: int, obs_ids: Sequence[int]) -> str:
    """One batch: convert the listed rows of one datastream that are still in the Pa band.

    Only rows already recorded in the IDs file are ever listed.
    """
    ids = "'{" + ",".join(str(int(i)) for i in obs_ids) + "}'::bigint[]"
    return _update_sql(ds_id, ids, len(obs_ids))


def busy_sql() -> str:
    """Other write sessions of this script that may still change rows (read-only)."""
    return f"""
SELECT 'busy', pid, state, coalesce(to_char(now() - query_start, 'HH24:MI:SS'), ''), '{END}'
  FROM pg_stat_activity
 WHERE application_name = '{WRITE_APP_NAME}' AND pid <> pg_backend_pid()
   AND state IN ('active', 'idle in transaction', 'idle in transaction (aborted)');
"""


# The rollback joins OBSERVATIONS o to the recorded rows v(id, num, str).
IS_CONVERTED = (
    f'o."RESULT_TYPE" = 0 AND o."RESULT_NUMBER" = {converted("v.num")}::double precision '
    f'AND o."RESULT_STRING" = {converted("v.num")}::text'
)
IS_RECORDED = 'o."RESULT_NUMBER" = v.num AND o."RESULT_STRING" IS NOT DISTINCT FROM v.str'


def _recorded_rows_sql(records: Sequence[Record]) -> str:
    """The records as a relation v(id, num, str): one JSON literal, exact for any text."""
    doc = json.dumps(
        [{"id": r.observation_id, "num": r.result_number, "str": r.result_string} for r in records],
        separators=(",", ":"),
    )
    return f"json_to_recordset({sql_literal(doc)}::json) AS v(id bigint, num double precision, str text)"


def rollback_state_sql(ds_id: int, records: Sequence[Record]) -> str:
    """How many recorded rows of one datastream exist, hold the converted value, or the recorded one."""
    return f"""
SELECT 'rbstate', {int(ds_id)}, count(*), count(o."ID"),
       count(*) FILTER (WHERE {IS_CONVERTED}),
       count(*) FILTER (WHERE {IS_RECORDED}),
       '{END}'
  FROM {_recorded_rows_sql(records)}
  LEFT JOIN "OBSERVATIONS" o ON o."ID" = v.id AND o."DATASTREAM_ID" = {int(ds_id)};
"""


def rollback_sql(ds_id: int, records: Sequence[Record]) -> str:
    """One batch: restore the recorded values of the rows that hold exactly the converted value."""
    return f"""
WITH restored AS (
    UPDATE "OBSERVATIONS" o
       SET "RESULT_NUMBER" = v.num,
           "RESULT_STRING" = v.str
      FROM {_recorded_rows_sql(records)}
     WHERE o."DATASTREAM_ID" = {int(ds_id)} AND o."ID" = v.id
       AND {IS_CONVERTED}
    RETURNING o."ID"
)
SELECT 'restored', {int(ds_id)}, {len(records)}, count(*), '{END}' FROM restored;
"""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Datastream:
    id: int
    unit: str
    name: str


@dataclass(frozen=True)
class Stats:
    datastream_id: int
    rows: int
    to_convert: int  # pressure readings in the Pa band
    misrouted: int  # readings tagged as another observed property, any value
    below_band: int  # pressure readings <= 2000: already hPa, or bad
    above_band: int  # pressure readings >= 200000: implausible in either unit
    in_band_misrouted: int  # in the Pa band but tagged as another property: not converted
    text_mismatch: int  # rows to convert whose RESULT_STRING is not RESULT_NUMBER
    min_value: float | None  # of the rows to convert
    max_value: float | None

    @property
    def other(self) -> int:
        """Pressure readings whose result is not a number (or NULL); left as is."""
        return self.rows - self.to_convert - self.misrouted - self.below_band - self.above_band


class Record(NamedTuple):
    """An observation to convert, with its values before the conversion: one line of the IDs file."""

    datastream_id: int
    observation_id: int
    result_number: str  # RESULT_NUMBER as PostgreSQL printed it
    result_string: str | None  # RESULT_STRING; None is NULL


@dataclass(frozen=True)
class RollbackState:
    datastream_id: int
    listed: int  # distinct recorded rows
    found: int  # of those, still in this datastream
    as_converted: int  # holding exactly what --apply wrote: restored by --rollback --apply
    as_recorded: int  # holding the recorded values: never converted, or restored already

    @property
    def changed(self) -> int:
        """Rows that hold neither: changed since, left alone."""
        return self.found - self.as_converted - self.as_recorded


def _tagged(lines: Iterable[str], tag: str, width: int, *, ended: bool = False) -> list[list[str]]:
    rows = []
    for line in lines:
        fields = line.split("|")
        if fields[0] != tag:
            continue
        if tag == "ds":  # the name is last and may contain '|'
            fields = line.split("|", width)
        if len(fields) != width + 1 or (ended and fields[-1] != END):
            raise ValueError(f"unexpected psql output line: {line!r}")
        rows.append(fields[1:-1] if ended else fields[1:])
    return rows


def _opt_float(text: str) -> float | None:
    return float(text) if text else None


def parse_datastreams(lines: Sequence[str]) -> list[Datastream]:
    return [Datastream(int(i), unit, name) for i, unit, name in _tagged(lines, "ds", 3)]


def parse_stats(lines: Sequence[str]) -> list[Stats]:
    return [
        Stats(
            datastream_id=int(f[0]),
            rows=int(f[1]),
            to_convert=int(f[2]),
            misrouted=int(f[3]),
            below_band=int(f[4]),
            above_band=int(f[5]),
            in_band_misrouted=int(f[6]),
            text_mismatch=int(f[7]),
            min_value=_opt_float(f[8]),
            max_value=_opt_float(f[9]),
        )
        for f in _tagged(lines, "stat", 10)
    ]


_INT = re.compile(r"[0-9]+")
_FLOAT_TEXT = re.compile(r"[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")


def make_record(ds_id: str | int, obs_id: str | int, number: str, text: str | None) -> Record:
    """A Record, checked: what --apply converts is a finite number in the Pa band, on one line."""
    if isinstance(ds_id, str) and not _INT.fullmatch(ds_id):
        raise ValueError(f"bad datastream ID {ds_id!r}")
    if isinstance(obs_id, str) and not _INT.fullmatch(obs_id):
        raise ValueError(f"bad observation ID {obs_id!r}")
    if not _FLOAT_TEXT.fullmatch(number) or not LOW < float(number) < HIGH:
        raise ValueError(f"observation {obs_id}: result_number {number!r} is not a number in the Pa band")
    if text is not None and ("\n" in text or "\r" in text):
        raise ValueError(f"observation {obs_id}: a RESULT_STRING with a line break is not supported")
    if text == number:
        text = number  # one string object for both: the usual case, and half the memory
    return Record(int(ds_id), int(obs_id), number, text)


def parse_targets(lines: Sequence[str]) -> list[Record]:
    """The rows to convert as read by targets_sql."""
    records = []
    for line in lines:
        if not line.startswith("target|"):
            continue
        fields = line.split("|", 4)  # the JSON text is last and may contain '|'
        if len(fields) != 5:
            raise ValueError(f"unexpected psql output line: {line!r}")
        _, ds_id, obs_id, number, text = fields
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"unexpected psql output line: {line!r}") from exc
        if value is not None and not isinstance(value, str):
            raise ValueError(f"unexpected psql output line: {line!r}")
        records.append(make_record(ds_id, obs_id, number, value))
    return records


def parse_counts(lines: Sequence[str], tag: str) -> list[tuple[int, int, int]]:
    """(datastream ID, rows listed, rows changed) per 'updated' or 'restored' statement."""
    batches = []
    for ds_id, listed, count in _tagged(lines, tag, 4, ended=True):
        if int(count) > int(listed):
            raise ValueError(f"datastream {ds_id}: {count} rows {tag} of {listed} listed")
        batches.append((int(ds_id), int(listed), int(count)))
    return batches


def parse_rollback_state(lines: Sequence[str]) -> list[RollbackState]:
    return [
        RollbackState(int(f[0]), int(f[1]), int(f[2]), int(f[3]), int(f[4]))
        for f in _tagged(lines, "rbstate", 6, ended=True)
    ]


def to_hpa(value: float) -> Decimal:
    """What the UPDATE turns ``value`` into (for display)."""
    return Decimal(repr(value)) * PA_TO_HPA


# ---------------------------------------------------------------------------
# The IDs file
# ---------------------------------------------------------------------------

IDS_HEADER = "datastream_id,observation_id,result_number,result_string"
_RECORD_LINE = re.compile(r'([^,]*),([^,]*),([^,]*),(|"(?:[^"]|"")*")')


class IdsFileError(RuntimeError):
    pass


def format_record(record: Record) -> str:
    text = "" if record.result_string is None else '"' + record.result_string.replace('"', '""') + '"'
    return f"{record.datastream_id},{record.observation_id},{record.result_number},{text}\n"


def parse_record(line: str) -> Record:
    """One line of the IDs file, without its line end."""
    match = _RECORD_LINE.fullmatch(line)
    if not match:
        raise ValueError(f"not a record: {line[:120]!r}")
    ds_id, obs_id, number, text = match.groups()
    return make_record(ds_id, obs_id, number, text[1:-1].replace('""', '"') if text else None)


def prepare_ids_file(path: Path) -> None:
    """Create the IDs file with its header, or check an existing one and drop a cut-off last line.

    A last line without its line end was being appended when a run was interrupted;
    the UPDATEs for it are only sent once the whole append is on disk, so its row
    was not converted by that run.
    """
    if not path.exists() or path.stat().st_size == 0:
        with path.open("w", encoding="utf-8", newline="\n") as out:
            out.write(IDS_HEADER + "\n")
            out.flush()
            os.fsync(out.fileno())
        return
    with path.open("rb+") as handle:
        header = handle.readline().decode("utf-8", "replace").rstrip("\r\n")
        if header != IDS_HEADER:
            raise IdsFileError(
                f"{path} does not start with {IDS_HEADER!r}: not an IDs file of this version of the script. "
                "Use a new file."
            )
        body = handle.tell()  # where the records start
        size = handle.seek(0, os.SEEK_END)
        start = max(body, size - 65536)
        handle.seek(start)
        tail = handle.read()
        if body == size and not tail:
            if size == len(IDS_HEADER):  # the header only, without its line end
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            return
        if tail.endswith(b"\n"):
            return
        cut = tail.rfind(b"\n")
        if cut < 0 and start > body:
            raise IdsFileError(f"{path}: its last line is cut off and longer than 64 KiB; repair it by hand")
        _progress(f"  WARNING: {path}: dropping the cut-off last line {tail[cut + 1 :][:80]!r} "
                  "(written by an interrupted run; its row was not converted)")
        handle.truncate(start + cut + 1)
        handle.flush()
        os.fsync(handle.fileno())


def append_records(path: Path, records: Sequence[Record]) -> None:
    """Append and fsync: on disk before any UPDATE that may convert these rows is sent."""
    with path.open("a", encoding="utf-8", newline="\n") as out:  # LF, also on Windows
        out.writelines(format_record(r) for r in records)
        out.flush()
        os.fsync(out.fileno())


def read_ids_file(path: Path) -> list[Record]:
    """Every record in the IDs file; a cut-off last line is skipped (its row was not converted)."""
    records = []
    with path.open(encoding="utf-8", newline="") as handle:
        if handle.readline().rstrip("\r\n") != IDS_HEADER:
            raise IdsFileError(f"{path} does not start with {IDS_HEADER!r}: not an IDs file of this script")
        for number, line in enumerate(handle, start=2):
            if not line.endswith("\n"):
                _progress(f"  WARNING: {path}, line {number}: cut-off last line skipped "
                          "(written by an interrupted run; its row was not converted)")
                break
            body = line.rstrip("\n").removesuffix("\r")
            if not body:
                continue
            try:
                records.append(parse_record(body))
            except ValueError as exc:
                raise IdsFileError(f"{path}, line {number}: {exc}") from exc
    return records


def group_records(records: Iterable[Record], datastreams: set[int] | None = None) -> dict[int, list[Record]]:
    """Distinct records per datastream, sorted by observation ID.

    A row recorded twice (a re-run after an interruption) has the same values both
    times; different values for one observation mean the file was edited or mixed up.
    """
    seen: dict[int, Record] = {}
    conflicts = set()
    for record in records:
        if datastreams and record.datastream_id not in datastreams:
            continue
        if seen.setdefault(record.observation_id, record) != record:
            conflicts.add(record.observation_id)
    if conflicts:
        raise IdsFileError(
            f"{len(conflicts):,} observations are recorded with different values, e.g. {sorted(conflicts)[:10]}; "
            "resolve that by hand first"
        )
    by_ds: dict[int, list[Record]] = {}
    for record in seen.values():
        by_ds.setdefault(record.datastream_id, []).append(record)
    return {ds_id: sorted(rs, key=lambda r: r.observation_id) for ds_id, rs in sorted(by_ds.items())}


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class PsqlError(RuntimeError):
    def __init__(self, message: str, lines: list[str] | None = None) -> None:
        super().__init__(message)
        self.lines = lines or []


def psql_argv(
    *,
    ssh: str | None = None,
    ssh_key: str | None = None,
    compose_dir: str = "/opt/frost",
    service: str = "frost-db",
    container: str | None = None,
) -> list[str]:
    """The command that runs psql inside the FROST database container."""
    in_container = ["sh", "-c", CONTAINER_PSQL]
    if container:
        return ["docker", "exec", "-i", container, *in_container]
    if not ssh:
        raise ValueError("either ssh or container is required")
    # One string for the host's shell; shlex keeps the psql command intact for the container's.
    remote = (
        f"cd {shlex.quote(compose_dir)} && "
        f"docker compose exec -T {shlex.quote(service)} {shlex.join(in_container)}"
    )
    # Keepalives: a silently dropped connection fails within about a minute instead of hanging.
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    if ssh_key:
        argv += ["-i", os.path.expanduser(ssh_key)]
    return [*argv, ssh, remote]


def complete_lines(output: str | bytes | None) -> list[str]:
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
            proc = subprocess.run(
                argv,
                input=sql,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PsqlError(f"psql session did not finish within {timeout:g} s", complete_lines(exc.stdout)) from exc
        except KeyboardInterrupt as exc:
            raise PsqlError("interrupted (Ctrl-C)") from exc
        if proc.returncode != 0:
            raise PsqlError(f"psql exited with {proc.returncode}: {proc.stderr.strip()}", complete_lines(proc.stdout))
        return proc.stdout.splitlines()

    return run


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _chunks[T](items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _progress(message: str) -> None:
    sys.stdout.flush()  # keep the report and the progress lines in order on a terminal
    print(message, file=sys.stderr, flush=True)


def scan(run_sql: RunSql, ds_ids: Sequence[int], chunk: int) -> dict[int, Stats]:
    """Collect Stats per datastream: read-only sessions of `chunk` statements each."""
    stats: dict[int, Stats] = {}
    for part in _chunks(ds_ids, chunk):
        lines = run_sql(READ_ONLY_SESSION + "".join(stats_sql(i) for i in part))
        stats.update((s.datastream_id, s) for s in parse_stats(lines))
        _progress(f"  scanned {len(stats)}/{len(ds_ids)} datastreams")
    missing = set(ds_ids) - set(stats)
    if missing:
        raise PsqlError(f"no stats returned for datastreams {sorted(missing)}")
    return stats


def refuse_if_busy(run_sql: RunSql) -> bool:
    """True, after saying so, when another write session of this script is active on the server."""
    busy = _tagged(run_sql(READ_ONLY_SESSION + busy_sql()), "busy", 4, ended=True)
    if not busy:
        return False
    print(f"Another write session of this script is still active on the server ({len(busy)}):")
    for pid, state, running in busy:
        print(f"  pid {pid}: {state}, statement running for {running or '?'}")
    print("It may be left from an interrupted run: every row it can convert is in the IDs file. Wait for it to end "
          "(each statement stops within the statement timeout), or end it with SELECT pg_terminate_backend(<pid>), "
          "then re-run.")
    return True


def print_stats(datastreams: Sequence[Datastream], stats: dict[int, Stats]) -> None:
    """One line per datastream with observations; min/max are of the rows to convert.

    convert, <=low, >=high and other count pressure readings; misrouted counts
    readings tagged as another observed property.
    """
    print(f"{'datastream':>10} {'rows':>6} {'convert':>7} {'<=low':>6} {'>=high':>6} {'other':>5} "
          f"{'misrouted':>9} {'min Pa':>10} {'max Pa':>10}  name")
    empty = 0
    for ds in datastreams:
        s = stats[ds.id]
        if s.rows == 0:
            empty += 1
            continue
        lo = "-" if s.min_value is None else f"{s.min_value:.2f}"
        hi = "-" if s.max_value is None else f"{s.max_value:.2f}"
        print(f"{ds.id:>10} {s.rows:>6} {s.to_convert:>7} {s.below_band:>6} {s.above_band:>6} {s.other:>5} "
              f"{s.misrouted:>9} {lo:>10} {hi:>10}  {ds.name}")
    if empty:
        print(f"({empty} datastreams without observations not listed)")


def print_totals(stats: dict[int, Stats]) -> None:
    values = list(stats.values())
    with_rows = [s for s in values if s.to_convert]
    mins = [s.min_value for s in with_rows if s.min_value is not None]
    maxs = [s.max_value for s in with_rows if s.max_value is not None]
    mismatch = sum(s.text_mismatch for s in values)

    def line(label: str, count: int) -> None:
        print(f"  {label:<44}{count:>12,}")

    print("\nTotals")
    line("datastreams scanned", len(values))
    line("datastreams with rows to convert", len(with_rows))
    line("observations scanned", sum(s.rows for s in values))
    line("pressure readings to convert (Pa band)", sum(s.to_convert for s in values))
    line("  of those, RESULT_STRING != RESULT_NUMBER", mismatch)
    if mins and maxs:
        lo, hi = min(mins), max(maxs)
        print(f"    value range: {lo} .. {hi} Pa -> {to_hpa(lo)} .. {to_hpa(hi)} hPa")
    line(f"pressure readings left as is, <= {LOW:g}", sum(s.below_band for s in values))
    line(f"pressure readings left as is, >= {HIGH:g}", sum(s.above_band for s in values))
    line("pressure readings left as is, not a number", sum(s.other for s in values))
    line("readings of other properties (misrouted)", sum(s.misrouted for s in values))
    line("  of those in the Pa band (not converted)", sum(s.in_band_misrouted for s in values))
    if mismatch:
        print("  WARNING: some rows to convert have a RESULT_STRING that is not their RESULT_NUMBER;"
              " the UPDATE rewrites both from RESULT_NUMBER (the IDs file keeps the original text).")


def print_samples(run_sql: RunSql, stats: dict[int, Stats], count: int) -> None:
    with_rows = [s for s in stats.values() if s.to_convert]
    if not with_rows or count <= 0:
        return
    lowest = min(with_rows, key=lambda s: s.min_value if s.min_value is not None else math.inf)
    highest = max(with_rows, key=lambda s: s.max_value if s.max_value is not None else -math.inf)
    busiest = max(with_rows, key=lambda s: s.to_convert)
    sql = (
        READ_ONLY_SESSION
        + sample_sql(busiest.datastream_id, '"ID" DESC', max(count - 2, 1))
        + sample_sql(lowest.datastream_id, '"RESULT_NUMBER"', 1)
        + sample_sql(highest.datastream_id, '"RESULT_NUMBER" DESC', 1)
    )
    print("\nSamples: before -> after, computed by the UPDATE's own expression (latest rows, lowest, highest)")
    for obs_id, ds_id, phen, num, text, new_num, new_text in _tagged(run_sql(sql), "sample", 7):
        print(f"  observation {obs_id:>10}  datastream {ds_id:>6}  {phen}  "
              f"{num} ({text!r}) -> {new_num} ({new_text!r})")


def _salvage(lines: Sequence[str], tag: str) -> int:
    """Rows changed according to what a failed session printed before it stopped."""
    done = 0
    for line in lines:
        try:
            done += sum(n for _, _, n in parse_counts([line], tag))
        except ValueError:
            _progress(f"  WARNING: unreadable psql output skipped: {line[:80]!r}")
    return done


def apply(run_sql: RunSql, ds_ids: Sequence[int], chunk: int, batch_rows: int, ids_file: Path) -> tuple[int, int]:
    """Convert the rows to convert of these datastreams. Returns (rows recorded, rows converted).

    Per session of `chunk` datastreams: read the rows to convert (read-only), append
    them with their values as stored to ids_file and fsync it, and only then send the
    UPDATE batches, which convert only listed rows still in the Pa band. Whatever
    happens to that session (an error, the timeout, Ctrl-C, a dropped connection, or
    psql finishing its statements on the server after the client is gone), every row
    it can convert is already in the file.
    """
    recorded = done = 0
    for part in _chunks(ds_ids, chunk):
        try:
            targets = parse_targets(run_sql(READ_ONLY_SESSION + "".join(targets_sql(i) for i in part)))
        except PsqlError as exc:
            raise PsqlError(f"{exc}\n  (while reading the rows to convert: nothing of this session was converted; "
                            f"rows converted before it: {done:,}; re-running is safe)") from exc
        if not targets:
            continue
        try:
            append_records(ids_file, targets)
        except OSError as exc:
            raise IdsFileError(f"cannot append to {ids_file}: {exc} (nothing of this session was converted; "
                               f"rows converted before it: {done:,})") from exc
        recorded += len(targets)
        by_ds: dict[int, list[int]] = {}
        for target in targets:
            by_ds.setdefault(target.datastream_id, []).append(target.observation_id)
        sql = WRITE_SESSION + "".join(
            update_sql(ds_id, batch) for ds_id, ids in by_ds.items() for batch in _chunks(ids, batch_rows)
        )
        try:
            lines = run_sql(sql)
        except PsqlError as exc:
            raise PsqlError(
                f"{exc}\n  Rows converted before the error, as far as psql reported: {done + _salvage(exc.lines, 'updated'):,}. "
                f"Every row this session can convert, also on the server after the client gave up, is already in "
                f"{ids_file}. Re-running is safe (use a smaller --batch-rows if a statement timed out); it refuses to "
                "start while a write session of this script is still active on the server."
            ) from exc
        done += sum(n for _, _, n in parse_counts(lines, "updated"))
        _progress(f"  converted {done:,} rows so far, {recorded:,} recorded (datastream {part[-1]} done)")
    return recorded, done


def check_dlq(path: Path, now: datetime, max_age_hours: float = DLQ_MAX_AGE_HOURS) -> int:
    """Count pre-fix Sensor.Community pressure readings (Pa band) in a copy of the connector's DLQ.

    Read-only: the file is a copy of data/failed_observations.jsonl. Its entries hold
    finished payloads, which the connector replays unchanged (bypassing the mapper fix)
    until they are delivered or expire max_age_hours after they were written.
    Returns 1 when there are such readings, else 0.
    """
    print(f"Dead-letter queue check (read-only): {path}")
    entries = unreadable = outside = 0
    found: dict[str, list[datetime | None]] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            entries += 1
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                unreadable += 1
                continue
            payload = _sc_pressure_payload(record)
            if payload is None:
                continue
            result = payload.get("result")
            if isinstance(result, bool) or not isinstance(result, int | float) or not LOW < result < HIGH:
                outside += 1
                continue
            host = urlparse(str(record.get("endpoint") or "")).netloc or "(no endpoint)"
            found.setdefault(host, []).append(_timestamp(record.get("timestamp")))

    total = sum(len(times) for times in found.values())
    print(f"  entries: {entries:,} ({unreadable:,} unreadable)")
    print(f"  Sensor.Community pressure readings in the Pa band: {total:,}")
    for host, times in sorted(found.items()):
        known = [t for t in times if t is not None]
        span = f", written {min(known).isoformat()} .. {max(known).isoformat()}" if known else ""
        print(f"    {host}: {len(times):,}{span}")
    if outside:
        print(f"  Sensor.Community pressure readings outside the band, left as is: {outside:,}")
    if not total:
        print("None: this queue cannot bring Pa readings back into FROST.")
        return 0
    stamps = [t for times in found.values() for t in times]
    known = [t for t in stamps if t is not None]
    print("They are replayed unchanged, in Pa (the mapper fix does not touch queued payloads), until they are "
          f"delivered or expire {max_age_hours:g} h after they were written.")
    if known:
        until = max(known) + timedelta(hours=max_age_hours)
        when = "already passed" if until <= now else f"{until - now} from now"
        print(f"  The last one expires by {until.isoformat()} ({when}).")
    if len(known) < len(stamps):
        print(f"  {len(stamps) - len(known):,} have no readable timestamp: they never expire; replay or clear them.")
    print("Run the backfill after the deploy. Once that time has passed (or a copy taken after the deploy shows "
          "none), run the dry run again and --apply again if it finds rows.")
    return 1


def _sc_pressure_payload(record: Any) -> dict[str, Any] | None:
    """The payload of a DLQ entry when it is a Sensor.Community pressure reading (v1.1 or v2.0)."""
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    params = payload.get("parameters")  # v2.0 payloads keep it too (app/sta/models.py)
    if not isinstance(params, dict) or params.get("observed_property") != "pressure":
        return None
    sensor_id = str(params.get("sensor_id") or "")
    thing_name = str(params.get("thing_name") or "")
    if not (sensor_id.startswith("sensor_community-") or thing_name.startswith("SensorCommunity ")):
        return None
    return payload


def _timestamp(value: object) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def _rollback_batches(by_ds: dict[int, list[Record]], batch_rows: int) -> list[tuple[int, Sequence[Record]]]:
    return [(ds_id, batch) for ds_id, records in by_ds.items() for batch in _chunks(records, batch_rows)]


def _sessions(
    batches: Sequence[tuple[int, Sequence[Record]]], chunk: int
) -> list[list[tuple[int, Sequence[Record]]]]:
    """Batches grouped into sessions of `chunk` datastreams each."""
    order: dict[int, int] = {}
    sessions: list[list[tuple[int, Sequence[Record]]]] = []
    for ds_id, batch in batches:
        index = order.setdefault(ds_id, len(order)) // chunk
        if index == len(sessions):
            sessions.append([])
        sessions[index].append((ds_id, batch))
    return sessions


def rollback_scan(
    run_sql: RunSql, batches: Sequence[tuple[int, Sequence[Record]]], chunk: int
) -> dict[int, RollbackState]:
    """The state of the recorded rows per datastream, read-only."""
    state: dict[int, RollbackState] = {}
    for session in _sessions(batches, chunk):
        lines = run_sql(READ_ONLY_SESSION + "".join(rollback_state_sql(ds_id, batch) for ds_id, batch in session))
        for s in parse_rollback_state(lines):
            prev = state.get(s.datastream_id)
            if prev is not None:
                s = RollbackState(s.datastream_id, prev.listed + s.listed, prev.found + s.found,
                                  prev.as_converted + s.as_converted, prev.as_recorded + s.as_recorded)
            state[s.datastream_id] = s
        _progress(f"  checked {len(state)}/{len({ds_id for ds_id, _ in batches})} datastreams")
    return state


def rollback_apply(run_sql: RunSql, batches: Sequence[tuple[int, Sequence[Record]]], chunk: int) -> int:
    """Restore the recorded values, one UPDATE (transaction) per batch. Returns rows restored."""
    done = 0
    for session in _sessions(batches, chunk):
        try:
            lines = run_sql(WRITE_SESSION + "".join(rollback_sql(ds_id, batch) for ds_id, batch in session))
        except PsqlError as exc:
            raise PsqlError(
                f"{exc}\n  Rows restored before the error, as far as psql reported: {done + _salvage(exc.lines, 'restored'):,}. "
                "Re-running the rollback is safe: it only restores rows that still hold the converted value."
            ) from exc
        done += sum(n for _, _, n in parse_counts(lines, "restored"))
        _progress(f"  restored {done:,} rows so far")
    return done


def print_rollback_state(state: dict[int, RollbackState]) -> None:
    values = list(state.values())

    def line(label: str, count: int) -> None:
        print(f"  {label:<52}{count:>12,}")

    print("\nRecorded rows")
    line("datastreams", len(values))
    line("observations recorded", sum(s.listed for s in values))
    line("  not found (deleted, or moved to another datastream)", sum(s.listed - s.found for s in values))
    line("  holding the converted value: to restore", sum(s.as_converted for s in values))
    line("  holding the recorded value: nothing to do", sum(s.as_recorded for s in values))
    line("  holding another value: changed since, left alone", sum(s.changed for s in values))


def run_rollback(args: argparse.Namespace, run_sql: RunSql) -> int:
    mode = "APPLY: writes to OBSERVATIONS" if args.apply else "DRY RUN: read-only, nothing is written"
    print(f"Rollback of the Sensor.Community \"{OBS_PROPERTY_NAME}\" Pa -> hPa backfill from {args.ids_file} ({mode})")
    print(f"Rows: those recorded whose values are exactly what --apply wrote: {IS_CONVERTED}")
    print("Set:  RESULT_NUMBER and RESULT_STRING as recorded\n")
    records = read_ids_file(args.ids_file)
    by_ds = group_records(records, set(args.datastream or ()))
    print(f"{len(records):,} records in the file; {sum(len(r) for r in by_ds.values()):,} distinct observations "
          f"over {len(by_ds):,} datastreams" + (f" (limited to {sorted(args.datastream)})" if args.datastream else ""))
    if not by_ds:
        return 0
    batches = _rollback_batches(by_ds, args.batch_rows)
    _progress("Checking the recorded rows, read-only ...")
    before = rollback_scan(run_sql, batches, args.chunk)
    print_rollback_state(before)
    planned = sum(s.as_converted for s in before.values())
    print(f"\nPlan: {len(batches):,} UPDATE statements of at most {args.batch_rows:,} rows, each its own transaction")
    if not args.apply:
        print("Dry run: nothing was written. Re-run with --apply to restore.")
        return 0
    if refuse_if_busy(run_sql):
        return 1
    _progress("Restoring ...")
    restored = rollback_apply(run_sql, batches, args.chunk)
    print(f"Restored {restored:,} observations (planned {planned:,}).")
    _progress("Verifying, read-only ...")
    left = sum(s.as_converted for s in rollback_scan(run_sql, batches, args.chunk).values())
    if left:
        print(f"Verification: {left:,} recorded rows still hold the converted value. Re-run to restore them.")
        return 1
    print("Verification: no recorded row holds the converted value any more.")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert Sensor.Community Air pressure observations in FROST from Pa to hPa, roll that back, "
                    "or check a copy of the connector's dead-letter queue for Pa readings still to come.",
        epilog="Dry run unless --apply is given. The module docstring has the runbook.",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--ssh", metavar="USER@HOST", help="SSH destination of the FROST host")
    target.add_argument("--container", metavar="NAME", help="local docker container running the FROST database")
    parser.add_argument("--ssh-key", metavar="PATH", help="identity file for --ssh")
    parser.add_argument("--compose-dir", default="/opt/frost", help="compose directory on the host (%(default)s)")
    parser.add_argument("--service", default="frost-db", help="database service in that compose project (%(default)s)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--rollback", action="store_true",
                        help="restore the rows recorded in --ids-file instead of converting")
    parser.add_argument("--ids-file", type=Path, metavar="PATH",
                        help="with --apply: CSV every row to convert is appended to, with its values, before it is "
                             "converted; with --rollback: that record")
    parser.add_argument("--check-dlq", type=Path, metavar="JSONL",
                        help="read-only: count pre-fix Sensor.Community pressure readings in the Pa band in a copy "
                             "of the connector's data/failed_observations.jsonl (no database needed)")
    parser.add_argument("--dlq-max-age-hours", type=float, default=DLQ_MAX_AGE_HOURS,
                        help="the connector's FAILED_DLQ_MAX_AGE_HOURS (%(default)g)")
    parser.add_argument("--datastream", type=int, action="append", metavar="ID",
                        help="limit to this datastream ID (repeatable), e.g. for a canary")
    parser.add_argument("--batch-rows", type=int, default=5000, help="max rows per UPDATE (%(default)s)")
    parser.add_argument("--chunk", type=int, default=50, help="datastreams per psql session (%(default)s)")
    parser.add_argument("--samples", type=int, default=5, help="before/after samples to show (%(default)s)")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds per psql session (%(default)s)")
    args = parser.parse_args(argv)
    if args.check_dlq is not None:
        if args.ssh or args.container or args.apply or args.rollback:
            parser.error("--check-dlq only reads a local file: no --ssh/--container, --apply or --rollback")
        return args
    if not (args.ssh or args.container):
        parser.error("one of --ssh or --container is required")
    if args.batch_rows < 1 or args.chunk < 1:
        parser.error("--batch-rows and --chunk must be positive")
    if args.rollback and args.ids_file is None:
        parser.error("--rollback needs --ids-file: the record written by --apply")
    if args.rollback and not args.ids_file.is_file():
        parser.error(f"--ids-file {args.ids_file} does not exist")
    if args.apply and args.ids_file is None:
        parser.error("--apply needs --ids-file: the record of every row to convert, for audit and rollback")
    return args


def run(args: argparse.Namespace, run_sql: RunSql) -> int:
    if args.rollback:
        return run_rollback(args, run_sql)
    mode = "APPLY: writes to OBSERVATIONS" if args.apply else "DRY RUN: read-only, nothing is written"
    print(f'Sensor.Community "{OBS_PROPERTY_NAME}" Pa -> hPa backfill ({mode})')
    print(f"Rows: {TO_CONVERT}")
    print(f"Set:  RESULT_NUMBER = {NEW_VALUE}::double precision, RESULT_STRING = {NEW_VALUE}::text\n")
    if args.apply:
        prepare_ids_file(args.ids_file)  # a wrong file fails before anything else

    found = parse_datastreams(run_sql(READ_ONLY_SESSION + datastreams_sql()))
    other_unit = [d for d in found if d.unit != UNIT_SYMBOL]
    datastreams = [d for d in found if d.unit == UNIT_SYMBOL]
    if other_unit:
        print(f"Skipping {len(other_unit)} datastreams whose unit is not {UNIT_SYMBOL}: "
              f"{[(d.id, d.unit) for d in other_unit]}")
    if args.datastream:
        unknown = sorted(set(args.datastream) - {d.id for d in datastreams})
        if unknown:
            print(f"Not Sensor.Community {OBS_PROPERTY_NAME} ({UNIT_SYMBOL}) datastreams: {unknown}")
            return 1
        datastreams = [d for d in datastreams if d.id in set(args.datastream)]
    print(f"{len(datastreams)} datastreams: Things '{THING_NAME_LIKE}' (source={THING_SOURCE}) "
          f"x ObservedProperty '{OBS_PROPERTY_NAME}' ({UNIT_SYMBOL})")
    if not datastreams:
        return 0

    _progress("Scanning observations, one read-only statement per datastream ...")
    stats = scan(run_sql, [d.id for d in datastreams], args.chunk)
    print()
    print_stats(datastreams, stats)
    print_totals(stats)
    print_samples(run_sql, stats, args.samples)

    with_rows = [d.id for d in datastreams if stats[d.id].to_convert]
    planned = sum(stats[i].to_convert for i in with_rows)
    statements = sum(math.ceil(stats[i].to_convert / args.batch_rows) for i in with_rows)
    print(f"\nPlan: {statements:,} UPDATE statements over {len(with_rows):,} datastreams, at most {args.batch_rows:,} "
          f"rows each, each its own transaction. Per session of {args.chunk} datastreams, the rows to convert are "
          "first read and appended to the IDs file with their values, then converted.")
    if with_rows:
        first = with_rows[0]
        listed = min(stats[first].to_convert, args.batch_rows)
        print("Each statement:" + _update_sql(first, "'{<the observation IDs recorded for it>}'::bigint[]", listed))

    if not args.apply:
        print("Dry run: nothing was written. Re-run with --apply to convert.")
        return 0

    if refuse_if_busy(run_sql):
        return 1
    _progress(f"Applying; every row is appended to {args.ids_file} before it is converted ...")
    recorded, done = apply(run_sql, with_rows, args.chunk, args.batch_rows, args.ids_file)
    print(f"Converted {done:,} observations (planned {planned:,}); {recorded:,} rows recorded in {args.ids_file} "
          "before conversion.")

    _progress("Verifying, read-only ...")
    remaining = sum(s.to_convert for s in scan(run_sql, [d.id for d in datastreams], args.chunk).values())
    if remaining:
        print(f"Verification: {remaining:,} pressure readings still in the Pa band (new Pa rows from a connector "
              "without the fix, or replayed from its dead-letter queue?). Re-run to convert them.")
        return 1
    print("Verification: no rows left in the Pa band, as of now. Pre-fix readings in the connector's dead-letter "
          f"queue can still bring Pa rows back until {args.dlq_max_age_hours:g} h after the fixed connector was "
          "deployed (check a copy taken after the deploy with --check-dlq): run the dry run again after that, and "
          "--apply again if it finds rows.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.check_dlq is not None:
            return check_dlq(args.check_dlq, datetime.now(UTC), args.dlq_max_age_hours)
        run_sql = psql_runner(
            psql_argv(
                ssh=args.ssh,
                ssh_key=args.ssh_key,
                compose_dir=args.compose_dir,
                service=args.service,
                container=args.container,
            ),
            args.timeout,
        )
        return run(args, run_sql)
    except (PsqlError, IdsFileError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        note = (" Every row converted, or still convertible by a session left on the server, is in the IDs file;"
                " run the same command again to continue.") if args.apply else ""
        print(f"Interrupted.{note}", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
