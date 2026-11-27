#!/usr/bin/env python
"""Move KNMI rain intensity out of the solar radiation datastreams (#48).

Until 59d014a the KNMI source read the EDR parameter ``rg`` as global solar
radiation. In KNMI's 10-minute dataset ``rg`` is the rain gauge's "past 10
minute mean precipitation intensity" (mm/h), so every KNMI '... - Solar
radiation' datastream holds rain intensity labelled solar_radiation / W/m2,
from the first KNMI rows in FROST (2026-11-11) up to the deploy. From 59d014a
on, the solar radiation datastreams get ``qg`` (real W/m2) and rain intensity
goes to each station's new '... - Rainfall rate' datastream (rain_rate, mm/h).

This script moves those old rows, in place, to their station's rain_rate
datastream and relabels their PARAMETERS (observed_property rain_rate, unit
mm/h). Ids, phenomenon and result times, results and every other parameter
stay as they are. FEATURE_ID stays too: both datastreams belong to one Thing.

Which rows
----------
Per KNMI Thing (PROPERTIES->>'source' = 'knmi_weather', never a TGV Thing):
the rows of its solar_radiation datastream with ID <= --max-id that are
labelled solar_radiation. --max-id is the last row the old code wrote: on
frost.geo-insights.nl 48614867 (the restarted connector's first qg rows start
at 48629671). Rows above it are real solar radiation and stay. A row whose
rain_rate datastream already holds a row at its phenomenon time ("twin") is
left and reported: the old code wrote rg for 11:00 on 2026-11-27 into solar
radiation, and the restarted connector wrote the same reading into rain_rate.
--twins-file FILE lists the twins whose rain_rate copy has the same result,
as cleanup_frost_observations.py --ids-file reads it (observation_id,
datastream_id); that script deletes them with its archive (not with
--require-twin: its twin must carry the same label, and these do not by
design). Skipped and reported: a Thing without a rain_rate datastream
(the connector registers it on its first KNMI cycle after the deploy) and a
Thing with several solar_radiation or rain_rate datastreams. --datastreams
ID,... limits the run to those solar_radiation datastreams: a canary.

FROST's UPDATE trigger ignores a changed DATASTREAM_ID, so after the moves (in
a finally block: also after an error) the script recomputes phenomenonTime and
resultTime of every datastream it touched; --apply and --revert also start by
recomputing those named in the rollback CSV (repairs a run killed before its
finally).

Safety
------
* Dry run by default: read-only sessions, nothing written to the database and
  no file but the --twins-file asked for.
* --apply needs --rollback-csv FILE: each batch's rows (observation id, from and
  to datastream) are appended and fsync'ed before its UPDATE is sent. An UPDATE
  moves a listed row only while it is still in its solar_radiation datastream,
  labelled solar_radiation, at or below --max-id and without a twin.
* Writes run in cleanup_frost_observations.py's write session (its
  application_name and advisory lock): the script refuses while a repair script
  writes, and they refuse while it writes.
* After --apply the rows are counted again: none of the planned ones may be left.
* --revert --rollback-csv FILE moves the recorded rows back and restores their
  labels (dry run unless --apply), only those still where this script put them.
* Production run only with the owner's OK, after a canary.

Exit status: 0 done (and verified); 1 error; 2 usage; 3 refused to write (a
repair script's write session is on the server); 4 verification found rows
still to move; 130 interrupted.

Usage (production)
------------------
    python scripts/fix_knmi_rain_rate.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner --max-id 48614867
    python scripts/fix_knmi_rain_rate.py --ssh ... --max-id 48614867 --datastreams 18 --apply --rollback-csv knmi_rain.csv
    python scripts/fix_knmi_rain_rate.py --ssh ... --max-id 48614867 --apply --rollback-csv knmi_rain.csv
    python scripts/fix_knmi_rain_rate.py --ssh ... --max-id 48614867 --revert --rollback-csv knmi_rain.csv [--apply]
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

# Run as `python scripts/fix_knmi_rain_rate.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.sta.canonical import CanonicalDatastream
from scripts import cleanup_frost_observations as cleanup
from scripts import fix_sc_pressure_units as pressure

RunSql = cleanup.RunSql
PsqlError = cleanup.PsqlError
EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_VERIFY, EXIT_INTERRUPTED = (
    cleanup.EXIT_OK, cleanup.EXIT_ERROR, cleanup.EXIT_REFUSED, cleanup.EXIT_VERIFY, cleanup.EXIT_INTERRUPTED)
END = cleanup.END
SOURCE = "knmi_weather"
SOLAR = CanonicalDatastream.SOLAR_RADIATION.value
RAIN = CanonicalDatastream.RAIN_RATE.value
SOLAR_UNIT = CanonicalDatastream.SOLAR_RADIATION.meta.unit
RAIN_UNIT = CanonicalDatastream.RAIN_RATE.meta.unit
ROLLBACK_HEADER = ["observation_id", "from_datastream_id", "to_datastream_id"]

SKIP_NO_RAIN = "no rain_rate datastream on the Thing yet"
SKIP_SEVERAL = "several solar_radiation or rain_rate datastreams on the Thing"

lit = pressure.sql_literal


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _chunks[T](items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# Reading and planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Station:
    thing: int
    name: str
    solar: tuple[int, ...]
    rain: tuple[int, ...]


@dataclass
class Counts:
    move: int = 0   # at or below --max-id, labelled solar_radiation, no twin
    twin: int = 0   # the same, but the rain_rate datastream holds a row at that time: left
    after: int = 0  # above --max-id: real solar radiation, stays
    other: int = 0  # at or below --max-id with another label: left


@dataclass
class Plan:
    station: Station
    counts: Counts = field(default_factory=Counts)
    skip: str = ""

    @property
    def solar(self) -> int:
        return self.station.solar[0]

    @property
    def rain(self) -> int:
        return self.station.rain[0]


def catalogue_sql() -> str:
    def ids_of(prop: str) -> str:
        return (f"""(SELECT coalesce(json_agg(d."ID" ORDER BY d."ID"), '[]'::json) FROM "DATASTREAMS" d """
                f"""WHERE d."THING_ID" = th."ID" AND d."PROPERTIES"->>'observed_property' = {lit(prop)})""")

    return f"""
