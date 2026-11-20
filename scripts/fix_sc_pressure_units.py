#!/usr/bin/env python
"""Backfill: convert Sensor.Community "Air pressure" observations in FROST from Pa to hPa.

Until the fix in app/sources/sensor_community.py, the connector pushed the
Sensor.Community ``pressure`` value as reported, in pascal, to the canonical
Air pressure datastreams, whose unit is hPa: FROST holds results like
100391.86 labelled hPa. The mapper now converts; this script repairs the
history already stored.

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
RESULT_NUMBER. Nothing else changes: FROST's UPDATE trigger on OBSERVATIONS
only touches DATASTREAMS when phenomenon or result times change.

Idempotent: a converted value is below 2000, so it is never selected again,
and values outside the band are never touched. Re-running is safe, e.g. after
an interrupted run, or to catch Pa rows that the connector replays from its
dead-letter queue after the fix is deployed.

Batching: the FROST database has a statement timeout and one UPDATE over the
whole history exceeds it, so every statement covers a single datastream, split
into ID ranges of at most about --batch-rows rows when the datastream is large.
Each UPDATE commits on its own; a session stops at its first error.

How it connects
---------------
It pipes SQL into psql inside the FROST database container, the same path as a
manual ``ssh <host> 'cd /opt/frost && docker compose exec -T frost-db psql ...'``.
psql there connects as the container's own $POSTGRES_USER to $POSTGRES_DB, so
this script never handles, needs or prints a credential. Use --ssh for the
FROST host, or --container for a local database container (e.g. one started
from docker-compose.test.yaml). The scan and verification sessions run READ ONLY.

Usage
-----
    # Dry run (default): per-datastream and total counts, value ranges,
    # before/after samples and the planned UPDATE batches. Writes nothing.
    python scripts/fix_sc_pressure_units.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner

    # Canary: convert one datastream, then check it in the FROST API.
    python scripts/fix_sc_pressure_units.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner \\
        --apply --ids-file sc_pressure_ids.csv --datastream 16

    # Write everything (owner-approved): scan, UPDATE batches, re-scan to verify.
    python scripts/fix_sc_pressure_units.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner \\
        --apply --ids-file sc_pressure_ids.csv

--apply appends every converted observation to --ids-file as
``datastream_id,observation_id``: the exact record of the change. To revert, load
those IDs into a table on the FROST database and, per datastream, set
RESULT_STRING = trim_scale(RESULT_STRING::numeric * 100)::text and RESULT_NUMBER
to the same value as double precision, for the listed IDs whose RESULT_NUMBER is
still below 2000.
"""

from __future__ import annotations

import argparse
import math
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

# Run as `python scripts/fix_sc_pressure_units.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.sources.sensor_community import PA_BAND, PA_TO_HPA

RunSql = Callable[[str], list[str]]

THING_NAME_LIKE = r"GEO\_SensorCommunity %"  # \_ : a literal underscore, not LIKE's wildcard
THING_SOURCE = "sensor_community"
OBS_PROPERTY_NAME = "Air pressure"
UNIT_SYMBOL = "hPa"

LOW, HIGH = PA_BAND

# The same open band as the mapper; RESULT_TYPE 0 = number.
IN_BAND = f'"RESULT_TYPE" = 0 AND "RESULT_NUMBER" > {LOW!r} AND "RESULT_NUMBER" < {HIGH!r}'

# The connector records each reading's own observed property in PARAMETERS. These
# datastreams also hold readings of other properties (PM10, PM2.5, humidity) routed
# there by a separate connector bug; never convert a row tagged as something else.
IS_PRESSURE = """coalesce("PARAMETERS"->>'observed_property', 'pressure') = 'pressure'"""

# Rows to convert.
TO_CONVERT = f"{IN_BAND} AND {IS_PRESSURE}"

# The converted value, as an exact decimal. PostgreSQL casts double precision to
# numeric with 15 significant digits, more than any stored Pa value carries;
# trim_scale drops the zeros the shift can leave (100000 -> 1000, not 1000.00).
NEW_VALUE = f'trim_scale("RESULT_NUMBER"::numeric * {PA_TO_HPA})'

# RESULT_STRING should be RESULT_NUMBER as text; the scan counts rows where it is not.
_PLAIN_NUMBER = "'^[-+]?[0-9]+([.][0-9]+)?([eE][-+]?[0-9]+)?$'"
TEXT_MATCHES_NUMBER = (
    f'coalesce(CASE WHEN "RESULT_STRING" ~ {_PLAIN_NUMBER} '
    'THEN "RESULT_STRING"::numeric::double precision = "RESULT_NUMBER" END, false)'
)

READ_ONLY_SESSION = (
    "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;\n"
    "SET application_name = 'fix_sc_pressure_units (read-only)';\n"
)
WRITE_SESSION = "SET application_name = 'fix_sc_pressure_units';\nSET lock_timeout = '10s';\n"

