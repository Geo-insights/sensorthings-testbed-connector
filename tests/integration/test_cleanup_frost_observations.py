"""scripts/cleanup_frost_observations.py against FROST-Server 2.8 on PostgreSQL 16 (PostGIS).

Runs the cleanup's real SQL through its real transport (docker exec + psql) on a throwaway FROST
stack, seeded through the FROST API with entities and observations shaped like the connector's:
every selection's dry run and --apply, conflicts and TGV datastreams left alone, the restore round
trip (byte for byte), the datastream time refresh, an interrupted batch, and the refusal to write
next to another repair script's write session. Containers and network are named lnclean-<random>;
the database runs on tmpfs and publishes no port, FROST only 127.0.0.1. Everything is removed afterwards.

    python -m pytest tests/integration/test_cleanup_frost_observations.py -m integration
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import requests

from scripts import cleanup_frost_observations as cleanup

pytestmark = pytest.mark.integration

DB_IMAGE = "postgis/postgis:16-3.4-alpine"
FROST_IMAGE = "fraunhoferiosb/frost-server:2.8"
DB_USER = DB_NAME = "sensorthings"  # a throwaway database, as in docker-compose.test.yaml
HTTP = requests.Session()  # keep-alive to FROST

SC, SC_B = "sensor_community-93182", "sensor_community-91154"
KNMI, KNMI_OTHER, KNMI_OTHER_WIGOS = "knmi-06344", "knmi-06260", "knmi-0-20000-0-06260"
TGV_OFFICE, TGV_HITTE, LEVELLOG = "tgv-officelab-climate", "tgv-hitteplein-climate-station", "tgv-levellog-abcd1234"
CLIMATICS = "tgv-weather-climatics-rooftop-station"

# Things as the connector registers them: (name, properties, sensor_id, observed properties).
THINGS: dict[str, tuple[str, dict[str, str], str, list[str]]] = {
    "sc": ("SensorCommunity 93182", {"source": "sensor_community", "site": "sensor_community_93182"}, SC,
           ["temperature", "humidity", "pressure", "pm10"]),
    "knmi": ("KNMI 06344", {"source": "knmi_weather", "site": "nl"}, KNMI, ["relative_humidity"]),
    "knmi2": ("KNMI 06260", {"source": "knmi_weather", "site": "nl"}, KNMI_OTHER_WIGOS, ["relative_humidity"]),
    "tgv": ("TGV Office Lab", {"site": "tgv", "campus": "TU Delft", "source": "kafka"}, TGV_OFFICE,
            ["humidity", "temperature"]),
    "levellog": ("Levellog Put 1", {"site": "tgv", "campus": "TU Delft", "source": "levellog"}, LEVELLOG,
                 ["water_level"]),
    "legacy": ("Weather Climatics", {"source": "legacy"}, CLIMATICS, ["air_pressure"]),
}

# label -> (datastream "thing.property", minute, raw JSON result, (sensor_id, observed_property, thing) or None)
SEED: dict[str, tuple[str, int, str, tuple[str, str, str] | None]] = {
    # --tgv-misplaced: TGV-labelled rows in other sources' datastreams (the first and last of sc.temperature)
    "sc_temp_1": ("sc.temperature", 10, "6.0", (SC, "temperature", "SensorCommunity 93182")),
    "sc_temp_2": ("sc.temperature", 20, "6.5", (SC, "temperature", "SensorCommunity 93182")),
    "tgv_in_sc_first": ("sc.temperature", 5, "19.5", (TGV_OFFICE, "temperature", "TGV Office Lab")),
    "tgv_in_sc_mid": ("sc.temperature", 15, "19.6", (TGV_OFFICE, "temperature", "TGV Office Lab")),
    "tgv_in_sc_last": ("sc.temperature", 30, "19.7", (TGV_OFFICE, "temperature", "TGV Office Lab")),
    "sc_hum_1": ("sc.humidity", 10, "81.0", (SC, "humidity", "SensorCommunity 93182")),
    "tgv_in_sc_hum": ("sc.humidity", 14, "45.2", (TGV_OFFICE, "humidity", "TGV Office Lab")),
    "knmi_hum_1": ("knmi.relative_humidity", 10, "88", (KNMI, "relative_humidity", "KNMI 06344")),
    "tgv_in_knmi": ("knmi.relative_humidity", 12, "77.7", (TGV_HITTE, "relative_humidity", "Hitteplein")),
    # left alone: 'tgv-' labels that are not TGV Kafka readings: a Levellog sensor, a location other than 'tgv'
    "levellog_in_knmi": ("knmi.relative_humidity", 13, "-1.25", (LEVELLOG, "water_level", "Levellog Put 1")),
    "tgv_noloc_in_sc": ("sc.humidity", 16, "46.0", (TGV_OFFICE, "humidity", "TGV Office Lab")),
    # left alone: TGV rows of TGV Things, and a TGV-labelled row in a datastream registered for it
    "tgv_own_1": ("tgv.humidity", 10, "45.0", (TGV_OFFICE, "humidity", "TGV Office Lab")),
    "tgv_own_dup_a": ("tgv.humidity", 11, "45.1", (TGV_OFFICE, "humidity", "TGV Office Lab")),
    "tgv_own_dup_b": ("tgv.humidity", 11, "45.1", (TGV_OFFICE, "humidity", "TGV Office Lab")),
    "tgv_listed": ("tgv.humidity", 12, "45.3", (TGV_OFFICE, "humidity", "TGV Office Lab")),
    "levellog_own": ("levellog.water_level", 10, "-1.20", (LEVELLOG, "water_level", "Levellog Put 1")),
    "tgv_own_unprotected": ("legacy.air_pressure", 10, "1012.5", (CLIMATICS, "air_pressure", "Weather Climatics")),
    # --duplicates: exact twins go, every row of a conflict group stays
    "dup1_a": ("sc.pressure", 40, "1013.25", (SC, "pressure", "SensorCommunity 93182")),
    "dup1_b": ("sc.pressure", 40, "1013.25", (SC, "pressure", "SensorCommunity 93182")),
    "dup1_c": ("sc.pressure", 40, "1013.25", (SC, "pressure", "SensorCommunity 93182")),
    "conf2_a": ("sc.pressure", 41, "1013.0", (SC, "pressure", "SensorCommunity 93182")),
    "conf2_b": ("sc.pressure", 41, "1014.0", (SC, "pressure", "SensorCommunity 93182")),
    "conf3_a": ("sc.pressure", 42, "1015.5", (SC, "pressure", "SensorCommunity 93182")),
    "conf3_b": ("sc.pressure", 42, "1015.5", (SC, "pressure", "SensorCommunity 93182")),
    "conf3_c": ("sc.pressure", 42, "1016.5", (SC, "pressure", "SensorCommunity 93182")),
    "conf4_a": ("sc.pressure", 43, "1017.0", (SC, "pressure", "SensorCommunity 93182")),
    "conf4_b": ("sc.pressure", 43, "1017.0", (SC_B, "pressure", "SensorCommunity 91154")),
    "conf5_a": ("sc.pressure", 44, "6.0", (SC, "pressure", "SensorCommunity 93182")),
    "conf5_b": ("sc.pressure", 44, "6", (SC, "pressure", "SensorCommunity 93182")),
    "dup6_a": ("sc.pressure", 45, "1018.0", None),
    "dup6_b": ("sc.pressure", 45, "1018.0", None),
    "conf7_a": ("sc.pressure", 46, '{"a": 1}', (SC, "pressure", "SensorCommunity 93182")),
    "conf7_b": ("sc.pressure", 46, '{"a": 2}', (SC, "pressure", "SensorCommunity 93182")),
    "dup8_a": ("sc.pressure", 47, '{"a": 1}', (SC, "pressure", "SensorCommunity 93182")),
    "dup8_b": ("sc.pressure", 47, '{"a": 1}', (SC, "pressure", "SensorCommunity 93182")),
    # co-located sensors: one sensor_id, told apart by sensor_name only: a conflict, not a duplicate
    "coloc_a": ("sc.pressure", 48, "1019.0", (SC, "pressure", "SensorCommunity 93182")),
    "coloc_b": ("sc.pressure", 48, "1019.0", (SC, "pressure", "SensorCommunity 93182")),
    # no sensor_name on either side: equal
    "noname_a": ("sc.pressure", 49, "1020.0", (SC, "pressure", "SensorCommunity 93182")),
    "noname_b": ("sc.pressure", 49, "1020.0", (SC, "pressure", "SensorCommunity 93182")),
    "knmi_dup_a": ("knmi.relative_humidity", 30, "90", (KNMI, "relative_humidity", "KNMI 06344")),
    "knmi_dup_b": ("knmi.relative_humidity", 30, "90", (KNMI, "relative_humidity", "KNMI 06344")),
    # --misrouted-leftovers: PM10 readings of 93182 in its pressure datastream
    "lo_twin": ("sc.pressure", 50, "12.5", (SC, "pm10", "SensorCommunity 93182")),
    "lo_twin_target": ("sc.pm10", 50, "12.5", (SC, "pm10", "SensorCommunity 93182")),
    "lo_conflict": ("sc.pressure", 51, "13.0", (SC, "pm10", "SensorCommunity 93182")),
    "lo_conflict_target": ("sc.pm10", 51, "14.0", (SC, "pm10", "SensorCommunity 93182")),
    "lo_move": ("sc.pressure", 52, "15.0", (SC, "pm10", "SensorCommunity 93182")),
    # --ids-file: KNMI cross-station rows
    "knmi_cross_1": ("knmi.relative_humidity", 20, "70", (KNMI_OTHER, "relative_humidity", "KNMI 06260")),
    "knmi_cross_2": ("knmi.relative_humidity", 21, "71", (KNMI_OTHER, "relative_humidity", "KNMI 06260")),
    # --require-twin: the copy in the station's own datastream, posted under its WIGOS sensor_id
    "knmi_cross_1_twin": ("knmi2.relative_humidity", 20, "70", (KNMI_OTHER, "relative_humidity", "KNMI 06260")),
    "knmi_cross_3": ("knmi.relative_humidity", 22, "72", (KNMI_OTHER, "relative_humidity", "KNMI 06260")),
    "knmi_cross_3_other": ("knmi2.relative_humidity", 22, "73", (KNMI_OTHER, "relative_humidity", "KNMI 06260")),
}

# PARAMETERS that differ from the defaults _seed derives from the label tuple.
PARAMS: dict[str, dict[str, Any]] = {
    "tgv_noloc_in_sc": {"location": "delft"},
    "coloc_b": {"sensor_name": "SensorCommunity 93182 BME280 sensor"},
    "noname_a": {"sensor_name": None},
    "noname_b": {"sensor_name": None},
    "knmi_cross_1_twin": {"sensor_id": KNMI_OTHER_WIGOS},
    "knmi_cross_3_other": {"sensor_id": KNMI_OTHER_WIGOS},
}

TGV_MISPLACED = {"tgv_in_sc_first", "tgv_in_sc_mid", "tgv_in_sc_last", "tgv_in_sc_hum", "tgv_in_knmi"}
DUPLICATES = {"dup1_b", "dup1_c", "dup6_b", "dup8_b", "noname_b", "knmi_dup_b"}
LEFTOVERS = {"lo_twin"}


@dataclass
class Stack:
    db: str  # database container
    url: str  # FROST v1.1 root
    ids: dict[str, int]  # observation ID per SEED label
    datastreams: dict[str, int]  # "thing.property" -> datastream ID


def _docker(*args: str, input: str | None = None, check: bool = True, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=check
    )


def _psql(db: str, sql: str) -> list[str]:
    proc = _docker(
        "exec", "-i", db, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", DB_USER, "-d", DB_NAME,
        input="SET TimeZone = 'UTC';\nSET extra_float_digits = 3;\n" + sql,
    )
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


def _post(url: str, path: str, body: dict[str, Any] | str) -> int:
    data = body if isinstance(body, str) else json.dumps(body)
    response = HTTP.post(f"{url}/{path}", data=data, headers={"Content-Type": "application/json"}, timeout=60)
    assert response.status_code == 201, response.text
    match = re.search(r"\((\d+)\)$", response.headers["Location"])
    assert match is not None
    return int(match[1])


def _seed(url: str) -> tuple[dict[str, int], dict[str, int]]:
    datastreams: dict[str, int] = {}
    for key, (name, properties, sensor_id, props) in THINGS.items():
        thing = _post(url, "Things", {
            "name": f"GEO_{name}", "description": "-", "properties": properties,
            "Locations": [{"name": f"GEO_{name} location", "description": "-", "encodingType": "application/geo+json",
                           "location": {"type": "Point", "coordinates": [4.37 + len(datastreams) / 100, 52.0]}}],
            "Datastreams": [{
                "name": f"GEO_{name} sensor - {prop}", "description": "-",
                "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                "unitOfMeasurement": {"name": prop, "symbol": "1", "definition": "-"},
                "properties": {"sensor_id": sensor_id, "observed_property": prop, "streamKey": prop},
                "Sensor": {"name": f"GEO_{name} {prop} sensor", "description": "-", "encodingType": "text/plain",
                           "metadata": "-"},
                "ObservedProperty": {"name": prop, "definition": "-", "description": "-"},
            } for prop in props],
        })
        listing = HTTP.get(f"{url}/Things({thing})/Datastreams", params={"$select": "id,properties"}, timeout=60).json()
        for ds in listing["value"]:
            datastreams[f"{key}.{ds['properties']['observed_property']}"] = ds["@iot.id"]
    ids = {}
    for label, (stream, minute, raw, tag) in SEED.items():
        body: dict[str, Any] = {
            "phenomenonTime": f"2026-11-12T14:{minute:02d}:27.788Z",
            "result": "__RESULT__",
            "resultQuality": ["good"],
            "Datastream": {"@iot.id": datastreams[stream]},
        }
        if tag is not None:  # as SensorThingsClient.push_observations sends them
            sensor_id, prop, thing_name = tag
            body["parameters"] = {
                "sensor_id": sensor_id, "sensor_name": f"{thing_name} sensor", "observed_property": prop,
                "observed_property_name": prop, "unit": "1", "thing_name": thing_name,
                "location": "tgv" if sensor_id.startswith("tgv-") else sensor_id.rsplit("-", 1)[-1],
                "device_eui": None, "stream_key": None, **PARAMS.get(label, {}),
            }
        ids[label] = _post(url, "Observations", json.dumps(body).replace('"__RESULT__"', raw))
    return ids, datastreams


SNAPSHOT_SQL = """
CREATE INDEX "OBS_DS_PHENTIME" ON "OBSERVATIONS" ("DATASTREAM_ID", "PHENOMENON_TIME_START" DESC);
CREATE TABLE lnclean_obs AS SELECT * FROM "OBSERVATIONS";
CREATE TABLE lnclean_ds AS
    SELECT "ID", "PHENOMENON_TIME_START", "PHENOMENON_TIME_END", "RESULT_TIME_START", "RESULT_TIME_END" FROM "DATASTREAMS";