SELECT json_build_object('t', 'knmi', 'thing', th."ID", 'name', th."NAME",
                         'solar', {ids_of(SOLAR)}, 'rain', {ids_of(RAIN)})::text
  FROM "THINGS" th
 WHERE th."PROPERTIES"->>'source' = {lit(SOURCE)} AND NOT {cleanup.protected_thing("th")}
 ORDER BY th."ID";
"""


def load_stations(run_sql: RunSql) -> list[Station]:
    rows = cleanup._json_lines(run_sql(cleanup.READ_ONLY_SESSION + catalogue_sql()), "knmi")
    return [Station(int(r["thing"]), str(r["name"]), tuple(int(i) for i in r["solar"]),
                    tuple(int(i) for i in r["rain"])) for r in rows]


def _labelled(o: str, prop: str) -> str:
    return f"""{o}."PARAMETERS"->>'observed_property' = {lit(prop)}"""


def _twin(o: str, rain: str) -> str:
    """The rain_rate datastream ``rain`` (an SQL expression) holds a row at the phenomenon time of row ``o``."""
    return (f"""EXISTS (SELECT 1 FROM "OBSERVATIONS" x WHERE x."DATASTREAM_ID" = {rain} """
            f"""AND x."PHENOMENON_TIME_START" = {o}."PHENOMENON_TIME_START")""")


def twins_sql(pairs: Sequence[tuple[int, int]], max_id: int) -> str:
    """The twins of these solar_radiation datastreams, and whether the rain_rate copy has the same result (read-only)."""
    return f"""
SELECT 'twin', o."ID", p.solar,
       CASE WHEN EXISTS (SELECT 1 FROM "OBSERVATIONS" x WHERE x."DATASTREAM_ID" = p.rain
                          AND x."PHENOMENON_TIME_START" = o."PHENOMENON_TIME_START"
                          AND x."RESULT_NUMBER" IS NOT DISTINCT FROM o."RESULT_NUMBER") THEN 1 ELSE 0 END, '{END}'
  FROM unnest({cleanup.int_array(s for s, _ in pairs)}, {cleanup.int_array(r for _, r in pairs)}) AS p(solar, rain)
  JOIN "OBSERVATIONS" o ON o."DATASTREAM_ID" = p.solar
 WHERE o."ID" <= {int(max_id)} AND {_labelled("o", SOLAR)} AND {_twin("o", "p.rain")}
 ORDER BY o."ID";
