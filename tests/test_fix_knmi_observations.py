"""Unit tests for scripts/fix_knmi_observations.py: the plan, the IDs and twins files, the apply and rollback
flows against an in-memory fake of the database, and the retirement against a fake FROST API.

The SQL itself runs against FROST 2.8 on PostgreSQL 16 in tests/integration/test_fix_knmi_observations.py.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app import config
from app.frost.odata_fields import odata_fields_for
from app.frost.target import FrostTarget
from scripts import fix_knmi_observations as fix
from scripts import fix_sc_misrouted_observations as misrouted
from scripts import fix_sc_pressure_units as pressure

PREFIX = "GEO_"
A, B = "GEO_KNMI KNMI-52.36-3.34", "GEO_KNMI KNMI-53.49-5.94"


def _ds(ds_id: int, thing_id: int, thing: str, prop: str, op_name: str, suffix: str, source: str = fix.SOURCE
        ) -> fix.Datastream:
    return fix.Datastream(ds_id, f"{thing} sensor - {suffix}", thing_id, thing, source, prop, op_name)


DATASTREAMS = [
    _ds(1, 10, A, "temperature", "Air temperature", "Air temperature"),
    _ds(2, 10, A, "ground_temperature", "Ground temperature", "Ground temperature"),
    _ds(3, 10, A, "ground_temperature", "Ground surface temperature", "Ground surface temperature"),
    _ds(4, 20, B, "temperature", "Air temperature", "Air temperature"),
    _ds(5, 20, B, "ground_temperature", "Ground temperature", "Ground temperature"),
    _ds(6, 20, B, "ground_temperature", "Ground surface temperature", "Ground surface temperature"),
]
FOIS = {10: 100, 20: 200}


def _catalogue(datastreams: list[fix.Datastream] | None = None, fois: dict[int, int | None] | None = None
               ) -> fix.Catalogue:
    catalogue = fix.Catalogue(PREFIX, {d.id: d for d in datastreams or DATASTREAMS},
                              dict(FOIS if fois is None else fois))
    fix.resolve_targets(catalogue)
    return catalogue


def _group(ds: int, thing: str | None, prop: str | None, rows: int = 1) -> fix.Group:
    return fix.Group(ds, thing and thing.removeprefix(PREFIX), prop, None if thing else "tgv", rows, "t0", "t1")


# ---------------------------------------------------------------------------
# Sessions and SQL
# ---------------------------------------------------------------------------


def test_sessions_set_the_timeouts_and_names() -> None:
    for session in (fix.READ_ONLY_SESSION, fix.WRITE_SESSION):
        assert "SET statement_timeout = '50s';" in session
        assert "SET lock_timeout = '10s';" in session
        assert "SET idle_in_transaction_session_timeout = '60s';" in session
    assert fix.READ_ONLY_SESSION.startswith("SET default_transaction_read_only = on;")
    assert "application_name = 'fix_knmi_observations (read-only)'" in fix.READ_ONLY_SESSION
    assert "application_name = 'fix_knmi_observations';" in fix.WRITE_SESSION
    assert "pg_try_advisory_lock(hashtext('fix_knmi_observations'))" in fix.WRITE_SESSION
    assert "read_only" not in fix.WRITE_SESSION


def test_writers_pin_the_other_repair_scripts() -> None:
    """The names of the write sessions to wait for; the cleanup script's is copied from its branch."""
    assert fix.WRITERS == ("fix_knmi_observations", "fix_sc_pressure_units", "fix_sc_misrouted_observations",
                           "cleanup_frost_observations")
    assert pressure.WRITE_APP_NAME == "fix_sc_pressure_units"
    assert "application_name = 'fix_sc_misrouted_observations'" in misrouted.WRITE_SESSION
    sql = fix.busy_sql()
    for name in fix.WRITERS:
        assert f"'{name}'" in sql
    assert "(read-only)" not in sql


def test_reused_helpers_keep_their_signatures() -> None:
    assert "PHENOMENON_TIME_START" in misrouted.refresh_sql(7) and '"ID" = 7' in misrouted.refresh_sql(7)
    argv = pressure.psql_argv(ssh="root@host", ssh_key="key")
    assert argv[:7] == ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    assert pressure.psql_argv(container="db")[:4] == ["docker", "exec", "-i", "db"]


def test_like_prefix_escapes_wildcards() -> None:
    assert fix.like_prefix("GEO_KNMI ") == r"GEO\_KNMI "
    assert fix.like_prefix("a%b\\") == "a\\%b\\\\"
    assert r"LIKE 'GEO\_KNMI %'" in fix.catalogue_sql("GEO_")


