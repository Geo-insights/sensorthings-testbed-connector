"""scripts/fix_knmi_observations.py against FROST-Server 2.8 on PostgreSQL 16 (PostGIS).

A throwaway FROST stack (database on tmpfs), seeded through the FROST API with KNMI
Things, Locations and Datastreams shaped like the connector's and observations
whose PARAMETERS carry the connector's labels: readings of two stations swapped at
the same time, readings in the stale 'Ground temperature' datastreams, an exact
twin, TGV and Sensor.Community rows in KNMI datastreams, and a reading of an unknown
station. The script runs its real SQL through its real transport (docker exec +
psql). Everything is removed afterwards (docker rm -f -v).

    python -m pytest tests/integration/test_fix_knmi_observations.py -m integration
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import requests

from app.frost.http_client import FrostHTTPClient
from app.frost.odata_fields import odata_fields_for
from scripts import fix_knmi_observations as fix

pytestmark = pytest.mark.integration

DB_IMAGE = "postgis/postgis:16-3.4-alpine"
FROST_IMAGE = "fraunhoferiosb/frost-server:2.8"
DB_USER = DB_NAME = "sensorthings"
DOCKER_PREFIX = os.environ.get("KNMI_IT_DOCKER_PREFIX", "lnkh")  # container and network names start with it
HTTP = requests.Session()

STATIONS = {  # key -> (name, lon, lat, old position id)
    "A": ("KNMI-52.36-3.34", 3.3417, 52.36, "knmi-0"),
    "B": ("KNMI-53.49-5.94", 5.9417, 53.4917, "knmi-1"),
    "C": ("KNMI-52.14-4.44", 4.4364, 52.1397, "knmi-2"),
}
PROPS = {  # datastream key -> (canonical property, ObservedProperty name, name suffix)
    "temp": ("temperature", "Air temperature", "Air temperature"),
    "hum": ("humidity", "Relative humidity", "Relative humidity"),
    "stale": ("ground_temperature", "Ground temperature", "Ground temperature"),
    "gst": ("ground_temperature", "Ground surface temperature", "Ground surface temperature"),
}
T = [f"2026-11-15T10:{10 * i:02d}:00Z" for i in range(6)]

# label -> (datastream "<station>.<key>", the reading's station or None for another source, property, time, result)
SEED: dict[str, tuple[str, str | None, str, str, float]] = {
    "a_temp_t0": ("A.temp", "A", "temperature", T[0], 1.0),  # in place
    "b_temp_t0": ("B.temp", "B", "temperature", T[0], 2.0),  # in place
    "a_temp_t1_in_b": ("B.temp", "A", "temperature", T[1], 1.1),  # the swap: both move,
    "b_temp_t1_in_a": ("A.temp", "B", "temperature", T[1], 2.1),  # each into the other's slot
    "a_hum_t2_in_b": ("B.hum", "A", "humidity", T[2], 81.0),
    "b_hum_t2_in_a": ("A.hum", "B", "humidity", T[2], 82.0),
    "a_gt_t0_stale": ("A.stale", "A", "ground_temperature", T[0], 3.0),  # same Thing: FoI kept
    "a_gt_t1_stale": ("A.stale", "A", "ground_temperature", T[1], 3.1),
    "b_gt_t2_in_a_stale": ("A.stale", "B", "ground_temperature", T[2], 4.2),  # another Thing's stale
    "a_gt_t3": ("A.gst", "A", "ground_temperature", T[3], 3.3),  # in place
    "a_temp_t3": ("A.temp", "A", "temperature", T[3], 1.3),  # in place, and the twin's twin
    "a_temp_t3_twin": ("B.temp", "A", "temperature", T[3], 1.3),  # twin: listed, not moved
    "a_temp_t4": ("A.temp", "A", "temperature", T[4], 1.5),  # in place
    "a_temp_t4_other": ("B.temp", "A", "temperature", T[4], 1.4),  # same time, other value: moved
    "tgv_in_a": ("A.temp", None, "tgv", T[1], 9.0),  # another source: left alone
    "sc_in_b": ("B.hum", None, "sc", T[1], 55.0),
    "unknown_station": ("A.temp", "X", "temperature", T[2], 7.0),  # no target
    "tgv_in_c_stale": ("C.stale", None, "tgv", T[0], 9.5),  # keeps C's stale datastream
}
# Where each row ends up after --apply: the datastream key, or None for "where it was".
EXPECTED = {
    "a_temp_t1_in_b": "A.temp", "b_temp_t1_in_a": "B.temp",
    "a_hum_t2_in_b": "A.hum", "b_hum_t2_in_a": "B.hum",
    "a_gt_t0_stale": "A.gst", "a_gt_t1_stale": "A.gst", "b_gt_t2_in_a_stale": "B.gst",
    "a_temp_t4_other": "A.temp",
}
TWINS = {"a_temp_t3_twin"}
UNKNOWN = {"X": ("KNMI-99.99-9.99",)}


@dataclass
class Stack:
    db: str
    url: str  # FROST v1.1 root
    ids: dict[str, int]  # observation per SEED label
    ds: dict[str, int]  # datastream per "<station>.<key>"
    things: dict[str, int]
    fois: dict[str, int] = field(default_factory=dict)  # generated FoI per station


def _docker(*args: str, input: str | None = None, check: bool = True, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], input=input, capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, check=check)


def _psql(db: str, sql: str) -> list[str]:
    proc = _docker("exec", "-i", db, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", DB_USER,
                   "-d", DB_NAME, input=sql)
    return proc.stdout.splitlines()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(what: str, ready: Any, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if ready():
                return
        except (requests.RequestException, subprocess.SubprocessError):
            pass
        time.sleep(1)
    raise TimeoutError(f"{what} not ready within {timeout:.0f} s")


def _post(url: str, path: str, body: dict[str, Any]) -> int:
    response = HTTP.post(f"{url}/{path}", json=body, timeout=60)
    assert response.status_code == 201, response.text
    match = re.search(r"\((\d+)\)$", response.headers["Location"])
    assert match is not None
    return int(match[1])


def _seed(url: str) -> Stack:
    ops = {key: _post(url, "ObservedProperties", {"name": op, "description": op, "definition": "https://example.org"})
           for key, (_, op, _) in PROPS.items() if key != "gst"}
    ops["gst"] = _post(url, "ObservedProperties", {"name": "Ground surface temperature", "description": "x",
                                                   "definition": "https://example.org"})
    things: dict[str, int] = {}
    datastreams: dict[str, int] = {}
    for key, (name, lon, lat, sensor_id) in STATIONS.items():
        thing_name = f"GEO_KNMI {name}"
        things[key] = _post(url, "Things", {
            "name": thing_name, "description": "KNMI official weather station",
            "properties": {"site": "nl", "source": "knmi_weather", "knmi_station_id": sensor_id[5:],
                           "station_name": name, "network": "KNMI"},
            "Locations": [{"name": f"{thing_name} location", "description": "x",
                           "encodingType": "application/geo+json",
                           "location": {"type": "Point", "coordinates": [lon, lat]}}],
        })
        sensor = _post(url, "Sensors", {"name": f"{thing_name} sensor", "description": "x",
                                        "encodingType": "application/json", "metadata": "https://knmi.nl"})
        for ds_key in ("temp", "hum", "stale", "gst"):  # the stale one first: the lower id, as on production
            prop, _, suffix = PROPS[ds_key]
            datastreams[f"{key}.{ds_key}"] = _post(url, "Datastreams", {
                "name": f"{thing_name} sensor - {suffix}", "description": "x",
                "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                "unitOfMeasurement": {"name": prop, "symbol": "x", "definition": "https://example.org"},
                "Thing": {"@iot.id": things[key]}, "Sensor": {"@iot.id": sensor},
                "ObservedProperty": {"@iot.id": ops[ds_key]},
                "properties": {"site_key": "nl", "thing_name": thing_name, "sensor_id": sensor_id,
                               "observed_property": prop, "streamKey": prop},
            })
    ids = {}
    for label, (ds_key, station, prop, phen, result) in SEED.items():
        if station is None and prop == "tgv":
            params: dict[str, Any] = {"sensor_id": "tgv-0004a30b001c", "thing_name": "TGV 0004a30b001c",
                                      "observed_property": "temperature", "location": "tgv"}
        elif station is None:
            params = {"sensor_id": "sensor_community-123", "thing_name": "SensorCommunity 123",
                      "observed_property": "humidity", "location": "123"}
        else:
            name = STATIONS[station][0] if station in STATIONS else UNKNOWN[station][0]
            params = {"unit": "x", "location": "nl", "sensor_id": f"knmi-{ord(station) - 64}", "device_eui": None,
                      "stream_key": None, "thing_name": f"KNMI {name}", "sensor_name": f"KNMI {name} sensor",
                      "observed_property": prop}
        body: dict[str, Any] = {"phenomenonTime": phen, "result": result, "parameters": params,
                                "Datastream": {"@iot.id": datastreams[ds_key]}}
        if label == "tgv_in_a":  # a row with its own FeatureOfInterest: A's observedArea is not a point
            body["FeatureOfInterest"] = {"name": "tgv", "description": "x", "encodingType": "application/geo+json",
                                         "feature": {"type": "Point", "coordinates": [5.0, 52.0]}}
        ids[label] = _post(url, "Observations", body)
    stack = Stack("", url, ids, datastreams, things)
    return stack


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    try:
        _docker("version", timeout=30)
    except (FileNotFoundError, subprocess.SubprocessError):
        pytest.skip("Docker is not available")
    name = f"{DOCKER_PREFIX}-{uuid.uuid4().hex[:8]}"
    db, frost, network = f"{name}-db", f"{name}-frost", f"{name}-net"
    port = _free_port()
    try:
        _docker("network", "create", network)
        _docker("run", "-d", "--name", db, "--network", network, "--tmpfs", "/var/lib/postgresql/data",
                "-e", f"POSTGRES_DB={DB_NAME}", "-e", f"POSTGRES_USER={DB_USER}", "-e", "POSTGRES_PASSWORD=sensorthings",
                DB_IMAGE)
        _wait("PostGIS", lambda: _docker("exec", db, "pg_isready", "-h", "127.0.0.1", "-U", DB_USER, check=False)
              .returncode == 0, 120)
        _docker("run", "-d", "--name", frost, "--network", network, "-p", f"127.0.0.1:{port}:8080",
                "-e", "JAVA_OPTS=-Xmx768m",
                "-e", f"serviceRootUrl=http://127.0.0.1:{port}/FROST-Server",
                "-e", "persistence_db_driver=org.postgresql.Driver",
                "-e", f"persistence_db_url=jdbc:postgresql://{db}:5432/{DB_NAME}",
                "-e", f"persistence_db_username={DB_USER}", "-e", "persistence_db_password=sensorthings",
                "-e", "persistence_autoUpdateDatabase=true",
                FROST_IMAGE)
        url = f"http://127.0.0.1:{port}/FROST-Server/v1.1"
        _wait("FROST", lambda: HTTP.get(f"{url}/Things", timeout=10).status_code == 200, 300)
        seeded = _seed(url)
        seeded.db = db
        for key, thing in seeded.things.items():
            seeded.fois[key] = int(_psql(db, f'SELECT l."GEN_FOI_ID" FROM "THINGS_LOCATIONS" tl JOIN "LOCATIONS" l '
                                             f'ON l."ID" = tl."LOCATION_ID" WHERE tl."THING_ID" = {thing};\n')[0])
        # FROST's pooled sessions report their writes when they go idle (up to 10 s later): wait for that,
        # so the counters the dry-run test compares move only for the script's own writes.
        _wait("the seed's table statistics", lambda: f"OBSERVATIONS|{len(SEED)}|0|0" in _writes(seeded), 60)
        _psql(db, 'CREATE TABLE knmi_test_obs AS SELECT "ID", "DATASTREAM_ID", "FEATURE_ID" FROM "OBSERVATIONS";\n'
                  f'CREATE TABLE knmi_test_ds AS {DS_STATE};\n')
        yield seeded
    finally:
        _docker("rm", "-f", "-v", frost, db, check=False)
        _docker("network", "rm", network, check=False)


DS_STATE = ('SELECT "ID", "PHENOMENON_TIME_START", "PHENOMENON_TIME_END", "RESULT_TIME_START", "RESULT_TIME_END", '
            'ST_AsText(ST_Normalize("OBSERVED_AREA")) AS area, "LAST_FOI_ID" FROM "DATASTREAMS"')
RESET_SQL = """
UPDATE "OBSERVATIONS" o SET "DATASTREAM_ID" = s."DATASTREAM_ID", "FEATURE_ID" = s."FEATURE_ID"
  FROM knmi_test_obs s WHERE o."ID" = s."ID"
   AND (o."DATASTREAM_ID" <> s."DATASTREAM_ID" OR o."FEATURE_ID" <> s."FEATURE_ID");