"""


TWINS_HEADER = "observation_id,datastream_id"


def write_twins(run_sql: RunSql, plans: Sequence[Plan], max_id: int, chunk: int, path: Path) -> tuple[int, int]:
    """Write the twins whose rain_rate copy has the same result to ``path`` in the format of
    cleanup_frost_observations.py --ids-file; returns (written, twins whose copy differs: not written)."""
    rows: list[tuple[int, int, bool]] = []
    with_twins = [p for p in plans if not p.skip and p.counts.twin]
    for part in _chunks(with_twins, chunk):
        lines = run_sql(cleanup.READ_ONLY_SESSION + twins_sql([(p.solar, p.rain) for p in part], max_id))
        rows += [(int(obs), int(ds), same == "1") for obs, ds, same in cleanup._tagged(lines, "twin", 3)]
    same = [(obs, ds) for obs, ds, ok in rows if ok]
    path.write_text(TWINS_HEADER + "\n" + "".join(f"{obs},{ds}\n" for obs, ds in same), encoding="utf-8", newline="\n")
    return len(same), len(rows) - len(same)


def counts_sql(pairs: Sequence[tuple[int, int]], max_id: int) -> str:
    """Per solar_radiation datastream: its rows to move, twins, rows above --max-id and other rows (read-only)."""
    below = f'o."ID" <= {int(max_id)}'
    return f"""
SELECT json_build_object('t', 'counts', 'ds', p.solar,
       'move', count(o."ID") FILTER (WHERE {below} AND {_labelled("o", SOLAR)} AND NOT {_twin("o", "p.rain")}),
       'twin', count(o."ID") FILTER (WHERE {below} AND {_labelled("o", SOLAR)} AND {_twin("o", "p.rain")}),
       'after', count(o."ID") FILTER (WHERE o."ID" > {int(max_id)}),
       'other', count(o."ID") FILTER (WHERE {below} AND NOT {_labelled("o", SOLAR)}))::text
  FROM unnest({cleanup.int_array(s for s, _ in pairs)}, {cleanup.int_array(r for _, r in pairs)}) AS p(solar, rain)
  LEFT JOIN "OBSERVATIONS" o ON o."DATASTREAM_ID" = p.solar
 GROUP BY p.solar;
"""


def count_rows(run_sql: RunSql, plans: Sequence[Plan], max_id: int, chunk: int) -> None:
    """Fill in the counts of the plans that are not skipped, ``chunk`` datastreams per read-only session."""
    todo = [p for p in plans if not p.skip]
    by_solar = {p.solar: p for p in todo}
    counted = 0
    for part in _chunks(todo, chunk):
        lines = run_sql(cleanup.READ_ONLY_SESSION + counts_sql([(p.solar, p.rain) for p in part], max_id))
        seen = set()
        for r in cleanup._json_lines(lines, "counts"):
            ds = int(r["ds"])
            by_solar[ds].counts = Counts(int(r["move"]), int(r["twin"]), int(r["after"]), int(r["other"]))
            seen.add(ds)
        missing = {p.solar for p in part} - seen
        if missing:
            raise PsqlError(f"no counts for datastreams {sorted(missing)}")
        counted += len(part)
        _progress(f"  counted {counted:,}/{len(todo):,} datastreams")


def build_plans(stations: Iterable[Station], only: Sequence[int] | None = None) -> list[Plan]:
    plans = []
    wanted = set(only or ())
    for st in stations:
        if wanted and not wanted & set(st.solar):
            continue
        if not st.solar:
            continue  # nothing to move from
        if len(st.solar) > 1 or len(st.rain) > 1:
            plans.append(Plan(st, skip=SKIP_SEVERAL))
        elif not st.rain:
            plans.append(Plan(st, skip=SKIP_NO_RAIN))
        else:
            plans.append(Plan(st))
    return plans


def report(plans: Sequence[Plan], examples: int = 5) -> None:
    active = [p for p in plans if not p.skip]
    total = Counts(*(sum(getattr(p.counts, f) for p in active) for f in ("move", "twin", "after", "other")))
    print(f"KNMI Things with a solar_radiation datastream: {len(plans):,}; skipped: {len(plans) - len(active):,}")
    print(f"  rows to move to rain_rate: {total.move:,} (in {sum(1 for p in active if p.counts.move):,} datastreams)")
    print(f"  twins (rain_rate already holds a row at that time), left: {total.twin:,}")
    print(f"  rows above --max-id (real solar radiation), staying: {total.after:,}")
    print(f"  rows at or below --max-id with another label, left: {total.other:,}")
    for reason, n in Counter(p.skip for p in plans if p.skip).most_common():
        names = ", ".join([p.station.name for p in plans if p.skip == reason][:examples])
        print(f"  skipped, {reason}: {n:,} (e.g. {names})")
    for p in [p for p in active if p.counts.move][:examples]:
        print(f"  e.g. {p.station.name}: {p.counts.move:,} rows, datastream {p.solar} -> {p.rain}")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def select_sql(plan: Plan, max_id: int, after_id: int, limit: int) -> str:
    """The next rows to move of one solar_radiation datastream (read-only)."""
    return f"""
