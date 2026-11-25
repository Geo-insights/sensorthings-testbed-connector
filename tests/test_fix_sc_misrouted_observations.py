"""scripts/fix_sc_misrouted_observations.py: plan, apply and rollback of misrouted rows.

Unit tests drive the script against a fake psql; the integration test
(python -m pytest -m integration) runs it end to end, through docker exec, on a
throwaway FROST-Server 2.8 + PostGIS.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests

from scripts import fix_sc_misrouted_observations as fix

# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def _ds(ds_id: int, thing_id: int, thing: str, sensor_id: str, prop: str, source: str = "sensor_community") -> fix.Datastream:
    return fix.Datastream(ds_id, f"{thing} sensor - {prop}", thing_id, thing, source, sensor_id, fix.canonical(prop))


def _catalogue() -> fix.Catalogue:
    a, b, c, d = "sensor_community-93182", "sensor_community-91154", "sensor_community-5", "sensor_community-6"
    datastreams = [
        _ds(1385, 10, "GEO_SensorCommunity 93182", a, "temperature"),
        _ds(1386, 10, "GEO_SensorCommunity 93182", a, "pressure"),
        _ds(1387, 10, "GEO_SensorCommunity 93182", a, "humidity"),
        _ds(1388, 10, "GEO_SensorCommunity 93182", a, "pm10"),
        _ds(1389, 10, "GEO_SensorCommunity 93182", a, "pm25"),  # registered under the old alias
        _ds(2015, 20, "GEO_SensorCommunity 91154", b, "pressure"),
        _ds(3001, 30, "GEO_SensorCommunity 5", c, "pressure"),
        _ds(4001, 40, "GEO_SensorCommunity 6", d, "pm10"),
        _ds(4002, 41, "GEO_SensorCommunity 6", d, "pm10"),  # a duplicate registration
        _ds(5001, 50, "GEO_SamenMeten SSK_X", "sm-ssk_x", "pm10", source="samen_meten"),
    ]
    fois = {10: fix.ThingFoi(1, 110), 20: fix.ThingFoi(1, 120), 30: fix.ThingFoi(2, None), 50: fix.ThingFoi(1, 150)}
    return fix.Catalogue({ds.id: ds for ds in datastreams}, fois, "GEO_")


def _group(ds: int, sid: str | None, prop: str | None, thing: str | None = None, rows: int = 10) -> fix.Group:
    return fix.Group(ds=ds, sid=sid, prop=prop, thing=thing, rows=rows, min_id=100, max_id=200)


def _plan(*groups: fix.Group) -> dict[tuple[Any, ...], fix.PlannedGroup]:
    return {p.group.key: p for p in fix.build_plan(groups, _catalogue()).groups}


def test_plan_places_every_kind_of_misrouted_group() -> None:
    a_pm10 = _group(1386, "sensor_community-93182", "pm10", "SensorCommunity 93182")
    b_pm10 = _group(1386, "sensor_community-91154", "pm10", "SensorCommunity 91154")
    b_pressure = _group(1387, "sensor_community-91154", "pressure", "SensorCommunity 91154")
    c_pressure = _group(1387, "sensor_community-5", "pressure", "SensorCommunity 5")
    d_pm10 = _group(1386, "sensor_community-6", "pm10", "SensorCommunity 6")
    tgv = _group(1387, "tgv-officelab-climate", "humidity", "Climate adaptation setup")
    in_sm = _group(5001, "sensor_community-93182", "pm10", "SensorCommunity 93182")
    unlabelled = _group(1385, None, None)
    correct_alias = _group(1389, "sensor_community-93182", "pm2_5", "SensorCommunity 93182")
    correct = _group(1386, "sensor_community-93182", "pressure", "SensorCommunity 93182")

    plan = _plan(a_pm10, b_pm10, b_pressure, c_pressure, d_pm10, tgv, in_sm, unlabelled, correct_alias, correct)

    def summary(group: fix.Group) -> tuple[str, int | None, int | None]:
        planned = plan[group.key]
        return planned.action, planned.target.id if planned.target else None, planned.foi

    assert summary(a_pm10) == (fix.MOVE, 1388, None)  # same Thing: keeps its FoI
    assert summary(b_pm10) == (fix.NEEDS_DATASTREAM, None, None)  # 91154 has no PM10 datastream yet
    assert summary(b_pressure) == (fix.MOVE, 2015, 120)  # another Thing: that Thing's generated FoI
    assert summary(c_pressure) == (fix.MOVE_BLOCKED, 3001, None)  # its Thing has two Locations
    assert summary(d_pm10) == (fix.AMBIGUOUS, None, None)
    assert summary(tgv) == (fix.NO_DATASTREAM, None, None)
    assert summary(in_sm) == (fix.MOVE, 1388, 110)
    assert summary(unlabelled) == (fix.UNLABELLED, None, None)
    assert correct_alias.key not in plan and correct.key not in plan


def test_another_sources_reading_goes_to_its_own_things_datastream() -> None:
    sm_reading = _group(1388, "sm-4711", "pm10", "SamenMeten SSK_X")

    planned = _plan(sm_reading)[sm_reading.key]

    assert (planned.action, planned.target and planned.target.id, planned.foi) == (fix.MOVE, 5001, 150)


def test_groups_from_several_id_ranges_are_merged() -> None:
    first = fix.Group(1386, "s", "pm10", "t", "9", rows=3, min_id=10, max_id=20, first="2026-11-12T10:00:00+00:00",
                      last="2026-11-12T11:00:00+00:00")
    second = fix.Group(1386, "s", "pm10", "t", "9", rows=4, min_id=200, max_id=300, first="2026-11-11T10:00:00+00:00",
                       last="2026-11-13T11:00:00+00:00")
    other = fix.Group(1386, "s", "pm2_5", "t", "9", rows=1, min_id=15, max_id=15)

    merged = {g.key: g for g in fix.merge_groups([first, second, other])}

    total = merged[first.key]
    assert (total.rows, total.min_id, total.max_id) == (7, 10, 300)
    assert (total.first, total.last) == ("2026-11-11T10:00:00+00:00", "2026-11-13T11:00:00+00:00")
    assert merged[other.key].rows == 1
    assert first.rows == 3  # inputs are not mutated


def test_scan_ranges_cover_all_ids_once() -> None:
    assert fix.scan_ranges((1, 10), 4) == [(1, 4), (5, 8), (9, 10)]
    assert fix.scan_ranges((7, 7), 200_000) == [(7, 7)]


def test_labels_are_quoted_as_sql_literals() -> None:
    group = fix.Group(1, "sensor_community-1", None, "O'Brien's garden")

    sql = fix.label_filter("o", group)

    assert "IS NOT DISTINCT FROM 'O''Brien''s garden'" in sql
    assert """o."PARAMETERS"->>'observed_property' IS NOT DISTINCT FROM NULL""" in sql


# ---------------------------------------------------------------------------
# Runs against a fake database
# ---------------------------------------------------------------------------


def _line(**fields: Any) -> str:
    return json.dumps(fields)


_DS_PROPS = {1386: "pressure", 1388: "pm10", 1389: "pm2_5"}  # datastreams of one Thing (10), FoI 110
_SID, _THING = "sensor_community-93182", "SensorCommunity 93182"
_VALUES_ID = re.compile(r"\((\d+)::bigint, ")
_STATE_ROW = re.compile(r"\((\d+)::bigint, ([^,()]+)::double precision, (true|false)\)")


@dataclass
class _Obs:
    ds: int
    prop: str
    num: float | None = 12.0
    foi: int | None = 110
    dup: bool = False  # its datastream-to-be already has an observation at its phenomenonTime


class FakeDb:
    """psql on the FROST database, in memory: answers the script's statements by their JSON tag.

    It keeps where every observation is, so plans, moves, rollbacks and refreshes can be
    followed end to end; it does not evaluate SQL (the integration tests below run the real
    SQL on FROST 2.8). fail_at makes the n-th move or restore statement fail, as a lock
    timeout would.
    """

    def __init__(self, ids_file: Path | None = None, observations: dict[int, _Obs] | None = None) -> None:
        self.obs = observations if observations is not None else {
            11: _Obs(1386, "pm10", num=12.0),
            12: _Obs(1386, "pm10", num=2500.0, dup=True),
        }
        self.ids_file = ids_file
        self.sessions: list[str] = []
        self.recorded_before_write: list[bool] = []
        self.busy: list[str] = []
        self.busy_while_writing: list[str] = []  # sessions that show up once the run has sent a write
        self.fail_at: set[int] = set()
        self.stuck: set[int] = set()  # rows a restore does not move, as if another writer put them back at once
        self.writes = 0
        self.refreshed: list[list[int]] = []  # the datastreams of each refresh session

    def __call__(self, sql: str) -> list[str]:
        self.sessions.append(sql)
        if 'UPDATE "DATASTREAMS"' in sql:
            self.refreshed.append([int(i) for i in re.findall(r'WHERE d\."ID" = (\d+);', sql)])
            return []
        if "'t', 'busy'" in sql:
            return [*self.busy, *(self.busy_while_writing if self.writes else [])]
        if "'t', 'ds'" in sql:
            return self._catalogue()
        if "'t', 'grp'" in sql:
            return self._groups(sql)
        if "'t', 'row'" in sql:
            return self._batch(sql)
        if "moved AS" in sql or "restored AS" in sql:
            return self._write(sql)
        if "'t', 'rbstate'" in sql:
            return [self._state(statement) for statement in sql.split(";\n") if "'t', 'rbstate'" in statement]
        return []  # duplicates: none

    def _catalogue(self) -> list[str]:
        lines = [
            _line(t="ds", id=ds, name=f"GEO_{_THING} sensor - {prop}", thing_id=10, thing=f"GEO_{_THING}",
                  source="sensor_community", sensor_id=_SID, prop=prop)
            for ds, prop in _DS_PROPS.items()
        ]
        return [*lines, _line(t="foi", thing_id=10, locations=1, foi=110, fois=1), _line(t="bounds", min=1, max=450_000)]

    def _groups(self, sql: str) -> list[str]:
        scoped = {int(ds) for ds in re.findall(r'WHERE o\."DATASTREAM_ID" = (\d+) AND', sql)}
        groups: dict[tuple[int, str], list[int]] = {}
        for i, o in sorted(self.obs.items()):
            if o.prop != _DS_PROPS.get(o.ds) and (not scoped or o.ds in scoped):
                groups.setdefault((o.ds, o.prop), []).append(i)
        return [
            _line(t="grp", ds=ds, sid=_SID, prop=prop, thing=_THING, loc="93182", n=len(ids), min_id=min(ids),
                  max_id=max(ids), first="2026-11-12T14:12:23+00:00", last="2026-11-12T14:17:23+00:00")
            for (ds, prop), ids in groups.items()
        ]

    def _batch(self, sql: str) -> list[str]:
        where = re.search(r'WHERE o\."DATASTREAM_ID" = (\d+) AND o\."ID" > (\d+)', sql)
        prop = re.search(r"'observed_property' IS NOT DISTINCT FROM '([^']*)'", sql)
        limit = re.search(r"LIMIT (\d+);", sql)
        assert where is not None and prop is not None and limit is not None
        ds, after = int(where[1]), int(where[2])
        rows = [(i, o) for i, o in sorted(self.obs.items()) if o.ds == ds and o.prop == prop[1] and i > after]
        return [_line(t="row", id=i, foi=o.foi, num=o.num, dup=o.dup) for i, o in rows[: int(limit[1])]]

    def _write(self, sql: str) -> list[str]:
        listed = [int(i) for i in _VALUES_ID.findall(sql)]
        if self.ids_file is not None and "moved AS" in sql:
            text = self.ids_file.read_text(encoding="utf-8")
            self.recorded_before_write.append(all(f"[{i}," in text for i in listed))
        self.writes += 1
        if self.writes in self.fail_at:
            raise fix.PsqlError("psql exited with 3: ERROR:  canceling statement due to lock timeout")
        new = re.search(r'SET "DATASTREAM_ID" = (\d+)', sql)
        now = re.search(r'WHERE o\."ID" = v\.id AND o\."DATASTREAM_ID" = (\d+)', sql)
        assert new is not None and now is not None
        count = 0
        for i in listed:
            if i in self.obs and self.obs[i].ds == int(now[1]) and not ("restored AS" in sql and i in self.stuck):
                self.obs[i].ds = int(new[1])
                count += 1
        return [_line(t="moved" if "moved AS" in sql else "restored", n=count)]

    def _state(self, statement: str) -> str:
        label = re.search(r"'batch', '([^']*)'", statement)
        to_ds = re.search(r"""'in_to', count\(\*\) FILTER \(WHERE o\."DATASTREAM_ID" = (\d+)\)""", statement)
        from_ds = re.search(r"""'in_from', count\(\*\) FILTER \(WHERE o\."DATASTREAM_ID" = (\d+)\)""", statement)
        assert label is not None and to_ds is not None and from_ds is not None
        counts: Counter[str] = Counter()
        for i, num, known in _STATE_ROW.findall(statement):
            counts["listed"] += 1
            o = self.obs.get(int(i))
            if o is None:
                counts["missing"] += 1
                continue
            counts["in_to"] += o.ds == int(to_ds[1])
            counts["in_from"] += o.ds == int(from_ds[1])
            recorded = None if num == "NULL" else float(num)
            counts["changed"] += o.ds == int(to_ds[1]) and known == "true" and o.num != recorded
        return _line(t="rbstate", batch=label[1], **{k: counts[k] for k in ("listed", "in_to", "in_from", "missing", "changed")})


def _apply(ids_file: Path, *extra: str) -> argparse.Namespace:
    return fix.parse_args(["--container", "db", "--apply", "--ids-file", str(ids_file), *extra])


def _rollback(ids_file: Path, *extra: str) -> argparse.Namespace:
    return fix.parse_args(["--container", "db", "--rollback", "--ids-file", str(ids_file), *extra])


def _ids_file(path: Path, *records: dict[str, Any]) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in (fix.IDS_HEADER, *records)), encoding="utf-8")
    return path


def _move_record(batch: str, from_ds: int, to_ds: int, rows: list[list[Any]], to_foi: int | None = None) -> dict[str, Any]:
    return {"op": "move", "batch": batch, "from_ds": from_ds, "to_ds": to_ds, "to_foi": to_foi, "rows": rows}


def _writes(db: FakeDb) -> list[str]:
    return [sql for sql in db.sessions if not sql.startswith(fix.READ_ONLY_SESSION)]


def test_dry_run_only_opens_read_only_sessions(tmp_path) -> None:
    db = FakeDb()
    args = fix.parse_args(["--container", "db", "--plan-file", str(tmp_path / "plan.csv")])

    assert fix.run(args, db) == 0

    assert db.sessions
    for sql in db.sessions:
        assert sql.startswith(fix.READ_ONLY_SESSION)
        assert "UPDATE" not in sql.upper()
    plan_rows = (tmp_path / "plan.csv").read_text(encoding="utf-8").splitlines()
    assert plan_rows[1].startswith("move,1386,")
    assert ",1388," in plan_rows[1]


def test_apply_records_every_batch_before_writing_it_and_skips_duplicates(tmp_path, capsys) -> None:
    ids_file = tmp_path / "moved.jsonl"
    db = FakeDb(ids_file)

    assert fix.run(_apply(ids_file), db) == 0

    assert db.recorded_before_write == [True]
    (move_sql,) = [sql for sql in db.sessions if "moved AS" in sql]
    assert move_sql.startswith(fix.WRITE_SESSION)
    assert '"DATASTREAM_ID" = 1388' in move_sql  # to its own datastream
    assert 'o."DATASTREAM_ID" = 1386' in move_sql  # only while still in the source
    assert "(11::bigint, 110::bigint)" in move_sql and "12::bigint" not in move_sql  # 12 is a duplicate
    assert 'SET "DATASTREAM_ID" = 1388, "FEATURE_ID" = o."FEATURE_ID"' in move_sql  # same Thing: FoI kept
    assert db.refreshed == [[1386, 1388]]
    records = [json.loads(line) for line in ids_file.read_text(encoding="utf-8").splitlines()]
    assert records[0] == fix.IDS_HEADER
    assert [r["op"] for r in records[1:]] == ["run", "move", "committed"]
    assert records[2] | {"batch": None} == {  # with each row's FEATURE_ID and RESULT_NUMBER before the move
        "op": "move", "batch": None, "from_ds": 1386, "to_ds": 1388, "to_foi": None, "rows": [[11, 110, 12.0]],
    }
    assert "Moved 1 observations (1 left in place" in capsys.readouterr().out


def test_batches_read_each_rows_result_number() -> None:
    planned = fix.PlannedGroup(fix.Group(1386, _SID, "pm10", _THING, min_id=1, max_id=9), fix.MOVE,
                               _catalogue().datastreams[1388])
    assert """'num', o."RESULT_NUMBER\"""" in fix.batch_select_sql(planned, 0, 10)