# psql inside the database container: no psqlrc, quiet (no command tags), unaligned
# '|'-separated tuples only, stop at the first error. The container's shell expands
# $POSTGRES_USER and $POSTGRES_DB from the container's own environment.
CONTAINER_PSQL = 'psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------


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
       max("RESULT_NUMBER") FILTER (WHERE {TO_CONVERT}),
       min("ID") FILTER (WHERE {TO_CONVERT}),
       max("ID") FILTER (WHERE {TO_CONVERT})
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


def update_sql(ds_id: int, first_id: int, last_id: int) -> str:
    """Convert the in-band rows of one datastream within one ID range; report their IDs."""
    return f"""
WITH changed AS (
    UPDATE "OBSERVATIONS"
       SET "RESULT_NUMBER" = {NEW_VALUE}::double precision,
           "RESULT_STRING" = {NEW_VALUE}::text
     WHERE "DATASTREAM_ID" = {int(ds_id)} AND "ID" BETWEEN {int(first_id)} AND {int(last_id)}
       AND {TO_CONVERT}
    RETURNING "ID"
)
SELECT 'updated', {int(ds_id)}, {int(first_id)}, {int(last_id)}, count(*),
       coalesce(string_agg("ID"::text, ' ' ORDER BY "ID"), '')
  FROM changed;
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
    min_id: int | None
    max_id: int | None

    @property
    def other(self) -> int:
        """Pressure readings whose result is not a number (or NULL); left as is."""
        return self.rows - self.to_convert - self.misrouted - self.below_band - self.above_band


def _tagged(lines: Sequence[str], tag: str, width: int) -> list[list[str]]:
    rows = []
    for line in lines:
        fields = line.split("|")
        if fields[0] != tag:
            continue
        if tag == "ds":  # the name is last and may contain '|'
            fields = line.split("|", width)
        if len(fields) != width + 1:
            raise ValueError(f"unexpected psql output line: {line!r}")
        rows.append(fields[1:])
    return rows


def _opt_float(text: str) -> float | None:
    return float(text) if text else None


def _opt_int(text: str) -> int | None:
    return int(text) if text else None


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
            min_id=_opt_int(f[10]),
            max_id=_opt_int(f[11]),
        )
        for f in _tagged(lines, "stat", 12)
    ]


def parse_updated(lines: Sequence[str]) -> list[tuple[int, list[int]]]:
    """(datastream ID, converted observation IDs) per UPDATE statement."""
    batches = []
    for ds_id, first, last, count, ids in _tagged(lines, "updated", 5):
        converted = [int(i) for i in ids.split()]
        if len(converted) != int(count):
            raise ValueError(f"datastream {ds_id}: {count} rows updated but {len(converted)} IDs reported")
        if any(not int(first) <= i <= int(last) for i in converted):
            raise ValueError(f"datastream {ds_id}: reported IDs outside {first}..{last}")
        batches.append((int(ds_id), converted))
    return batches


def to_hpa(value: float) -> Decimal:
    """What the UPDATE turns ``value`` into (for display)."""
    return Decimal(repr(value)) * PA_TO_HPA


def plan_batches(stats: Stats, batch_rows: int) -> list[tuple[int, int]]:
    """Split the rows to convert into inclusive ID ranges of about batch_rows rows.

    A datastream's rows are spread over the whole ID sequence (all datastreams
    share it), so equal ID ranges hold roughly equal numbers of rows.
    """
    if stats.to_convert == 0 or stats.min_id is None or stats.max_id is None:
        return []
    span = stats.max_id - stats.min_id + 1
    parts = min(math.ceil(stats.to_convert / batch_rows), span)
    bounds = [stats.min_id + span * i // parts for i in range(parts + 1)]
    return [(bounds[i], bounds[i + 1] - 1) for i in range(parts)]


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
    argv = ["ssh", "-o", "BatchMode=yes"]
    if ssh_key:
        argv += ["-i", os.path.expanduser(ssh_key)]
    return [*argv, ssh, remote]


def psql_runner(argv: list[str], timeout: float) -> RunSql:
    """Run one psql session per call: SQL on stdin, result lines back."""

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
            raise PsqlError(f"psql session did not finish within {timeout:.0f} s") from exc
        lines = proc.stdout.splitlines()
        if proc.returncode != 0:
            if lines and not proc.stdout.endswith("\n"):
                lines.pop()  # cut off mid-line
            raise PsqlError(f"psql exited with {proc.returncode}: {proc.stderr.strip()}", lines)
        return lines

    return run


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _chunks(items: Sequence[int], size: int) -> list[Sequence[int]]:
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
              " the UPDATE rewrites both from RESULT_NUMBER.")


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


def apply(run_sql: RunSql, plan: dict[int, list[tuple[int, int]]], chunk: int, ids_file: Path) -> int:
    """Run the UPDATE batches, `chunk` datastreams per session. Returns rows converted.

    Appends every converted observation to ids_file as ``datastream_id,observation_id``,
    as each session ends (also when it fails part-way): an exact record of the change.
    """

    def record(batches: list[tuple[int, list[int]]]) -> int:
        with ids_file.open("a", encoding="utf-8", newline="\n") as out:  # LF, also on Windows
            for ds_id, obs_ids in batches:
                out.writelines(f"{ds_id},{obs_id}\n" for obs_id in obs_ids)
        return sum(len(obs_ids) for _, obs_ids in batches)

    def salvage(lines: Sequence[str]) -> list[tuple[int, list[int]]]:
        """What a failed session reported before it stopped; a cut-off last line is skipped."""
        batches = []
        for line in lines:
            try:
                batches += parse_updated([line])
            except ValueError:
                _progress(f"  WARNING: unreadable psql output skipped: {line[:80]!r}")
        return batches

    if not ids_file.exists() or ids_file.stat().st_size == 0:
        ids_file.write_text("datastream_id,observation_id\n", encoding="utf-8", newline="\n")
    converted = 0
    ds_ids = [i for i, batches in plan.items() if batches]
    for part in _chunks(ds_ids, chunk):
        sql = WRITE_SESSION + "".join(update_sql(i, lo, hi) for i in part for lo, hi in plan[i])
        try:
            lines = run_sql(sql)
        except PsqlError as exc:
            done = converted + record(salvage(exc.lines))
            raise PsqlError(
                f"{exc} (rows converted before the error: {done:,}; re-running is safe, with a smaller "
                "--batch-rows if a statement timed out)"
            ) from exc
        converted += record(parse_updated(lines))
        _progress(f"  converted {converted:,} rows so far (datastream {part[-1]} done)")
    return converted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert Sensor.Community Air pressure observations in FROST from Pa to hPa.",
        epilog="Dry run unless --apply is given.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--ssh", metavar="USER@HOST", help="SSH destination of the FROST host")
    target.add_argument("--container", metavar="NAME", help="local docker container running the FROST database")
    parser.add_argument("--ssh-key", metavar="PATH", help="identity file for --ssh")
    parser.add_argument("--compose-dir", default="/opt/frost", help="compose directory on the host (%(default)s)")
    parser.add_argument("--service", default="frost-db", help="database service in that compose project (%(default)s)")
    parser.add_argument("--apply", action="store_true", help="write the conversion (default: dry run)")
    parser.add_argument("--ids-file", type=Path, metavar="PATH",
                        help="required with --apply: CSV that every converted observation ID is appended to")
    parser.add_argument("--datastream", type=int, action="append", metavar="ID",
                        help="limit to this datastream ID (repeatable), e.g. for a canary")
    parser.add_argument("--batch-rows", type=int, default=5000, help="max rows per UPDATE, about (%(default)s)")
    parser.add_argument("--chunk", type=int, default=50, help="datastreams per psql session (%(default)s)")
    parser.add_argument("--samples", type=int, default=5, help="before/after samples to show (%(default)s)")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds per psql session (%(default)s)")
    args = parser.parse_args(argv)
    if args.batch_rows < 1 or args.chunk < 1:
        parser.error("--batch-rows and --chunk must be positive")
    if args.apply and args.ids_file is None:
        parser.error("--apply needs --ids-file: the record of converted observations (for audit and rollback)")
    return args


def run(args: argparse.Namespace, run_sql: RunSql) -> int:
    mode = "APPLY: writes to OBSERVATIONS" if args.apply else "DRY RUN: read-only, nothing is written"
    print(f'Sensor.Community "{OBS_PROPERTY_NAME}" Pa -> hPa backfill ({mode})')
    print(f"Rows: {TO_CONVERT}")
    print(f"Set:  RESULT_NUMBER = {NEW_VALUE}::double precision, RESULT_STRING = {NEW_VALUE}::text\n")

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

    plan = {d.id: plan_batches(stats[d.id], args.batch_rows) for d in datastreams}
    batches = [(i, lo, hi) for i, ranges in plan.items() for lo, hi in ranges]
    print(f"\nPlan: {len(batches):,} UPDATE statements over {sum(1 for r in plan.values() if r):,} datastreams "
          f"(ID ranges of about {args.batch_rows:,} rows or fewer), each committed on its own")
    if batches:
        print("First statement:" + update_sql(*batches[0]))

    if not args.apply:
        print("Dry run: nothing was written. Re-run with --apply to convert.")
        return 0

    _progress(f"Applying; converted observation IDs go to {args.ids_file} ...")
    converted = apply(run_sql, plan, args.chunk, args.ids_file)
    print(f"Converted {converted:,} observations (planned {sum(s.to_convert for s in stats.values()):,}); "
          f"IDs appended to {args.ids_file}.")

    _progress("Verifying, read-only ...")
    touched = [i for i, ranges in plan.items() if ranges]
    remaining = sum(s.to_convert for s in scan(run_sql, touched, args.chunk).values()) if touched else 0
    if remaining:
        print(f"Verification: {remaining:,} pressure readings still in the Pa band (new Pa rows from a connector "
              "without the fix, or a replayed DLQ?). Re-run to convert them.")
        return 1
    print("Verification: no rows left in the Pa band.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
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
    try:
        return run(args, run_sql)
    except PsqlError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
