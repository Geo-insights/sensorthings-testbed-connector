#!/usr/bin/env python
"""Give each Samen Meten datastream in one FROST database the sensor_id its readings carry (#45).

Until #45 the connector registered Samen Meten datastreams with
properties.sensor_id = 'sm-{RIVM Thing name, lowercased}', while their readings
carry PARAMETERS.sensor_id = 'sm-{RIVM Thing id}'. From #45 on both use the RIVM
Thing id (app.sources.samen_meten.samen_meten_sensor_id). This script sets the
existing datastreams' properties.sensor_id to that id in place: entity ids,
names, streamKey, observed_property and every other property stay as they are.

Scope
-----
Datastreams of Things with PROPERTIES->>'source' = 'samen_meten' (never a TGV
Thing, as in cleanup_frost_observations.py). --datastreams ID,... limits the
writes to those datastreams: a canary.

The new sensor_id, by the first rule that gives one
--------------------------------------------------
1. Its own rows: the distinct PARAMETERS->>'sensor_id' of its observations.
2. No rows: the same over all datastreams of its Thing.
3. No rows on the Thing: RIVM's Thing named the Thing's
   PROPERTIES->>'original_name' (GET /Things?$select=id,name, every page).
Skipped and reported, left as they are: rows with more than one sensor_id, a
sensor_id not of the form sm-<digits>, a name RIVM has no Thing or several
Things for, and (cross-check) rows whose id differs from the id of RIVM's only
Thing of that name. A datastream that already holds its new id is not touched,
so a re-run plans nothing.

Order with the #45 deploy
-------------------------
Either order is safe. The connector finds Samen Meten datastreams by name, so
neither the code nor this script creates or re-links one. When a reading's
cached datastream lists another sensor_id, routing falls back to the live
lookup by Thing name and streamKey (one lookup per datastream, then cached).
Running this script first means the deploy changes nothing that is visible.
streamKey is the observed property, not the sensor id: it is not changed.

Safety
------
* Dry run by default: read-only sessions, nothing written, not even a file.
* --apply needs --rollback-csv FILE: each datastream's old and new sensor_id are
  appended and fsync'ed before its UPDATE is sent, and the UPDATE changes a
  datastream only while its sensor_id is still the recorded old one.
* Writes run in cleanup_frost_observations.py's write session (its
  application_name and advisory lock): the script refuses while a repair script
  writes, and they refuse while it writes.
* After --apply the datastreams are read again: each one updated must hold its
  new sensor_id.
* --revert --rollback-csv FILE sets them back (dry run unless --apply), only
  where the sensor_id is still the one this script wrote.
* Production run only with the owner's OK, after a canary.

Exit status: 0 done (and verified); 1 error; 2 usage; 3 refused to write (a
repair script's write session is on the server); 4 verification found a
datastream without its new id; 130 interrupted.

Usage (production)
------------------
    python scripts/fix_samen_meten_sensor_ids.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner
    python scripts/fix_samen_meten_sensor_ids.py --ssh ... --datastreams 4231,4232 --apply --rollback-csv sm_ids.csv
    python scripts/fix_samen_meten_sensor_ids.py --ssh ... --apply --rollback-csv sm_ids.csv
    python scripts/fix_samen_meten_sensor_ids.py --ssh ... --revert --rollback-csv sm_ids.csv [--apply]
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Run as `python scripts/fix_samen_meten_sensor_ids.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.sources.samen_meten import samen_meten_sensor_id
from scripts import cleanup_frost_observations as cleanup
from scripts import fix_samen_meten_calibrated as sm_kal
from scripts import fix_sc_pressure_units as pressure

RunSql = cleanup.RunSql
PsqlError = cleanup.PsqlError
EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_VERIFY, EXIT_INTERRUPTED = (
    cleanup.EXIT_OK, cleanup.EXIT_ERROR, cleanup.EXIT_REFUSED, cleanup.EXIT_VERIFY, cleanup.EXIT_INTERRUPTED)
END = cleanup.END
SOURCE = "samen_meten"
ROLLBACK_HEADER = ["id", "old", "new"]

_SENSOR_ID = re.compile(r"sm-([0-9]+)")

# Where the new sensor_id comes from.
RULE_ROWS = "its own rows"
RULE_THING = "the rows of its Thing's other datastreams"
RULE_RIVM = "RIVM's Thing of the same name (no rows on the Thing)"
RULES = (RULE_ROWS, RULE_THING, RULE_RIVM)

# Why a datastream is left as it is.
SKIP_SEVERAL = "rows carry more than one sensor_id"
SKIP_FORM = "rows' sensor_id is not sm-<RIVM Thing id>"
SKIP_MISMATCH = "rows' sensor_id differs from RIVM's only Thing of that name"
SKIP_NO_NAME = "no rows on the Thing, and the Thing has no original_name"
SKIP_NOT_AT_RIVM = "no rows on the Thing, and RIVM has no Thing of that name"
SKIP_NAME_TWICE = "no rows on the Thing, and RIVM has several Things of that name"
SKIP_NO_RIVM = "no rows on the Thing, and RIVM was not read (--no-rivm)"

RivmNames = dict[str, list[int]]


@dataclass(frozen=True)
class SmDatastream:
    id: int
    thing: int
    sensor_id: str | None
    original_name: str | None
    row_ids: frozenset[str | None]  # distinct PARAMETERS->>'sensor_id' of its observations


@dataclass(frozen=True)
class Plan:
    ds: SmDatastream
    new: str | None = None
    rule: str = ""
    skip: str = ""

    @property
    def change(self) -> bool:
        return self.new is not None and self.new != self.ds.sensor_id


class RivmThings(sm_kal.RivmClient):
    """The Samen Meten STA's Things, read-only (timeouts and retries of the #41 client).

    Paged by id ($filter=id gt <last>, $orderby=id), not by @iot.nextLink: RIVM
    serves at most 400 per page, and its $skip pages time out (HTTP 504) from
    about $skip=5000, while an id-filtered page comes back in seconds.
    """

    PAGE = 400

    def names(self) -> RivmNames:
        out: defaultdict[str, list[int]] = defaultdict(list)
        last = 0
        for _ in range(self.MAX_PAGES):
            data = self.get(f"{self.base_url}/Things", {"$filter": f"id gt {last}", "$orderby": "id asc",
                                                        "$top": str(self.PAGE), "$select": "id,name"})
            things = [t for t in data.get("value", []) if isinstance(t, dict) and "@iot.id" in t]
            for thing in things:
                if thing.get("name"):
                    out[str(thing["name"])].append(int(thing["@iot.id"]))
            ids = [int(t["@iot.id"]) for t in things]
            if not ids or not data.get("@iot.nextLink"):
                return dict(out)
            if max(ids) <= last:
                raise sm_kal.RivmError(f"Things: page after id {last} does not move on")
            last = max(ids)
        raise sm_kal.RivmError(f"Things: more than {self.MAX_PAGES} pages")


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Reading and planning
# ---------------------------------------------------------------------------


def catalogue_sql() -> str:
    return f"""