UPDATE "DATASTREAMS" d SET "PHENOMENON_TIME_START" = s."PHENOMENON_TIME_START",
       "PHENOMENON_TIME_END" = s."PHENOMENON_TIME_END", "RESULT_TIME_START" = s."RESULT_TIME_START",
       "RESULT_TIME_END" = s."RESULT_TIME_END", "OBSERVED_AREA" = ST_GeomFromText(s.area, 4326),
       "LAST_FOI_ID" = s."LAST_FOI_ID"
  FROM knmi_test_ds s WHERE d."ID" = s."ID";
"""


@pytest.fixture()
def fresh(stack: Stack) -> Stack:
    _wait("no session of the script", lambda: _psql(
        stack.db, f"SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE '{fix.APP_NAME}%';\n") == ["0"], 60)
    _psql(stack.db, RESET_SQL)
    return stack


def _rows(stack: Stack) -> dict[int, tuple[int, int]]:
    """(DATASTREAM_ID, FEATURE_ID) per observation."""
    lines = _psql(stack.db, 'SELECT "ID", "DATASTREAM_ID", "FEATURE_ID" FROM "OBSERVATIONS" ORDER BY 1;\n')
    return {int(i): (int(d), int(f)) for i, d, f in (line.split("|") for line in lines)}


def _ds_state(stack: Stack) -> list[str]:
    return _psql(stack.db, DS_STATE + ' ORDER BY "ID";\n')


def _recomputed(stack: Stack) -> list[str]:
    """Every datastream's times, observedArea and LAST_FOI_ID as its rows give them."""
    return _psql(stack.db, """
SELECT d."ID", (SELECT min("PHENOMENON_TIME_START") FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" = d."ID"),
       (SELECT max(coalesce("PHENOMENON_TIME_END", "PHENOMENON_TIME_START")) FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" = d."ID"),
       (SELECT min("RESULT_TIME") FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" = d."ID"),
       (SELECT max("RESULT_TIME") FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" = d."ID"),
       ST_AsText(ST_Normalize((SELECT ST_ConvexHull(ST_Collect(f."GEOM")) FROM "FEATURES" f
                   WHERE f."ID" IN (SELECT "FEATURE_ID" FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" = d."ID")))),
       (SELECT "FEATURE_ID" FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" = d."ID" ORDER BY "ID" DESC LIMIT 1)
  FROM "DATASTREAMS" d ORDER BY 1;
""")