SELECT 'row', o."ID", '{END}'
  FROM "OBSERVATIONS" o
 WHERE o."DATASTREAM_ID" = {plan.solar} AND o."ID" > {int(after_id)} AND o."ID" <= {int(max_id)}
   AND {_labelled("o", SOLAR)} AND NOT {_twin("o", str(plan.rain))}
 ORDER BY o."ID"
 LIMIT {int(limit)};
"""


def move_sql(plan: Plan, ids: Sequence[int], max_id: int) -> str:
    """Move the listed rows still in the solar_radiation datastream, labelled so and without a twin; relabel them."""
    return f"""
WITH moved AS (
    UPDATE "OBSERVATIONS" o
       SET "DATASTREAM_ID" = {plan.rain},
           "PARAMETERS" = o."PARAMETERS" || jsonb_build_object('observed_property', {lit(RAIN)}, 'unit', {lit(RAIN_UNIT)})
     WHERE o."ID" = ANY({cleanup.int_array(ids)}) AND o."DATASTREAM_ID" = {plan.solar} AND o."ID" <= {int(max_id)}
       AND {_labelled("o", SOLAR)} AND NOT {_twin("o", str(plan.rain))}
    RETURNING 1
)
SELECT 'moved', {plan.solar}, {len(ids)}, count(*), '{END}' FROM moved;
"""


def restore_sql(from_ds: int, to_ds: int, ids: Sequence[int]) -> str:
    """Move recorded rows still in ``to_ds`` and labelled rain_rate back to ``from_ds``; restore their labels."""
    return f"""
WITH restored AS (
    UPDATE "OBSERVATIONS" o
       SET "DATASTREAM_ID" = {int(from_ds)},
           "PARAMETERS" = o."PARAMETERS" || jsonb_build_object('observed_property', {lit(SOLAR)}, 'unit', {lit(SOLAR_UNIT)})
     WHERE o."ID" = ANY({cleanup.int_array(ids)}) AND o."DATASTREAM_ID" = {int(to_ds)} AND {_labelled("o", RAIN)}
    RETURNING 1
)
SELECT 'restored', {int(from_ds)}, {len(ids)}, count(*), '{END}' FROM restored;
"""


def _counts(lines: Iterable[str], tag: str) -> tuple[int, int]:
    rows = cleanup._tagged(lines, tag, 3)
    if len(rows) != 1:
        raise PsqlError(f"expected one '{tag}' line, got {len(rows)}")
    _, listed, count = (int(v) for v in rows[0])
    return listed, count


def prepare_rollback(path: Path) -> None:
    """A new file gets the header; an existing one must be this script's (re-runs append)."""
    if path.exists() and path.stat().st_size:
        with path.open(encoding="utf-8", newline="") as f:
            header = next(csv.reader(f), [])
        if header != ROLLBACK_HEADER:
            raise ValueError(f"{path} is not a rollback CSV of this script (header {header})")
        return
    cleanup._append(path, ",".join(ROLLBACK_HEADER) + "\n")


def read_rollback(path: Path) -> list[tuple[int, int, int]]:
    """(observation id, from, to) per recorded row; a cut-off last line is dropped (its UPDATE was never sent)."""
    out: dict[int, tuple[int, int, int]] = {}
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        if next(reader, []) != ROLLBACK_HEADER:
            raise ValueError(f"{path} is not a rollback CSV of this script")
        for row in reader:
            if len(row) != 3 or not all(v.isdigit() for v in row):
                continue
            obs, frm, to = (int(v) for v in row)
            out.setdefault(obs, (obs, frm, to))
    return list(out.values())


def _csv_lines(rows: Iterable[Sequence[object]]) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


def recorded_datastreams(path: Path) -> set[int]:
    if not path.exists() or not path.stat().st_size:
        return set()
    return {ds for _, frm, to in read_rollback(path) for ds in (frm, to)}