def test_only_the_same_reading_in_the_target_keeps_a_row_in_place() -> None:
    """Another row at that phenomenonTime (e.g. another misrouted row, which moves away itself) must not strand
    it: only a row with the same label, time and result is a duplicate, as the cleanup script's --duplicates."""
    planned = fix.PlannedGroup(fix.Group(1386, _SID, "pm10", _THING, min_id=1, max_id=9), fix.MOVE,
                               _catalogue().datastreams[1388])
    for sql in (fix.batch_select_sql(planned, 0, 10), fix.duplicates_sql(planned)):
        assert fix.same_reading_sql(1388) in sql
    same = fix.same_reading_sql(1388)
    assert 'x."DATASTREAM_ID" = 1388' in same
    assert 'x."PHENOMENON_TIME_START" = o."PHENOMENON_TIME_START"' in same
    for key in ("sensor_id", "observed_property"):
        assert f"""x."PARAMETERS"->>'{key}' IS NOT DISTINCT FROM o."PARAMETERS"->>'{key}'""" in same
    for column in ("PHENOMENON_TIME_END", "RESULT_TYPE", "RESULT_NUMBER", "RESULT_STRING", "RESULT_BOOLEAN",
                   "RESULT_JSON"):
        assert f'x."{column}" IS NOT DISTINCT FROM o."{column}"' in same


def _two_groups() -> dict[int, _Obs]:
    return {11: _Obs(1386, "pm10"), 12: _Obs(1386, "pm10"), 13: _Obs(1386, "pm2_5", num=7.0)}


def test_apply_refreshes_each_group_once_its_batches_are_done(tmp_path) -> None:
    db = FakeDb(tmp_path / "moved.jsonl", _two_groups())

    assert fix.run(_apply(tmp_path / "moved.jsonl", "--batch-rows", "1"), db) == 0

    kinds = ["move" if "moved AS" in sql else "refresh" for sql in _writes(db)]
    assert kinds == ["move", "move", "refresh", "move", "refresh"]
    assert db.refreshed == [[1386, 1388], [1386, 1389]]


def test_a_failed_batch_still_refreshes_what_its_group_touched(tmp_path) -> None:
    """A lock timeout, statement timeout, dropped connection or Ctrl-C ends the run: FROST's trigger ignores
    a changed DATASTREAM_ID, so nothing else would ever fix the times of the datastreams moved between."""
    db = FakeDb(tmp_path / "moved.jsonl", _two_groups())
    db.fail_at = {3}  # the pm2_5 group's batch

    with pytest.raises(fix.PsqlError, match=r"lock timeout.*Datastreams \[1386, 1389\] were refreshed"):
        fix.run(_apply(tmp_path / "moved.jsonl", "--batch-rows", "1"), db)

    assert db.refreshed == [[1386, 1388], [1386, 1389]]


def test_a_failed_batch_is_not_refreshed_while_its_session_may_still_run(tmp_path) -> None:
    """After a client-side timeout or Ctrl-C the UPDATE can still finish on the server: a refresh before it has
    would leave the times stale again, so the error says to re-run (which refreshes first) instead."""
    db = FakeDb(tmp_path / "moved.jsonl")
    db.fail_at = {1}
    db.busy_while_writing = [_line(t="busy", pid=4242, state="active", running="00:00:12")]

    with pytest.raises(fix.PsqlError, match=r"Not refreshed yet: datastreams \[1386, 1388\]"):
        fix.run(_apply(tmp_path / "moved.jsonl"), db)

    assert db.refreshed == []


def test_every_write_run_first_refreshes_the_datastreams_the_ids_file_names(tmp_path, capsys) -> None:
    """An interrupted run may have left any of them stale; a re-run moves other rows, so it refreshes them all."""
    ids_file = _ids_file(tmp_path / "moved.jsonl", _move_record("b1", 5, 6, [[31, 1, 1.0]]))
    db = FakeDb(ids_file)
    assert fix.run(_apply(ids_file), db) == 0
    assert db.refreshed == [[5, 6], [1386, 1388]]

    db = FakeDb(ids_file, {11: _Obs(1388, "pm10"), 31: _Obs(6, "pm10")})
    assert fix.run(_rollback(ids_file, "--apply", "--datastream", "1386"), db) == 0
    assert db.refreshed == [[5, 6], [1386, 1388]]  # the rest first; then what it moves back

    db = FakeDb(ids_file)
    assert fix.run(fix.parse_args(["--container", "db", "--refresh-only", "--ids-file", str(ids_file)]), db) == 0
    assert db.refreshed == [[5, 6, 1386, 1388]]
    assert [sql for sql in _writes(db) if 'UPDATE "DATASTREAMS"' not in sql] == []
    assert "Refreshed 4 datastreams." in capsys.readouterr().out


def test_writes_refuse_while_another_write_session_is_active(tmp_path, capsys) -> None:
    ids_file = _ids_file(tmp_path / "moved.jsonl", _move_record("b1", 1386, 1388, [[11, 110, 12.0]]))
    for args in (_apply(ids_file), _rollback(ids_file, "--apply"),
                 fix.parse_args(["--container", "db", "--refresh-only", "--ids-file", str(ids_file)])):
        db = FakeDb(ids_file)
        db.busy = [_line(t="busy", pid=4242, state="active", running="00:00:12")]
        assert fix.run(args, db) == 1
        assert _writes(db) == []
        assert "pid 4242: active" in capsys.readouterr().out


def test_rollback_is_a_dry_run_unless_apply_is_given(tmp_path, capsys) -> None:
    ids_file = tmp_path / "moved.jsonl"
    db = FakeDb(ids_file)
    assert fix.run(_apply(ids_file), db) == 0
    sessions = len(db.sessions)
    capsys.readouterr()

    assert fix.run(_rollback(ids_file), db) == 0

    assert db.sessions[sessions:]
    assert all(sql.startswith(fix.READ_ONLY_SESSION) for sql in db.sessions[sessions:])
    assert db.obs[11].ds == 1388  # still moved
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert re.search(r" 1386 <- 1388\s+1\s+0\s+1\s+0\s+0\s+0\s+0\n", out)  # rows, later, in to, back, else, gone, changed
    assert re.search(r"still where the script put them: to move back\s+1\n", out)
    assert "Re-run with --apply to move 1 rows back." in out


def test_rollback_apply_moves_back_newest_batch_first_and_verifies(tmp_path, capsys) -> None:
    """Row 11 was moved twice (1386 -> 1388, later 1388 -> 1389): undone in reverse order it ends where it began."""
    ids_file = _ids_file(
        tmp_path / "moved.jsonl",
        _move_record("b1", 1386, 1388, [[11, 110, 12.0]]),
        _move_record("b2", 1388, 1389, [[11, 110, 12.0]]),
    )
    db = FakeDb(ids_file, {11: _Obs(1389, "pm10")})

    assert fix.run(_rollback(ids_file, "--apply"), db) == 0

    assert db.obs[11].ds == 1386
    restores = [sql for sql in db.sessions if "restored AS" in sql]
    assert ['o."DATASTREAM_ID" = 1389' in sql for sql in restores] == [True, False]  # b2 first
    out = capsys.readouterr().out
    assert "Moved back 2 observations (planned 1)" in out  # b1's row only got back into 1388 by b2's rollback
    assert "Verification: no recorded row is left where the script put it." in out
    ops = [json.loads(line)["op"] for line in ids_file.read_text(encoding="utf-8").splitlines()]
    assert ops == ["ids-file", "move", "move", "rollback", "rolled_back", "rolled_back"]


