"""scripts/fix_sc_misrouted_observations.py: plan, apply and rollback of misrouted rows.

Unit tests drive the script against a fake psql; the integration test
(python -m pytest -m integration) runs it end to end, through docker exec, on a
throwaway FROST-Server 2.8 + PostGIS.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
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


class FakeDb:
    """Answers the script's statements from fixed rows and records every session."""

    def __init__(self, ids_file: Path | None = None) -> None:
        self.sessions: list[str] = []
        self.ids_file = ids_file
        self.rows_served = False
        self.recorded_before_write: list[bool] = []

    def __call__(self, sql: str) -> list[str]:
        self.sessions.append(sql)
        if "'t', 'ds'" in sql:
            return [
                _line(t="ds", id=1386, name="GEO_SensorCommunity 93182 sensor - Air pressure", thing_id=10,
                      thing="GEO_SensorCommunity 93182", source="sensor_community",
                      sensor_id="sensor_community-93182", prop="pressure"),
                _line(t="ds", id=1388, name="GEO_SensorCommunity 93182 sensor - PM10", thing_id=10,
                      thing="GEO_SensorCommunity 93182", source="sensor_community",
                      sensor_id="sensor_community-93182", prop="pm10"),
                _line(t="foi", thing_id=10, locations=1, foi=110, fois=1),
                _line(t="bounds", min=1, max=450_000),
            ]
        if "'t', 'grp'" in sql:
            return [_line(t="grp", ds=1386, sid="sensor_community-93182", prop="pm10", thing="SensorCommunity 93182",
                          loc="93182", n=2, min_id=11, max_id=12, first="2026-11-12T14:12:23+00:00",
                          last="2026-11-12T14:17:23+00:00")] if "BETWEEN 1 AND" in sql or '= 1386' in sql else []
        if "'t', 'dup'" in sql:
            return []
        if "'t', 'row'" in sql:
            if self.rows_served:
                return []
            self.rows_served = True
            return [_line(t="row", id=11, foi=110, dup=False), _line(t="row", id=12, foi=110, dup=True)]
        if "moved AS" in sql:
            recorded = self.ids_file is not None and '"rows":[[11,110]]' in self.ids_file.read_text(encoding="utf-8")
            self.recorded_before_write.append(recorded)
            return [_line(t="moved", n=1)]
        if "restored AS" in sql:
            return [_line(t="restored", n=1)]
        return []


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
    args = fix.parse_args(["--container", "db", "--apply", "--ids-file", str(ids_file)])

    assert fix.run(args, db) == 0

    assert db.recorded_before_write == [True]
    (move_sql,) = [sql for sql in db.sessions if "moved AS" in sql]
    assert move_sql.startswith(fix.WRITE_SESSION)
    assert '"DATASTREAM_ID" = 1388' in move_sql  # to its own datastream
    assert 'o."DATASTREAM_ID" = 1386' in move_sql  # only while still in the source
    assert "(11::bigint, 110::bigint)" in move_sql and "12::bigint" not in move_sql  # 12 is a duplicate
    assert 'SET "DATASTREAM_ID" = 1388, "FEATURE_ID" = o."FEATURE_ID"' in move_sql  # same Thing: FoI kept
    refreshed = [sql for sql in db.sessions if 'UPDATE "DATASTREAMS"' in sql]
    assert refreshed and 'd."ID" = 1386' in refreshed[0] and 'd."ID" = 1388' in refreshed[0]
    records = [json.loads(line) for line in ids_file.read_text(encoding="utf-8").splitlines()]
    assert [r["op"] for r in records] == ["run", "move", "committed"]
    assert records[1] | {"batch": None} == {
        "op": "move", "batch": None, "from_ds": 1386, "to_ds": 1388, "to_foi": None, "rows": [[11, 110]],
    }
    assert "Moved 1 observations (1 left in place" in capsys.readouterr().out


def test_rollback_moves_recorded_rows_back_with_their_feature_ids(tmp_path) -> None:
    ids_file = tmp_path / "moved.jsonl"
    ids_file.write_text(
        json.dumps({"op": "move", "batch": "b1", "from_ds": 1386, "to_ds": 2016, "to_foi": 120,
                    "rows": [[11, 110], [12, None]]}) + "\n" + json.dumps({"op": "committed", "batch": "b1", "moved": 2}) + "\n",
        encoding="utf-8",
    )
    db = FakeDb(ids_file)

    assert fix.run(fix.parse_args(["--container", "db", "--rollback", "--ids-file", str(ids_file)]), db) == 0

    (restore,) = [sql for sql in db.sessions if "restored AS" in sql]
    assert 'SET "DATASTREAM_ID" = 1386, "FEATURE_ID" = v.foi' in restore
    assert 'o."DATASTREAM_ID" = 2016' in restore  # only rows still where the script put them
    assert "(11::bigint, 110::bigint), (12::bigint, NULL::bigint)" in restore
    assert any('UPDATE "DATASTREAMS"' in sql and 'd."ID" = 2016' in sql for sql in db.sessions)
    assert json.loads(ids_file.read_text(encoding="utf-8").splitlines()[-1])["op"] == "rolled_back"


@pytest.mark.parametrize("mode", ["--apply", "--rollback"])
def test_writes_need_an_ids_file(mode: str) -> None:
    with pytest.raises(SystemExit):
        fix.parse_args(["--container", "db", mode])


# ---------------------------------------------------------------------------
# Integration: end to end on FROST-Server 2.8 (python -m pytest -m integration)
# ---------------------------------------------------------------------------


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="module")
def frost() -> Iterator[tuple[str, str]]:
    """(v1.1 base URL, database container) of a throwaway FROST 2.8 + PostGIS, torn down afterwards."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    suffix = uuid.uuid4().hex[:8]
    net, db, server = f"scfix-it-{suffix}", f"scfix-it-db-{suffix}", f"scfix-it-frost-{suffix}"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = f"http://localhost:{port}/FROST-Server"
    try:
        _docker("network", "create", net)
        _docker("run", "-d", "--name", db, "--network", net, "-e", "POSTGRES_DB=sensorthings",
                "-e", "POSTGRES_USER=sensorthings", "-e", "POSTGRES_PASSWORD=sensorthings",
                "postgis/postgis:16-3.4-alpine")
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
        _docker("rm", "-f", server, db, check=False)
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

    assert fix.main([*script, "--rollback", "--ids-file", str(ids_file)]) == 0
    assert _state(db) == before