SELECT json_build_object('t', 'smid', 'id', d."ID", 'thing', d."THING_ID", 'sid', d."PROPERTIES"->>'sensor_id',
                         'orig', th."PROPERTIES"->>'original_name',
                         'rows', (SELECT coalesce(json_agg(DISTINCT o."PARAMETERS"->>'sensor_id'), '[]'::json)
                                    FROM "OBSERVATIONS" o WHERE o."DATASTREAM_ID" = d."ID"))::text
  FROM "DATASTREAMS" d JOIN "THINGS" th ON th."ID" = d."THING_ID"
 WHERE th."PROPERTIES"->>'source' = {pressure.sql_literal(SOURCE)}
   AND NOT {cleanup.protected_thing("th")}
 ORDER BY d."ID";
"""


def parse_catalogue(lines: Iterable[str]) -> dict[int, SmDatastream]:
    out = {}
    for r in cleanup._json_lines(lines, "smid"):
        rows = r.get("rows") or []
        out[int(r["id"])] = SmDatastream(int(r["id"]), int(r["thing"]), r.get("sid"), r.get("orig"),
                                         frozenset(None if v is None else str(v) for v in rows))
    return out


def load_catalogue(run_sql: RunSql) -> dict[int, SmDatastream]:
    return parse_catalogue(run_sql(cleanup.READ_ONLY_SESSION + catalogue_sql()))


def _from_rows(ds: SmDatastream, ids: frozenset[str | None], rule: str, rivm: RivmNames | None) -> Plan:
    if len(ids) > 1:
        return Plan(ds, skip=SKIP_SEVERAL)
    (only,) = ids
    match = _SENSOR_ID.fullmatch(only or "")
    if match is None:
        return Plan(ds, skip=SKIP_FORM)
    new = samen_meten_sensor_id(int(match.group(1)))
    named = rivm.get(ds.original_name or "", []) if rivm is not None else []
    if len(named) == 1 and samen_meten_sensor_id(named[0]) != new:
        return Plan(ds, skip=SKIP_MISMATCH)
    return Plan(ds, new, rule)


def plan_one(ds: SmDatastream, thing_ids: frozenset[str | None], rivm: RivmNames | None) -> Plan:
    if ds.row_ids:
        return _from_rows(ds, ds.row_ids, RULE_ROWS, rivm)
    if thing_ids:
        return _from_rows(ds, thing_ids, RULE_THING, rivm)
    if rivm is None:
        return Plan(ds, skip=SKIP_NO_RIVM)
    if not ds.original_name:
        return Plan(ds, skip=SKIP_NO_NAME)
    named = rivm.get(ds.original_name, [])
    if not named:
        return Plan(ds, skip=SKIP_NOT_AT_RIVM)
    if len(named) > 1:
        return Plan(ds, skip=SKIP_NAME_TWICE)
    return Plan(ds, samen_meten_sensor_id(named[0]), RULE_RIVM)


def build_plans(catalogue: dict[int, SmDatastream], rivm: RivmNames | None,
                only: Sequence[int] | None = None) -> list[Plan]:
    by_thing: defaultdict[int, set[str | None]] = defaultdict(set)
    for ds in catalogue.values():
        by_thing[ds.thing] |= ds.row_ids
    wanted = sorted(set(only)) if only else sorted(catalogue)
    return [plan_one(catalogue[i], frozenset(by_thing[catalogue[i].thing]), rivm) for i in wanted]


def report(plans: Sequence[Plan], examples: int = 5) -> None:
    changes = [p for p in plans if p.change]
    done = sum(1 for p in plans if p.new is not None and not p.change)
    skipped = [p for p in plans if p.skip]
    print(f"Datastreams: {len(plans):,}; already holding their new sensor_id: {done:,}; to change: {len(changes):,}; "
          f"skipped: {len(skipped):,}")
    by_rule = Counter(p.rule for p in changes)
    for rule in RULES:
        if by_rule[rule]:
            print(f"  to change, id from {rule}: {by_rule[rule]:,}")
    for reason, n in Counter(p.skip for p in skipped).most_common():
        ids = ", ".join(str(p.ds.id) for p in [s for s in skipped if s.skip == reason][:examples])
        print(f"  skipped, {reason}: {n:,} (e.g. {ids})")
    for p in changes[:examples]:
        print(f"  e.g. datastream {p.ds.id}: {p.ds.sensor_id} -> {p.new} ({p.rule})")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _set_sql(tag: str, ids: Sequence[int], frm: Sequence[str | None], to: Sequence[str | None]) -> str:
    """Set properties.sensor_id from ``frm`` to ``to``, per datastream only while it still holds ``frm``."""
    return f"""