@dataclass
class Applied:
    moved: int = 0
    kept: int = 0  # changed since they were selected: left
    touched: set[int] = field(default_factory=set)


def apply_plans(run_sql: RunSql, plans: Sequence[Plan], args: argparse.Namespace) -> Applied:
    done = Applied()
    try:
        for plan in plans:
            after = 0
            while True:
                rows = cleanup._tagged(run_sql(cleanup.READ_ONLY_SESSION + select_sql(plan, args.max_id, after,
                                                                                        args.batch)), "row", 1)
                ids = [int(r[0]) for r in rows]
                if not ids:
                    break
                cleanup._append(args.rollback_csv, _csv_lines((i, plan.solar, plan.rain) for i in ids))
                done.touched |= {plan.solar, plan.rain}
                try:
                    lines = run_sql(cleanup.WRITE_SESSION + move_sql(plan, ids, args.max_id))
                except PsqlError as exc:
                    raise PsqlError(f"{exc}\n  Every row this session can move is in {args.rollback_csv}. "
                                    "Re-running is safe.") from exc
                listed, count = _counts(lines, "moved")
                done.moved += count
                done.kept += listed - count
                after = max(ids)
            _progress(f"  {plan.station.name}: moved; {done.moved:,} rows so far")
    finally:
        if done.touched:
            _progress(f"Recomputing the times of {len(done.touched):,} datastreams ...")
            cleanup.refresh(run_sql, done.touched, args.chunk)
    return done


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def run_fix(args: argparse.Namespace, run_sql: RunSql) -> int:
    mode = ("APPLY: moves rows to the rain_rate datastreams (rollback CSV first)" if args.apply
            else "DRY RUN: read-only, nothing is written")
    print(f"KNMI rain intensity out of solar_radiation, #48, rows with ID <= {args.max_id:,} ({mode})")
    if args.apply:
        prepare_rollback(args.rollback_csv)
        earlier = recorded_datastreams(args.rollback_csv)
        if earlier:
            if cleanup.refuse_if_busy(run_sql):
                return EXIT_REFUSED
            _progress(f"Recomputing the times of the {len(earlier):,} datastreams an earlier run recorded ...")
            cleanup.refresh(run_sql, earlier, args.chunk)
    _progress("Reading KNMI Things and their datastreams, read-only ...")
    stations = load_stations(run_sql)
    known = {ds for st in stations for ds in st.solar}
    unknown = sorted(set(args.datastreams or ()) - known)
    if unknown:
        raise ValueError(f"not solar_radiation datastreams of a KNMI Thing: {unknown}")
    plans = build_plans(stations, args.datastreams)
    _progress("Counting their rows, read-only ...")
    count_rows(run_sql, plans, args.max_id, args.chunk)
    report(plans)
    if args.twins_file is not None:
        written, differing = write_twins(run_sql, plans, args.max_id, args.chunk, args.twins_file)
        print(f"Twins file {args.twins_file}: {written:,} rows whose rain_rate copy has the same result "
              f"(for cleanup_frost_observations.py --ids-file); {differing:,} twins with another result, not listed.")
    todo = [p for p in plans if not p.skip and p.counts.move]
    if not args.apply:
        print("\nDry run: nothing was written. Re-run with --apply --rollback-csv FILE to move the rows.")
        return EXIT_OK
    if not todo:
        print("Nothing to move.")
        return EXIT_OK
    if cleanup.refuse_if_busy(run_sql):
        return EXIT_REFUSED
    _progress(f"Applying; each batch's rows go to {args.rollback_csv} before its update ...")
    done = apply_plans(run_sql, todo, args)
    planned = sum(p.counts.move for p in todo)
    print(f"\nMoved {done.moved:,} rows (planned {planned:,}; {done.kept:,} changed since they were selected, left); "
          f"recorded in {args.rollback_csv}.")
    _progress("Verifying, read-only ...")
    for p in todo:
        p.counts = Counts()
    count_rows(run_sql, todo, args.max_id, args.chunk)
    left = sum(p.counts.move for p in todo)
    if left:
        print(f"Verification: {left:,} rows still to move. Re-run with the same file.")
        return EXIT_VERIFY
    print(f"Verification: no rows left to move in the {len(todo):,} datastreams.")
    return EXIT_OK