def test_rollback_counts_a_row_listed_by_a_failed_batch_and_the_reruns_once(tmp_path, capsys) -> None:
    """A batch that failed (a lock timeout) is in the IDs file; the re-run moved its rows in a batch of its own."""
    ids_file = _ids_file(
        tmp_path / "moved.jsonl",
        _move_record("b1", 1386, 1388, [[11, 110, 12.0], [12, 110, 12.0]]),
        _move_record("b2", 1386, 1388, [[11, 110, 12.0], [12, 110, 12.0]]),
        {"op": "committed", "batch": "b2", "moved": 2},
    )
    db = FakeDb(ids_file, {11: _Obs(1388, "pm10"), 12: _Obs(1388, "pm10")})

    assert fix.run(_rollback(ids_file), db) == 0
    out = capsys.readouterr().out
    assert re.search(r"b2\s+1386 <- 1388\s+2\s+0\s+2\s+0\s+0\s+0\s+0\n", out)
    assert re.search(r"b1\s+1386 <- 1388\s+2\s+2\s+0\s+0\s+0\s+0\s+0\n", out)
    assert re.search(r"still where the script put them: to move back\s+2\n", out)

    assert fix.run(_rollback(ids_file, "--apply"), db) == 0
    assert "Moved back 2 observations (planned 2)" in capsys.readouterr().out
    assert (db.obs[11].ds, db.obs[12].ds) == (1386, 1386)


def test_rollback_apply_checks_that_no_recorded_row_is_left_behind(tmp_path, capsys) -> None:
    ids_file = _ids_file(tmp_path / "moved.jsonl", _move_record("b1", 1386, 1388, [[11, 110, 12.0], [12, 110, 12.0]]))
    db = FakeDb(ids_file, {11: _Obs(1388, "pm10"), 12: _Obs(1388, "pm10")})
    db.stuck = {12}

    assert fix.run(_rollback(ids_file, "--apply"), db) == 1

    out = capsys.readouterr().out
    assert "Moved back 1 observations (planned 2)" in out
    assert "Verification: 1 recorded rows are still where the script put them. Re-run to move them back." in out


def test_rollback_datastream_limits_it_to_batches_from_those_sources(tmp_path) -> None:
    ids_file = _ids_file(
        tmp_path / "moved.jsonl",
        _move_record("b1", 1386, 1388, [[11, 110, 12.0]]),
        _move_record("b2", 5, 1389, [[20, 110, 3.0]]),
    )
    db = FakeDb(ids_file, {11: _Obs(1388, "pm10"), 20: _Obs(1389, "pm2_5", num=3.0)})

    assert fix.run(_rollback(ids_file, "--apply", "--datastream", "1386"), db) == 0
    assert (db.obs[11].ds, db.obs[20].ds) == (1386, 1389)

    assert fix.run(_rollback(ids_file, "--apply", "--datastream", "77"), db) == 1  # no batch from 77
    assert fix.run(_rollback(ids_file, "--apply"), db) == 0
    assert (db.obs[11].ds, db.obs[20].ds) == (1386, 5)


def test_rollback_reports_rows_whose_value_changed_since_the_move(tmp_path, capsys) -> None:
    ids_file = tmp_path / "moved.jsonl"
    db = FakeDb(ids_file, {11: _Obs(1386, "pm10", num=100500.0), 12: _Obs(1386, "pm10", num=100501.0)})
    assert fix.run(_apply(ids_file), db) == 0
    db.obs[11].num = 1005.0  # converted by the pressure backfill's re-run
    capsys.readouterr()

    assert fix.run(_rollback(ids_file), db) == 0

    out = capsys.readouterr().out
    assert re.search(r"of those with another RESULT_NUMBER than when moved\s+1\n", out)
    assert "fix_sc_pressure_units.py" in out


def test_rollback_moves_recorded_rows_back_with_their_feature_ids(tmp_path) -> None:
    ids_file = _ids_file(
        tmp_path / "moved.jsonl",
        _move_record("b1", 1386, 2016, [[11, 110, 12.0], [12, None, None]], to_foi=120),
        {"op": "committed", "batch": "b1", "moved": 2},
    )
    db = FakeDb(ids_file, {11: _Obs(2016, "pm10"), 12: _Obs(2016, "pm10", num=None, foi=120)})

    assert fix.run(_rollback(ids_file, "--apply"), db) == 0

    (restore,) = [sql for sql in db.sessions if "restored AS" in sql]
    assert 'SET "DATASTREAM_ID" = 1386, "FEATURE_ID" = v.foi' in restore
    assert 'o."DATASTREAM_ID" = 2016' in restore  # only rows still where the script put them
    assert "(11::bigint, 110::bigint), (12::bigint, NULL::bigint)" in restore
    assert db.refreshed == [[1386, 2016]]
    assert json.loads(ids_file.read_text(encoding="utf-8").splitlines()[-1])["op"] == "rolled_back"


@pytest.mark.parametrize("mode", ["--apply", "--rollback", "--refresh-only"])
def test_writes_need_an_ids_file(mode: str) -> None:
    with pytest.raises(SystemExit):
        fix.parse_args(["--container", "db", mode])


def test_rollback_and_refresh_only_need_an_existing_ids_file(tmp_path) -> None:
    for mode in ("--rollback", "--refresh-only"):
        with pytest.raises(SystemExit):
            fix.parse_args(["--container", "db", mode, "--ids-file", str(tmp_path / "none.jsonl")])
    ids_file = _ids_file(tmp_path / "moved.jsonl")
    for extra in (["--apply"], ["--rollback"]):
        with pytest.raises(SystemExit):
            fix.parse_args(["--container", "db", "--refresh-only", "--ids-file", str(ids_file), *extra])


# --- the IDs file ------------------------------------------------------------


def test_ids_file_refuses_another_scripts_file(tmp_path) -> None:
    """E.g. the pressure backfill's CSV: appending to it would corrupt both records."""
    csv_file = tmp_path / "sc_pressure_ids.csv"
    text = "datastream_id,observation_id,result_number,result_string\n16,1,100391.86,\"100391.86\"\n"
    csv_file.write_text(text, encoding="utf-8")
    for args in (_apply(csv_file), _rollback(csv_file), _rollback(csv_file, "--apply")):
        db = FakeDb(csv_file)
        with pytest.raises(fix.IdsFileError, match="not an IDs file of this script"):
            fix.run(args, db)
        assert db.sessions == []
    assert csv_file.read_text(encoding="utf-8") == text
    assert fix.main(["--container", "db", "--apply", "--ids-file", str(csv_file)]) == 1


def test_a_cut_off_last_line_is_dropped_before_anything_is_appended(tmp_path, capsys) -> None:
    ids_file = _ids_file(tmp_path / "moved.jsonl", _move_record("b1", 1386, 1388, [[11, 110, 12.0]]))
    complete = ids_file.read_bytes()
    ids_file.write_bytes(complete + b'{"op":"move","batch":"b2","from_ds":1386,"to_ds":13')  # an interrupted append

    assert [r["batch"] for r in fix.IdsFile(ids_file).moves()] == ["b1"]  # skipped when read
    assert "unreadable last line skipped" in capsys.readouterr().err
    fix.IdsFile(ids_file).prepare()
    assert ids_file.read_bytes() == complete
    assert "dropping the cut-off last line" in capsys.readouterr().err

    db = FakeDb(ids_file, {11: _Obs(1388, "pm10"), 12: _Obs(1386, "pm10")})
    assert fix.run(_apply(ids_file), db) == 0  # appends whole lines after it
    batches = [r["batch"] for r in fix.IdsFile(ids_file).moves()]
    assert len(batches) == 2 and batches[0] == "b1"
    assert fix.run(_rollback(ids_file, "--apply"), db) == 0
    assert (db.obs[11].ds, db.obs[12].ds) == (1386, 1386)


def test_an_unreadable_line_before_the_last_is_refused(tmp_path) -> None:
    ids_file = _ids_file(tmp_path / "moved.jsonl", _move_record("b1", 1386, 1388, [[11, 110, 12.0]]))
    lines = ids_file.read_text(encoding="utf-8").splitlines()
    ids_file.write_text("\n".join([lines[0], '{"op":"move","batch":"b0",', *lines[1:]]) + "\n", encoding="utf-8")
    with pytest.raises(fix.IdsFileError, match="line 2"):
        fix.IdsFile(ids_file).moves()


def test_ids_file_gets_its_header_when_it_is_new_or_empty(tmp_path) -> None:
    for path in (tmp_path / "new.jsonl", tmp_path / "empty.jsonl"):
        if path.name == "empty.jsonl":
            path.write_bytes(b"")
        fix.IdsFile(path).append({"op": "run"})
        assert [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] == [fix.IDS_HEADER, {"op": "run"}]
    header_only = tmp_path / "header.jsonl"
    header_only.write_text(json.dumps(fix.IDS_HEADER, separators=(",", ":")), encoding="utf-8")  # its line end lost
    fix.IdsFile(header_only).prepare()
    assert header_only.read_text(encoding="utf-8").endswith("}\n")


# --- SQL and transport -----------------------------------------------------------


def test_refresh_gives_a_datastream_without_an_observed_area_one() -> None:
    """A datastream registered after the misrouting has no OBSERVED_AREA; moves are its first rows."""
    sql = fix.refresh_sql(1388)
    assert """"OBSERVED_AREA" = CASE WHEN d."OBSERVED_AREA" IS NOT NULL THEN d."OBSERVED_AREA" ELSE""" in sql
    assert """SELECT ST_ConvexHull(ST_Collect(f."GEOM")) FROM "FEATURES" f""" in sql
    assert """"LAST_FOI_ID" = CASE WHEN d."OBSERVED_AREA" IS NOT NULL THEN d."LAST_FOI_ID" ELSE""" in sql
    assert '"PHENOMENON_TIME_START" = a.p0' in sql and 'WHERE d."ID" = 1388;' in sql


def test_psql_over_ssh_uses_keepalives() -> None:
    """A silently dropped connection must fail within about a minute, not hang until --timeout."""
    argv = fix.psql_argv(ssh="root@frost.example", ssh_key="~/.ssh/key")
    assert argv[:7] == ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    assert argv[7] == "-i" and not argv[8].startswith("~")
    assert argv[9] == "root@frost.example"
    assert "PASSWORD" not in " ".join(argv).upper()


def test_psql_runner_turns_ctrl_c_into_an_error(monkeypatch) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(fix.subprocess, "run", interrupted)
    with pytest.raises(fix.PsqlError, match="interrupted"):
        fix.psql_runner(["psql"], timeout=1)("SELECT 1;")


# ---------------------------------------------------------------------------
# Integration: end to end on FROST-Server 2.8 (python -m pytest -m integration)
# ---------------------------------------------------------------------------


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="module")
def frost() -> Iterator[tuple[str, str]]:
    """(v1.1 base URL, database container) of a throwaway FROST 2.8 + PostGIS, torn down afterwards.

    Container and network names start with $TEST_DOCKER_PREFIX when set (default scfix-it).
    """
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    name = f"{os.environ.get('TEST_DOCKER_PREFIX') or 'scfix-it'}-{uuid.uuid4().hex[:8]}"
    net, db, server = f"{name}-net", f"{name}-db", f"{name}-frost"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = f"http://127.0.0.1:{port}/FROST-Server"  # not localhost: IPv6 first is slow on Windows
    try:
        _docker("network", "create", net)
        _docker("run", "-d", "--name", db, "--network", net, "-e", "POSTGRES_DB=sensorthings",
                "-e", "POSTGRES_USER=sensorthings", "-e", "POSTGRES_PASSWORD=sensorthings",
                "postgis/postgis:16-3.4-alpine")
        deadline = time.monotonic() + 120
        while _docker("exec", db, "pg_isready", "-h", "127.0.0.1", "-U", "sensorthings", check=False).returncode:
            if time.monotonic() > deadline:  # over TCP: the image's init phase runs a socket-only server first
                raise TimeoutError("PostGIS not ready")
            time.sleep(1)
        _docker("run", "-d", "--name", server, "--network", net, "-p", f"127.0.0.1:{port}:8080",
                "-e", f"serviceRootUrl={root}", "-e", "persistence_db_driver=org.postgresql.Driver",
                "-e", f"persistence_db_url=jdbc:postgresql://{db}:5432/sensorthings",
                "-e", "persistence_db_username=sensorthings", "-e", "persistence_db_password=sensorthings",
                "-e", "persistence_autoUpdateDatabase=true", "fraunhoferiosb/frost-server:2.8")
        deadline = time.monotonic() + 240
        while True:
            try:
                if requests.get(f"{root}/v1.1/Things", timeout=5).status_code == 200:
                    break
            except requests.RequestException:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"FROST at {root} not ready")
            time.sleep(2)
        yield f"{root}/v1.1", db
    finally:
        _docker("rm", "-f", "-v", server, db, check=False)  # -v: the PostGIS image's anonymous data volume too
        _docker("network", "rm", net, check=False)