def test_move_sql_guards_each_row() -> None:
    sql = fix.move_sql(4, [fix.Move(11, 4, 1, 200, 100)])
    assert '"DATASTREAM_ID" = v.to_ds, "FEATURE_ID" = v.to_foi' in sql
    assert 'o."DATASTREAM_ID" = 4 AND o."FEATURE_ID" = v.from_foi' in sql
    assert "AND NOT EXISTS" in sql and 'x."DATASTREAM_ID" = v.to_ds' in sql
    for column in ("RESULT_TYPE", "RESULT_NUMBER", "RESULT_STRING", "RESULT_BOOLEAN", "RESULT_JSON"):
        assert f'x."{column}" IS NOT DISTINCT FROM o."{column}"' in sql
    assert "x.\"PARAMETERS\"->>'thing_name' = o.\"PARAMETERS\"->>'thing_name'" in sql
    restore = fix.restore_sql([fix.Move(11, 4, 1, 200, 100)])
    assert 'SET "DATASTREAM_ID" = v.from_ds, "FEATURE_ID" = v.from_foi' in restore
    assert 'o."DATASTREAM_ID" = v.to_ds AND o."FEATURE_ID" = v.to_foi' in restore


def test_area_sql_recomputes_area_and_last_foi() -> None:
    sql = fix.area_sql(9)
    assert '"OBSERVED_AREA" = (SELECT ST_ConvexHull(ST_Collect(f."GEOM"))' in sql
    assert '"LAST_FOI_ID" = (SELECT "FEATURE_ID" FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" = 9' in sql
    assert 'ORDER BY "ID" DESC LIMIT 1' in sql


# ---------------------------------------------------------------------------
# Targets and plan
# ---------------------------------------------------------------------------


def test_the_current_ground_temperature_datastream_wins() -> None:
    catalogue = _catalogue()
    assert catalogue.targets[(A, "ground_temperature")].datastream == DATASTREAMS[2]
    assert catalogue.targets[(B, "ground_temperature")].datastream == DATASTREAMS[5]
    assert catalogue.stale == {2, 5}
    assert catalogue.targets[(A, "temperature")].datastream == DATASTREAMS[0]


def test_two_datastreams_without_a_canonical_name_are_ambiguous() -> None:
    catalogue = _catalogue([
        _ds(1, 10, A, "temperature", "Temperature", "Temperature"),
        _ds(2, 10, A, "temperature", "Temperature (old)", "Temperature (old)"),
    ])
    target = catalogue.targets[(A, "temperature")]
    assert target.datastream is None and "1,2" in target.reason
    assert catalogue.stale == set()


def test_things_of_another_source_are_neither_scanned_nor_targets() -> None:
    catalogue = _catalogue([*DATASTREAMS, _ds(9, 30, "GEO_KNMI KNMI-1.00-1.00", "temperature", "Air temperature",
                                              "Air temperature", source="other")])
    assert 9 not in catalogue.knmi and "GEO_KNMI KNMI-1.00-1.00" not in catalogue.things


@pytest.mark.parametrize(
    ("group", "category", "target", "foi", "reason"),
    [
        (_group(1, A, "temperature"), fix.IN_PLACE, 1, None, ""),
        (_group(4, A, "temperature"), fix.MOVE, 1, 100, ""),  # another station's datastream: A's FoI
        (_group(2, A, "ground_temperature"), fix.MOVE, 3, None, ""),  # own stale one: FoI kept
        (_group(2, B, "ground_temperature"), fix.MOVE, 6, 200, ""),  # another station's stale one
        (_group(1, A, "ground_temperature"), fix.MOVE, 3, None, ""),  # wrong property, same Thing
        (_group(1, A, "groundtemperature"), fix.MOVE, 3, None, ""),  # an alias resolves
        (_group(1, "GEO_KNMI KNMI-0.00-0.00", "temperature"), fix.NO_TARGET, None, None, "no KNMI Thing"),
        (_group(1, A, "humidity"), fix.NO_TARGET, None, None, "has no datastream of humidity"),
        (_group(1, A, None), fix.NO_TARGET, None, None, "no observed_property label"),
    ],
)
def test_plan_group(group: fix.Group, category: str, target: int | None, foi: int | None, reason: str) -> None:
    planned = fix.plan_group(group, _catalogue())
    assert planned.category == category
    assert (planned.target.id if planned.target else None) == target
    assert planned.foi == foi
    assert reason in planned.reason


def test_a_target_thing_without_a_generated_foi_has_no_target() -> None:
    planned = fix.plan_group(_group(4, A, "temperature"), _catalogue(fois={10: None, 20: 200}))
    assert planned.category == fix.NO_TARGET and "generated FoI" in planned.reason


def test_build_plan_limits_to_source_or_target() -> None:
    groups = [_group(4, A, "temperature"), _group(1, B, "temperature"), _group(2, A, "ground_temperature"),
              _group(1, None, None, rows=5)]
    plan = fix.build_plan(groups, _catalogue(), {1})
    assert [(p.group.ds, p.target.id if p.target else None) for p in plan.groups] == [(4, 1), (1, 4)]
    assert [g.ds for g in plan.others] == [1]
    assert len(fix.build_plan(groups, _catalogue()).groups) == 3