"""
RESET_SQL = """
DELETE FROM "OBSERVATIONS" o WHERE NOT EXISTS (SELECT 1 FROM lnclean_obs s WHERE s."ID" = o."ID");
INSERT INTO "OBSERVATIONS" OVERRIDING SYSTEM VALUE SELECT * FROM lnclean_obs ON CONFLICT DO NOTHING;
UPDATE "DATASTREAMS" d
   SET "PHENOMENON_TIME_START" = s."PHENOMENON_TIME_START", "PHENOMENON_TIME_END" = s."PHENOMENON_TIME_END",
       "RESULT_TIME_START" = s."RESULT_TIME_START", "RESULT_TIME_END" = s."RESULT_TIME_END"
  FROM lnclean_ds s WHERE d."ID" = s."ID";
"""


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    try:
        _docker("version", timeout=30)
    except (FileNotFoundError, subprocess.SubprocessError):
        pytest.skip("Docker is not available")
    name = f"lnclean-{uuid.uuid4().hex[:8]}"
    db, frost, network = f"{name}-db", f"{name}-frost", f"{name}-net"
    port = _free_port()
    try:
        _docker("network", "create", network)
        _docker(
            "run", "-d", "--name", db, "--network", network,
            # in memory: no anonymous volume for the image's VOLUME to leave behind
            "--mount", "type=tmpfs,destination=/var/lib/postgresql/data",
            "-e", f"POSTGRES_DB={DB_NAME}", "-e", f"POSTGRES_USER={DB_USER}", "-e", "POSTGRES_PASSWORD=sensorthings",
            DB_IMAGE,
        )
        # Over TCP: the image's init phase runs a socket-only server first.
        _wait("PostGIS", lambda: _docker("exec", db, "pg_isready", "-h", "127.0.0.1", "-U", DB_USER, check=False)
              .returncode == 0, 120)
        _docker(
            "run", "-d", "--name", frost, "--network", network, "-p", f"127.0.0.1:{port}:8080",
            "-e", "JAVA_OPTS=-Xmx768m",
            "-e", f"serviceRootUrl=http://127.0.0.1:{port}/FROST-Server",
            "-e", "persistence_db_driver=org.postgresql.Driver",
            "-e", f"persistence_db_url=jdbc:postgresql://{db}:5432/{DB_NAME}",
            "-e", f"persistence_db_username={DB_USER}", "-e", "persistence_db_password=sensorthings",
            "-e", "persistence_autoUpdateDatabase=true",
            FROST_IMAGE,
        )
        url = f"http://127.0.0.1:{port}/FROST-Server/v1.1"  # not localhost: IPv6 first is slow on Windows
        _wait("FROST", lambda: HTTP.get(f"{url}/Things", timeout=10).status_code == 200, 300)
        ids, datastreams = _seed(url)
        _psql(db, SNAPSHOT_SQL)
        yield Stack(db, url, ids, datastreams)
    finally:
        _docker("rm", "-f", "-v", frost, db, check=False)
        _docker("network", "rm", network, check=False)


def _wait_until_no_script_session(db: str, timeout: float = 60) -> None:
    sql = f"SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE '{cleanup.APP_NAME}%';\n"
    _wait("no session of the script", lambda: _psql(db, sql) == ["0"], timeout)


@pytest.fixture()
def fresh(stack: Stack) -> Stack:
    """The seeded rows and datastream times back, and no session of the script left over."""
    _wait_until_no_script_session(stack.db)
    _psql(stack.db, RESET_SQL)
    return stack


def _rows(stack: Stack) -> dict[int, str]:
    """row_to_json of every observation, by ID: what the round trip must bring back byte for byte."""
    lines = _psql(stack.db, 'SELECT "ID", row_to_json(o)::text FROM "OBSERVATIONS" o ORDER BY 1;\n')
    return {int(i): row for i, row in (line.split("|", 1) for line in lines)}


def _times(stack: Stack) -> dict[int, tuple[str, ...]]:
    """phenomenonTime and resultTime extents per datastream."""
    lines = _psql(stack.db, 'SELECT "ID", "PHENOMENON_TIME_START", "PHENOMENON_TIME_END", "RESULT_TIME_START", '
                            '"RESULT_TIME_END" FROM "DATASTREAMS" ORDER BY 1;\n')
    return {int(f[0]): tuple(f[1:]) for f in (line.split("|") for line in lines)}


def _labels(stack: Stack, ids: set[int]) -> set[str]:
    return {label for label, obs_id in stack.ids.items() if obs_id in ids}


def _script(stack: Stack, *args: str) -> int:
    return cleanup.main(["--container", stack.db, *args])


def _archived(archive: Path) -> dict[int, str]:
    """Observation ID -> archived row text."""
    rows = {}
    for line in archive.read_text(encoding="utf-8").splitlines()[1:]:
        if line.startswith('{"sel":'):
            row = cleanup.parse_row_line(line)
            rows[row.id] = row.row
    return rows


def _api(stack: Stack, labels: set[str]) -> dict[str, Any]:
    return {label: HTTP.get(f"{stack.url}/Observations({stack.ids[label]})", timeout=60).json() for label in labels}


def _round_trip(stack: Stack, archive: Path, seeded: dict[int, str], deleted: set[str],
                capsys: pytest.CaptureFixture[str]) -> None:
    """--restore: a dry run, then everything back exactly; a second restore has nothing to do."""
    gone = _rows(stack)
    assert _script(stack, "--restore", "--archive", str(archive)) == 0
    out = capsys.readouterr().out
    assert _rows(stack) == gone
    assert re.search(rf"absent: to restore\s+{len(deleted)}\n", out)
    assert _script(stack, "--restore", "--archive", str(archive), "--apply") == 0
    out = capsys.readouterr().out
    assert f"Restored {len(deleted)} observations (planned {len(deleted)})." in out
    assert "every restorable row is back, byte for byte" in out
    assert _rows(stack) == seeded
    assert _script(stack, "--restore", "--archive", str(archive), "--apply") == 0
    assert "Restored 0 observations (planned 0)." in capsys.readouterr().out


def test_dry_runs_write_nothing(fresh: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seeded, times = _rows(fresh), _times(fresh)
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text(f"{fresh.ids['knmi_cross_1']}\n", encoding="utf-8")
    archive = tmp_path / "never.jsonl"
    for selection in (["--tgv-misplaced"], ["--duplicates"], ["--misrouted-leftovers"], ["--ids-file", str(ids_file)],
                      ["--ids-file", str(ids_file), "--require-twin"],
                      ["--tgv-misplaced", "--datastream", str(fresh.datastreams["sc.temperature"])]):
        assert _script(fresh, *selection, "--archive", str(archive)) == 0
        assert "Dry run: nothing was written." in capsys.readouterr().out
        assert _rows(fresh) == seeded
        assert _times(fresh) == times
    assert not archive.exists()


def test_read_only_session_cannot_delete(fresh: Stack) -> None:
    run_sql = cleanup.pressure.psql_runner(cleanup.pressure.psql_argv(container=fresh.db), timeout=120)
    with pytest.raises(cleanup.PsqlError, match="read-only transaction"):
        run_sql(cleanup.READ_ONLY_SESSION + f'DELETE FROM "OBSERVATIONS" WHERE "ID" = {fresh.ids["tgv_in_knmi"]};\n')


def test_tgv_misplaced_apply_refresh_and_restore(fresh: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seeded = _rows(fresh)
    api_before = _api(fresh, {"tgv_in_sc_first", "tgv_in_knmi"})
    sc_temp, knmi = fresh.datastreams["sc.temperature"], fresh.datastreams["knmi.relative_humidity"]
    untouched = fresh.datastreams["sc.pm10"]
    # Stale resultTime extents: FROST's delete trigger never fixes these (the rows have no resultTime).
    _psql(fresh.db, f"""UPDATE "DATASTREAMS" SET "RESULT_TIME_START" = '2000-01-01T00:00:00Z',
                        "RESULT_TIME_END" = '2000-01-02T00:00:00Z' WHERE "ID" IN ({sc_temp}, {knmi}, {untouched});""")
    archive = tmp_path / "tgv.jsonl"

    assert _script(fresh, "--tgv-misplaced") == 0
    out = capsys.readouterr().out
    assert re.search(r"to delete: 5 in 3 datastreams", out)
    assert re.search(r"left alone, in datastreams of TGV Things: 5\n", out)  # 4 TGV rows + the Levellog one
    assert re.search(r"left alone, in a datastream registered for their own sensor_id: 1\n", out)
    assert re.search(r"left alone, not a TGV Kafka label \(a Levellog sensor, or location not 'tgv'\): 2\n", out)

    # Canary: one datastream only.
    assert _script(fresh, "--tgv-misplaced", "--apply", "--archive", str(archive), "--datastream", str(knmi)) == 0
    assert _labels(fresh, set(seeded) - set(_rows(fresh))) == {"tgv_in_knmi"}  # not the Levellog row next to it
    capsys.readouterr()

    assert _script(fresh, "--tgv-misplaced", "--apply", "--archive", str(archive), "--batch-rows", "2") == 0
    out = capsys.readouterr().out
    assert "Verification: no rows left in the selection, as of now." in out
    after = _rows(fresh)
    deleted = set(seeded) - set(after)
    assert _labels(fresh, deleted) == TGV_MISPLACED
    assert all(after[i] == seeded[i] for i in after)  # every other row exactly as it was
    assert _archived(archive) == {i: seeded[i] for i in deleted}  # the archive holds exactly what was deleted

    times = _times(fresh)
    assert times[sc_temp] == ("2026-11-12 14:10:27.788+00", "2026-11-12 14:20:27.788+00", "", "")  # refreshed
    assert times[knmi][2:] == ("", "")
    assert times[untouched][2:] == ("2000-01-01 00:00:00+00", "2000-01-02 00:00:00+00")  # not touched, not refreshed
    lines = archive.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["datastreams"] == sorted({sc_temp, fresh.datastreams["sc.humidity"]})

    _round_trip(fresh, archive, seeded, TGV_MISPLACED, capsys)
    assert _api(fresh, {"tgv_in_sc_first", "tgv_in_knmi"}) == api_before  # as FROST serves them
    assert _times(fresh)[sc_temp][:2] == ("2026-11-12 14:05:27.788+00", "2026-11-12 14:30:27.788+00")


def test_duplicates_keep_the_lowest_id_and_leave_conflicts(fresh: Stack, tmp_path: Path,
                                                          capsys: pytest.CaptureFixture[str]) -> None:
    seeded = _rows(fresh)
    archive = tmp_path / "duplicates.jsonl"

    assert _script(fresh, "--duplicates", "--chunk-rows", "5") == 0
    out = capsys.readouterr().out
    assert re.search(r"exact duplicates to delete: 6 in 2 datastreams", out)
    # conflicts: 41 (2 rows), 42 (3), 43 (2), 44 (2), 46 (2), 48 (2: sensor_name); the twin at 42 stays as well
    assert re.search(r"conflicts \(same datastream and phenomenonTime, another value or label\): 6 times, 13 rows", out)
    assert re.search(r"exact twins of another row of their group \(left as well\): 1\n", out)
    assert "left alone: 2 datastreams of TGV Things (5 rows)" in out  # with the twins at minute 11

    assert _script(fresh, "--duplicates", "--apply", "--archive", str(archive), "--chunk-rows", "5") == 0
    after = _rows(fresh)
    deleted = set(seeded) - set(after)
    assert _labels(fresh, deleted) == DUPLICATES
    assert _archived(archive) == {i: seeded[i] for i in deleted}
    assert "Verification: no rows left in the selection, as of now." in capsys.readouterr().out

    _round_trip(fresh, archive, seeded, DUPLICATES, capsys)


def test_misrouted_leftovers(fresh: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seeded = _rows(fresh)
    archive = tmp_path / "leftovers.jsonl"

    assert _script(fresh, "--misrouted-leftovers") == 0
    out = capsys.readouterr().out
    assert re.search(r"the same reading \(label, phenomenon time and result\) is in the target: 1: to delete", out)
    assert re.search(r"another reading at that time in the target \(conflict\): 1: left", out)

    assert _script(fresh, "--misrouted-leftovers", "--apply", "--archive", str(archive)) == 0
    deleted = set(seeded) - set(_rows(fresh))
    assert _labels(fresh, deleted) == LEFTOVERS
    archived = [cleanup.parse_row_line(line) for line in archive.read_text(encoding="utf-8").splitlines()
                if line.startswith('{"sel":')]
    assert [(r.id, r.target) for r in archived] == [(fresh.ids["lo_twin"], fresh.datastreams["sc.pm10"])]
    capsys.readouterr()

    _round_trip(fresh, archive, seeded, LEFTOVERS, capsys)


def test_ids_file(fresh: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seeded = _rows(fresh)
    ids, ds = fresh.ids, fresh.datastreams
    ids_file = tmp_path / "knmi_cross_station.csv"
    ids_file.write_text(
        "observation_id,datastream_id\n"
        f"{ids['knmi_cross_1']},{ds['knmi.relative_humidity']}\n"
        f"{ids['knmi_cross_2']},{ds['sc.humidity']}\n"  # not (or no longer) there: left alone
        f"{ids['tgv_listed']},{ds['tgv.humidity']}\n"  # a TGV datastream: never touched
        "999999999,\n",
        encoding="utf-8",
    )
    archive = tmp_path / "knmi.jsonl"

    assert _script(fresh, "--ids-file", str(ids_file), "--apply", "--archive", str(archive)) == 0
    out = capsys.readouterr().out
    assert "found: 3; not found (deleted already?): 1" in out
    assert "left alone, in datastreams of TGV Things: 1" in out
    assert "left alone, no longer in their listed datastream: 1" in out
    deleted = set(seeded) - set(_rows(fresh))
    assert _labels(fresh, deleted) == {"knmi_cross_1"}

    _round_trip(fresh, archive, seeded, {"knmi_cross_1"}, capsys)


def test_ids_file_require_twin(fresh: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seeded = _rows(fresh)
    ids, ds = fresh.ids, fresh.datastreams
    knmi, knmi2 = ds["knmi.relative_humidity"], ds["knmi2.relative_humidity"]
    ids_file = tmp_path / "knmi_twins.csv"
    ids_file.write_text(
        "observation_id,datastream_id\n"
        f"{ids['knmi_cross_1']},{knmi}\n"  # its copy is in its station's datastream (another sensor_id): deleted
        f"{ids['knmi_cross_1_twin']},{knmi2}\n"  # that copy: KNMI 06344 is not its Thing nor its label's: stays
        f"{ids['knmi_cross_3']},{knmi}\n"  # another value at that time in its station's datastream: stays
        f"{ids['knmi_dup_a']},{knmi}\n"  # listed together, each the other's copy: the lower ID stays
        f"{ids['knmi_dup_b']},{knmi}\n",
        encoding="utf-8",
    )
    archive = tmp_path / "knmi.jsonl"

    assert _script(fresh, "--ids-file", str(ids_file), "--require-twin") == 0
    out = capsys.readouterr().out
    assert "to delete: 2\n" in out
    assert "left alone, no other row holds the same reading (--require-twin): 3" in out

    assert _script(fresh, "--ids-file", str(ids_file), "--require-twin", "--apply", "--archive", str(archive)) == 0
    assert "Verification: no rows left in the selection, as of now." in capsys.readouterr().out
    deleted = set(seeded) - set(_rows(fresh))
    assert _labels(fresh, deleted) == {"knmi_cross_1", "knmi_dup_b"}
    assert _archived(archive) == {i: seeded[i] for i in deleted}

    _round_trip(fresh, archive, seeded, {"knmi_cross_1", "knmi_dup_b"}, capsys)


def test_require_twin_rechecks_the_copy_in_the_delete(fresh: Stack) -> None:
    """The copy disappears between the read (and archive) and the DELETE: the listed row stays."""
    run_sql = cleanup.pressure.psql_runner(cleanup.pressure.psql_argv(container=fresh.db), timeout=120)
    knmi, obs_id = fresh.datastreams["knmi.relative_humidity"], fresh.ids["knmi_cross_1"]
    twin = cleanup.twin_sql("o", "GEO_")
    cands = cleanup.parse_candidates(
        run_sql(cleanup.READ_ONLY_SESSION + cleanup.ids_read_sql([(obs_id, knmi)], set(), twin)))
    assert [c.id for c in cands] == [obs_id]

    _psql(fresh.db, f'DELETE FROM "OBSERVATIONS" WHERE "ID" = {fresh.ids["knmi_cross_1_twin"]};\n')
    lines = run_sql(cleanup.WRITE_SESSION + cleanup.delete_sql(knmi, cands, twin))
    assert cleanup.parse_counts(lines, "deleted") == [(knmi, 1, 0)]
    assert obs_id in _rows(fresh)

    _psql(fresh.db, RESET_SQL)  # the copy is back
    lines = run_sql(cleanup.WRITE_SESSION + cleanup.delete_sql(knmi, cands, twin))
    assert cleanup.parse_counts(lines, "deleted") == [(knmi, 1, 1)]
    assert obs_id not in _rows(fresh)


class _RowLock:
    """Holds a row lock from another session until released, like a slow concurrent writer."""

    def __init__(self, db: str, obs_id: int) -> None:
        self.proc = subprocess.Popen(
            ["docker", "exec", "-i", db, "psql", "-X", "-q", "-U", DB_USER, "-d", DB_NAME],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        assert self.proc.stdin is not None
        self.proc.stdin.write(
            "SET application_name = 'lnclean_test_lock';\nBEGIN;\n"
            f'SELECT 1 FROM "OBSERVATIONS" WHERE "ID" = {obs_id} FOR UPDATE;\n'
        )
        self.proc.stdin.flush()
        held = ("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'lnclean_test_lock' "
                "AND state = 'idle in transaction';\n")
        _wait("the row lock", lambda: _psql(db, held) == ["1"], 60)

    def release(self) -> None:
        if self.proc.poll() is None:
            assert self.proc.stdin is not None
            self.proc.stdin.write("COMMIT;\n")
            self.proc.stdin.close()
            self.proc.wait(timeout=60)


def test_interrupted_batch_deletes_nothing_unarchived(fresh: Stack, tmp_path: Path,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    """The client gives up mid-session (its timeout; Ctrl-C or a dropped SSH connection alike) while psql
    carries on in the container and finishes the session's DELETEs once a lock is gone."""
    seeded = _rows(fresh)
    knmi = fresh.datastreams["knmi.relative_humidity"]
    _psql(fresh.db, f"""UPDATE "DATASTREAMS" SET "RESULT_TIME_END" = '2000-01-02T00:00:00Z' WHERE "ID" = {knmi};""")
    archive = tmp_path / "tgv.jsonl"
    lock = _RowLock(fresh.db, fresh.ids["tgv_in_knmi"])
    try:
        assert _script(fresh, "--tgv-misplaced", "--apply", "--archive", str(archive), "--batch-rows", "1",
                       "--timeout", "4") == 1
    finally:
        lock.release()  # well within the session's lock_timeout: the blocked DELETE goes on
    _wait_until_no_script_session(fresh.db)
    assert "is already in" in capsys.readouterr().err

    deleted = set(seeded) - set(_rows(fresh))
    assert "tgv_in_knmi" in _labels(fresh, deleted)  # finished on the server after the client gave up
    assert deleted <= set(_archived(archive))
    assert _times(fresh)[knmi][3] == "2000-01-02 00:00:00+00"  # no refresh yet: the run did not get that far

    # The same command finishes the job, including the refresh the interrupted run left.
    assert _script(fresh, "--tgv-misplaced", "--apply", "--archive", str(archive)) == 0
    assert _labels(fresh, set(seeded) - set(_rows(fresh))) == TGV_MISPLACED
    assert _times(fresh)[knmi][3] == ""
    capsys.readouterr()
    _round_trip(fresh, archive, seeded, TGV_MISPLACED, capsys)


def test_refuses_to_write_next_to_another_repair_script(fresh: Stack, tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    seeded = _rows(fresh)
    other = subprocess.Popen(
        ["docker", "exec", "-i", fresh.db, "psql", "-X", "-q", "-U", DB_USER, "-d", DB_NAME, "-c",
         "SET application_name = 'fix_sc_pressure_units'; SELECT pg_sleep(60);"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        busy = "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'fix_sc_pressure_units';\n"
        _wait("the other session", lambda: _psql(fresh.db, busy) == ["1"], 60)
        assert _script(fresh, "--tgv-misplaced", "--apply", "--archive", str(tmp_path / "tgv.jsonl")) == 3
        assert "Refusing to write" in capsys.readouterr().out
        assert _rows(fresh) == seeded
    finally:
        _psql(fresh.db, "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE application_name = 'fix_sc_pressure_units';\n")
        other.wait(timeout=60)