WITH chg AS (
    UPDATE "DATASTREAMS" d
       SET "PROPERTIES" = CASE WHEN v.to_id IS NULL THEN coalesce(d."PROPERTIES", '{{}}'::jsonb) - 'sensor_id'
                               ELSE jsonb_set(coalesce(d."PROPERTIES", '{{}}'::jsonb), '{{sensor_id}}', to_jsonb(v.to_id))
                          END
      FROM unnest({cleanup.int_array(ids)}, {sm_kal.text_array(frm)}, {sm_kal.text_array(to)}) AS v(id, from_id, to_id)
     WHERE d."ID" = v.id AND d."PROPERTIES"->>'sensor_id' IS NOT DISTINCT FROM v.from_id
    RETURNING d."ID"
)
SELECT '{tag}', {len(ids)}, count(*), '{END}' FROM chg;
"""


def update_sql(plans: Sequence[Plan]) -> str:
    return _set_sql("updated", [p.ds.id for p in plans], [p.ds.sensor_id for p in plans], [p.new for p in plans])


def _counts(lines: Iterable[str], tag: str) -> tuple[int, int]:
    rows = cleanup._tagged(lines, tag, 2)
    if len(rows) != 1:
        raise PsqlError(f"expected one '{tag}' line, got {len(rows)}")
    listed, count = (int(v) for v in rows[0])
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


def _csv_lines(rows: Iterable[Sequence[object]]) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


def read_rollback(path: Path) -> list[tuple[int, str | None, str | None]]:
    """(id, old, new) per datastream, the first record of each (a re-run records the same values again)."""
    seen: dict[int, tuple[int, str | None, str | None]] = {}
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        if next(reader, []) != ROLLBACK_HEADER:
            raise ValueError(f"{path} is not a rollback CSV of this script")
        for row in reader:
            if len(row) != 3:
                continue  # a cut-off last line: its UPDATE was never sent
            ds = int(row[0])
            seen.setdefault(ds, (ds, row[1] or None, row[2] or None))
    return list(seen.values())


@dataclass
class Applied:
    updated: int = 0
    kept: int = 0  # changed since the scan: left


def apply_plans(run_sql: RunSql, plans: Sequence[Plan], args: argparse.Namespace) -> Applied:
    done = Applied()
    for batch in sm_kal._chunks(plans, args.batch):
        cleanup._append(args.rollback_csv, _csv_lines((p.ds.id, p.ds.sensor_id or "", p.new) for p in batch))
        try:
            lines = run_sql(cleanup.WRITE_SESSION + update_sql(batch))
        except PsqlError as exc:
            raise PsqlError(f"{exc}\n  Every datastream this session can change is in {args.rollback_csv}. "
                            "Re-running is safe.") from exc
        listed, count = _counts(lines, "updated")
        done.updated += count
        done.kept += listed - count
        _progress(f"  updated {done.updated:,} datastreams so far")
    return done


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def run_fix(args: argparse.Namespace, run_sql: RunSql, rivm_names: Callable[[], RivmNames]) -> int:
    mode = ("APPLY: updates datastream properties.sensor_id (rollback CSV first)" if args.apply
            else "DRY RUN: read-only, nothing is written")
    print(f"Samen Meten datastream sensor_id = readings' sensor_id, #45 ({mode})")
    if args.apply:
        prepare_rollback(args.rollback_csv)
    _progress("Reading Samen Meten datastreams and their rows' sensor ids, read-only ...")
    catalogue = load_catalogue(run_sql)
    unknown = sorted(set(args.datastreams or ()) - set(catalogue))
    if unknown:
        raise ValueError(f"not Samen Meten datastreams (of a non-TGV Thing): {unknown}")
    rivm: RivmNames | None = None
    if not args.no_rivm:
        _progress("Reading RIVM's Things ...")
        rivm = rivm_names()
        print(f"RIVM: {sum(len(v) for v in rivm.values()):,} Things, {len(rivm):,} names")
    plans = build_plans(catalogue, rivm, args.datastreams)
    report(plans)
    todo = [p for p in plans if p.change]
    if not args.apply:
        print("\nDry run: nothing was written. Re-run with --apply --rollback-csv FILE to update.")
        return EXIT_OK
    if not todo:
        print("Nothing to update.")
        return EXIT_OK
    if cleanup.refuse_if_busy(run_sql):
        return EXIT_REFUSED
    _progress(f"Applying; old values go to {args.rollback_csv} before each update ...")
    done = apply_plans(run_sql, todo, args)
    print(f"\nUpdated {done.updated:,} datastreams (planned {len(todo):,}; {done.kept:,} changed since the scan, "
          f"left); old values in {args.rollback_csv}.")
    _progress("Verifying, read-only ...")
    again = load_catalogue(run_sql)
    wrong = [p.ds.id for p in todo if again.get(p.ds.id) is None or again[p.ds.id].sensor_id != p.new]
    if wrong:
        print(f"Verification: {len(wrong):,} datastreams do not hold their new sensor_id (e.g. {wrong[:10]}). "
              "Re-run with the same file.")
        return EXIT_VERIFY
    print(f"Verification: all {len(todo):,} updated datastreams hold their new sensor_id.")
    return EXIT_OK


def run_revert(args: argparse.Namespace, run_sql: RunSql) -> int:
    records = read_rollback(args.rollback_csv)
    print(f"Revert {len(records):,} datastreams from {args.rollback_csv} "
          f"({'APPLY' if args.apply else 'DRY RUN: nothing is written'})")
    catalogue = load_catalogue(run_sql)
    state = Counter("new" if catalogue.get(ds) is not None and catalogue[ds].sensor_id == new
                    else "old" if catalogue.get(ds) is not None and catalogue[ds].sensor_id == old
                    else "other" for ds, old, new in records)
    print(f"  holding the id this script wrote: {state['new']:,}; back at the old id: {state['old']:,}; "
          f"other or gone: {state['other']:,} (left)")
    if not args.apply:
        return EXIT_OK
    if cleanup.refuse_if_busy(run_sql):
        return EXIT_REFUSED
    reverted = 0
    for batch in sm_kal._chunks(records, args.batch):
        lines = run_sql(cleanup.WRITE_SESSION + _set_sql("reverted", [r[0] for r in batch], [r[2] for r in batch],
                                                         [r[1] for r in batch]))
        reverted += _counts(lines, "reverted")[1]
    print(f"Reverted {reverted:,} datastreams.")
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
        description="Set Samen Meten datastreams' properties.sensor_id to their readings' sensor_id (#45).",
        epilog="Dry run unless --apply is given. The module docstring has the rules and the runbook.",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--ssh", metavar="USER@HOST", help="SSH destination of the FROST host")
    target.add_argument("--container", metavar="NAME", help="local docker container running the FROST database")
    parser.add_argument("--ssh-key", metavar="PATH", help="identity file for --ssh")
    parser.add_argument("--compose-dir", default="/opt/frost", help="compose directory on the host (%(default)s)")
    parser.add_argument("--service", default="frost-db", help="database service in that compose project (%(default)s)")
    parser.add_argument("--datastreams", type=_id_list, metavar="ID,...", help="limit to these datastreams (canary)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--rollback-csv", type=Path, metavar="FILE",
                        help="with --apply: CSV of old and new sensor_id per datastream; with --revert: that file")
    parser.add_argument("--revert", action="store_true", help="set the datastreams of --rollback-csv back")
    parser.add_argument("--no-rivm", action="store_true",
                        help="do not read RIVM: no rule 3 and no cross-check (datastreams without rows are skipped)")
    parser.add_argument("--rivm-url", default=sm_kal.RIVM_URL, help="RIVM SensorThings API (%(default)s)")
    parser.add_argument("--rivm-timeout", type=float, default=60, help="seconds per RIVM request (%(default)s)")
    parser.add_argument("--rivm-retries", type=int, default=3, help="retries per RIVM request (%(default)s)")
    parser.add_argument("--batch", type=int, default=500, help="datastreams per UPDATE (%(default)s)")
    parser.add_argument("--timeout", type=float, default=600, help="seconds per psql session (%(default)s)")
    args = parser.parse_args(argv)
    if not (args.ssh or args.container):
        parser.error("one of --ssh or --container is required")
    if args.batch < 1 or args.rivm_retries < 0:
        parser.error("--batch must be positive and --rivm-retries not negative")
    if args.revert:
        if args.rollback_csv is None or not args.rollback_csv.is_file():
            parser.error("--revert needs an existing --rollback-csv FILE")
        if args.datastreams:
            parser.error("--revert takes every datastream of the rollback CSV; it does not take --datastreams")
    elif args.apply and args.rollback_csv is None:
        parser.error("--apply needs --rollback-csv FILE")
    return args


def main(argv: Sequence[str] | None = None, *, rivm_names: Callable[[], RivmNames] | None = None,
         run_sql: RunSql | None = None) -> int:
    args = parse_args(argv)
    if run_sql is None:
        run_sql = pressure.psql_runner(
            pressure.psql_argv(ssh=args.ssh, ssh_key=args.ssh_key, compose_dir=args.compose_dir,
                               service=args.service, container=args.container),
            args.timeout,
        )
    if rivm_names is None:
        client = RivmThings(args.rivm_url, timeout=args.rivm_timeout, retries=args.rivm_retries)
        rivm_names = client.names
    note = " Every datastream changed is in the rollback CSV; run the same command again."
    try:
        if args.revert:
            return run_revert(args, run_sql)
        return run_fix(args, run_sql, rivm_names)
    except PsqlError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if cleanup._interrupted(exc):
            print(f"Interrupted.{note if args.apply else ''}", file=sys.stderr)
            return EXIT_INTERRUPTED
        return EXIT_ERROR
    except (sm_kal.RivmError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(f"Interrupted.{note if args.apply else ''}", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