def test_twins_are_not_moves_and_stale_forecast() -> None:
    catalogue = _catalogue()
    plan = fix.build_plan([_group(2, A, "ground_temperature", rows=4), _group(5, B, "ground_temperature", rows=2),
                           _group(5, None, None, rows=1)], catalogue)
    plan.groups[0].twins = 1
    assert plan.rows(fix.MOVE) == 5 and plan.rows(fix.TWIN) == 1
    assert fix.stale_forecast(plan, catalogue) == [(2, 4, 1), (5, 3, 1)]


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def test_ids_file_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "moves.csv"
    fix.prepare_ids_file(path)
    moves = [fix.Move(11, 4, 1, 200, 100), fix.Move(12, 2, 3, 100, 100)]
    fix.append_moves(path, moves)
    assert path.read_text(encoding="utf-8") == f"{fix.IDS_HEADER}\n11,4,1,200,100\n12,2,3,100,100\n"
    assert fix.read_ids_file(path) == moves
    fix.prepare_ids_file(path)  # an existing good file stays as it is
    assert fix.read_ids_file(path) == moves


def test_ids_file_refuses_a_foreign_file(tmp_path: Path) -> None:
    path = tmp_path / "twins.csv"
    path.write_text("observation_id,datastream_id\n1,2\n", encoding="utf-8")
    with pytest.raises(fix.IdsFileError, match="not an IDs file of this script"):
        fix.prepare_ids_file(path)
    with pytest.raises(fix.IdsFileError, match="not an IDs file of this script"):
        fix.read_ids_file(path)


def test_ids_file_drops_a_cut_off_last_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "moves.csv"
    path.write_text(f"{fix.IDS_HEADER}\n11,4,1,200,100\n12,2,3", encoding="utf-8")
    assert fix.read_ids_file(path) == [fix.Move(11, 4, 1, 200, 100)]
    fix.prepare_ids_file(path)
    assert path.read_text(encoding="utf-8") == f"{fix.IDS_HEADER}\n11,4,1,200,100\n"
    assert "dropping the cut-off last line" in capsys.readouterr().err
    header_only = tmp_path / "header.csv"
    header_only.write_text(fix.IDS_HEADER, encoding="utf-8")
    fix.prepare_ids_file(header_only)
    assert header_only.read_text(encoding="utf-8") == fix.IDS_HEADER + "\n"


def test_ids_file_rejects_a_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "moves.csv"
    path.write_text(f"{fix.IDS_HEADER}\n11,4,x,200,100\n", encoding="utf-8")
    with pytest.raises(fix.IdsFileError, match="line 2"):
        fix.read_ids_file(path)


def test_distinct_moves() -> None:
    moves = [fix.Move(12, 2, 3, 100, 100), fix.Move(11, 4, 1, 200, 100), fix.Move(12, 2, 3, 100, 100)]
    assert fix.distinct_moves(moves) == [moves[1], moves[0]]
    assert fix.distinct_moves(moves, {3}) == [moves[0]]
    assert fix.distinct_moves(moves, {4}) == [moves[1]]
    with pytest.raises(fix.IdsFileError, match="different moves"):
        fix.distinct_moves([*moves, fix.Move(12, 2, 6, 100, 200)])


def test_ids_file_is_refused_by_the_cleanup_and_twins_file_is_read(tmp_path: Path) -> None:
    """cleanup_frost_observations.py --ids-file reads an observation_id (or id) column, and a datastream_id."""
    assert not {"observation_id", "id", "@iot.id"} & set(fix.IDS_HEADER.split(","))
    path = tmp_path / "twins.csv"
    fix.write_twins_file(path, {30: 4, 7: 1})
    assert path.read_text(encoding="utf-8") == "observation_id,datastream_id\n7,1\n30,4\n"
    with path.open(encoding="utf-8", newline="") as handle:
        assert [(r["observation_id"], r["datastream_id"]) for r in csv.DictReader(handle)] == [("7", "1"), ("30", "4")]


# ---------------------------------------------------------------------------
# A fake database for the apply and rollback flows
# ---------------------------------------------------------------------------


@dataclass
class Row:
    ds: int
    foi: int
    thing: str | None  # label without prefix
    prop: str | None
    time: str
    result: float


