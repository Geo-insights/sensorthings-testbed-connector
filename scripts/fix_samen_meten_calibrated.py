#!/usr/bin/env python
"""Repair the Samen Meten PM history in one FROST database against RIVM's calibrated (_kal) series (#41).

Until #38 the connector wrote both RIVM series of a Samen Meten PM sensor, the raw
one (``...-pm25``) and the calibrated one (``...-pm25_kal``), into one of our
datastreams: up to six rows per hour, raw and calibrated mixed. This script makes
each hour of those datastreams hold one row with RIVM's calibrated value.

Scope
-----
Datastreams whose Thing has PROPERTIES->>'source' = 'samen_meten' and whose
PROPERTIES->>'observed_property' is 'pm2_5' or 'pm10' (datastreams of TGV Things
are never touched, as in cleanup_frost_observations.py), rows with
OBSERVATIONS."ID" <= --max-id: the highest ID before the #38 deploy (production:
40184234). Rows above it are never read for planning and never changed.
--datastreams ID,... limits a run to those datastreams: a canary.

Per datastream
--------------
* The RIVM Thing is the one named by the rows' PARAMETERS->>'sensor_id'
  ('sm-{RIVM Thing id}', e.g. 'sm-8272'), not the datastream's
  PROPERTIES->>'sensor_id' ('sm-{thing name lowercased}'). Rows with another
  sensor_id, or rows naming more than one, skip the datastream.
* Cross-check: the RIVM Thing's name must equal the datastream's
  PROPERTIES->>'thing_name' minus 'GEO_SamenMeten '; otherwise skipped.
* The calibrated datastream is the Thing's datastream whose name ends in
  '-pm25_kal' (pm2_5) or '-pm10_kal' (pm10); RIVM often has no ObservedProperty
  on them. None: skipped. Several: the lowest id, as the connector does (#38).
* RIVM's _kal observations are fetched for [first hour, last hour] of our rows
  ($filter on phenomenonTime, $select=phenomenonTime,result, $top=1000,
  following @iot.nextLink), --concurrency requests at a time, each with
  --rivm-timeout and --rivm-retries retries with exponential backoff. A
  datastream whose Thing or observations still cannot be read is skipped.
* A datastream with a non-numeric row (RESULT_TYPE <> 0 or no RESULT_NUMBER) is
  skipped: the rules below compare numbers.

Per (datastream, phenomenonTime hour, UTC)
------------------------------------------
A row "equals _kal" when |RESULT_NUMBER - kal| < 1e-6 and its RESULT_STRING
reads as the same number (FROST serves a numeric result from RESULT_STRING).

1. _kal value exists and >= 1 row equals it: keep the lowest-ID matching row;
   archive and delete the others.
2. _kal value exists and no row equals it: keep the lowest-ID row and set its
   RESULT_NUMBER, and RESULT_STRING, to the _kal value (old values to the
   rollback CSV first); archive and delete the others.
3. No _kal value for the hour: archive and delete every row of the hour.

An hour for which RIVM returns several different _kal values is left alone and
reported. A repaired datastream plans nothing on a re-run: the script is
idempotent. Both RESULT_NUMBER and RESULT_STRING are updated, because FROST's
API serves RESULT_STRING while its $filter and our SQL readers use RESULT_NUMBER.

Safety
------
* Dry run by default: read-only sessions, nothing written, not even a file.
* --apply needs --archive FILE and --rollback-csv FILE. Deletions use the
  archive helpers of cleanup_frost_observations.py: rows read in full
  (row_to_json and its md5, read-only), appended to the archive and fsync'ed,
  only then deleted, each DELETE rechecking the md5 (and, for rules 1 and 2,
  that the kept row of the hour still exists). Updates: the rows' old values are
  appended to the rollback CSV and fsync'ed before the UPDATE, which changes a
  row only while it still holds exactly those old values.
* Write sessions are cleanup_frost_observations.py's own (its application_name
  and advisory lock): the repair scripts that refuse to run next to a cleanup
  refuse next to this one, and this one refuses while any of them writes.
* After deleting, phenomenonTime/resultTime of the touched datastreams are
  recomputed (cleanup's refresh), and an interrupted run's leftovers are caught
  up by the next --apply with the same archive.
* After --apply the processed datastreams are scanned again and re-planned
  against the _kal values already fetched: nothing may be left to do.
* Production run only with the owner's OK, after a canary.

Files
-----
The archive is cleanup_frost_observations.py's format (selection
'samen-meten-kal'); undo deletions with that script's --restore.
The rollback CSV (UTF-8, LF) has the header
    id,datastream,phenomenon_time,old,new,old_string,new_string
old/new are RESULT_NUMBER before/after (exact: they read back as the same
double); old_string is the original RESULT_STRING as JSON (a string, or null),
new_string the text written. A cut-off last line (an interrupted append: no
UPDATE was sent for it) is dropped by the next --apply.

--verify (read-only, after an apply): hours with more than one row in scope,
and a random sample of --sample hours (--seed) checked against RIVM's _kal value.

Exit status: 0 done (and verified); 1 error; 2 usage; 3 refused to write (a
repair script's write session is on the server, or a foreign key references
OBSERVATIONS); 4 verification found something left; 130 interrupted.

Usage (production; see the PR / issue #41 for the runbook)
-----------------------------------------------------------
    python scripts/fix_samen_meten_calibrated.py --ssh root@<frost-host> --ssh-key ~/.ssh/id_hetzner --max-id 40184234 --datastreams 16094
    python scripts/fix_samen_meten_calibrated.py --ssh ... --max-id 40184234
    python scripts/fix_samen_meten_calibrated.py --ssh ... --max-id 40184234 --apply --archive sm_kal.jsonl --rollback-csv sm_kal_rollback.csv
    python scripts/fix_samen_meten_calibrated.py --ssh ... --max-id 40184234 --verify --sample 1000
    python scripts/fix_samen_meten_calibrated.py --ssh ... --rollback-csv sm_kal_rollback.csv --revert [--apply]
    python scripts/cleanup_frost_observations.py --ssh ... --restore --archive sm_kal.jsonl [--apply]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
import random
import re
import sys
import threading
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import requests

# Run as `python scripts/fix_samen_meten_calibrated.py` from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import cleanup_frost_observations as cleanup
from scripts import fix_sc_pressure_units as pressure

RunSql = cleanup.RunSql
PsqlError = cleanup.PsqlError
EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_VERIFY, EXIT_INTERRUPTED = (
    cleanup.EXIT_OK, cleanup.EXIT_ERROR, cleanup.EXIT_REFUSED, cleanup.EXIT_VERIFY, cleanup.EXIT_INTERRUPTED)
END = cleanup.END

SELECTION = "samen-meten-kal"  # the archive's "sel"
SOURCE = "samen_meten"
KAL_SUFFIX = {"pm2_5": "-pm25_kal", "pm10": "-pm10_kal"}
THING_PREFIX = "GEO_SamenMeten "
TOLERANCE = 1e-6
RIVM_URL = "https://api-samenmeten.rivm.nl/v1.0"
USER_AGENT = "GeoInsights-Connector/1.0 (fix_samen_meten_calibrated)"
HOUR = timedelta(hours=1)

_SENSOR_ID = re.compile(r"sm-([0-9]+)")

# Why a datastream is skipped.
SKIP_NO_ID = "rows' sensor_id is not sm-<RIVM Thing id>"
SKIP_SEVERAL_IDS = "rows carry more than one sensor_id"
SKIP_NON_NUMERIC = "rows with a non-numeric result"
SKIP_NAME = "RIVM Thing name differs from the datastream's thing_name"
SKIP_NO_THING = "RIVM Thing not found"
SKIP_NO_KAL = "RIVM Thing has no _kal datastream for the property"
SKIP_RIVM_ERROR = "RIVM request failed after retries"

# Why a row is deleted.
DEL_MATCH = "another row of the hour equals _kal (rule 1)"
DEL_UPDATED = "another row of an hour whose kept row is set to _kal (rule 2)"
DEL_NO_KAL = "no _kal value for the hour (rule 3)"
DELETE_REASONS = (DEL_MATCH, DEL_UPDATED, DEL_NO_KAL)

ROLLBACK_HEADER = "id,datastream,phenomenon_time,old,new,old_string,new_string"


class RollbackError(RuntimeError):
    pass


def _progress(message: str) -> None:
    sys.stdout.flush()
    print(message, file=sys.stderr, flush=True)


def _chunks[T](items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def number_text(value: float) -> str:
    """The shortest text that reads back as exactly this double (also in PostgreSQL's float8 input)."""
    return repr(float(value))


def float_array(values: Iterable[float]) -> str:
    return "'{" + ",".join(number_text(v) for v in values) + "}'::double precision[]"


def text_array(values: Iterable[str | None]) -> str:
    return "ARRAY[" + ",".join("NULL" if v is None else pressure.sql_literal(v) for v in values) + "]::text[]"


def parse_time(text: str) -> datetime:
    """An ISO 8601 instant (the start of an interval) as an aware UTC datetime."""
    value = datetime.fromisoformat(text.split("/", 1)[0].replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def hour_of(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ").replace(".000000Z", "Z")


# ---------------------------------------------------------------------------
# What the database holds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SmDatastream:
    id: int
    name: str
    prop: str  # pm2_5 or pm10
    thing_name: str  # the datastream's PROPERTIES->>'thing_name', e.g. 'GEO_SamenMeten LUC_354724647888301'


@dataclass(frozen=True)
class Row:
    id: int
    ds: int
    time: datetime
    type: int | None
    number: float | None
    string: str | None
    sensor_id: str | None

    @property
    def hour(self) -> datetime:
        return hour_of(self.time)

    @property
    def numeric(self) -> bool:
        return self.type == 0 and isinstance(self.number, float) and math.isfinite(self.number)

    def equals(self, kal: float) -> bool:
        """RESULT_NUMBER and the RESULT_STRING FROST serves both hold kal."""
        if not self.numeric or self.number is None or abs(self.number - kal) >= TOLERANCE:
            return False
        try:
            return self.string is not None and abs(float(self.string) - kal) < TOLERANCE
        except ValueError:
            return False


def sm_catalogue_sql() -> str:
    return f"""
SELECT json_build_object('t', 'smds', 'id', d."ID", 'name', d."NAME", 'prop', d."PROPERTIES"->>'observed_property',
                         'thing_name', d."PROPERTIES"->>'thing_name')::text
  FROM "DATASTREAMS" d JOIN "THINGS" th ON th."ID" = d."THING_ID"
 WHERE th."PROPERTIES"->>'source' = {pressure.sql_literal(SOURCE)}
   AND d."PROPERTIES"->>'observed_property' IN ({", ".join(pressure.sql_literal(p) for p in KAL_SUFFIX)})
   AND NOT {cleanup.protected_thing("th")}
 ORDER BY d."ID";
"""


def parse_sm_catalogue(lines: Iterable[str]) -> dict[int, SmDatastream]:
    return {int(r["id"]): SmDatastream(int(r["id"]), str(r.get("name") or ""), str(r["prop"]),
                                       str(r.get("thing_name") or ""))
            for r in cleanup._json_lines(lines, "smds")}


def rows_sql(ds_ids: Sequence[int], max_id: int) -> str:
    """Every row in scope of these datastreams: what the rules need, not the full row."""
    return f"""
SELECT json_build_object('t', 'r', 'id', o."ID", 'ds', o."DATASTREAM_ID", 'pt', o."PHENOMENON_TIME_START",
                         'type', o."RESULT_TYPE", 'num', o."RESULT_NUMBER", 'str', o."RESULT_STRING",
                         'sid', o."PARAMETERS"->>'sensor_id')::text
  FROM "OBSERVATIONS" o
 WHERE o."DATASTREAM_ID" = ANY({cleanup.int_array(ds_ids)}) AND o."ID" <= {int(max_id)};
"""


def above_sql(ds_ids: Sequence[int], max_id: int) -> str:
    """Per datastream: hours holding rows above max_id as well as rows in scope (reported, left as they are)."""
    return f"""
SELECT json_build_object('t', 'above', 'ds', a.ds, 'hours', count(*))::text
  FROM (SELECT DISTINCT o."DATASTREAM_ID" AS ds, date_trunc('hour', o."PHENOMENON_TIME_START") AS h
          FROM "OBSERVATIONS" o
         WHERE o."DATASTREAM_ID" = ANY({cleanup.int_array(ds_ids)}) AND o."ID" > {int(max_id)}) a
 WHERE EXISTS (SELECT 1 FROM "OBSERVATIONS" b
                WHERE b."DATASTREAM_ID" = a.ds AND b."ID" <= {int(max_id)}
                  AND b."PHENOMENON_TIME_START" >= a.h AND b."PHENOMENON_TIME_START" < a.h + interval '1 hour')
 GROUP BY a.ds;
"""


def parse_rows(lines: Iterable[str]) -> list[Row]:
    rows = []
    for r in cleanup._json_lines(lines, "r"):
        num = r.get("num")
        rows.append(Row(
            int(r["id"]), int(r["ds"]), parse_time(str(r["pt"])),
            None if r.get("type") is None else int(r["type"]),
            float(num) if isinstance(num, int | float) and not isinstance(num, bool) else None,
            None if r.get("str") is None else str(r["str"]),
            None if r.get("sid") is None else str(r["sid"]),
        ))
    return rows


def scan_rows(run_sql: RunSql, ds_ids: Sequence[int], max_id: int, batch: int, chunk: int,
              *, above: bool = True) -> tuple[dict[int, list[Row]], Counter[int]]:
    """Rows in scope by datastream (sorted by ID), and per datastream the hours that also hold rows above max_id."""
    statements = []
    for part in _chunks(sorted(ds_ids), batch):
        statements.append(rows_sql(part, max_id))
        if above:
            statements.append(above_sql(part, max_id))
    lines = cleanup.run_reads(run_sql, statements, chunk, "reading rows")
    by_ds: dict[int, list[Row]] = defaultdict(list)
    for row in parse_rows(lines):
        by_ds[row.ds].append(row)
    for rows in by_ds.values():
        rows.sort(key=lambda r: r.id)
    above_hours: Counter[int] = Counter({int(r["ds"]): int(r["hours"]) for r in cleanup._json_lines(lines, "above")})
    return dict(by_ds), above_hours


# ---------------------------------------------------------------------------
# RIVM
# ---------------------------------------------------------------------------


class RivmError(RuntimeError):
    pass


class RivmNotFound(RivmError):
    pass


@dataclass(frozen=True)
class RivmThing:
    id: int
    name: str
    datastreams: tuple[tuple[int, str], ...]  # (id, name)


class RivmApi(Protocol):
    def thing(self, thing_id: int) -> RivmThing: ...

    def observations(self, ds_id: int, start: datetime, end: datetime) -> list[tuple[datetime, float]]: ...


class RivmClient:
    """The Samen Meten STA, read-only: timeouts, retries with exponential backoff, @iot.nextLink paging."""

    MAX_PAGES = 10_000

    def __init__(self, base_url: str = RIVM_URL, *, timeout: float = 60, retries: int = 3, backoff: float = 2.0,
                 session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self.session = session or requests.Session()
        self.sleep = sleep

    def get(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                self.sleep(self.backoff * 2 ** (attempt - 1))
            try:
                response = self.session.get(url, params=params, timeout=self.timeout,
                                            headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            except requests.RequestException as exc:
                last = exc
                continue
            if response.status_code == 404:
                raise RivmNotFound(f"{url}: HTTP 404")
            if response.status_code == 429 or response.status_code >= 500:
                last = RivmError(f"HTTP {response.status_code}")
                continue
            if response.status_code != 200:
                raise RivmError(f"{url}: HTTP {response.status_code}")
            try:
                data = response.json()
            except ValueError as exc:
                last = exc
                continue
            if not isinstance(data, dict):
                last = RivmError("the response is not a JSON object")
                continue
            return data
        raise RivmError(f"{url}: {last} (after {self.retries + 1} attempts)")

    def _pages(self, url: str, params: dict[str, str] | None) -> Iterable[dict[str, Any]]:
        next_url: str | None = url
        for _ in range(self.MAX_PAGES):
            if not next_url:
                return
            data = self.get(next_url, params)
            params = None  # the next link carries them
            values = data.get("value", [])
            if not isinstance(values, list):
                raise RivmError(f"{next_url}: 'value' is not a list")
            yield from (v for v in values if isinstance(v, dict))
            link = data.get("@iot.nextLink")
            next_url = str(link) if link else None
        raise RivmError(f"{url}: more than {self.MAX_PAGES} pages")

    def thing(self, thing_id: int) -> RivmThing:
        url = f"{self.base_url}/Things({int(thing_id)})"
        data = self.get(url, {"$select": "id,name", "$expand": "Datastreams($select=id,name)"})
        datastreams = [d for d in data.get("Datastreams", []) if isinstance(d, dict)]
        link = data.get("Datastreams@iot.nextLink")
        if link:
            datastreams += list(self._pages(str(link), None))
        return RivmThing(int(data.get("@iot.id", thing_id)), str(data.get("name") or ""),
                         tuple((int(d["@iot.id"]), str(d.get("name") or "")) for d in datastreams if "@iot.id" in d))

    def observations(self, ds_id: int, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        """Observations with start <= phenomenonTime < end."""
        params = {
            "$filter": f"phenomenonTime ge {iso(start)} and phenomenonTime lt {iso(end)}",
            "$select": "phenomenonTime,result",
            "$top": "1000",
        }
        out = []
        for obs in self._pages(f"{self.base_url}/Datastreams({int(ds_id)})/Observations", params):
            result, when = obs.get("result"), obs.get("phenomenonTime")
            if isinstance(result, bool) or not isinstance(result, int | float) or not when:
                continue
            if math.isfinite(float(result)):
                out.append((parse_time(str(when)), float(result)))
        return out


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Update:
    id: int
    ds: int
    time: datetime
    old: float
    new: float
    old_string: str | None

    @property
    def new_string(self) -> str:
        return number_text(self.new)


@dataclass(frozen=True)
class Delete:
    row: Row
    reason: str
    keep: int | None  # the row kept in its hour (rules 1 and 2)


@dataclass
class DsPlan:
    ds: SmDatastream
    rows: list[Row] = field(default_factory=list)
    rivm_thing: int | None = None
    kal_ds: int | None = None
    skip: str | None = None
    detail: str = ""
    keep: list[int] = field(default_factory=list)  # kept unchanged
    updates: list[Update] = field(default_factory=list)
    deletes: list[Delete] = field(default_factory=list)
    ambiguous_hours: int = 0
    ambiguous_rows: int = 0
    above_hours: int = 0
    kal: dict[datetime, list[float]] = field(default_factory=dict)

    @property
    def hours(self) -> int:
        return len({r.hour for r in self.rows})

    @property
    def actions(self) -> int:
        return len(self.updates) + len(self.deletes)

    def deleted(self, reason: str) -> int:
        return sum(1 for d in self.deletes if d.reason == reason)

    def window(self) -> tuple[datetime, datetime]:
        """[first hour, last hour + 1 h) of the rows."""
        hours = [r.hour for r in self.rows]
        return min(hours), max(hours) + HOUR


def rivm_thing_of(rows: Sequence[Row]) -> tuple[int | None, str | None, str]:
    """(RIVM Thing id, skip reason, detail) from the rows' sensor_id labels."""
    labels = sorted({r.sensor_id or "" for r in rows})
    if len(labels) > 1:
        return None, SKIP_SEVERAL_IDS, ", ".join(repr(s) for s in labels[:5])
    match = _SENSOR_ID.fullmatch(labels[0]) if labels else None
    if match is None:
        return None, SKIP_NO_ID, repr(labels[0]) if labels else ""
    return int(match[1]), None, ""


def expected_rivm_name(ds: SmDatastream) -> str | None:
    return ds.thing_name.removeprefix(THING_PREFIX) if ds.thing_name.startswith(THING_PREFIX) else None


def kal_datastream(thing: RivmThing, prop: str) -> tuple[int | None, list[int]]:
    """The Thing's _kal datastream for prop (lowest id of several), and all candidates."""
    suffix = KAL_SUFFIX[prop]
    ids = sorted(ds_id for ds_id, name in thing.datastreams if name.strip().lower().endswith(suffix))
    return (ids[0] if ids else None), ids


def kal_by_hour(observations: Iterable[tuple[datetime, float]]) -> dict[datetime, list[float]]:
    by_hour: dict[datetime, list[float]] = defaultdict(list)
    for when, value in observations:
        by_hour[hour_of(when)].append(value)
    return dict(by_hour)


def plan_rows(plan: DsPlan) -> None:
    """Apply the three rules to plan.rows against plan.kal."""
    plan.keep, plan.updates, plan.deletes = [], [], []
    plan.ambiguous_hours = plan.ambiguous_rows = 0
    by_hour: dict[datetime, list[Row]] = defaultdict(list)
    for row in sorted(plan.rows, key=lambda r: r.id):
        by_hour[row.hour].append(row)
    for hour, rows in sorted(by_hour.items()):
        values = plan.kal.get(hour, [])
        if values and max(values) - min(values) >= TOLERANCE:
            plan.ambiguous_hours += 1
            plan.ambiguous_rows += len(rows)
            continue
        if not values:
            plan.deletes += [Delete(r, DEL_NO_KAL, None) for r in rows]
            continue
        kal = values[0]
        matching = [r for r in rows if r.equals(kal)]
        if matching:
            kept = matching[0]
            plan.keep.append(kept.id)
            plan.deletes += [Delete(r, DEL_MATCH, kept.id) for r in rows if r.id != kept.id]
            continue
        kept = rows[0]
        assert kept.number is not None  # numeric: checked before planning
        plan.updates.append(Update(kept.id, kept.ds, kept.time, kept.number, kal, kept.string))
        plan.deletes += [Delete(r, DEL_UPDATED, kept.id) for r in rows if r.id != kept.id]


def resolve(plans: Sequence[DsPlan], api: RivmApi, concurrency: int) -> None:
    """Find each datastream's RIVM Thing and _kal datastream, then fetch its _kal values; skip what fails."""
    todo: list[DsPlan] = []
    for plan in plans:
        if not plan.rows or plan.skip:
            continue
        if not all(r.numeric for r in plan.rows):
            plan.skip = SKIP_NON_NUMERIC
            plan.detail = f"{sum(1 for r in plan.rows if not r.numeric)} rows"
            continue
        plan.rivm_thing, plan.skip, plan.detail = rivm_thing_of(plan.rows)
        if not plan.skip:
            todo.append(plan)

    thing_ids = sorted({p.rivm_thing for p in todo if p.rivm_thing is not None})
    things: dict[int, RivmThing | Exception] = {}
    lock = threading.Lock()
    done = [0]

    def fetch_thing(thing_id: int) -> None:
        try:
            result: RivmThing | Exception = api.thing(thing_id)
        except Exception as exc:  # reported per datastream
            result = exc
        with lock:
            things[thing_id] = result
            done[0] += 1
            if done[0] % 50 == 0 or done[0] == len(thing_ids):
                _progress(f"  RIVM Things: {done[0]}/{len(thing_ids)}")

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(fetch_thing, thing_ids))

    fetch: list[DsPlan] = []
    for plan in todo:
        assert plan.rivm_thing is not None
        thing = things[plan.rivm_thing]
        if isinstance(thing, RivmNotFound):
            plan.skip, plan.detail = SKIP_NO_THING, f"Thing {plan.rivm_thing}"
        elif isinstance(thing, Exception):
            plan.skip, plan.detail = SKIP_RIVM_ERROR, f"Thing {plan.rivm_thing}: {thing}"
        elif thing.name != expected_rivm_name(plan.ds):
            plan.skip, plan.detail = SKIP_NAME, f"RIVM {thing.name!r}, ours {plan.ds.thing_name!r}"
        else:
            plan.kal_ds, candidates = kal_datastream(thing, plan.ds.prop)
            if plan.kal_ds is None:
                plan.skip, plan.detail = SKIP_NO_KAL, f"Thing {thing.id} {thing.name}"
            else:
                if len(candidates) > 1:
                    plan.detail = f"several _kal datastreams {candidates}; using {plan.kal_ds}"
                fetch.append(plan)

    done[0] = 0

    def fetch_kal(plan: DsPlan) -> None:
        assert plan.kal_ds is not None
        start, end = plan.window()
        try:
            plan.kal = kal_by_hour(api.observations(plan.kal_ds, start, end))
        except Exception as exc:  # reported per datastream
            plan.skip, plan.detail = SKIP_RIVM_ERROR, f"Datastream {plan.kal_ds}: {exc}"
        with lock:
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == len(fetch):
                _progress(f"  RIVM _kal series: {done[0]}/{len(fetch)}")

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(fetch_kal, fetch))


def build_plans(catalogue: dict[int, SmDatastream], rows: dict[int, list[Row]], above: Counter[int],
                api: RivmApi, concurrency: int) -> list[DsPlan]:
    plans = [DsPlan(ds, rows.get(ds_id, []), above_hours=above.get(ds_id, 0)) for ds_id, ds in sorted(catalogue.items())]
    resolve(plans, api, concurrency)
    for plan in plans:
        if plan.rows and not plan.skip:
            plan_rows(plan)
    return plans


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def report(plans: Sequence[DsPlan], *, per_datastream: bool = True) -> None:
    with_rows = [p for p in plans if p.rows]
    processed = [p for p in with_rows if not p.skip]
    skipped = [p for p in with_rows if p.skip]
    if per_datastream and with_rows:
        print("\nPer datastream")
        print(f"  {'ds':>8} {'prop':<6}{'rivm':>8}{'_kal ds':>9}{'rows':>8}{'hours':>7}{'keep':>7}{'update':>8}"
              f"{'del r1':>8}{'del r2':>8}{'del r3':>8}{'ambig':>7}{'above':>7}  status")
        for p in with_rows:
            status = f"SKIPPED: {p.skip}" if p.skip else "ok"
            if p.detail:
                status += f" ({p.detail})"
            print(f"  {p.ds.id:>8} {p.ds.prop:<6}{p.rivm_thing or '-':>8}{p.kal_ds or '-':>9}{len(p.rows):>8}"
                  f"{p.hours:>7}{len(p.keep):>7}{len(p.updates):>8}{p.deleted(DEL_MATCH):>8}"
                  f"{p.deleted(DEL_UPDATED):>8}{p.deleted(DEL_NO_KAL):>8}{p.ambiguous_hours:>7}{p.above_hours:>7}"
                  f"  {status}")

    def line(text: str, count: int) -> None:
        print(f"  {text:<72}{count:>12,}")

    print("\nTotal")
    props = Counter(p.ds.prop for p in plans)
    line(f"datastreams in scope ({', '.join(f'{k} {v}' for k, v in sorted(props.items()))})", len(plans))
    line("  without rows in scope", len(plans) - len(with_rows))
    line("  processed", len(processed))
    line("  skipped", len(skipped))
    reasons = Counter(p.skip for p in skipped)
    for reason in (SKIP_NO_ID, SKIP_SEVERAL_IDS, SKIP_NON_NUMERIC, SKIP_NO_THING, SKIP_NAME, SKIP_NO_KAL,
                   SKIP_RIVM_ERROR):
        line(f"    {reason}", reasons[reason])
    line("rows in scope", sum(len(p.rows) for p in with_rows))
    line("  in skipped datastreams (left as they are)", sum(len(p.rows) for p in skipped))
    line("  in processed datastreams", sum(len(p.rows) for p in processed))
    line("    to keep unchanged (equal to _kal)", sum(len(p.keep) for p in processed))
    line("    to update to _kal (rule 2)", sum(len(p.updates) for p in processed))
    line("    to delete", sum(len(p.deletes) for p in processed))
    for reason in DELETE_REASONS:
        line(f"      {reason}", sum(p.deleted(reason) for p in processed))
    line("    left: hour with several different _kal values at RIVM", sum(p.ambiguous_rows for p in processed))
    line("hours in processed datastreams", sum(p.hours for p in processed))
    line("  left: several different _kal values at RIVM", sum(p.ambiguous_hours for p in processed))
    line("hours that also hold rows above --max-id (those rows are left as they are)",
         sum(p.above_hours for p in with_rows))


# ---------------------------------------------------------------------------
# The rollback CSV
# ---------------------------------------------------------------------------


def format_update(u: Update) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerow(
        [u.id, u.ds, u.time.isoformat(), number_text(u.old), number_text(u.new),
         json.dumps(u.old_string, ensure_ascii=False), u.new_string])
    return out.getvalue()


def parse_update(fields: Sequence[str]) -> Update:
    if len(fields) != 7:
        raise ValueError(f"{len(fields)} fields, expected 7")
    obs_id, ds, when, old, new, old_string, new_string = fields
    old_text = json.loads(old_string)
    if old_text is not None and not isinstance(old_text, str):
        raise ValueError("old_string is not a JSON string or null")
    update = Update(int(obs_id), int(ds), parse_time(when), float(old), float(new), old_text)
    if update.new_string != new_string:
        raise ValueError(f"new_string {new_string!r} is not new {new!r} as text")
    return update


def prepare_rollback(path: Path) -> None:
    """Create the CSV with its header, or check an existing one and drop a cut-off last line."""
    if not path.exists() or path.stat().st_size == 0:
        cleanup._append(path, ROLLBACK_HEADER + "\n")
        return
    with path.open("rb") as handle:
        if handle.readline().rstrip(b"\r\n") != ROLLBACK_HEADER.encode():
            raise RollbackError(f"{path} does not start with {ROLLBACK_HEADER}: not a rollback CSV of this script")
    with path.open("rb+") as handle:
        data = handle.read()
        if data.endswith(b"\n"):
            return
        cut = data.rfind(b"\n")
        _progress(f"  WARNING: {path}: dropping the cut-off last line {data[cut + 1 :][:80]!r} (an interrupted append: "
                  "no UPDATE was sent for it)")
        handle.truncate(cut + 1)
        handle.flush()
        os.fsync(handle.fileno())


def read_rollback(path: Path) -> list[Update]:
    """The recorded updates, one per observation (records of one observation with different values are refused)."""
    with path.open(encoding="utf-8", newline="") as handle:
        text = handle.read()
    lines = text.split("\n")
    if lines[0].rstrip("\r") != ROLLBACK_HEADER:
        raise RollbackError(f"{path} does not start with {ROLLBACK_HEADER}")
    if not text.endswith("\n"):
        _progress(f"  WARNING: {path}: cut-off last line skipped (no UPDATE was sent for it)")
        lines = lines[:-1]
    by_id: dict[int, Update] = {}
    for number, fields in enumerate(csv.reader(line for line in lines[1:] if line), start=2):
        try:
            update = parse_update(fields)
        except (ValueError, json.JSONDecodeError) as exc:
            raise RollbackError(f"{path}, line {number}: {exc}") from exc
        if by_id.setdefault(update.id, update) != update:
            raise RollbackError(f"{path}, line {number}: observation {update.id} is recorded with other values before")
    return sorted(by_id.values(), key=lambda u: (u.ds, u.id))


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _update_values(updates: Sequence[Update]) -> str:
    return (f"unnest({cleanup.int_array(u.id for u in updates)}, {float_array(u.old for u in updates)}, "
            f"{float_array(u.new for u in updates)}, {text_array(u.old_string for u in updates)}, "
            f"{text_array(u.new_string for u in updates)}) AS v(id, old, new, olds, news)")


def update_sql(ds: int, updates: Sequence[Update], max_id: int) -> str:
    """Set the rows of one datastream to _kal, each only while it still holds exactly its recorded old values."""
    return f"""
WITH upd AS (
    UPDATE "OBSERVATIONS" o SET "RESULT_NUMBER" = v.new, "RESULT_STRING" = v.news
      FROM {_update_values(updates)}
     WHERE o."ID" = v.id AND o."DATASTREAM_ID" = {int(ds)} AND o."ID" <= {int(max_id)} AND o."RESULT_TYPE" = 0
       AND o."RESULT_NUMBER" = v.old AND o."RESULT_STRING" IS NOT DISTINCT FROM v.olds
    RETURNING o."ID"
)
SELECT 'updated', {int(ds)}, {len(updates)}, count(*), '{END}' FROM upd;
"""


def revert_sql(ds: int, updates: Sequence[Update]) -> str:
    """Set the rows back to their old values, each only while it still holds exactly what the update wrote."""
    return f"""
WITH rev AS (
    UPDATE "OBSERVATIONS" o SET "RESULT_NUMBER" = v.old, "RESULT_STRING" = v.olds
      FROM {_update_values(updates)}
     WHERE o."ID" = v.id AND o."DATASTREAM_ID" = {int(ds)} AND o."RESULT_TYPE" = 0
       AND o."RESULT_NUMBER" = v.new AND o."RESULT_STRING" IS NOT DISTINCT FROM v.news
    RETURNING o."ID"
)
SELECT 'reverted', {int(ds)}, {len(updates)}, count(*), '{END}' FROM rev;
"""


def revert_state_sql(ds: int, updates: Sequence[Update]) -> str:
    """Per datastream: recorded rows present, holding the new values, holding the old values."""
    return f"""
SELECT 'rvstate', {int(ds)}, count(*), count(o."ID"),
       count(*) FILTER (WHERE o."RESULT_NUMBER" = v.new AND o."RESULT_STRING" IS NOT DISTINCT FROM v.news),
       count(*) FILTER (WHERE o."RESULT_NUMBER" = v.old AND o."RESULT_STRING" IS NOT DISTINCT FROM v.olds),
       '{END}'
  FROM {_update_values(updates)}
  LEFT JOIN "OBSERVATIONS" o ON o."ID" = v.id AND o."DATASTREAM_ID" = {int(ds)} AND o."RESULT_TYPE" = 0;
"""


def read_sql(ds: int, ids: Sequence[int], max_id: int) -> str:
    """The rows to delete, in full (cleanup's candidate lines)."""
    return cleanup.candidates_sql(f"""
SELECT o."DATASTREAM_ID" AS ds, o."ID" AS id, row_to_json(o)::text AS j
  FROM "OBSERVATIONS" o
 WHERE o."ID" = ANY({cleanup.int_array(ids)}) AND o."DATASTREAM_ID" = {int(ds)} AND o."ID" <= {int(max_id)}""")


def keep_recheck(keep_ids: Iterable[int]) -> str:
    """For a DELETE of rules 1 and 2: the row kept in the deleted row's hour still exists."""
    return (f'EXISTS (SELECT 1 FROM "OBSERVATIONS" k WHERE k."ID" = ANY({cleanup.int_array(sorted(set(keep_ids)))}) '
            'AND k."ID" <> o."ID" AND k."DATASTREAM_ID" = o."DATASTREAM_ID" '
            """AND date_trunc('hour', k."PHENOMENON_TIME_START") = date_trunc('hour', o."PHENOMENON_TIME_START"))""")


@dataclass
class Applied:
    updated: int = 0
    update_kept: int = 0  # recorded, but changed since the scan: left
    archived: int = 0
    deleted: int = 0
    delete_kept: int = 0
    touched: set[int] = field(default_factory=set)


def _pages(plans: Sequence[DsPlan], page_rows: int) -> list[list[DsPlan]]:
    pages: list[list[DsPlan]] = []
    rows = 0
    for plan in plans:
        if not plan.actions:
            continue
        if pages and rows + plan.actions <= page_rows:
            pages[-1].append(plan)
            rows += plan.actions
        else:
            pages.append([plan])
            rows = plan.actions
    return pages


def apply_plans(run_sql: RunSql, plans: Sequence[DsPlan], args: argparse.Namespace, run_id: str) -> Applied:
    """Per page of datastreams: record and run the updates, then read, archive and delete the rows."""
    done = Applied()
    for page in _pages(plans, args.page_rows):
        updates = [(p.ds.id, batch) for p in page for batch in _chunks(p.updates, args.batch_rows)]
        if updates:
            cleanup._append(args.rollback_csv, "".join(format_update(u) for _, b in updates for u in b))
            try:
                lines = run_sql(cleanup.WRITE_SESSION + "".join(update_sql(ds, b, args.max_id) for ds, b in updates))
            except PsqlError as exc:
                raise PsqlError(f"{exc}\n  Every row this session can update is in {args.rollback_csv}. Re-running is "
                                "safe.") from exc
            counts = cleanup.parse_counts(lines, "updated")
            if len(counts) != len(updates):
                raise PsqlError(f"{len(updates)} UPDATE statements sent, {len(counts)} confirmed")
            for _, listed, count in counts:
                done.updated += count
                done.update_kept += listed - count
        reads = [(p, reason, batch)
                 for p in page
                 for reason in DELETE_REASONS
                 for batch in _chunks([d for d in p.deletes if d.reason == reason], args.batch_rows)]
        if not reads:
            continue
        lines = run_sql(cleanup.READ_ONLY_SESSION
                        + "".join(read_sql(p.ds.id, [d.row.id for d in batch], args.max_id) for p, _, batch in reads))
        cands = {c.id: c for c in cleanup.parse_candidates(lines)}
        if not cands:
            continue
        cleanup.append_rows(args.archive, run_id, SELECTION, sorted(cands.values(), key=lambda c: c.id))
        done.archived += len(cands)
        statements = []
        for p, reason, batch in reads:
            found = [cands[d.row.id] for d in batch if d.row.id in cands]
            if not found:
                continue
            recheck = "true" if reason == DEL_NO_KAL else keep_recheck(d.keep for d in batch if d.keep is not None)
            statements.append(cleanup.delete_sql(p.ds.id, found, recheck))
        try:
            lines = run_sql(cleanup.WRITE_SESSION + "".join(statements))
        except PsqlError as exc:
            raise PsqlError(f"{exc}\n  Every row this session can delete is already in {args.archive}. Re-running is "
                            "safe.") from exc
        counts = cleanup.parse_counts(lines, "deleted")
        if len(counts) != len(statements):
            raise PsqlError(f"{len(statements)} DELETE statements sent, {len(counts)} confirmed")
        for ds, listed, count in counts:
            done.deleted += count
            done.delete_kept += listed - count
            if count:
                done.touched.add(ds)
        _progress(f"  updated {done.updated:,}, deleted {done.deleted:,} rows so far ({done.archived:,} archived)")
    return done


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def load_scope(run_sql: RunSql, only: Sequence[int] | None) -> dict[int, SmDatastream]:
    catalogue = parse_sm_catalogue(run_sql(cleanup.READ_ONLY_SESSION + sm_catalogue_sql()))
    if only:
        unknown = sorted(set(only) - set(catalogue))
        if unknown:
            raise ValueError(f"not Samen Meten pm2_5/pm10 datastreams (of a non-TGV Thing): {unknown}")
        catalogue = {ds: catalogue[ds] for ds in sorted(set(only))}
    return catalogue


def make_api(args: argparse.Namespace) -> RivmApi:
    return RivmClient(args.rivm_url, timeout=args.rivm_timeout, retries=args.rivm_retries)


def run_repair(args: argparse.Namespace, run_sql: RunSql, api: RivmApi) -> int:
    mode = ("APPLY: updates and deletes observations (archive and rollback CSV first)" if args.apply
            else "DRY RUN: read-only, nothing is written")
    print(f"Samen Meten PM repair against RIVM _kal ({mode})")
    pending: set[int] = set()
    if args.apply:
        pending = cleanup.prepare_archive(args.archive)  # a wrong file fails before anything else
        prepare_rollback(args.rollback_csv)
    catalogue = load_scope(run_sql, args.datastreams)
    print(f"Scope: {len(catalogue):,} datastreams (Thing source {SOURCE}, observed_property pm2_5/pm10)"
          + (f", limited to {sorted(args.datastreams)}" if args.datastreams else "")
          + f"; rows with ID <= {args.max_id:,}; RIVM {args.rivm_url}")
    _progress("Reading rows, read-only ...")
    rows, above = scan_rows(run_sql, list(catalogue), args.max_id, args.ds_batch, args.chunk)
    _progress("Reading RIVM ...")
    plans = build_plans(catalogue, rows, above, api, args.concurrency)
    report(plans)
    if not args.apply:
        print("\nDry run: nothing was written. Re-run with --apply --archive FILE --rollback-csv FILE to repair.")
        return EXIT_OK
    frost = cleanup.load_catalogue(run_sql)
    if frost.foreign_keys:
        print(f"Refusing to delete: foreign keys reference OBSERVATIONS: {frost.foreign_keys}")
        return EXIT_REFUSED
    if cleanup.refuse_if_busy(run_sql):
        return EXIT_REFUSED
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    cleanup.append_run(args.archive, run_id, SELECTION, args.datastreams)
    _progress(f"Applying; rows go to {args.archive} before they are deleted, old values to {args.rollback_csv} "
              "before they are updated ...")
    done = apply_plans(run_sql, [p for p in plans if not p.skip], args, run_id)
    planned_updates = sum(len(p.updates) for p in plans if not p.skip)
    planned_deletes = sum(len(p.deletes) for p in plans if not p.skip)
    print(f"\nUpdated {done.updated:,} observations to _kal (planned {planned_updates:,}; {done.update_kept:,} changed "
          f"since the scan, left); old values in {args.rollback_csv}.")
    print(f"Deleted {done.deleted:,} observations (planned {planned_deletes:,}); {done.archived:,} rows archived in "
          f"{args.archive} first; {done.delete_kept:,} of them left (changed, or no longer selected, when their "
          "DELETE ran).")
    to_refresh = pending | done.touched
    if to_refresh:
        _progress("Refreshing datastream times ...")
        cleanup.refresh(run_sql, to_refresh, args.chunk)
    cleanup.append_refreshed(args.archive, run_id, to_refresh)
    print(f"Refreshed phenomenonTime/resultTime of {len(to_refresh):,} datastreams.")
    _progress("Verifying, read-only ...")
    processed = {p.ds.id: p for p in plans if p.rows and not p.skip}
    again, _ = scan_rows(run_sql, list(processed), args.max_id, args.ds_batch, args.chunk, above=False)
    left = 0
    multi = 0
    for ds_id, plan in processed.items():
        check = DsPlan(plan.ds, again.get(ds_id, []), kal=plan.kal)
        plan_rows(check)
        left += check.actions
        multi += sum(1 for n in Counter(r.hour for r in check.rows).values() if n > 1)
    if left:
        print(f"Verification: {left:,} rows of the processed datastreams still need an update or delete "
              f"({multi:,} hours with more than one row). Re-run with the same files.")
        return EXIT_VERIFY
    print("Verification: in every processed datastream each hour holds one row equal to _kal (hours with several "
          "_kal values at RIVM excepted), as of now.")
    return EXIT_OK


def run_verify(args: argparse.Namespace, run_sql: RunSql, api: RivmApi) -> int:
    """Read-only: hours with more than one row, and --sample random hours checked against RIVM."""
    print("Samen Meten PM verification against RIVM _kal (read-only)")
    catalogue = load_scope(run_sql, args.datastreams)
    rows, _ = scan_rows(run_sql, list(catalogue), args.max_id, args.ds_batch, args.chunk, above=False)
    multi = Counter({ds: sum(1 for n in Counter(r.hour for r in rs).values() if n > 1) for ds, rs in rows.items()})
    hours = sorted({(r.ds, r.hour) for rs in rows.values() for r in rs})
    sample = sorted(random.Random(args.seed).sample(hours, min(args.sample, len(hours))))
    print(f"Scope: {len(catalogue):,} datastreams, {sum(len(rs) for rs in rows.values()):,} rows with ID <= "
          f"{args.max_id:,} in {len(hours):,} hours; hours with more than one row: {sum(multi.values()):,} in "
          f"{sum(1 for n in multi.values() if n):,} datastreams")
    sampled: dict[int, set[datetime]] = defaultdict(set)
    for ds, hour in sample:
        sampled[ds].add(hour)
    plans = [DsPlan(catalogue[ds], [r for r in rows[ds] if r.hour in hs]) for ds, hs in sorted(sampled.items())]
    _progress(f"Checking {len(sample):,} random hours in {len(plans):,} datastreams against RIVM ...")
    resolve(plans, api, args.concurrency)
    outcome: Counter[str] = Counter()
    bad: list[str] = []
    for plan in plans:
        n = len({r.hour for r in plan.rows})
        if plan.skip:
            outcome[f"not checked: {plan.skip}"] += n
            continue
        for hour in sorted({r.hour for r in plan.rows}):
            in_hour = [r for r in plan.rows if r.hour == hour]
            values = plan.kal.get(hour, [])
            if not values:
                outcome["no _kal value at RIVM"] += 1
            elif len(in_hour) > 1:
                outcome["more than one row"] += 1
            elif max(values) - min(values) >= TOLERANCE:
                outcome["several _kal values at RIVM"] += 1
            elif in_hour[0].equals(values[0]):
                outcome["equal to _kal"] += 1
                continue
            else:
                outcome["differs from _kal"] += 1
            bad.append(f"ds {plan.ds.id} {iso(hour)}: rows {[(r.id, r.number) for r in in_hour]}, _kal {values}")
    print(f"\nSampled hours: {len(sample):,}")
    for key, count in sorted(outcome.items()):
        print(f"  {key:<64}{count:>10,}")
    for text in bad[: args.top]:
        print(f"  {text}")
    if sum(multi.values()) or bad:
        print("Verification: not every hour holds one row equal to _kal.")
        return EXIT_VERIFY
    print("Verification: no hour with more than one row; every sampled hour equals _kal.")
    return EXIT_OK


def run_revert(args: argparse.Namespace, run_sql: RunSql) -> int:
    mode = "APPLY: restores old values" if args.apply else "DRY RUN: read-only, nothing is written"
    print(f"Revert of the updates recorded in {args.rollback_csv} ({mode})")
    updates = read_rollback(args.rollback_csv)
    if args.datastreams:
        updates = [u for u in updates if u.ds in set(args.datastreams)]
    batches = [(ds, batch) for ds, group in _group(updates).items() for batch in _chunks(group, args.batch_rows)]

    def state() -> tuple[int, int, int, int]:
        listed = present = at_new = at_old = 0
        for part in _chunks(batches, args.chunk):
            lines = run_sql(cleanup.READ_ONLY_SESSION + "".join(revert_state_sql(ds, b) for ds, b in part))
            for f in cleanup._tagged(lines, "rvstate", 5):
                _, n, p, new, old = map(int, f)
                listed, present, at_new, at_old = listed + n, present + p, at_new + new, at_old + old
        return listed, present, at_new, at_old

    def show(values: tuple[int, int, int, int]) -> None:
        listed, present, at_new, at_old = values
        print(f"  recorded observations{'':<44}{listed:>10,}")
        print(f"    holding the value the update wrote: to revert{'':<20}{at_new:>10,}")
        print(f"    holding the old value already{'':<36}{at_old:>10,}")
        print(f"    changed since, or not of their datastream: left{'':<18}{present - at_new - at_old:>10,}")
        print(f"    absent (deleted since){'':<43}{listed - present:>10,}")

    before = state()
    show(before)
    if not args.apply:
        print("Dry run: nothing was written. Re-run with --apply to revert.")
        return EXIT_OK
    if cleanup.refuse_if_busy(run_sql):
        return EXIT_REFUSED
    reverted = 0
    for part in _chunks(batches, args.chunk):
        lines = run_sql(cleanup.WRITE_SESSION + "".join(revert_sql(ds, b) for ds, b in part))
        counts = cleanup.parse_counts(lines, "reverted")
        if len(counts) != len(part):
            raise PsqlError(f"{len(part)} UPDATE statements sent, {len(counts)} confirmed")
        reverted += sum(n for _, _, n in counts)
    print(f"Reverted {reverted:,} observations (planned {before[2]:,}).")
    after = state()
    print("\nVerification")
    show(after)
    if after[2]:
        print(f"Verification: {after[2]:,} rows still hold the updated value. Re-run.")
        return EXIT_VERIFY
    print("Verification: no recorded row holds the updated value any more.")
    return EXIT_OK


def _group(updates: Iterable[Update]) -> dict[int, list[Update]]:
    by_ds: dict[int, list[Update]] = defaultdict(list)
    for u in updates:
        by_ds[u.ds].append(u)
    return dict(sorted(by_ds.items()))


def _id_list(text: str) -> list[int]:
    try:
        ids = [int(part) for part in text.split(",") if part.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a comma-separated list of datastream IDs: {text!r}") from exc
    if not ids:
        raise argparse.ArgumentTypeError("no datastream ID given")
    return ids


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair Samen Meten PM observations against RIVM's calibrated (_kal) series (#41).",
        epilog="Dry run unless --apply is given. The module docstring has the rules and the runbook.",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--ssh", metavar="USER@HOST", help="SSH destination of the FROST host")
    target.add_argument("--container", metavar="NAME", help="local docker container running the FROST database")
    parser.add_argument("--ssh-key", metavar="PATH", help="identity file for --ssh")
    parser.add_argument("--compose-dir", default="/opt/frost", help="compose directory on the host (%(default)s)")
    parser.add_argument("--service", default="frost-db", help="database service in that compose project (%(default)s)")
    parser.add_argument("--max-id", type=int, help="highest observation ID in scope (production: 40184234)")
    parser.add_argument("--datastreams", type=_id_list, metavar="ID,...", help="limit to these datastreams (canary)")
    parser.add_argument("--apply", action="store_true", help="write (default: dry run)")
    parser.add_argument("--archive", type=Path, metavar="FILE",
                        help="with --apply: cleanup_frost_observations.py archive every row goes to before deletion")
    parser.add_argument("--rollback-csv", type=Path, metavar="FILE",
                        help="with --apply: CSV of the old values of updated rows; with --revert: that file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--revert", action="store_true", help="set the rows of --rollback-csv back to their old values")
    mode.add_argument("--verify", action="store_true",
                      help="read-only: hours with >1 row, and --sample random hours checked against RIVM")
    parser.add_argument("--sample", type=int, default=1000, help="hours checked by --verify (%(default)s)")
    parser.add_argument("--seed", type=int, default=41, help="random seed of --verify's sample (%(default)s)")
    parser.add_argument("--rivm-url", default=RIVM_URL, help="RIVM SensorThings API (%(default)s)")
    parser.add_argument("--concurrency", type=int, default=4, help="RIVM requests at a time (%(default)s)")
    parser.add_argument("--rivm-timeout", type=float, default=60, help="seconds per RIVM request (%(default)s)")
    parser.add_argument("--rivm-retries", type=int, default=3, help="retries per RIVM request (%(default)s)")
    parser.add_argument("--ds-batch", type=int, default=50, help="datastreams per row-reading statement (%(default)s)")
    parser.add_argument("--page-rows", type=int, default=20_000,
                        help="planned updates + deletes per page of datastreams (%(default)s)")
    parser.add_argument("--batch-rows", type=int, default=5000, help="rows per UPDATE or DELETE (%(default)s)")
    parser.add_argument("--chunk", type=int, default=20, help="statements per read or refresh session (%(default)s)")
    parser.add_argument("--top", type=int, default=20, help="mismatches listed by --verify (%(default)s)")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds per psql session (%(default)s)")
    args = parser.parse_args(argv)
    if not (args.ssh or args.container):
        parser.error("one of --ssh or --container is required")
    if min(args.sample, args.concurrency, args.ds_batch, args.page_rows, args.batch_rows, args.chunk, args.top) < 1:
        parser.error("--sample, --concurrency, --ds-batch, --page-rows, --batch-rows, --chunk and --top must be "
                     "positive")
    if args.rivm_retries < 0:
        parser.error("--rivm-retries must not be negative")
    if args.revert:
        if args.rollback_csv is None or not args.rollback_csv.is_file():
            parser.error("--revert needs an existing --rollback-csv FILE")
        return args
    if args.max_id is None or args.max_id < 1:
        parser.error("--max-id is required (the highest observation ID before the #38 deploy; production 40184234)")
    if args.verify and args.apply:
        parser.error("--verify is read-only; it does not take --apply")
    if args.apply and (args.archive is None or args.rollback_csv is None):
        parser.error("--apply needs --archive FILE (deleted rows) and --rollback-csv FILE (old values of updated rows)")
    return args


def main(argv: Sequence[str] | None = None, *, api: RivmApi | None = None, run_sql: RunSql | None = None) -> int:
    args = parse_args(argv)
    if run_sql is None:
        run_sql = pressure.psql_runner(
            pressure.psql_argv(ssh=args.ssh, ssh_key=args.ssh_key, compose_dir=args.compose_dir,
                               service=args.service, container=args.container),
            args.timeout,
        )
    note = " Every row deleted is in the archive, every row updated in the rollback CSV; run the same command again."
    try:
        if args.revert:
            return run_revert(args, run_sql)
        if args.verify:
            return run_verify(args, run_sql, api or make_api(args))
        return run_repair(args, run_sql, api or make_api(args))
    except PsqlError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if cleanup._interrupted(exc):
            print(f"Interrupted.{note if args.apply else ''}", file=sys.stderr)
            return EXIT_INTERRUPTED
        return EXIT_ERROR
    except (cleanup.ArchiveError, RollbackError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(f"Interrupted.{note if args.apply else ''}", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