def _datastream(name: str, sensor_id: str, prop: str) -> dict[str, Any]:
    return {
        "name": f"GEO_{name} sensor - {prop}", "description": "-",
        "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
        "unitOfMeasurement": {"name": prop, "symbol": "1", "definition": "-"},
        "properties": {"sensor_id": sensor_id, "observed_property": prop, "streamKey": prop},
        "Sensor": {"name": f"GEO_{name} {prop} sensor", "description": "-", "encodingType": "text/plain", "metadata": "-"},
        "ObservedProperty": {"name": f"{name} {prop}", "definition": "-", "description": "-"},
    }


def _thing(base: str, name: str, source: str, sensor_id: str, props: list[str], lon: float) -> tuple[str, dict[str, int]]:
    """Deep-insert a Thing the way the connector registers one: (Thing URL, {property: datastream id})."""
    body = {
        "name": f"GEO_{name}",
        "description": "-",
        "properties": {"source": source},
        "Locations": [{"name": f"GEO_{name} location", "description": "-", "encodingType": "application/geo+json",
                       "location": {"type": "Point", "coordinates": [lon, 52.0]}}],
        "Datastreams": [_datastream(name, sensor_id, prop) for prop in props],
    }
    response = requests.post(f"{base}/Things", json=body, timeout=15)
    response.raise_for_status()
    return response.headers["Location"], _datastreams_of(response.headers["Location"])


def _datastreams_of(thing_url: str) -> dict[str, int]:
    listing = requests.get(f"{thing_url}/Datastreams", params={"$select": "id,properties"}, timeout=15).json()
    return {ds["properties"]["observed_property"]: ds["@iot.id"] for ds in listing["value"]}


def _add_datastreams(base: str, thing_url: str, name: str, sensor_id: str, props: list[str]) -> dict[str, int]:
    """What registering a later-discovered entity set adds to an existing Thing."""
    thing_id = int(thing_url.rsplit("(", 1)[1].rstrip(")"))
    for prop in props:
        body = _datastream(name, sensor_id, prop) | {"Thing": {"@iot.id": thing_id}}
        requests.post(f"{base}/Datastreams", json=body, timeout=15).raise_for_status()
    return _datastreams_of(thing_url)


def _observe(base: str, ds_id: int, minute: int, sensor_id: str | None, prop: str | None, thing: str | None) -> None:
    body: dict[str, Any] = {"phenomenonTime": f"2026-11-12T14:{minute:02d}:00Z", "result": minute, "Datastream": {"@iot.id": ds_id}}
    if sensor_id is not None:
        body["parameters"] = {"sensor_id": sensor_id, "observed_property": prop, "thing_name": thing,
                              "location": sensor_id.rsplit("-", 1)[-1]}
    requests.post(f"{base}/Observations", json=body, timeout=15).raise_for_status()


def _psql(db: str, sql: str) -> list[str]:
    proc = subprocess.run(["docker", "exec", "-i", db, "sh", "-c", fix.CONTAINER_PSQL], input=sql,
                          capture_output=True, text=True, check=True, timeout=120)
    return proc.stdout.splitlines()


def _state(db: str) -> dict[str, list[str]]:
    return {
        "observations": _psql(db, 'SELECT "ID", "DATASTREAM_ID", "FEATURE_ID" FROM "OBSERVATIONS" ORDER BY 1;'),
        "datastreams": _psql(db, 'SELECT "ID", "PHENOMENON_TIME_START", "PHENOMENON_TIME_END" FROM "DATASTREAMS" ORDER BY 1;'),
    }


@pytest.mark.integration
def test_plan_apply_and_rollback_on_frost_28(frost, tmp_path, capsys) -> None:
    base, db = frost
    a, b = "sensor_community-93182", "sensor_community-91154"
    a_thing_url, a_ds = _thing(base, "SensorCommunity 93182", "sensor_community", a, ["temperature", "pressure", "humidity"], 4.1)
    b_thing_url, b_ds = _thing(base, "SensorCommunity 91154", "sensor_community", b, ["temperature", "pressure", "humidity"], 4.2)
    _, sm_ds = _thing(base, "SamenMeten SSK_X", "samen_meten", "sm-ssk_x", ["pm10"], 4.3)

    a_thing, b_thing = "SensorCommunity 93182", "SensorCommunity 91154"
    for minute in (1, 2, 3):
        _observe(base, a_ds["pressure"], minute, a, "pressure", a_thing)  # belongs there
        _observe(base, b_ds["temperature"], minute, b, "temperature", b_thing)
    _observe(base, a_ds["pressure"], 10, a, "pm10", a_thing)  # bare sensor-id fallback
    _observe(base, a_ds["pressure"], 11, a, "pm10", a_thing)
    _observe(base, a_ds["pressure"], 10, a, "pm2_5", a_thing)
    _observe(base, a_ds["pressure"], 12, b, "pm10", b_thing)  # stale id from the old server
    _observe(base, b_ds["humidity"], 13, b, "pressure", b_thing)  # same Thing, other property
    _observe(base, b_ds["pressure"], 13, b, "pressure", b_thing)  # ... already there: a duplicate
    _observe(base, b_ds["humidity"], 14, b, "pressure", b_thing)
    _observe(base, a_ds["humidity"], 15, "tgv-officelab-climate", "humidity", "Climate adaptation setup")
    _observe(base, sm_ds["pm10"], 16, a, "pm10", a_thing)  # a Sensor.Community reading elsewhere
    _observe(base, a_ds["temperature"], 17, None, None, None)  # no labels

    script = ["--container", db, "--range-ids", "3", "--chunk", "2"]
    plan_file = tmp_path / "plan.csv"
    assert fix.main([*script, "--plan-file", str(plan_file)]) == 0
    plan = {(r.split(",")[0], r.split(",")[3], r.split(",")[4]) for r in plan_file.read_text(encoding="utf-8").splitlines()[1:]}
    assert plan == {
        ("needs-datastream", a, "pm10"), ("needs-datastream", a, "pm2_5"), ("needs-datastream", b, "pm10"),
        ("move", b, "pressure"), ("no-datastream", "tgv-officelab-climate", "humidity"), ("unlabelled", "", ""),
    }

    # The fixed connector registers the PM datastreams of both locations.
    a_pm = _add_datastreams(base, a_thing_url, "SensorCommunity 93182", a, ["pm10", "pm2_5"])
    b_pm = _add_datastreams(base, b_thing_url, "SensorCommunity 91154", b, ["pm10"])
    foi = {name: _psql(db, f"""SELECT l."GEN_FOI_ID" FROM "LOCATIONS" l WHERE l."NAME" = 'GEO_{name} location';""")[0]
           for name in ("SensorCommunity 93182", "SensorCommunity 91154", "SamenMeten SSK_X")}
    assert len(set(foi.values())) == 3
    before = _state(db)

    ids_file = tmp_path / "moved.jsonl"
    capsys.readouterr()
    assert fix.main([*script, "--apply", "--ids-file", str(ids_file), "--batch-rows", "1"]) == 0
    out = capsys.readouterr().out
    assert "Moved 6 observations (1 left in place" in out

    def where(sensor_id: str, prop: str, minute: int) -> tuple[str, str]:
        rows = _psql(db, f"""SELECT "DATASTREAM_ID", "FEATURE_ID" FROM "OBSERVATIONS"
                              WHERE "PARAMETERS"->>'sensor_id' = '{sensor_id}' AND "PARAMETERS"->>'observed_property' = '{prop}'
                                AND "PHENOMENON_TIME_START" = '2026-11-12T14:{minute:02d}:00Z' ORDER BY "ID";""")
        return tuple(rows[0].split("|"))  # type: ignore[return-value]

    assert where(a, "pm10", 10) == (str(a_pm["pm10"]), foi["SensorCommunity 93182"])  # same Thing: FoI kept
    assert where(a, "pm2_5", 10) == (str(a_pm["pm2_5"]), foi["SensorCommunity 93182"])
    assert where(a, "pm10", 16) == (str(a_pm["pm10"]), foi["SensorCommunity 93182"])  # out of Samen Meten: A's FoI
    assert where(b, "pm10", 12) == (str(b_pm["pm10"]), foi["SensorCommunity 91154"])  # out of A: B's FoI
    assert where(b, "pressure", 14) == (str(b_ds["pressure"]), foi["SensorCommunity 91154"])
    assert where(b, "pressure", 13)[0] == str(b_ds["humidity"])  # duplicate left in place
    assert where("tgv-officelab-climate", "humidity", 15)[0] == str(a_ds["humidity"])  # not ours to move
    pressure_extent = _psql(db, f"""SELECT "PHENOMENON_TIME_START", "PHENOMENON_TIME_END" FROM "DATASTREAMS" WHERE "ID" = {a_ds["pressure"]};""")
    assert pressure_extent == ["2026-11-12 14:01:00+00|2026-11-12 14:03:00+00"]  # refreshed: only its pressure rows
    records = [json.loads(line) for line in ids_file.read_text(encoding="utf-8").splitlines()]
    assert sum(len(r["rows"]) for r in records if r["op"] == "move") == 6

    assert fix.main([*script, "--apply", "--ids-file", str(ids_file)]) == 0  # nothing left to move
    assert "Moved 0 observations" in capsys.readouterr().out

    assert fix.main([*script, "--rollback", "--ids-file", str(ids_file)]) == 0  # a dry run
    assert "Re-run with --apply to move 6 rows back." in capsys.readouterr().out
    assert fix.main([*script, "--rollback", "--apply", "--ids-file", str(ids_file)]) == 0
    assert _state(db) == before


# --- the 2026-11-20 review's scenario: five Things, rows misrouted the ways the pre-fix connector did ---

_TABLES = ('"OBSERVATIONS", "DATASTREAMS", "FEATURES", "THINGS_LOCATIONS", "LOCATIONS_HIST_LOCATIONS", '
           '"HIST_LOCATIONS", "LOCATIONS", "THINGS", "SENSORS", "OBS_PROPERTIES"')
_UNITS = {"pressure": ("Air pressure", "hPa"), "pm10": ("PM10 concentration", "ug/m3"),
          "pm2_5": ("PM2.5 concentration", "ug/m3"), "temperature": ("Air temperature", "Cel"),
          "humidity": ("Relative humidity", "%"), "air_pressure": ("Air pressure", "hPa")}
_HTTP = requests.Session()
_STALE_SQL = """
SELECT d."ID" FROM "DATASTREAMS" d
  LEFT JOIN (SELECT "DATASTREAM_ID" AS id, min("PHENOMENON_TIME_START") AS p0,
                    max(coalesce("PHENOMENON_TIME_END", "PHENOMENON_TIME_START")) AS p1,
                    min("RESULT_TIME") AS r0, max("RESULT_TIME") AS r1, count(*) AS n
               FROM "OBSERVATIONS" GROUP BY 1) a ON a.id = d."ID"
 WHERE (d."PHENOMENON_TIME_START", d."PHENOMENON_TIME_END", d."RESULT_TIME_START", d."RESULT_TIME_END")
       IS DISTINCT FROM (a.p0, a.p1, a.r0, a.r1)
    OR (a.n > 0 AND d."OBSERVED_AREA" IS NULL)
 ORDER BY 1;
"""


@dataclass
class Scenario:
    base: str
    db: str
    ds: dict[str, int]  # datastream ID per label, e.g. "A.pressure"
    obs: dict[str, int]  # observation ID per label, e.g. "B.p_in_A.h.2"


def _post(base: str, path: str, body: dict[str, Any]) -> int:
    response = _HTTP.post(f"{base}/{path}", json=body, timeout=60)
    assert response.status_code == 201, response.text
    match = re.search(r"\((\d+)\)$", response.headers["Location"])
    assert match is not None
    return int(match[1])