@dataclass
class FakeDb:
    datastreams: list[fix.Datastream]
    rows: dict[int, Row]
    busy: list[str] = field(default_factory=list)
    sessions: list[str] = field(default_factory=list)
    refreshed: list[int] = field(default_factory=list)
    fail_write: int = 0  # fail the n-th move session (after it committed), 0: never
    writes: int = 0
    on_write: Any = None
    deleted: set[int] = field(default_factory=set)  # datastreams and FeaturesOfInterest deleted since the moves

    def __call__(self, sql: str) -> list[str]:
        self.sessions.append(sql)
        out: list[str] = []
        statements = sql.split(";\n")
        for stmt in statements:
            out += self._statement(stmt)
        if "'moved'" in sql:
            self.writes += 1
            if self.on_write:
                self.on_write()
            if self.writes == self.fail_write:
                raise fix.PsqlError("connection reset", out[:1])
        return out

    def _json(self, **kw: Any) -> str:
        return json.dumps(kw)

    def _twin(self, obs_id: int, to_ds: int) -> bool:
        row = self.rows[obs_id]
        return any(i != obs_id and r.ds == to_ds and r.time == row.time and r.thing == row.thing
                   and r.prop == row.prop and r.result == row.result for i, r in self.rows.items())

    def _statement(self, stmt: str) -> list[str]:
        if "'t', 'ds', 'id', d.\"ID\"" in stmt:
            return [self._json(t="ds", id=d.id, name=d.name, thing_id=d.thing_id, thing=d.thing, source=d.source,
                               prop=d.prop, stream_key=d.prop, op_name=d.op_name) for d in self.datastreams]
        if "'t', 'foi'" in stmt:
            return [self._json(t="foi", thing_id=t, locations=1, foi=f, fois=1) for t, f in FOIS.items()]
        if "pg_stat_activity" in stmt:
            return [self._json(t="busy", pid=1, app=a, state="active", running="") for a in self.busy]
        if m := re.search(r"'t', 'scanned', 'ds', (\d+)", stmt):
            return [self._json(t="scanned", ds=int(m[1]))]
        if m := re.search(r"'t', 'grp', 'ds', (\d+)", stmt):
            ds = int(m[1])
            groups: dict[tuple[Any, Any, Any], int] = {}
            for r in self.rows.values():
                if r.ds == ds:
                    key = (r.thing, r.prop, None) if r.thing else (None, None, "tgv")
                    groups[key] = groups.get(key, 0) + 1
            return [self._json(t="grp", ds=ds, thing=k[0], prop=k[1], other=k[2], n=n, first="t0", last="t1")
                    for k, n in groups.items()]
        if m := re.search(r"'t', 'checked', 'ds', (\d+)", stmt):
            return [self._json(t="checked", ds=int(m[1]))]
        if "'t', 'twin'" in stmt:
            ds = int(re.search(r'WHERE o."DATASTREAM_ID" = (\d+)', stmt)[1])  # type: ignore[index]
            mapping = self._mapping(stmt)
            return [self._json(t="twin", id=i, ds=ds, thing=r.thing, prop=r.prop)
                    for i, r in sorted(self.rows.items())
                    if r.ds == ds and (r.thing, r.prop) in mapping and self._twin(i, mapping[(r.thing, r.prop)][0])]
        if m := re.search(r"'t', 'selected', 'ds', (\d+)", stmt):
            return [self._json(t="selected", ds=int(m[1]))]
        if "'t', 'row'" in stmt:
            ds = int(re.search(r'WHERE o."DATASTREAM_ID" = (\d+)', stmt)[1])  # type: ignore[index]
            after = int(re.search(r'o."ID" > (\d+)', stmt)[1])  # type: ignore[index]
            limit = int(re.search(r"LIMIT (\d+)", stmt)[1])  # type: ignore[index]
            mapping = self._mapping(stmt)
            found = [(i, r) for i, r in sorted(self.rows.items())
                     if r.ds == ds and i > after and (r.thing, r.prop) in mapping][:limit]
            return [self._json(t="row", ds=ds, id=i, to=mapping[(r.thing, r.prop)][0], from_foi=r.foi,
                               to_foi=mapping[(r.thing, r.prop)][1] or r.foi,
                               twin=self._twin(i, mapping[(r.thing, r.prop)][0])) for i, r in found]
        if "moved AS (" in stmt:
            ds = int(re.search(r'o."DATASTREAM_ID" = (\d+) AND', stmt)[1])  # type: ignore[index]
            n = listed = 0
            for i, to_ds, from_foi, to_foi in re.findall(r"\((\d+)::bigint, (\d+)::bigint, (\d+)::bigint, (\d+)::bigint\)", stmt):
                listed += 1
                row = self.rows.get(int(i))
                if row and row.ds == ds and row.foi == int(from_foi) and not self._twin(int(i), int(to_ds)):
                    row.ds, row.foi = int(to_ds), int(to_foi)
                    n += 1
            return [self._json(t="moved", ds=ds, listed=listed, n=n)]
        if "'t', 'rbstate'" in stmt or "restored AS (" in stmt:
            moves = [fix.Move(*map(int, v)) for v in re.findall(
                r"\((\d+)::bigint, (\d+)::bigint, (\d+)::bigint, (\d+)::bigint, (\d+)::bigint\)", stmt)]
            at = [m for m in moves if m.observation_id in self.rows
                  and (self.rows[m.observation_id].ds, self.rows[m.observation_id].foi) == (m.to_ds, m.to_foi)]
            guarded = 'FROM "DATASTREAMS" sd' in stmt and 'FROM "FEATURES" sf' in stmt
            orphans = [m for m in at if m.from_ds in self.deleted or m.from_foi in self.deleted]
            if "restored AS (" in stmt:
                if orphans and not guarded:  # the foreign keys fail the whole statement, as in PostgreSQL
                    raise fix.PsqlError("violates foreign key constraint", [])
                for m in at:
                    if m not in orphans:
                        self.rows[m.observation_id].ds, self.rows[m.observation_id].foi = m.from_ds, m.from_foi
                return [self._json(t="restored", listed=len(moves), n=len(at) - len(orphans))]
            back = [m for m in moves if m.observation_id in self.rows
                    and (self.rows[m.observation_id].ds, self.rows[m.observation_id].foi) == (m.from_ds, m.from_foi)]
            return [self._json(t="rbstate", listed=len(moves), moved=len(at) - len(orphans),
                               source_gone=len(orphans), back=len(back),
                               gone=sum(1 for m in moves if m.observation_id not in self.rows))]
        if m := re.search(r"'t', 'refreshed', 'ds', (\d+)", stmt):
            self.refreshed.append(int(m[1]))
            return [self._json(t="refreshed", ds=int(m[1]))]
        return []

    @staticmethod
    def _mapping(stmt: str) -> dict[tuple[str, str], tuple[int, int | None]]:
        found = re.findall(r"\('((?:[^']|'')*)'::text, '((?:[^']|'')*)'::text, (\d+)::bigint(?:, (NULL|\d+)::bigint)?\)",
                           stmt)
        return {(t, p): (int(d), None if f in ("", "NULL") else int(f)) for t, p, d, f in found}