def _writes(stack: Stack) -> list[str]:
    return _psql(stack.db, "SELECT pg_stat_force_next_flush();\n"
                           "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables "
                           "WHERE relname IN ('OBSERVATIONS', 'DATASTREAMS', 'FEATURES') ORDER BY 1;\n")


def _script(stack: Stack, *args: str) -> int:
    return fix.main(["--container", stack.db, *args])


def _expected_after_apply(stack: Stack, before: dict[int, tuple[int, int]]) -> dict[int, tuple[int, int]]:
    expected = dict(before)
    for label, ds_key in EXPECTED.items():
        obs = stack.ids[label]
        station = ds_key.split(".")[0]
        same_thing = SEED[label][0].split(".")[0] == station
        expected[obs] = (stack.ds[ds_key], before[obs][1] if same_thing else stack.fois[station])
    return expected


def _http(stack: Stack, url: str | None = None) -> FrostHTTPClient:
    return FrostHTTPClient(base_urls=[url or stack.url], timeout=60)


def test_dry_run_reports_and_writes_nothing(fresh: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows, ds_state, writes = _rows(fresh), _ds_state(fresh), _writes(fresh)
    twins = tmp_path / "twins.csv"
    plan = tmp_path / "plan.csv"
    assert _script(fresh, "--twins-file", str(twins), "--plan-file", str(plan)) == 0
    out = capsys.readouterr().out
    assert re.search(r"\n  in place\s+5\n", out), out
    assert re.search(r"\n  move\s+8\n", out), out
    assert re.search(r"\n  twin\s+1\n", out), out
    assert re.search(r"\n  no target\s+1\n", out), out
    assert re.search(r"moves to another Thing \(FEATURE_ID set\)\s+6\n", out)
    assert re.search(r"moves within one Thing \(FEATURE_ID kept\)\s+2\n", out)
    assert "no target: no KNMI Thing 'GEO_KNMI KNMI-99.99-9.99': 1\n" in out
    assert re.search(r"\n  tgv\s+2\n", out) and re.search(r"\n  sensor_community\s+1\n", out)
    assert "Stale datastreams: 3; empty after the moves: 2 (already empty: 1)" in out
    assert f"{fresh.ds['C.stale']:>8} keeps 1 of 1 rows" in out
    assert twins.read_text(encoding="utf-8") == (
        f"observation_id,datastream_id\n{fresh.ids['a_temp_t3_twin']},{fresh.ds['B.temp']}\n")
    assert "move," in plan.read_text(encoding="utf-8")
    # Nothing written: the rows, the datastreams, and the table counters.
    assert _rows(fresh) == rows and _ds_state(fresh) == ds_state
    assert _writes(fresh) == writes
    # The retirement's dry run writes nothing either.
    assert fix.retire(_http(fresh), "/Things", "/Datastreams", "GEO_", odata_fields_for("v1.1"), apply=False) \
        == fix.EXIT_REFUSED
    assert _writes(fresh) == writes and _ds_state(fresh) == ds_state


def test_apply_rerun_and_rollback(fresh: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before, ds_before = _rows(fresh), _ds_state(fresh)
    ids_file, twins = tmp_path / "moves.csv", tmp_path / "twins.csv"
    assert _script(fresh, "--apply", "--ids-file", str(ids_file), "--twins-file", str(twins),
                   "--batch-rows", "2", "--chunk", "2") == 0
    out = capsys.readouterr().out
    assert "Moved 8 observations (planned 8); 8 recorded" in out
    assert "Verification: no rows to move remain" in out
    after = _rows(fresh)
    assert after == _expected_after_apply(fresh, before)  # nothing else moved: twins, TGV, SC, unknown station
    assert _ds_state(fresh) == _recomputed(fresh)  # times, observedArea and LAST_FOI_ID follow the rows
    assert {m.observation_id for m in fix.read_ids_file(ids_file)} == {fresh.ids[k] for k in EXPECTED}
    assert twins.read_text(encoding="utf-8").splitlines()[1:] == [f"{fresh.ids['a_temp_t3_twin']},{fresh.ds['B.temp']}"]
    # The swap: each station's reading at T1 now sits in its own datastream, next to nothing else.
    api = HTTP.get(f"{fresh.url}/Datastreams({fresh.ds['A.temp']})/Observations",
                   params={"$filter": f"phenomenonTime eq {T[1]}"}, timeout=60).json()["value"]
    assert sorted(o["result"] for o in api) == [1.1, 9.0]  # A's own reading, and the TGV row left alone

    # Re-run: nothing to move.
    assert _script(fresh, "--apply", "--ids-file", str(ids_file)) == 0
    assert "Moved 0 observations (planned 0)" in capsys.readouterr().out
    assert _rows(fresh) == after

    # Rollback: a dry run first, then the exact previous state, datastreams included.
    writes = _writes(fresh)
    assert _script(fresh, "--rollback", "--ids-file", str(ids_file)) == 0
    assert re.search(r"where the move put them: to restore\s+8\n", capsys.readouterr().out)
    assert _writes(fresh) == writes and _rows(fresh) == after
    assert _script(fresh, "--rollback", "--ids-file", str(ids_file), "--apply") == 0
    assert "Verification: no recorded row is where the move put it any more." in capsys.readouterr().out
    assert _rows(fresh) == before
    assert _ds_state(fresh) == ds_before


def test_canary_moves_only_rows_from_or_to_its_datastream(fresh: Stack, tmp_path: Path) -> None:
    before = _rows(fresh)
    ids_file = tmp_path / "moves.csv"
    assert _script(fresh, "--apply", "--ids-file", str(ids_file), "--datastream", str(fresh.ds["A.temp"])) == 0
    moved = {k for k in EXPECTED if _rows(fresh)[fresh.ids[k]] != before[fresh.ids[k]]}
    assert moved == {"a_temp_t1_in_b", "b_temp_t1_in_a", "a_temp_t4_other"}
    # Rollback honours --datastream too.
    assert _script(fresh, "--rollback", "--apply", "--ids-file", str(ids_file), "--datastream",
                   str(fresh.ds["B.hum"])) == 0
    assert _rows(fresh) != before
    assert _script(fresh, "--rollback", "--apply", "--ids-file", str(ids_file), "--datastream",
                   str(fresh.ds["B.temp"])) == 0
    assert _rows(fresh) == before


def test_interrupted_batch_then_rerun(fresh: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    before, ds_before = _rows(fresh), _ds_state(fresh)
    ids_file = tmp_path / "moves.csv"
    real = fix.pressure.psql_runner(fix.pressure.psql_argv(container=fresh.db), 300)
    writes = {"n": 0}

    def dropped_after_first_write(sql: str) -> list[str]:
        lines = real(sql)
        if "UPDATE \"OBSERVATIONS\"" in sql and "'moved'" in sql:
            writes["n"] += 1
            if writes["n"] == 1:  # the server committed; the client lost the connection
                raise fix.PsqlError("ssh: connection reset", lines[:0])
        return lines

    args = fix.parse_args(["--container", fresh.db, "--apply", "--ids-file", str(ids_file),
                           "--batch-rows", "1", "--chunk", "1"])
    with pytest.raises(fix.PsqlError, match="connection reset"):
        fix.run(args, dropped_after_first_write)
    capsys.readouterr()
    partial = _rows(fresh)
    recorded = fix.read_ids_file(ids_file)
    assert len(recorded) == 1 and partial[recorded[0].observation_id][0] == recorded[0].to_ds
    assert _ds_state(fresh) == _recomputed(fresh)  # the finally block refreshed what the batch touched
    # A run killed while appending leaves a cut-off line; the next run drops it.
    with ids_file.open("a", encoding="utf-8", newline="\n") as out:
        out.write(f"{fresh.ids['a_hum_t2_in_b']},{fresh.ds['B.hum']}")

    assert _script(fresh, "--apply", "--ids-file", str(ids_file)) == 0
    assert "dropping the cut-off last line" in capsys.readouterr().err
    assert _rows(fresh) == _expected_after_apply(fresh, before)
    assert len(fix.distinct_moves(fix.read_ids_file(ids_file))) == 8

    # A run killed before its finally block leaves stale datastream times: the next run refreshes
    # every datastream named in the IDs file first, even with nothing left to move.
    _psql(fresh.db, f'UPDATE "DATASTREAMS" SET "PHENOMENON_TIME_START" = \'2000-01-01Z\', "LAST_FOI_ID" = NULL '
                    f'WHERE "ID" = {fresh.ds["A.stale"]};\n')
    assert _script(fresh, "--apply", "--ids-file", str(ids_file)) == 0
    assert _ds_state(fresh) == _recomputed(fresh)

    assert _script(fresh, "--rollback", "--apply", "--ids-file", str(ids_file)) == 0
    assert _rows(fresh) == before and _ds_state(fresh) == ds_before


def test_refuses_foreign_ids_file_and_busy_server(fresh: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rows = _rows(fresh)
    foreign = tmp_path / "twins.csv"
    foreign.write_text("observation_id,datastream_id\n1,2\n", encoding="utf-8")
    assert _script(fresh, "--apply", "--ids-file", str(foreign)) == fix.EXIT_ERROR
    assert "not an IDs file of this script" in capsys.readouterr().err

    sleeper = subprocess.Popen(
        ["docker", "exec", "-i", fresh.db, "psql", "-X", "-q", "-U", DB_USER, "-d", DB_NAME],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        assert sleeper.stdin is not None
        sleeper.stdin.write("SET application_name = 'cleanup_frost_observations';\nSELECT pg_sleep(60);\n")
        sleeper.stdin.flush()
        _wait("the other script's session", lambda: _psql(fresh.db, "SELECT count(*) FROM pg_stat_activity "
              "WHERE application_name = 'cleanup_frost_observations';\n") == ["1"], 30)
        assert _script(fresh, "--apply", "--ids-file", str(tmp_path / "moves.csv")) == fix.EXIT_REFUSED
        assert "Refusing to write" in capsys.readouterr().out
        assert _rows(fresh) == rows
    finally:
        _psql(fresh.db, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE application_name = 'cleanup_frost_observations';\n")
        sleeper.kill()
        sleeper.wait(timeout=30)


def test_retire_deletes_only_empty_stale_datastreams(fresh: Stack, tmp_path: Path,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    fields = odata_fields_for("v1.1")
    # Before the moves A's stale datastream has rows: nothing of it is deleted.
    assert fix.retire(_http(fresh), "/Things", "/Datastreams", "GEO_", fields, apply=True,
                      only={str(fresh.ds["A.stale"])}) == fix.EXIT_REFUSED
    assert "3 observations; never deleted while it has any" in capsys.readouterr().out
    assert _script(fresh, "--apply", "--ids-file", str(tmp_path / "moves.csv")) == 0
    capsys.readouterr()
    # Read on the v2.0 endpoint as production does (undeclared, so v1.1 conventions), dry run.
    v2 = fresh.url.replace("/v1.1", "/v2.0")
    assert fix.retire(_http(fresh, v2), "/Things", "/Datastreams", "GEO_", fields, apply=False) == fix.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "3 stale ground temperature datastreams" in out and out.count("empty, would be deleted") == 2
    assert fix.retire(_http(fresh, v2), "/Things", "/Datastreams", "GEO_", odata_fields_for("v2.0"),
                      apply=False) == fix.EXIT_REFUSED
    rows = _rows(fresh)
    assert fix.retire(_http(fresh), "/Things", "/Datastreams", "GEO_", fields, apply=True) == fix.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "2 empty, 2 deleted; 1 with observations left alone" in out
    remaining = {int(line) for line in _psql(fresh.db, 'SELECT "ID" FROM "DATASTREAMS";\n')}
    assert fresh.ds["A.stale"] not in remaining and fresh.ds["B.stale"] not in remaining
    assert fresh.ds["C.stale"] in remaining and fresh.ds["A.gst"] in remaining
    assert _rows(fresh) == rows  # no observation went with them
    _restore_retired(fresh)


def test_retirement_holds_back_sources_and_rollback_restores_the_rest(
        fresh: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """After the emptied stale datastreams are retired, the rollback still moves every other row back."""
    before = _rows(fresh)
    ids_file = tmp_path / "moves.csv"
    assert _script(fresh, "--apply", "--ids-file", str(ids_file)) == 0
    after = _rows(fresh)
    recorded = fix.recorded_sources(fix.read_ids_file(ids_file))
    fields = odata_fields_for("v1.1")
    try:
        # The IDs file names A's stale datastream as a source: it is held back. B's held no rows (nothing
        # was moved out of it) and goes; C's keeps a TGV row.
        assert fix.retire(_http(fresh), "/Things", "/Datastreams", "GEO_", fields, apply=True,
                          recorded=recorded) == fix.EXIT_REFUSED
        out = capsys.readouterr().out
        assert "empty, but the source of 3 moves in the IDs file" in out, out
        assert "1 with observations left alone; 1 empty but named as a source in the IDs file left alone" in out
        assert fresh.ds["A.stale"] in _datastream_ids(fresh) and fresh.ds["B.stale"] not in _datastream_ids(fresh)
        # Forced, they go: the point of no return for the three rows moved out of A's.
        assert fix.retire(_http(fresh), "/Things", "/Datastreams", "GEO_", fields, apply=True,
                          recorded=recorded, force=True) == fix.EXIT_REFUSED
        assert "they can no longer be rolled back" in capsys.readouterr().out
        assert not {fresh.ds["A.stale"], fresh.ds["B.stale"]} & _datastream_ids(fresh)
        assert _rows(fresh) == after

        # Rollback, all recorded rows in one batch: the five cross-station moves go back, the three
        # rows of the deleted datastream stay, and nothing fails on the foreign key.
        rollback = ["--rollback", "--ids-file", str(ids_file), "--batch-rows", "100"]
        assert _script(fresh, *rollback) == 0
        out = capsys.readouterr().out
        assert re.search(r"where the move put them: to restore\s+5\n", out), out
        assert re.search(r"where the move put them, source gone: they stay\s+3\n", out), out
        assert _script(fresh, *rollback, "--apply") == 0
        assert "except 3 whose source is gone" in capsys.readouterr().out
        stale_rows = {fresh.ids[k] for k in ("a_gt_t0_stale", "a_gt_t1_stale", "b_gt_t2_in_a_stale")}
        assert _rows(fresh) == {i: (after if i in stale_rows else before)[i] for i in before}
        assert _ds_state(fresh) == _recomputed(fresh)
        # A re-run finds nothing to restore and still succeeds.
        assert _script(fresh, *rollback, "--apply") == 0
        assert "Moved back 0 observations" in capsys.readouterr().out
    finally:
        _restore_retired(fresh)


def _datastream_ids(stack: Stack) -> set[int]:
    return {int(line) for line in _psql(stack.db, 'SELECT "ID" FROM "DATASTREAMS";\n')}


def _restore_retired(stack: Stack) -> None:
    """Put the two deleted stale datastreams back under their ids, for the tests that follow."""
    for key in ("A.stale", "B.stale"):
        station = key.split(".")[0]
        name, *_rest, sensor_id = STATIONS[station]
        _psql(stack.db, f"""
INSERT INTO "DATASTREAMS" ("ID", "NAME", "DESCRIPTION", "PROPERTIES", "OBSERVATION_TYPE", "SENSOR_ID",
                           "OBS_PROPERTY_ID", "THING_ID", "UNIT_NAME", "UNIT_SYMBOL", "UNIT_DEFINITION")
SELECT {stack.ds[key]}, 'GEO_KNMI {name} sensor - Ground temperature', 'x',
       jsonb_build_object('site_key', 'nl', 'thing_name', 'GEO_KNMI {name}', 'sensor_id', '{sensor_id}',
                          'observed_property', 'ground_temperature', 'streamKey', 'ground_temperature'),
       g."OBSERVATION_TYPE", g."SENSOR_ID",
       (SELECT "ID" FROM "OBS_PROPERTIES" WHERE "NAME" = 'Ground temperature'), g."THING_ID",
       g."UNIT_NAME", g."UNIT_SYMBOL", g."UNIT_DEFINITION"
  FROM "DATASTREAMS" g WHERE g."ID" = {stack.ds[f"{station}.gst"]}
   AND NOT EXISTS (SELECT 1 FROM "DATASTREAMS" WHERE "ID" = {stack.ds[key]});
""")