def _seed_scenario(base: str, db: str) -> Scenario:
    """Things A, B, D (Sensor.Community), SM (Samen Meten) and K (KNMI); their correct rows first (so each Thing's
    FoI is generated from its own Location), then what the pre-fix connector misrouted, then A's PM datastreams
    as the fixed connector registers them (empty)."""
    ops: dict[str, int] = {}
    sensors: dict[str, int] = {}

    def op(prop: str) -> int:
        name = _UNITS[prop][0]
        if name not in ops:
            ops[name] = _post(base, "ObservedProperties", {"name": name, "description": name,
                                                           "definition": f"https://example.org/{prop}"})
        return ops[name]

    def thing(name: str, source: str, lon: float, lat: float) -> int:
        thing_id = _post(base, "Things", {"name": name, "description": "-", "properties": {"source": source}})
        _post(base, "Locations", {"name": f"{name} location", "description": "-", "encodingType": "application/geo+json",
                                  "location": {"type": "Point", "coordinates": [lon, lat]},
                                  "Things": [{"@iot.id": thing_id}]})
        return thing_id

    def datastream(thing_id: int, thing_name: str, sid: str, prop: str) -> int:
        if sid not in sensors:
            sensors[sid] = _post(base, "Sensors", {"name": f"{thing_name} sensor", "description": "-",
                                                   "encodingType": "application/json", "metadata": "-"})
        name, symbol = _UNITS[prop]
        return _post(base, "Datastreams", {
            "name": f"{thing_name} sensor - {name}", "description": "-",
            "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
            "unitOfMeasurement": {"name": name, "symbol": symbol, "definition": "-"},
            "Thing": {"@iot.id": thing_id}, "Sensor": {"@iot.id": sensors[sid]}, "ObservedProperty": {"@iot.id": op(prop)},
            "properties": {"thing_name": thing_name, "sensor_id": sid, "observed_property": prop, "streamKey": prop},
        })

    a, b, d = "sensor_community-93182", "sensor_community-91154", "sensor_community-77"
    ta, tb, td = "SensorCommunity 93182", "SensorCommunity 91154", "SensorCommunity 77"
    things = {
        "A": thing(f"GEO_{ta}", "sensor_community", 4.1, 52.1), "B": thing(f"GEO_{tb}", "sensor_community", 4.2, 52.2),
        "D": thing(f"GEO_{td}", "sensor_community", 4.3, 52.3),
        "SM": thing("GEO_SamenMeten SSK_X", "samen_meten", 4.4, 52.4), "K": thing("GEO_KNMI 260", "knmi_weather", 5.18, 52.1),
    }
    ds: dict[str, int] = {}
    for prop in ("temperature", "pressure", "humidity"):
        ds[f"A.{prop}"] = datastream(things["A"], f"GEO_{ta}", a, prop)
    for prop in ("temperature", "pressure", "humidity", "pm10", "pm2_5"):
        ds[f"B.{prop}"] = datastream(things["B"], f"GEO_{tb}", b, prop)
    ds["D.pressure"] = datastream(things["D"], f"GEO_{td}", d, "pressure")
    ds["SM.pm10"] = datastream(things["SM"], "GEO_SamenMeten SSK_X", "sm-ssk_x", "pm10")
    ds["K.air_pressure"] = datastream(things["K"], "GEO_KNMI 260", "knmi-0", "air_pressure")
    obs: dict[str, int] = {}

    def add(label: str, stream: str, day: int, minute: int, value: float, sid: str | None = None, prop: str = "",
            thing_name: str = "") -> None:
        body: dict[str, Any] = {"phenomenonTime": f"2026-11-{day:02d}T{minute // 60:02d}:{minute % 60:02d}:00Z",
                                "result": value, "resultQuality": ["good"], "Datastream": {"@iot.id": ds[stream]}}
        if sid is not None:  # labelled as SensorThingsClient.push_observations labels a reading
            body["parameters"] = {"sensor_id": sid, "sensor_name": f"{thing_name} sensor", "observed_property": prop,
                                  "unit": "", "location": sid.rsplit("-", 1)[-1], "thing_name": thing_name,
                                  "device_eui": None, "stream_key": None}
        obs[label] = _post(base, "Observations", body)

    for k in range(20):
        add(f"A.p.{k}", "A.pressure", 10, k, 100000 + k * 11.37, a, "pressure", ta)
    for k in range(3):
        add(f"A.p.hpa.{k}", "A.pressure", 10, 100 + k, 1013.25 + k, a, "pressure", ta)
    for k in range(5):
        add(f"A.h.{k}", "A.humidity", 10, k, 60 + k, a, "humidity", ta)
    for k in range(3):
        add(f"A.t.{k}", "A.temperature", 10, k, 7.5 + k, a, "temperature", ta)
    for k in range(10):
        add(f"B.p.{k}", "B.pressure", 10, k, 101000 + k * 3.3, b, "pressure", tb)
    for k in range(3):
        add(f"B.pm10.{k}", "B.pm10", 10, k, 10 + k, b, "pm10", tb)
    for k in range(5):
        add(f"D.p.{k}", "D.pressure", 10, k, 99000 + k * 7.1, d, "pressure", td)
    for k in range(3):
        add(f"SM.pm10.{k}", "SM.pm10", 10, k, 20 + k, "sm-ssk_x", "pm10", "SamenMeten SSK_X")
    for k in range(3):
        add(f"K.ap.{k}", "K.air_pressure", 10, k, 1012.5 + k, "knmi-0", "air_pressure", "KNMI 260")
    # Misrouted. A's PM readings in A's Air pressure datastream (the bare sensor-id fallback), one in the Pa band:
    for k, value in enumerate([12.0, 2500.0, 15.5, 30.0, 8.0]):
        add(f"A.pm10_in_A.p.{k}", "A.pressure", 11, k, value, a, "pm10", ta)
    for k in range(3):
        add(f"A.pm25_in_A.p.{k}", "A.pressure", 11, 10 + k, 5.0 + k, a, "pm2_5", ta)
    for k in range(5):  # B's pressure in Pa in A's Air pressure datastream: a stale id of the previous server
        add(f"B.p_in_A.p.{k}", "A.pressure", 11, 20 + k, 100500 + k * 1.25, b, "pressure", tb)
    for k in range(4):  # ... and in A's humidity datastream; the first at a phenomenonTime B.pressure has already
        # (another reading: another value), the second at the time of a misrouted PM10 row in B.pressure below
        add(f"B.p_in_A.h.{k}", "A.humidity", 11 if k else 10, 30 + k if k else 0, 100700 + k * 2.5, b, "pressure", tb)
    add("B.p_dup_in_A.h", "A.humidity", 10, 1, 101000 + 1 * 3.3, b, "pressure", tb)  # B.p.1's very reading: stays
    for k in range(2):
        add(f"B.pm25_in_A.h.{k}", "A.humidity", 11, 40 + k, 9.0 + k, b, "pm2_5", tb)
    # B's PM10 in B's Air pressure datastream, at B.p_in_A.h.1's phenomenonTime: it moves out to B.pm10 after
    # (B.pressure's rows are scanned after A.humidity's), so it must not keep B.p_in_A.h.1 from moving in.
    add("B.pm10_in_B.p.0", "B.pressure", 11, 31, 13.5, b, "pm10", tb)
    for k in range(2):  # TGV readings (a bare stream key): no datastream on this server
        add(f"tgv_in_A.p.{k}", "A.pressure", 11, 50 + k, 100800.5 + k, "tgv-officelab-climate", "pressure",
            "Climate adaptation setup")
    for k in range(2):  # no labels
        add(f"nolabel_in_A.p.{k}", "A.pressure", 11, 60 + k, 101000 + k)
    for k in range(3):  # Sensor.Community readings in other sources' datastreams
        add(f"A.pm10_in_SM.{k}", "SM.pm10", 11, 70 + k, 11.0 + k, a, "pm10", ta)
    for k in range(2):
        add(f"A.p_in_K.{k}", "K.air_pressure", 11, 80 + k, 100900.25 + k, a, "pressure", ta)
    ds["A.pm10"] = datastream(things["A"], f"GEO_{ta}", a, "pm10")
    ds["A.pm2_5"] = datastream(things["A"], f"GEO_{ta}", a, "pm2_5")
    return Scenario(base, db, ds, obs)