def _rows() -> dict[int, Row]:
    a, b = A.removeprefix(PREFIX), B.removeprefix(PREFIX)
    return {
        1: Row(1, 100, a, "temperature", "t0", 1.0),  # in place
        2: Row(4, 200, a, "temperature", "t1", 1.1),  # swapped with 3
        3: Row(1, 100, b, "temperature", "t1", 2.1),
        4: Row(2, 100, a, "ground_temperature", "t0", 3.0),  # own stale
        5: Row(2, 100, b, "ground_temperature", "t1", 4.0),  # another station's stale
        6: Row(1, 100, a, "temperature", "t2", 1.2),  # twin's twin
        7: Row(4, 200, a, "temperature", "t2", 1.2),  # twin
        8: Row(1, 100, None, None, "t0", 9.0),  # TGV
        9: Row(4, 200, a, "temperature", "t3", 1.3),
    }


AFTER = {2: (1, 100), 3: (4, 200), 4: (3, 100), 5: (6, 200), 9: (1, 100)}


def _args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    return fix.parse_args(["--container", "db", "--ids-file", str(tmp_path / "moves.csv"), *extra])


def _state(db: FakeDb) -> dict[int, tuple[int, int]]:
    return {i: (r.ds, r.foi) for i, r in db.rows.items()}


def test_dry_run_sends_only_read_only_sessions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = FakeDb(DATASTREAMS, _rows())
    before = _state(db)
    twins = tmp_path / "twins.csv"
    assert fix.run(fix.parse_args(["--container", "db", "--twins-file", str(twins)]), db) == 0
    assert all(s.startswith(fix.READ_ONLY_SESSION) for s in db.sessions)
    assert _state(db) == before
    out = capsys.readouterr().out
    assert re.search(r"\n  move\s+5\n", out) and re.search(r"\n  twin\s+1\n", out)
    assert re.search(r"\n  in place\s+2\n", out)
    assert "Stale datastreams: 2; empty after the moves: 2 (already empty: 1)" in out
    assert twins.read_text(encoding="utf-8") == "observation_id,datastream_id\n7,4\n"
    assert not (tmp_path / "moves.csv").exists()


def test_apply_records_before_each_write_and_refreshes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = FakeDb(DATASTREAMS, _rows())
    before = _state(db)
    ids_file = tmp_path / "moves.csv"
    recorded_at_write: list[set[int]] = []
    db.on_write = lambda: recorded_at_write.append({m.observation_id for m in fix.read_ids_file(ids_file)})
    assert fix.run(_args(tmp_path, "--apply", "--batch-rows", "1", "--chunk", "1"), db) == 0
    expected = {**before, **AFTER}
    assert _state(db) == expected
    # Each write session found its rows on disk already.
    moved_by_session = [set(re.findall(r"\((\d+)::bigint, \d+::bigint", s)) for s in db.sessions if "'moved'" in s]
    for on_disk, listed in zip(recorded_at_write, moved_by_session, strict=True):
        assert {int(i) for i in listed} <= on_disk
    assert {m.observation_id for m in fix.read_ids_file(ids_file)} == set(AFTER)
    assert set(db.refreshed) == {1, 2, 3, 4, 6}
    assert "Moved 5 observations (planned 5)" in capsys.readouterr().out
    write_sessions = [s for s in db.sessions if not s.startswith(fix.READ_ONLY_SESSION)]
    assert all(s.startswith(fix.WRITE_SESSION) for s in write_sessions)

    # Rollback: a dry run changes nothing, then everything goes back.
    db.sessions.clear()
    assert fix.run(fix.parse_args(["--container", "db", "--rollback", "--ids-file", str(ids_file)]), db) == 0
    assert _state(db) == expected and all(s.startswith(fix.READ_ONLY_SESSION) for s in db.sessions)
    db.refreshed.clear()
    assert fix.run(fix.parse_args(["--container", "db", "--rollback", "--apply", "--ids-file", str(ids_file)]), db) == 0
    assert _state(db) == before
    assert set(db.refreshed) == {1, 2, 3, 4, 6}