def run_revert(args: argparse.Namespace, run_sql: RunSql) -> int:
    records = read_rollback(args.rollback_csv)
    print(f"Revert {len(records):,} rows from {args.rollback_csv} ({'APPLY' if args.apply else 'DRY RUN: nothing is written'})")
    groups: defaultdict[tuple[int, int], list[int]] = defaultdict(list)
    for obs, frm, to in records:
        groups[(frm, to)].append(obs)
    print(f"  in {len(groups):,} pairs of datastreams")
    if not args.apply:
        return EXIT_OK
    if cleanup.refuse_if_busy(run_sql):
        return EXIT_REFUSED
    touched = {ds for pair in groups for ds in pair}
    restored = 0
    try:
        for (frm, to), ids in sorted(groups.items()):
            for part in _chunks(sorted(ids), args.batch):
                restored += _counts(run_sql(cleanup.WRITE_SESSION + restore_sql(frm, to, part)), "restored")[1]
    finally:
        cleanup.refresh(run_sql, touched, args.chunk)
    print(f"Restored {restored:,} of {len(records):,} recorded rows (the others were no longer where this script put them).")
    return EXIT_OK


def _id_list(text: str) -> list[int]:
    try:
        ids = [int(part) for part in text.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a list of datastream ids: {text!r}") from exc
    if not ids:
        raise argparse.ArgumentTypeError("no datastream ids given")
    return ids


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move KNMI rain intensity out of the solar radiation datastreams (#48).",
        epilog="Dry run unless --apply is given. The module docstring has the rules and the runbook.",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--ssh", metavar="USER@HOST", help="SSH destination of the FROST host")
    target.add_argument("--container", metavar="NAME", help="local docker container running the FROST database")
    parser.add_argument("--ssh-key", metavar="PATH", help="identity file for --ssh")
    parser.add_argument("--compose-dir", default="/opt/frost", help="compose directory on the host (%(default)s)")
    parser.add_argument("--service", default="frost-db", help="database service in that compose project (%(default)s)")
    parser.add_argument("--max-id", type=int, required=True,
                        help="the last row the old code wrote; rows above it are real solar radiation")
    parser.add_argument("--datastreams", type=_id_list, metavar="ID,...",
                        help="limit to these solar_radiation datastreams (canary)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--rollback-csv", type=Path, metavar="FILE",
                        help="with --apply: CSV of the moved rows; with --revert: that file")
    parser.add_argument("--revert", action="store_true", help="move the rows of --rollback-csv back")
    parser.add_argument("--twins-file", type=Path, metavar="FILE",
                        help="write the twins whose rain_rate copy has the same result, for "
                             "cleanup_frost_observations.py --ids-file (also in a dry run)")
    parser.add_argument("--batch", type=int, default=2000, help="rows per UPDATE (%(default)s)")
    parser.add_argument("--chunk", type=int, default=10, help="datastreams per counting session (%(default)s)")
    parser.add_argument("--timeout", type=float, default=600, help="seconds per psql session (%(default)s)")
    args = parser.parse_args(argv)
    if not (args.ssh or args.container):
        parser.error("one of --ssh or --container is required")
    if args.batch < 1 or args.chunk < 1 or args.max_id < 1:
        parser.error("--batch, --chunk and --max-id must be positive")
    if args.revert:
        if args.rollback_csv is None or not args.rollback_csv.is_file():
            parser.error("--revert needs an existing --rollback-csv FILE")
        if args.datastreams:
            parser.error("--revert takes every row of the rollback CSV; it does not take --datastreams")
    elif args.apply and args.rollback_csv is None:
        parser.error("--apply needs --rollback-csv FILE")
    return args


def main(argv: Sequence[str] | None = None, *, run_sql: RunSql | None = None) -> int:
    args = parse_args(argv)
    if run_sql is None:
        run_sql = pressure.psql_runner(
            pressure.psql_argv(ssh=args.ssh, ssh_key=args.ssh_key, compose_dir=args.compose_dir,
                               service=args.service, container=args.container),
            args.timeout,
        )
    note = " Every row moved is in the rollback CSV; run the same command again."
    try:
        if args.revert:
            return run_revert(args, run_sql)
        return run_fix(args, run_sql)
    except PsqlError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if cleanup._interrupted(exc):
            print(f"Interrupted.{note if args.apply else ''}", file=sys.stderr)
            return EXIT_INTERRUPTED
        return EXIT_ERROR
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(f"Interrupted.{note if args.apply else ''}", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