def _wipe(db: str) -> None:
    _psql(db, f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE;")


@pytest.fixture()
def scenario(frost: tuple[str, str]) -> Iterator[Scenario]:
    """The scenario in an emptied database, emptied again afterwards (the module's first test seeds its own)."""
    base, db = frost
    _wipe(db)
    try:
        yield _seed_scenario(base, db)
    finally:
        _wipe(db)


def _rows(db: str) -> dict[int, tuple[str, ...]]:
    """(DATASTREAM_ID, FEATURE_ID, RESULT_NUMBER, RESULT_STRING) per observation, as PostgreSQL prints them."""
    lines = _psql(db, """SELECT "ID", "DATASTREAM_ID", coalesce("FEATURE_ID"::text, ''),
                                coalesce(to_json("RESULT_NUMBER")::text, 'null'), coalesce(to_json("RESULT_STRING")::text, 'null')
                           FROM "OBSERVATIONS" ORDER BY 1;""")
    return {int(i): tuple(rest.split("|", 3)) for i, rest in (line.split("|", 1) for line in lines)}


def _datastreams(db: str) -> dict[int, tuple[str, ...]]:
    """(phenomenon and result time extents, OBSERVED_AREA as text, LAST_FOI_ID) per datastream."""
    lines = _psql(db, """SELECT "ID", "PHENOMENON_TIME_START", "PHENOMENON_TIME_END", "RESULT_TIME_START", "RESULT_TIME_END",
                                coalesce(ST_AsText("OBSERVED_AREA"), ''), coalesce("LAST_FOI_ID"::text, '')
                           FROM "DATASTREAMS" ORDER BY 1;""")
    return {int(i): tuple(rest.split("|")) for i, rest in (line.split("|", 1) for line in lines)}


def _stale(db: str) -> list[str]:
    """Datastreams whose times are not their rows', or that have rows but no observed area."""
    return _psql(db, _STALE_SQL)


class _RowLock:
    """A concurrent writer holding a row lock, here past the scripts' 10 s lock timeout."""

    def __init__(self, db: str, obs_id: int) -> None:
        self.proc = subprocess.Popen(["docker", "exec", "-i", db, "sh", "-c", fix.CONTAINER_PSQL], stdin=subprocess.PIPE,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        assert self.proc.stdin is not None
        self.proc.stdin.write("SET application_name = 'scfix_test_lock';\nBEGIN;\n"
                              f'SELECT 1 FROM "OBSERVATIONS" WHERE "ID" = {obs_id} FOR UPDATE;\n')
        self.proc.stdin.flush()
        held = ("SELECT count(*) FROM pg_stat_activity "
                "WHERE application_name = 'scfix_test_lock' AND state = 'idle in transaction';")
        deadline = time.monotonic() + 60
        while _psql(db, held) != ["1"]:
            if time.monotonic() > deadline:
                raise TimeoutError("the row lock was not taken")
            time.sleep(0.5)

    def __enter__(self) -> _RowLock:
        return self

    def __exit__(self, *exc: object) -> None:
        if self.proc.poll() is None:
            assert self.proc.stdin is not None
            self.proc.stdin.write("COMMIT;\n")
            self.proc.stdin.close()
            self.proc.wait(timeout=60)


_EMPTY_BEFORE = ("A.pm10", "A.pm2_5", "B.pm2_5")  # targets without observations before the moves


@pytest.mark.integration
def test_interrupted_moves_leave_no_stale_datastream_and_roll_back_on_frost_28(
    scenario: Scenario, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A batch failing mid-run (a concurrent writer past the lock timeout) leaves no datastream with stale times;
    targets that had no observations get the observed area FROST would give them; the rollback is a dry run
    unless --apply, counts rows whose value changed since the move, can be limited to source datastreams, and
    puts every row back."""
    s = scenario
    seeded_rows, seeded_ds = _rows(s.db), _datastreams(s.db)
    ids_file = tmp_path / "moved.jsonl"
    script = ["--container", s.db]

    # Its group (A.humidity -> B.pressure) comes after A.pressure's; one row per batch, so the batch moving
    # B.p_in_A.h.2 (later than any B.pressure row, B.pm10_in_B.p.0 included) has committed when the next one fails.
    with _RowLock(s.db, s.obs["B.p_in_A.h.3"]):
        assert fix.main([*script, "--apply", "--ids-file", str(ids_file), "--batch-rows", "1"]) == 1
    err = capsys.readouterr().err
    assert "lock timeout" in err and "were refreshed" in err
    rows = _rows(s.db)
    assert rows[s.obs["B.p_in_A.h.0"]][0] == str(s.ds["B.pressure"])  # B.pressure has that time, not that reading
    assert rows[s.obs["B.p_in_A.h.1"]][0] == str(s.ds["B.pressure"])  # that time held by a row about to move out
    assert rows[s.obs["B.p_in_A.h.2"]][0] == str(s.ds["B.pressure"])
    assert rows[s.obs["B.pm10_in_B.p.0"]][0] == str(s.ds["B.pressure"])  # ... which has not moved yet
    assert _stale(s.db) == []

    assert fix.main([*script, "--apply", "--ids-file", str(ids_file)]) == 0
    out = capsys.readouterr().out
    assert "Moved 9 observations (1 left in place: their target already holds the same reading)" in out
    assert _stale(s.db) == []
    rows = _rows(s.db)
    assert rows[s.obs["B.pm10_in_B.p.0"]][:2] == (str(s.ds["B.pm10"]), seeded_rows[s.obs["B.pm10_in_B.p.0"]][1])
    assert rows[s.obs["B.p_dup_in_A.h"]] == seeded_rows[s.obs["B.p_dup_in_A.h"]]  # B.p.1's reading: left in place
    moved = {i for i, row in rows.items() if row[0] != seeded_rows[i][0]}
    assert len(moved) == 25  # all but the identical reading; not the TGV or unlabelled rows
    now = _datastreams(s.db)
    for empty, sibling in zip(_EMPTY_BEFORE, ("A.pressure", "A.pressure", "B.pressure"), strict=True):
        assert seeded_ds[s.ds[empty]][4:] == ("", "")  # no area, no last FoI
        assert now[s.ds[empty]][4:] == now[s.ds[sibling]][4:]  # what FROST's insert trigger gave its sibling

    k_rows = (s.obs["A.p_in_K.0"], s.obs["A.p_in_K.1"])  # moved into A's Air pressure; converted, as the backfill would
    _psql(s.db, f'UPDATE "OBSERVATIONS" SET "RESULT_NUMBER" = "RESULT_NUMBER" / 100 WHERE "ID" IN {k_rows};')
    converted = (_rows(s.db), _datastreams(s.db))
    assert fix.main([*script, "--rollback", "--ids-file", str(ids_file)]) == 0  # a dry run
    assert (_rows(s.db), _datastreams(s.db)) == converted
    out = capsys.readouterr().out
    assert re.search(r"still where the script put them: to move back\s+25\n", out)
    assert re.search(r"of those with another RESULT_NUMBER than when moved\s+2\n", out)

    k = s.ds["K.air_pressure"]
    assert fix.main([*script, "--rollback", "--apply", "--ids-file", str(ids_file), "--datastream", str(k)]) == 0
    rows = _rows(s.db)
    assert {i for i, row in rows.items() if row[0] != seeded_rows[i][0]} == moved - set(k_rows)
    assert all(rows[i][:2] == seeded_rows[i][:2] for i in k_rows)
    assert _stale(s.db) == []
    capsys.readouterr()

    assert fix.main([*script, "--rollback", "--apply", "--ids-file", str(ids_file)]) == 0
    assert "Verification: no recorded row is left where the script put it." in capsys.readouterr().out
    rows = _rows(s.db)
    assert {i: row[:2] for i, row in rows.items()} == {i: row[:2] for i, row in seeded_rows.items()}
    assert {i: r for i, r in rows.items() if i not in k_rows} == {i: r for i, r in seeded_rows.items() if i not in k_rows}
    assert _stale(s.db) == []
    after = _datastreams(s.db)
    emptied = {s.ds[label] for label in _EMPTY_BEFORE}
    assert {i: v[:4] for i, v in after.items()} == {i: v[:4] for i, v in seeded_ds.items()}  # times as seeded
    assert {i: v for i, v in after.items() if i not in emptied} == {i: v for i, v in seeded_ds.items() if i not in emptied}
    assert all(after[i][4] for i in emptied)  # an observed area stays, as FROST keeps it on a delete


@pytest.mark.integration
@pytest.mark.parametrize("case", ["pressure-rollback-first", "move-rollback-first", "interrupted-backfill"])
def test_with_the_pressure_backfill_either_undo_order_restores_the_seed_on_frost_28(
    scenario: Scenario, tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str
) -> None:
    """The review's combined sequence: the pressure backfill (b), the moves (d), the backfill again (e). Both
    rollbacks, in either order, give back every seeded row exactly; the pressure rollback alone restores every
    value wherever the row is. With b interrupted and not re-run before d, rows are recorded under two
    datastreams, and the pressure rollback still takes the file.

    Needs scripts/fix_sc_pressure_units.py (fix/sc-pressure-hpa): it runs once both branches are merged.
    """
    backfill = pytest.importorskip("scripts.fix_sc_pressure_units", reason="needs fix/sc-pressure-hpa merged in")
    s = scenario
    seeded_rows, seeded_ds = _rows(s.db), _datastreams(s.db)
    db = ["--container", s.db]
    pressure_ids, moved_ids = str(tmp_path / "sc_pressure_ids.csv"), str(tmp_path / "moved.jsonl")

    if case == "interrupted-backfill":
        with _RowLock(s.db, s.obs["B.p_in_A.p.0"]):  # A's Air pressure batch waits past the lock timeout
            assert backfill.main([*db, "--apply", "--ids-file", pressure_ids]) == 1
    else:
        assert backfill.main([*db, "--apply", "--ids-file", pressure_ids]) == 0  # b
    assert fix.main([*db, "--apply", "--ids-file", moved_ids]) == 0  # d
    assert backfill.main([*db, "--apply", "--ids-file", pressure_ids]) == 0  # e
    capsys.readouterr()
    assert backfill.main(db) == 0  # its dry run: no Sensor.Community pressure reading left in Pa
    assert re.search(r"pressure readings to convert \(Pa band\)\s+0\n", capsys.readouterr().out)

    if case == "move-rollback-first":
        assert fix.main([*db, "--rollback", "--apply", "--ids-file", moved_ids]) == 0
        # Moved in Pa by d, converted by e: B.p_in_A.h.0-3, A.p_in_K.0-1, and B.p_dup_in_A.h (no longer
        # B.p.1's reading once b converted B.p.1).
        assert re.search(r"of those with another RESULT_NUMBER than when moved\s+7\n", capsys.readouterr().out)
        assert backfill.main([*db, "--rollback", "--apply", "--ids-file", pressure_ids]) == 0
    else:
        assert backfill.main([*db, "--rollback", "--apply", "--ids-file", pressure_ids]) == 0
        values = {i: row[2:] for i, row in _rows(s.db).items()}
        assert values == {i: row[2:] for i, row in seeded_rows.items()}  # wherever the rows are now
        assert fix.main([*db, "--rollback", "--apply", "--ids-file", moved_ids]) == 0
    assert _rows(s.db) == seeded_rows
    assert _stale(s.db) == []
    after = _datastreams(s.db)
    emptied = {s.ds[label] for label in _EMPTY_BEFORE}
    assert {i: v for i, v in after.items() if i not in emptied} == {i: v for i, v in seeded_ds.items() if i not in emptied}


# ---------------------------------------------------------------------------
# --sensor-type-split (#40): non-owner sensor types out of Sensor.Community location datastreams
# ---------------------------------------------------------------------------

# Shaped like production's mixed datastreams (#37): 6591 BME280 + DHT22 (temperature and humidity), 55085
# SPS30 + SDS011 (PM), 95248 BME280 + SCD30 whose SCD30 humidity datastream doesn't exist yet, and 777 whose
# location never reported after the #39 deploy, so it has no owner.
_SPLIT_OWNERS = {"6591": {"temperature": "BME280", "humidity": "BME280"},
                 "55085": {"pm2_5": "SPS30", "pm10": "SPS30"},
                 "95248": {"temperature": "BME280", "humidity": "BME280"}}
_SPLIT_DS: dict[int, tuple[int, str, str]] = {  # id: (Thing, sensor_id, observed_property)
    2001: (10, "sensor_community-6591", "temperature"), 2002: (10, "sensor_community-6591", "humidity"),
    2003: (10, "sensor_community-6591-dht22", "temperature"), 2004: (10, "sensor_community-6591-dht22", "humidity"),
    2101: (20, "sensor_community-55085", "pm2_5"), 2102: (20, "sensor_community-55085", "pm10"),
    2103: (20, "sensor_community-55085-sds011", "pm2_5"), 2104: (20, "sensor_community-55085-sds011", "pm10"),
    2201: (30, "sensor_community-95248", "temperature"), 2202: (30, "sensor_community-95248", "humidity"),
    2203: (30, "sensor_community-95248-scd30", "temperature"),
    2301: (40, "sensor_community-777", "temperature"),
}
_SPLIT_MAX_ID = 100
_VALUES_OWNER = re.compile(r"\((\d+), ('(?:[^']|'')*'|NULL)::text, ('(?:[^']|'')*'|NULL)::text\)")


@dataclass
class _TObs:
    ds: int
    sid: str
    prop: str
    sname: str | None
    num: float = 20.0
    foi: int = 110


def _sname(loc: str, sensor_type: str) -> str:
    return f"SensorCommunity {loc} {sensor_type} sensor"


def _split_obs() -> dict[int, _TObs]:
    a, b, c, d = "sensor_community-6591", "sensor_community-55085", "sensor_community-95248", "sensor_community-777"
    return {
        1: _TObs(2001, a, "temperature", _sname("6591", "BME280")),
        2: _TObs(2001, a, "temperature", _sname("6591", "DHT22"), num=21.0),
        3: _TObs(2001, a, "temperature", _sname("6591", "BME280")),
        4: _TObs(2001, a, "temperature", _sname("6591", "DHT22"), num=22.0),
        5: _TObs(2002, a, "humidity", _sname("6591", "BME280")),
        6: _TObs(2002, a, "humidity", _sname("6591", "DHT22"), num=70.0),
        7: _TObs(2101, b, "pm2_5", _sname("55085", "SPS30"), foi=120),
        8: _TObs(2101, b, "pm2_5", _sname("55085", "SDS011"), num=5.0, foi=120),
        9: _TObs(2102, b, "pm10", _sname("55085", "SDS011"), num=9.0, foi=120),
        10: _TObs(2201, c, "temperature", _sname("95248", "SCD30"), foi=130),
        11: _TObs(2202, c, "humidity", _sname("95248", "SCD30"), foi=130),
        12: _TObs(2301, d, "temperature", _sname("777", "DHT22"), foi=140),
        13: _TObs(2001, a, "temperature", "airrohr 6591", foi=110),
        500: _TObs(2001, a, "temperature", _sname("6591", "DHT22")),  # after --max-id: written after #39
    }


def _unquote(literal: str) -> str | None:
    return None if literal == "NULL" else literal[1:-1].replace("''", "'")


class TypeSplitDb:
    """psql for --sensor-type-split, in memory: like FakeDb it answers by JSON tag and keeps where every row
    is and its PARAMETERS.sensor_id; the type-split scan is evaluated from the statement's VALUES list (owner
    per datastream), its ID bounds and the same sensor_name pattern. The real SQL runs on FROST in
    tests/integration/test_sc_type_split_it.py."""

    def __init__(self, obs: dict[int, _TObs] | None = None, bounds: tuple[int, int] = (1, 500)) -> None:
        self.obs = obs if obs is not None else _split_obs()
        self.bounds = bounds
        self.sessions: list[str] = []
        self.scanned: list[tuple[str, int, int]] = []  # ("range", lo, hi) or ("ds", id, max id)
        self.refreshed: list[list[int]] = []

    def __call__(self, sql: str) -> list[str]:
        self.sessions.append(sql)
        if 'UPDATE "DATASTREAMS"' in sql:
            self.refreshed.append([int(i) for i in re.findall(r'WHERE d\."ID" = (\d+);', sql)])
            return []
        if "'t', 'busy'" in sql:
            return []
        if "'t', 'ds'" in sql:
            return self._catalogue()
        if "'t', 'tgrp'" in sql:
            return [line for statement in sql.split(";\n") if "'t', 'tgrp'" in statement
                    for line in self._groups(statement)]
        if "'t', 'row'" in sql:
            return self._batch(sql)
        if "moved AS" in sql or "restored AS" in sql:
            return self._write(sql)
        return []  # duplicates: none

    def _catalogue(self) -> list[str]:
        things = {10: "6591", 20: "55085", 30: "95248", 40: "777"}
        lines = [
            _line(t="ds", id=ds, name=f"GEO_SensorCommunity {sid.split('-', 1)[1]} sensor - {prop}", thing_id=thing,
                  thing=f"GEO_SensorCommunity {things[thing]}", source="sensor_community", sensor_id=sid, prop=prop)
            for ds, (thing, sid, prop) in _SPLIT_DS.items()
        ]
        fois = [_line(t="foi", thing_id=t, locations=1, foi=100 + t, fois=1) for t in things]
        return [*lines, *fois, _line(t="bounds", min=self.bounds[0], max=self.bounds[1])]

    def _groups(self, statement: str) -> list[str]:
        owners = {int(ds): (_unquote(sid), _unquote(owner)) for ds, sid, owner in _VALUES_OWNER.findall(statement)}
        if m := re.search(r'o\."ID" BETWEEN (\d+) AND (\d+)', statement):
            self.scanned.append(("range", int(m[1]), int(m[2])))
            selected = {i for i in self.obs if int(m[1]) <= i <= int(m[2])}
        else:
            m = re.search(r'o\."DATASTREAM_ID" = (\d+) AND o\."ID" <= (\d+)', statement)
            assert m is not None
            self.scanned.append(("ds", int(m[1]), int(m[2])))
            selected = {i for i, o in self.obs.items() if o.ds == int(m[1]) and i <= int(m[2])}
        groups: dict[tuple[int, str, str, str | None], list[int]] = defaultdict(list)
        for i in sorted(selected):
            o = self.obs[i]
            if o.ds in owners and o.sid == owners[o.ds][0] and fix.parse_sensor_type(o.sname) != owners[o.ds][1]:
                groups[(o.ds, o.sid, o.prop, o.sname)].append(i)
        return [
            _line(t="tgrp", ds=ds, sid=sid, prop=prop, thing=f"SensorCommunity {sid.split('-')[1]}",
                  loc=sid.split("-")[1], sname=sname, n=len(ids), min_id=min(ids), max_id=max(ids),
                  first="2026-11-12T14:12:23+00:00", last="2026-11-20T14:17:23+00:00")
            for (ds, sid, prop, sname), ids in groups.items()
        ]

    def _batch(self, sql: str) -> list[str]:
        where = re.search(r'WHERE o\."DATASTREAM_ID" = (\d+) AND o\."ID" > (\d+) AND o\."ID" <= (\d+)', sql)
        sname = re.search(r"'sensor_name' = ('(?:[^']|'')*')", sql)
        limit = re.search(r"LIMIT (\d+);", sql)
        assert where is not None and sname is not None and limit is not None
        ds, after, last = int(where[1]), int(where[2]), int(where[3])
        rows = [(i, o) for i, o in sorted(self.obs.items())
                if o.ds == ds and after < i <= last and o.sname == _unquote(sname[1])]
        return [_line(t="row", id=i, foi=o.foi, num=o.num, dup=False) for i, o in rows[: int(limit[1])]]

    def _write(self, sql: str) -> list[str]:
        new = re.search(r'SET "DATASTREAM_ID" = (\d+)', sql)
        now = re.search(r'WHERE o\."ID" = v\.id AND o\."DATASTREAM_ID" = (\d+)', sql)
        label = re.search(r"to_jsonb\(('(?:[^']|'')*')::text\)", sql)
        assert new is not None and now is not None
        count = 0
        for i in (int(i) for i in _VALUES_ID.findall(sql)):
            if i in self.obs and self.obs[i].ds == int(now[1]):
                self.obs[i].ds = int(new[1])
                if label is not None:
                    self.obs[i].sid = str(_unquote(label[1]))
                count += 1
        return [_line(t="moved" if "moved AS" in sql else "restored", n=count)]


def _owners_file(path: Path, owners: dict[str, dict[str, str]] | None = None) -> Path:
    path.write_text(json.dumps({"version": 1, "owners": _SPLIT_OWNERS if owners is None else owners}), encoding="utf-8")
    return path


def _split_args(tmp_path: Path, *extra: str, max_id: int = _SPLIT_MAX_ID) -> argparse.Namespace:
    owners = _owners_file(tmp_path / "sc_sensor_owners.json")
    return fix.parse_args(["--container", "db", "--sensor-type-split", "--owners", str(owners),
                           "--max-id", str(max_id), *extra])


def _split_catalogue() -> fix.Catalogue:
    catalogue, _ = fix.parse_catalogue(TypeSplitDb()._catalogue(), "GEO_")
    return catalogue


def _tgroup(ds: int, loc: str, sensor_type: str | None, sname: str | None = None, prop: str | None = None) -> fix.Group:
    if sname is None and sensor_type is not None:
        sname = _sname(loc, sensor_type)
    return fix.Group(ds=ds, sid=f"sensor_community-{loc}", prop=prop or _SPLIT_DS[ds][2],
                     thing=f"SensorCommunity {loc}", loc=loc, rows=10, min_id=1, max_id=99, sensor_name=sname)


def _split_plan(*groups: fix.Group) -> dict[Any, fix.PlannedGroup]:
    return {p.group.key: p for p in fix.build_type_split_plan(groups, _split_catalogue(), _SPLIT_OWNERS).groups}


def test_type_split_moves_a_non_owner_type_to_its_own_datastream_with_its_label() -> None:
    """protects=selection + target resolution: a non-owner type's rows in a location datastream go to the
    datastream of type_sensor_id(loc, type) and the same property, relabelled with its sensor_id, keeping their
    FoI (same Thing); fails_when=the target is looked up by another slug/property, the label is not changed, or
    the owner's rows are planned; no_claim=that the SQL selects these rows (integration test)."""
    dht22 = _tgroup(2001, "6591", "DHT22")
    sds011_pm10 = _tgroup(2102, "55085", "SDS011")
    owner = _tgroup(2001, "6591", "BME280")

    plan = _split_plan(dht22, sds011_pm10, owner)

    moved = plan[dht22.key]
    assert (moved.action, moved.target and moved.target.id, moved.foi) == (fix.MOVE, 2003, None)
    assert moved.relabel == "sensor_community-6591-dht22" == fix.type_sensor_id("6591", "DHT22")
    pm10 = plan[sds011_pm10.key]
    assert (pm10.target and pm10.target.id, pm10.relabel) == (2104, "sensor_community-55085-sds011")
    assert owner.key not in plan


def test_type_split_target_uses_the_connectors_slug() -> None:
    """protects=the target sensor_id is the connector's own (app.sources.sensor_community.type_sensor_id), also
    for types with odd characters; fails_when=the script builds its own slug ('DNMS (Laerm)' -> 'dnms (laerm)');
    no_claim=that every SC type name is in OWNER_RANK."""
    catalogue = fix.Catalogue(
        {1: fix.Datastream(1, "n", 10, "GEO_SensorCommunity 9", "sensor_community", "sensor_community-9", "noise_laeq"),
         2: fix.Datastream(2, "n", 10, "GEO_SensorCommunity 9", "sensor_community", "sensor_community-9-dnms-laerm",
                           "noise_laeq")},
        {10: fix.ThingFoi(1, 110)}, "GEO_")
    group = fix.Group(1, "sensor_community-9", "noise_laeq", "SensorCommunity 9", sensor_name=_sname("9", "DNMS (Laerm)"))

    (planned,) = fix.build_type_split_plan([group], catalogue, {"9": {"noise_laeq": "SEN5x"}}).groups

    assert (planned.action, planned.target and planned.target.id) == (fix.MOVE, 2)
    assert planned.relabel == "sensor_community-9-dnms-laerm"


def test_type_split_reports_a_missing_type_datastream_as_needs_datastream() -> None:
    """protects=needs-datastream: a non-owner type without its datastream yet is reported, not moved;
    fails_when=it is moved elsewhere (e.g. another property's type datastream) or dropped from the report;
    no_claim=that the connector creates the datastream later."""
    scd30_humidity = _tgroup(2202, "95248", "SCD30")

    planned = _split_plan(scd30_humidity)[scd30_humidity.key]

    assert (planned.action, planned.target, planned.relabel) == (fix.NEEDS_DATASTREAM, None, None)
    assert "sensor_community-95248-scd30 / humidity" in planned.note


def test_type_split_never_moves_unparsable_sensor_names_or_locations_without_an_owner() -> None:
    """protects=rows whose type can't be read, or whose (loc, property) has no owner, or that carry another
    property's label, are reported and never moved; fails_when=such a group gets a target (e.g. treated as
    type '' or as 'not the owner'); no_claim=what should happen to them later."""
    garbage = _tgroup(2001, "6591", None, sname="airrohr 6591")
    no_name = _tgroup(2001, "6591", None)
    trailing = _tgroup(2001, "6591", None, sname="SensorCommunity 6591 DHT22 sensor (old)")
    no_owner = _tgroup(2301, "777", "DHT22")
    other_label = _tgroup(2001, "6591", "DHT22", prop="pm10")

    plan = _split_plan(garbage, no_name, trailing, no_owner, other_label)

    assert {g.sensor_name: plan[g.key].action for g in (garbage, no_name, trailing)} == {
        "airrohr 6591": fix.UNPARSABLE, None: fix.UNPARSABLE, "SensorCommunity 6591 DHT22 sensor (old)": fix.UNPARSABLE}
    assert plan[no_owner.key].action == fix.OWNER_MISSING
    assert plan[other_label.key].action == fix.OTHER_LABEL
    assert all(p.target is None and p.relabel is None for p in plan.values())
    assert fix.parse_sensor_type(_sname("6591", "DNMS (Laerm)")) == "DNMS (Laerm)"


def test_type_split_scan_never_reads_past_max_id(tmp_path) -> None:
    """protects=the --max-id cut: every scan statement (by ID range, or per datastream with --datastream) stops
    at --max-id, so rows written after the #39 deploy are never selected; fails_when=a range or datastream scan
    reaches past it; no_claim=that --max-id is the right watermark (the operator reads it from production)."""
    db = TypeSplitDb(bounds=(1, 1000))
    assert fix.run(_split_args(tmp_path, "--range-ids", "40"), db) == 0
    assert db.scanned == [("range", 1, 40), ("range", 41, 80), ("range", 81, 100)]

    db = TypeSplitDb()
    assert fix.run(_split_args(tmp_path, "--datastream", "2001"), db) == 0
    assert db.scanned == [("ds", 2001, _SPLIT_MAX_ID)]

    db = TypeSplitDb(bounds=(150, 500))
    assert fix.run(_split_args(tmp_path), db) == 0
    assert db.scanned == []  # every row is newer than --max-id
    sql = fix.type_split_range_sql(1, 500, 100, [(2001, "sensor_community-6591", "BME280")])
    assert 'o."ID" BETWEEN 1 AND 100' in sql


def test_type_split_scan_sql_selects_location_rows_whose_type_is_not_the_owner() -> None:
    """protects=the selection SQL: only rows labelled with their location datastream's sensor_id, type parsed
    by the issue's pattern, compared NULL-safely with that datastream's owner (all rows when it has none);
    fails_when=the label check, pattern or owner comparison changes; no_claim=PostgreSQL's evaluation of it
    (integration test)."""
    sql = fix.type_split_range_sql(1, 50, 100, [(2001, "sensor_community-6591", "BME280"),
                                                (2301, "sensor_community-777", None)])
    assert "(2001, 'sensor_community-6591'::text, 'BME280'::text), (2301, 'sensor_community-777'::text, NULL::text)" in sql
    assert """o."PARAMETERS"->>'sensor_id' = v.sid""" in sql
    assert (r"""substring(o."PARAMETERS"->>'sensor_name' from '^SensorCommunity \S+ (.*) sensor$')"""
            """ IS DISTINCT FROM v.owner""") in sql
    assert fix.SENSOR_NAME_PATTERN == r"^SensorCommunity \S+ (.*) sensor$"


def test_type_split_only_scans_location_datastreams() -> None:
    """protects=only Sensor.Community location datastreams (sensor_id 'sensor_community-{loc}') are scanned,
    each with its owner of (loc, property); fails_when=type datastreams or other sources are scanned (their rows
    would be 'not the owner' and moved again); no_claim=none."""
    catalogue = _split_catalogue()
    other = fix.Datastream(9, "x", 50, "GEO_SamenMeten X", "samen_meten", "sensor_community-1", "pm10")
    catalogue.datastreams[9] = other

    assert fix.location_datastreams(catalogue, _SPLIT_OWNERS) == [
        (2001, "sensor_community-6591", "BME280"), (2002, "sensor_community-6591", "BME280"),
        (2101, "sensor_community-55085", "SPS30"), (2102, "sensor_community-55085", "SPS30"),
        (2201, "sensor_community-95248", "BME280"), (2202, "sensor_community-95248", "BME280"),
        (2301, "sensor_community-777", None),
    ]
    assert fix.location_datastreams(catalogue, _SPLIT_OWNERS, [2202]) == [(2202, "sensor_community-95248", "BME280")]


def test_type_split_dry_run_reports_per_datastream_and_totals_by_type(tmp_path, capsys) -> None:
    """protects=acceptance 1: the dry run is read-only and lists per source datastream the rows to move with
    their target, needs-datastream and other unmovable rows, and totals by sensor type (also as CSV);
    fails_when=it writes, or a group or total is missing or wrong; no_claim=layout details beyond these."""
    db = TypeSplitDb()
    plan_file = tmp_path / "split.csv"

    assert fix.run(_split_args(tmp_path, "--plan-file", str(plan_file)), db) == 0

    assert all(sql.startswith(fix.READ_ONLY_SESSION) and "UPDATE" not in sql.upper() for sql in db.sessions)
    out = capsys.readouterr().out
    assert re.search(r"DHT22\s+2\s+move\s+-> 2003 ", out)
    assert re.search(r"SCD30\s+1\s+needs-datastream\s+no datastream sensor_community-95248-scd30 / humidity", out)
    dht22 = re.search(r"\n  DHT22" + r"\s+([\d,]+)" * 7 + r"\n", out)
    assert dht22 is not None and dht22.groups() == ("3", "0", "0", "0", "1", "0", "0")  # 777's DHT22: no owner
    totals = re.search(r"\n  total" + r"\s+([\d,]+)" * 7 + r"\n", out)
    assert totals is not None and totals.groups() == ("6", "0", "1", "0", "1", "1", "0")
    with plan_file.open(encoding="utf-8", newline="") as src:
        rows = list(csv.DictReader(src))
    moves = {(r["source_datastream"], r["sensor_type"]): (r["target_datastream"], r["target_sensor_id"], r["rows"])
             for r in rows if r["action"] == fix.MOVE}
    assert moves == {
        ("2001", "DHT22"): ("2003", "sensor_community-6591-dht22", "2"),
        ("2002", "DHT22"): ("2004", "sensor_community-6591-dht22", "1"),
        ("2101", "SDS011"): ("2103", "sensor_community-55085-sds011", "1"),
        ("2102", "SDS011"): ("2104", "sensor_community-55085-sds011", "1"),
        ("2201", "SCD30"): ("2203", "sensor_community-95248-scd30", "1"),
    }
    assert {(r["action"], r["source_datastream"]) for r in rows if r["action"] != fix.MOVE} == {
        (fix.NEEDS_DATASTREAM, "2202"), (fix.OWNER_MISSING, "2301"), (fix.UNPARSABLE, "2001")}
    assert "Dry run: nothing was written." in out


def test_type_split_apply_moves_the_planned_rows_and_records_label_and_datastream(tmp_path, capsys) -> None:
    """protects=acceptance 2+3 at unit level: --apply moves exactly the planned rows (not rows after --max-id,
    not needs-datastream/owner-missing/unparsable rows), sets PARAMETERS.sensor_id to the target's, records
    each batch with its old datastream and old sensor_id before writing, refreshes source and target, and
    verifies 0 movable rows left; fails_when=any of these changes; no_claim=the SQL's effect on FROST
    (integration test)."""
    ids_file = tmp_path / "split.jsonl"
    db = TypeSplitDb()

    assert fix.run(_split_args(tmp_path, "--apply", "--ids-file", str(ids_file)), db) == 0

    where = {i: (o.ds, o.sid) for i, o in db.obs.items()}
    assert where[2] == where[4] == (2003, "sensor_community-6591-dht22")
    assert where[6] == (2004, "sensor_community-6591-dht22")
    assert (where[8], where[9]) == ((2103, "sensor_community-55085-sds011"), (2104, "sensor_community-55085-sds011"))
    assert where[10] == (2203, "sensor_community-95248-scd30")
    unmoved = {1: 2001, 3: 2001, 5: 2002, 7: 2101, 11: 2202, 12: 2301, 13: 2001, 500: 2001}
    assert {i: where[i][0] for i in unmoved} == unmoved
    assert sorted(db.refreshed) == [[2001, 2003], [2002, 2004], [2101, 2103], [2102, 2104], [2201, 2203]]

    records = [json.loads(line) for line in ids_file.read_text(encoding="utf-8").splitlines()]
    assert records[0] == fix.IDS_HEADER
    assert records[1]["op"] == "run" and records[1]["mode"] == "sensor-type-split" and records[1]["max_id"] == 100
    moves = [r for r in records if r["op"] == "move"]
    assert sorted((r["from_ds"], r["to_ds"], r["from_sensor_id"], r["to_sensor_id"], [row[0] for row in r["rows"]])
                  for r in moves) == [
        (2001, 2003, "sensor_community-6591", "sensor_community-6591-dht22", [2, 4]),
        (2002, 2004, "sensor_community-6591", "sensor_community-6591-dht22", [6]),
        (2101, 2103, "sensor_community-55085", "sensor_community-55085-sds011", [8]),
        (2102, 2104, "sensor_community-55085", "sensor_community-55085-sds011", [9]),
        (2201, 2203, "sensor_community-95248", "sensor_community-95248-scd30", [10]),
    ]
    assert next(r for r in moves if r["from_ds"] == 2001)["rows"] == [[2, 110, 21.0], [4, 110, 22.0]]
    first_move = next(sql for sql in db.sessions if "moved AS" in sql and "= 2003," in sql)
    assert ('"PARAMETERS" = jsonb_set(o."PARAMETERS", \'{sensor_id}\', '
            "to_jsonb('sensor_community-6591-dht22'::text))") in first_move
    assert 'WHERE o."ID" = v.id AND o."DATASTREAM_ID" = 2001' in first_move
    out = capsys.readouterr().out
    assert "Moved 6 observations (0 left in place" in out
    assert re.search(r"Verification: .* to move 0, kept as duplicates 0; .*needs-datastream 1", out)


def test_type_split_apply_exits_1_when_movable_rows_are_left(tmp_path, capsys) -> None:
    """protects=acceptance 3's check: when the re-scan after --apply still finds rows to move (here a fake
    whose UPDATE moves nothing), the run says so and exits 1; fails_when=the verification is skipped or always
    passes; no_claim=why rows were left (rerunning is safe)."""
    class StuckDb(TypeSplitDb):
        def _write(self, sql: str) -> list[str]:
            return [_line(t="moved", n=0)]

    assert fix.run(_split_args(tmp_path, "--apply", "--ids-file", str(tmp_path / "split.jsonl")), StuckDb()) == 1
    assert re.search(r"Verification: .* to move 6, kept as duplicates 0", capsys.readouterr().out)


def test_type_split_duplicates_compare_with_the_label_the_row_gets() -> None:
    """protects=the duplicate check of a type-split move compares the target's rows with the row's label
    after the move (the type sensor_id), not its location label, which no target row carries; fails_when=it
    compares o's own sensor_id again; no_claim=how many duplicates production has."""
    planned = fix.PlannedGroup(_tgroup(2001, "6591", "DHT22"), fix.MOVE, _split_catalogue().datastreams[2003],
                               relabel="sensor_community-6591-dht22")

    for sql in (fix.batch_select_sql(planned, 0, 10), fix.duplicates_sql(planned)):
        assert """x."PARAMETERS"->>'sensor_id' IS NOT DISTINCT FROM 'sensor_community-6591-dht22'""" in sql
        assert """x."PARAMETERS"->>'observed_property' IS NOT DISTINCT FROM o."PARAMETERS"->>'observed_property'""" in sql
        assert """o."PARAMETERS"->>'sensor_name' = 'SensorCommunity 6591 DHT22 sensor'""" in sql
    assert """IS NOT DISTINCT FROM o."PARAMETERS"->>'sensor_id'""" in fix.same_reading_sql(2003)


def test_rollback_restores_the_sensor_id_of_type_split_moves_only(tmp_path) -> None:
    """protects=--rollback of a type-split batch restores DATASTREAM_ID, FEATURE_ID and the recorded old
    PARAMETERS.sensor_id, while a default-mode batch's restore leaves PARAMETERS alone; fails_when=the old
    sensor_id is not restored, or a misrouted batch gets its label rewritten; no_claim=rows moved again since."""
    ids_file = _ids_file(
        tmp_path / "split.jsonl",
        _move_record("b1", 1386, 1388, [[31, 110, 1.0]]),
        _move_record("b2", 2001, 2003, [[2, 110, 21.0], [4, 110, 22.0]])
        | {"from_sensor_id": "sensor_community-6591", "to_sensor_id": "sensor_community-6591-dht22"},
    )
    obs = {2: _TObs(2003, "sensor_community-6591-dht22", "temperature", _sname("6591", "DHT22")),
           4: _TObs(2003, "sensor_community-6591-dht22", "temperature", _sname("6591", "DHT22")),
           31: _TObs(1388, "sensor_community-93182", "pm10", None)}
    db = TypeSplitDb(obs)

    restored, touched = fix.rollback(db, fix.IdsFile(ids_file), chunk=10)

    assert (restored, touched) == (3, {1386, 1388, 2001, 2003})
    split_restore, misrouted_restore = [sql for sql in db.sessions if "restored AS" in sql]  # newest batch first
    assert ('SET "DATASTREAM_ID" = 2001, "FEATURE_ID" = v.foi, "PARAMETERS" = jsonb_set(o."PARAMETERS", '
            "'{sensor_id}', to_jsonb('sensor_community-6591'::text))") in split_restore
    assert 'SET "DATASTREAM_ID" = 1386, "FEATURE_ID" = v.foi\n' in misrouted_restore
    assert "jsonb_set" not in misrouted_restore
    assert {i: (o.ds, o.sid) for i, o in db.obs.items()} == {
        2: (2001, "sensor_community-6591"), 4: (2001, "sensor_community-6591"), 31: (1386, "sensor_community-93182")}
    assert "to_jsonb('it''s'::text)" in fix.restore_sql(2001, 2003, [(2, None)], "it's")


def test_type_split_needs_its_inputs_and_a_valid_owners_file(tmp_path) -> None:
    """protects=--sensor-type-split refuses to run without --owners and --max-id, with them in another mode,
    or with an owners file that is not the connector's version 1 map; fails_when=it falls back to an empty map
    (as the connector does) and plans every type as owner-missing, or accepts a broken file; no_claim=the file
    matches the connector's current owners."""
    owners = _owners_file(tmp_path / "owners.json")
    for argv in (["--sensor-type-split"], ["--sensor-type-split", "--owners", str(owners)],
                 ["--sensor-type-split", "--max-id", "5"], ["--owners", str(owners), "--max-id", "5"],
                 ["--sensor-type-split", "--owners", str(tmp_path / "none.json"), "--max-id", "5"],
                 ["--sensor-type-split", "--owners", str(owners), "--max-id", "0"]):
        with pytest.raises(SystemExit):
            fix.parse_args(["--container", "db", *argv])
    ids_file = _ids_file(tmp_path / "split.jsonl")
    with pytest.raises(SystemExit):
        fix.parse_args(["--container", "db", "--rollback", "--ids-file", str(ids_file), "--sensor-type-split",
                        "--owners", str(owners), "--max-id", "5"])

    assert fix.load_owners_file(owners)["6591"] == {"temperature": "BME280", "humidity": "BME280"}
    alias = _owners_file(tmp_path / "alias.json", {"1": {"pm25": "SDS011"}})
    assert fix.load_owners_file(alias) == {"1": {"pm2_5": "SDS011"}}
    for bad in ('{"version": 2, "owners": {}}', '{"owners": {}}', "[]", "{", '{"version": 1, "owners": {"1": "x"}}',
                '{"version": 1, "owners": {"1": {"pm10": 3}}}'):
        (tmp_path / "bad.json").write_text(bad, encoding="utf-8")
        with pytest.raises(fix.OwnersFileError):
            fix.load_owners_file(tmp_path / "bad.json")
    (tmp_path / "bad.json").write_text("{", encoding="utf-8")
    assert fix.main(["--container", "db", "--sensor-type-split", "--owners", str(tmp_path / "bad.json"),
                     "--max-id", "5"]) == 1