def test_rollback_skips_rows_whose_source_was_retired(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = FakeDb(DATASTREAMS, _rows())
    before = _state(db)
    ids_file = tmp_path / "moves.csv"
    assert fix.run(_args(tmp_path, "--apply"), db) == 0
    # Retirement deleted the emptied stale datastream 2: rows 4 and 5 were moved out of it.
    db.deleted.add(2)
    capsys.readouterr()
    rollback = ["--container", "db", "--rollback", "--ids-file", str(ids_file), "--batch-rows", "100"]
    assert fix.run(fix.parse_args(rollback), db) == 0
    out = capsys.readouterr().out
    assert re.search(r"where the move put them: to restore\s+3\n", out), out
    assert re.search(r"where the move put them, source gone: they stay\s+2\n", out), out
    assert "2 rows cannot go back" in out
    # One batch holds all five rows: the three with a source go back, the other two stay.
    assert fix.run(fix.parse_args([*rollback, "--apply"]), db) == 0
    assert _state(db) == {**before, 4: AFTER[4], 5: AFTER[5]}
    assert "except 2 whose source is gone" in capsys.readouterr().out
    # A re-run has nothing left to do and does not fail either.
    assert fix.run(fix.parse_args([*rollback, "--apply"]), db) == 0
    assert _state(db) == {**before, 4: AFTER[4], 5: AFTER[5]}


def test_a_rollback_without_the_source_check_would_fail_its_batch(tmp_path: Path) -> None:
    db = FakeDb(DATASTREAMS, _rows())
    assert fix.run(_args(tmp_path, "--apply"), db) == 0
    db.deleted.add(2)
    moves = fix.distinct_moves(fix.read_ids_file(tmp_path / "moves.csv"))
    unguarded = fix.restore_sql(moves).replace(f"AND {fix._SOURCE_EXISTS}", "")
    with pytest.raises(fix.PsqlError, match="foreign key"):
        db(unguarded)
    assert fix._SOURCE_EXISTS in fix.restore_sql(moves) and fix._SOURCE_EXISTS in fix.rollback_state_sql(moves)


def test_a_failed_write_still_refreshes_what_it_touched(tmp_path: Path) -> None:
    db = FakeDb(DATASTREAMS, _rows(), fail_write=1)
    with pytest.raises(fix.PsqlError, match="connection reset"):
        fix.run(_args(tmp_path, "--apply", "--batch-rows", "1", "--chunk", "1"), db)
    moved = fix.read_ids_file(tmp_path / "moves.csv")
    assert len(moved) == 1
    assert set(db.refreshed) == {moved[0].from_ds, moved[0].to_ds}
    # The re-run refreshes the datastreams named in the IDs file first, then finishes.
    db.refreshed.clear()
    db.sessions.clear()
    db.fail_write = 0
    assert fix.run(_args(tmp_path, "--apply"), db) == 0
    first_refresh = next(s for s in db.sessions if "'refreshed'" in s)
    assert set(map(int, re.findall(r"'refreshed', 'ds', (\d+)", first_refresh))) == {moved[0].from_ds, moved[0].to_ds}
    assert _state(db) == {**_state(FakeDb(DATASTREAMS, _rows())), **AFTER}


def test_apply_refuses_while_a_repair_script_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = FakeDb(DATASTREAMS, _rows(), busy=["fix_sc_pressure_units"])
    before = _state(db)
    assert fix.run(_args(tmp_path, "--apply"), db) == fix.EXIT_REFUSED
    assert _state(db) == before and "Refusing to write" in capsys.readouterr().out
    assert all(s.startswith(fix.READ_ONLY_SESSION) for s in db.sessions)


def test_verification_fails_when_moves_remain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = FakeDb(DATASTREAMS, _rows())
    real = db._statement

    def lose_moves(stmt: str) -> list[str]:  # the UPDATE moves nothing (e.g. rows changed meanwhile)
        if "moved AS (" in stmt:
            return [json.dumps({"t": "moved", "ds": 0, "listed": 1, "n": 0})]
        return real(stmt)

    db._statement = lose_moves  # type: ignore[method-assign]
    assert fix.run(_args(tmp_path, "--apply"), db) == fix.EXIT_VERIFY
    assert "rows to move remain" in capsys.readouterr().out


def test_canary_moves_only_rows_from_or_to_it(tmp_path: Path) -> None:
    db = FakeDb(DATASTREAMS, _rows())
    before = _state(db)
    assert fix.run(_args(tmp_path, "--apply", "--datastream", "2"), db) == 0
    assert _state(db) == {**before, 4: AFTER[4], 5: AFTER[5]}


def test_unknown_canary_datastream_is_an_error(tmp_path: Path) -> None:
    assert fix.run(_args(tmp_path, "--apply", "--datastream", "99"), FakeDb(DATASTREAMS, _rows())) == fix.EXIT_ERROR


def test_parse_args() -> None:
    with pytest.raises(SystemExit):
        fix.parse_args(["--container", "db", "--apply"])  # no --ids-file
    with pytest.raises(SystemExit):
        fix.parse_args(["--apply", "--ids-file", "x"])  # no transport
    with pytest.raises(SystemExit):
        fix.parse_args(["--container", "db", "--rollback", "--ids-file", "does-not-exist.csv"])
    with pytest.raises(SystemExit):
        fix.parse_args(["--retire-stale-ground-temperature", "--container", "db"])
    with pytest.raises(SystemExit):
        fix.parse_args(["--rollback", "--retire-stale-ground-temperature"])
    with pytest.raises(SystemExit):
        fix.parse_args(["--retire-stale-ground-temperature", "--ids-file", "does-not-exist.csv"])
    with pytest.raises(SystemExit):
        fix.parse_args(["--retire-stale-ground-temperature", "--force-retire"])  # nothing to override
    with pytest.raises(SystemExit):
        fix.parse_args(["--container", "db", "--force-retire"])
    assert fix.parse_args(["--retire-stale-ground-temperature"]).apply is False
    assert fix.parse_args(["--container", "db"]).apply is False


# ---------------------------------------------------------------------------
# Retirement through a fake FROST API
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body

    def json(self) -> Any:
        return self._body


class FakeFrost:
    def __init__(self, counts: dict[str, int], things: list[dict[str, Any]]) -> None:
        self.counts = counts
        self.things = things
        self.deleted: list[str] = []
        self.calls: list[str] = []

    def endpoint(self, path: str) -> str:
        return f"http://frost{path}"

    def headers(self) -> dict[str, str]:
        return {}

    def get(self, url: str, params: dict[str, str] | None = None) -> FakeResponse:
        self.calls.append(f"GET {url}")
        if url.endswith("/Things"):
            return FakeResponse(200, {"value": self.things})
        ds = re.search(r"Datastreams\((\d+)\)/Observations$", url)[1]  # type: ignore[index]
        if ds not in self.counts:
            return FakeResponse(500, {})
        n = self.counts[ds]
        return FakeResponse(200, {"@iot.count": n, "value": [{"@iot.id": 1}] if n else []})


def _thing(name: str, datastreams: list[tuple[int, str, str]], source: str = fix.SOURCE) -> dict[str, Any]:
    return {"@iot.id": 1, "name": name, "properties": {"source": source}, "Datastreams": [
        {"@iot.id": i, "name": f"{name} sensor - {suffix}", "properties": {"observed_property": prop}}
        for i, suffix, prop in datastreams]}


THINGS = [
    _thing(A, [(1, "Air temperature", "temperature"), (2, "Ground temperature", "ground_temperature"),
               (3, "Ground surface temperature", "ground_temperature")]),
    _thing(B, [(5, "Ground temperature", "ground_temperature"), (6, "Ground surface temperature", "ground_temperature")]),
    _thing("GEO_KNMI KNMI-1.00-1.00", [(7, "Ground temperature", "ground_temperature")]),  # no current one
    _thing("GEO_KNMI KNMI-2.00-2.00", [(8, "Ground temperature", "ground_temperature"),
                                       (9, "Ground surface temperature", "ground_temperature")], source="other"),
]


@pytest.fixture()
def frost(monkeypatch: pytest.MonkeyPatch) -> FakeFrost:
    fake = FakeFrost({"2": 0, "5": 3}, THINGS)

    def delete(http: Any, endpoint: str, timeout: float) -> FakeResponse:
        ds = re.search(r"Datastreams\((\d+)\)$", endpoint)[1]  # type: ignore[index]
        assert fake.calls[-1] == f"GET http://frost/Datastreams({ds})/Observations"  # checked right before
        fake.deleted.append(ds)
        return FakeResponse(200)

    monkeypatch.setattr(fix, "delete_datastream", delete)
    monkeypatch.setattr(fix.migrate, "fetch_knmi_things", lambda http, path, prefix, fields: fake.things)
    return fake


def test_stale_ground_temperature_needs_a_current_one() -> None:
    stale = fix.stale_ground_temperature(THINGS, odata_fields_for("v1.1"))
    assert [s.id for s in stale] == ["2", "5"]


def test_retire_dry_run_deletes_nothing(frost: FakeFrost, capsys: pytest.CaptureFixture[str]) -> None:
    assert fix.retire(frost, "/Things", "/Datastreams", PREFIX, odata_fields_for("v1.1"), apply=False) \
        == fix.EXIT_REFUSED
    assert frost.deleted == []
    out = capsys.readouterr().out
    assert "empty, would be deleted" in out and "3 observations; never deleted" in out


def test_retire_deletes_only_empty_ones(frost: FakeFrost) -> None:
    assert fix.retire(frost, "/Things", "/Datastreams", PREFIX, odata_fields_for("v1.1"), apply=True) \
        == fix.EXIT_REFUSED
    assert frost.deleted == ["2"]
    frost.counts["5"] = 0
    assert fix.retire(frost, "/Things", "/Datastreams", PREFIX, odata_fields_for("v1.1"), apply=True,
                      only={"5"}) == fix.EXIT_OK
    assert frost.deleted == ["2", "5"]


def test_retire_keeps_a_source_of_recorded_moves_unless_forced(frost: FakeFrost, tmp_path: Path,
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    ids_file = tmp_path / "moves.csv"
    fix.prepare_ids_file(ids_file)
    fix.append_moves(ids_file, [fix.Move(10, 2, 3, 100, 100), fix.Move(11, 2, 3, 100, 100), fix.Move(12, 9, 1, 1, 1)])
    recorded = fix.recorded_sources(fix.read_ids_file(ids_file))
    assert recorded == {"2": 2, "9": 1}
    frost.counts["5"] = 0
    fields = odata_fields_for("v1.1")
    assert fix.retire(frost, "/Things", "/Datastreams", PREFIX, fields, apply=True, recorded=recorded) \
        == fix.EXIT_REFUSED
    assert frost.deleted == ["5"]
    out = capsys.readouterr().out
    assert "empty, but the source of 2 moves in the IDs file" in out
    assert "1 empty but named as a source in the IDs file left alone" in out
    assert fix.retire(frost, "/Things", "/Datastreams", PREFIX, fields, apply=False, recorded=recorded,
                      force=True) == fix.EXIT_OK
    assert "would be deleted (source of 2 recorded moves: they can no longer be rolled back)" in \
        capsys.readouterr().out
    assert fix.retire(frost, "/Things", "/Datastreams", PREFIX, fields, apply=True, recorded=recorded,
                      force=True) == fix.EXIT_OK
    assert frost.deleted == ["5", "2", "5"]


def test_run_retire_reads_the_ids_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture[str]) -> None:
    ids_file = tmp_path / "moves.csv"
    fix.prepare_ids_file(ids_file)
    fix.append_moves(ids_file, [fix.Move(10, 2, 3, 100, 100)])
    seen: dict[str, Any] = {}

    def fake_retire(*args: Any, **kwargs: Any) -> int:
        seen.update(kwargs)
        return fix.EXIT_OK

    monkeypatch.setattr(fix, "retire", fake_retire)
    target = FrostTarget(url="http://frost/v1.1", version="v1.1", label="t")
    monkeypatch.setattr(config, "settings", dataclasses.replace(config.settings, frost_targets=(target,)))
    assert fix.main(["--retire-stale-ground-temperature", "--ids-file", str(ids_file)]) == fix.EXIT_OK
    assert seen["recorded"] == {"2": 1} and seen["force"] is False
    assert "a stale datastream named as their source is left alone" in capsys.readouterr().out
    assert fix.main(["--retire-stale-ground-temperature", "--ids-file", str(ids_file), "--force-retire"]) == 0
    assert seen["force"] is True
    assert fix.main(["--retire-stale-ground-temperature"]) == fix.EXIT_OK
    assert seen["recorded"] is None
    assert "No --ids-file" in capsys.readouterr().out


def test_retire_never_deletes_when_the_count_fails(frost: FakeFrost) -> None:
    del frost.counts["2"]
    frost.counts["5"] = 0
    assert fix.retire(frost, "/Things", "/Datastreams", PREFIX, odata_fields_for("v1.1"), apply=True) \
        == fix.EXIT_ERROR
    assert frost.deleted == ["5"]


@pytest.mark.parametrize("status", [401, 403])
def test_retire_stops_at_the_first_refused_delete(frost: FakeFrost, monkeypatch: pytest.MonkeyPatch, status: int,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    """Without FROST's delete role (the connector's 'write' user) the first DELETE is refused: stop, ask for admin."""
    frost.counts["5"] = 0
    attempts: list[str] = []

    def refused(http: Any, endpoint: str, timeout: float) -> FakeResponse:
        attempts.append(endpoint)
        return FakeResponse(status)

    monkeypatch.setattr(fix, "delete_datastream", refused)
    assert fix.retire(frost, "/Things", "/Datastreams", PREFIX, odata_fields_for("v1.1"), apply=True)         == fix.EXIT_ERROR
    assert len(attempts) == 1  # not once per datastream
    out = capsys.readouterr().out
    assert f"DELETE refused with HTTP {status}" in out and "admin credentials" in out


@pytest.mark.parametrize("body", [{"value": []}, {"@iot.count": "0", "value": []}, {"@iot.count": 0}, []])
def test_observation_count_needs_a_count_and_a_list(body: Any) -> None:
    class Http:
        def endpoint(self, path: str) -> str:
            return path

        def get(self, url: str, params: dict[str, str] | None = None) -> FakeResponse:
            return FakeResponse(200, body)

    with pytest.raises(RuntimeError, match="no count"):
        fix.observation_count(Http(), "/Datastreams", "2", odata_fields_for("v1.1"))


def test_observation_count_reads_both_conventions() -> None:
    class Http:
        def __init__(self, body: Any) -> None:
            self.body = body

        def endpoint(self, path: str) -> str:
            return path

        def get(self, url: str, params: dict[str, str] | None = None) -> FakeResponse:
            return FakeResponse(200, self.body)

    v1, v2 = odata_fields_for("v1.1"), odata_fields_for("v2.0")
    assert fix.observation_count(Http({"@iot.count": 4, "value": [{}]}), "/D", "1", v2) == 4
    assert fix.observation_count(Http({"@count": 0, "value": [{}]}), "/D", "1", v1) == 1
