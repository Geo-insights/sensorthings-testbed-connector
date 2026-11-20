"""scripts/fix_sc_pressure_units.py against FROST-Server 2.8 on PostgreSQL 16 (PostGIS).

Runs the backfill's real SQL through its real transport (docker exec + psql) on a
throwaway FROST stack, seeded through the FROST API with observations shaped like the
connector's: the dry run, --apply, an interrupted batch, the re-run, and --rollback.
Each run gets its own container and network names; the database publishes no port,
FROST only on 127.0.0.1. Everything is removed afterwards.

    python -m pytest tests/integration/test_sc_pressure_backfill.py -m integration
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
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import requests

from scripts import fix_sc_pressure_units as backfill

pytestmark = pytest.mark.integration

DB_IMAGE = "postgis/postgis:16-3.4-alpine"
FROST_IMAGE = "fraunhoferiosb/frost-server:2.8"
DB_USER = DB_NAME = "sensorthings"  # a throwaway database, as in docker-compose.test.yaml
HTTP = requests.Session()  # keep-alive to FROST

# label -> (datastream, raw JSON result, observed_property in PARAMETERS or None for no
# PARAMETERS, or "{}" for empty ones). The raw JSON keeps the text FROST stores as is.
SEED: dict[str, tuple[str, str, str | None]] = {
    "pa": ("A", "100391.86", "pressure"),
    "pa_decimals": ("A", "99954.35", "pressure"),
    "pa_dot_zero": ("A", "100199.0", "pressure"),
    "pa_exponent": ("A", "1.0E+5", "pressure"),
    "edge_low": ("A", "2000", "pressure"),
    "edge_low_in": ("A", "2000.01", "pressure"),
    "edge_high_in": ("A", "199999.99", "pressure"),
    "edge_high": ("A", "200000", "pressure"),
    "hpa": ("A", "1013.25", "pressure"),
    "negative": ("A", "-21526.13", "pressure"),
    "pm10_tagged": ("A", "2500.0", "pm10"),
    "humidity_tagged": ("A", "72.3", "humidity"),
    "no_parameters": ("A", "101000", None),
    "empty_parameters": ("A", "101100", "{}"),
    "string_result": ("A", '"100391.86"', "pressure"),
    "json_result": ("A", '{"value": 100391.86}', "pressure"),
    **{f"b{k}": ("B", f"{100000 + 1.5 * k}", "pressure") for k in range(12)},
    "pm10_stream": ("PM10", "2500.0", "pressure"),  # not an Air pressure datastream
    "unit_pa": ("PA_UNIT", "100391.86", "pressure"),  # Air pressure, but labelled Pa
    "like_decoy": ("LIKE_DECOY", "100391.86", "pressure"),  # Thing 'GEOxSensorCommunity ...'
    "other_source": ("OTHER_SOURCE", "100391.86", "pressure"),  # PROPERTIES.source is not ours
}

def _hpa(raw: str) -> str:
    """raw Pa / 100 as an exact decimal without trailing zeros, as trim_scale() prints it."""
    text = format(Decimal(raw) * Decimal("0.01"), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


# What --apply writes: (RESULT_NUMBER as PostgreSQL prints it, RESULT_STRING).
CONVERTED = {
    "pa": ("1003.9186", "1003.9186"),
    "pa_decimals": ("999.5435", "999.5435"),
    "pa_dot_zero": ("1001.99", "1001.99"),
    "pa_exponent": ("1000", "1000"),
    "edge_low_in": ("20.0001", "20.0001"),
    "edge_high_in": ("1999.9999", "1999.9999"),
    "no_parameters": ("1010", "1010"),
    "empty_parameters": ("1011", "1011"),
    **{f"b{k}": (_hpa(SEED[f"b{k}"][1]), _hpa(SEED[f"b{k}"][1])) for k in range(12)},
}


@dataclass
class Stack:
    db: str  # database container
    url: str  # FROST v1.1 root
    ids: dict[str, int]  # observation ID per SEED label
    datastreams: dict[str, int]


def _docker(*args: str, input: str | None = None, check: bool = True, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=check
    )


def _psql(db: str, sql: str) -> list[str]:
    proc = _docker(
        "exec", "-i", db, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", DB_USER, "-d", DB_NAME,
        input=sql,
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
    pressure = _post(url, "ObservedProperties", {
        "name": "Air pressure", "description": "Atmospheric air pressure at sensor location.",
        "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_pressure",
    })
    pm10 = _post(url, "ObservedProperties", {
        "name": "PM10 concentration", "description": "Particulate matter concentration (PM10) in ambient air.",
        "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html",
    })
    sensor = _post(url, "Sensors", {
        "name": "SensorCommunity test sensor", "description": "test", "encodingType": "application/json",
        "metadata": "https://sensor.community",
    })

    def thing(name: str, source: str) -> int:
        return _post(url, "Things", {
            "name": name, "description": "test", "properties": {"source": source, "network": "Sensor.Community"},
            "Locations": [{
                "name": f"{name} location", "description": "test", "encodingType": "application/geo+json",
                "location": {"type": "Point", "coordinates": [4.36, 52.01]},
            }],
        })

    def datastream(thing_id: int, prop: int, name: str, symbol: str) -> int:
        return _post(url, "Datastreams", {
            "name": name, "description": "test",
            "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
            "unitOfMeasurement": {"name": symbol, "symbol": symbol, "definition": "https://ucum.org"},
            "Thing": {"@iot.id": thing_id}, "Sensor": {"@iot.id": sensor}, "ObservedProperty": {"@iot.id": prop},
        })

    t1001 = thing("GEO_SensorCommunity 1001", "sensor_community")
    datastreams = {
        "A": datastream(t1001, pressure, "GEO_SensorCommunity 1001 sensor - Air pressure", "hPa"),
        "B": datastream(thing("GEO_SensorCommunity 1002", "sensor_community"), pressure,
                        "GEO_SensorCommunity 1002 sensor - Air pressure", "hPa"),
        "PM10": datastream(t1001, pm10, "GEO_SensorCommunity 1001 sensor - PM10 concentration", "ug/m3"),
        "PA_UNIT": datastream(thing("GEO_SensorCommunity 1003", "sensor_community"), pressure,
                              "GEO_SensorCommunity 1003 sensor - Air pressure", "Pa"),
        "LIKE_DECOY": datastream(thing("GEOxSensorCommunity 1004", "sensor_community"), pressure,
                                 "GEOxSensorCommunity 1004 sensor - Air pressure", "hPa"),
        "OTHER_SOURCE": datastream(thing("GEO_SensorCommunity 1005", "other"), pressure,
                                   "GEO_SensorCommunity 1005 sensor - Air pressure", "hPa"),
    }
    ids = {}
    for n, (label, (stream, raw, prop)) in enumerate(SEED.items()):
        body: dict[str, Any] = {
            "phenomenonTime": f"2026-11-{1 + n // 24:02d}T{n % 24:02d}:00:00Z",
            "result": "__RESULT__",
            "resultQuality": ["good"],
            "Datastream": {"@iot.id": datastreams[stream]},
        }
        if prop == "{}":
            body["parameters"] = {}
        elif prop is not None:  # as SensorThingsClient.push_observations sends them
            body["parameters"] = {
                "sensor_id": "sensor_community-1001", "sensor_name": "SensorCommunity 1001 BME280 sensor",
                "observed_property": prop, "unit": "hPa", "location": "1001", "thing_name": "SensorCommunity 1001",
                "device_eui": None, "stream_key": None,
            }
        ids[label] = _post(url, "Observations", json.dumps(body).replace('"__RESULT__"', raw))
    return ids, datastreams


SNAPSHOT_SQL = (
    'CREATE TABLE sc_test_snapshot AS SELECT "ID", "RESULT_NUMBER", "RESULT_STRING" FROM "OBSERVATIONS";\n'
)
RESET_SQL = """
UPDATE "OBSERVATIONS" o SET "RESULT_NUMBER" = s."RESULT_NUMBER", "RESULT_STRING" = s."RESULT_STRING"
  FROM sc_test_snapshot s
 WHERE o."ID" = s."ID"
   AND (o."RESULT_NUMBER" IS DISTINCT FROM s."RESULT_NUMBER" OR o."RESULT_STRING" IS DISTINCT FROM s."RESULT_STRING");
"""


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    try:
        _docker("version", timeout=30)
    except (FileNotFoundError, subprocess.SubprocessError):
        pytest.skip("Docker is not available")
    name = f"scpressure-{uuid.uuid4().hex[:8]}"
    db, frost, network = f"{name}-db", f"{name}-frost", f"{name}-net"
    port = _free_port()
    try:
        _docker("network", "create", network)
        _docker(
            "run", "-d", "--name", db, "--network", network,
            "-e", f"POSTGRES_DB={DB_NAME}", "-e", f"POSTGRES_USER={DB_USER}", "-e", "POSTGRES_PASSWORD=sensorthings",
            DB_IMAGE,
        )
        # Over TCP: the image's init phase runs a socket-only server first.
        _wait("PostGIS", lambda: _docker("exec", db, "pg_isready", "-h", "127.0.0.1", "-U", DB_USER, check=False)
              .returncode == 0, 120)
        _docker(
            "run", "-d", "--name", frost, "--network", network, "-p", f"127.0.0.1:{port}:8080",
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
        _docker("rm", "-f", frost, db, check=False)
        _docker("network", "rm", network, check=False)


@pytest.fixture()
def fresh(stack: Stack) -> Stack:
    """The seeded values back, and no session of the script left over."""
    _wait_until_no_script_session(stack.db)
    _psql(stack.db, RESET_SQL)
    return stack


def _wait_until_no_script_session(db: str, timeout: float = 60) -> None:
    sql = f"SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE '{backfill.WRITE_APP_NAME}%';\n"
    _wait("no session of the script", lambda: _psql(db, sql) == ["0"], timeout)


def _state(stack: Stack) -> dict[str, tuple[str | None, str | None]]:
    """(RESULT_NUMBER as PostgreSQL prints it, RESULT_STRING) per SEED label."""
    lines = _psql(
        stack.db,
        'SELECT "ID", coalesce(to_json("RESULT_NUMBER")::text, \'null\'), '
        'coalesce(to_json("RESULT_STRING")::text, \'null\') FROM "OBSERVATIONS";\n',
    )
    by_id = {}
    for line in lines:
        obs_id, number, text = line.split("|", 2)
        by_id[int(obs_id)] = (None if number == "null" else number, json.loads(text))
    return {label: by_id[obs_id] for label, obs_id in stack.ids.items()}


def _api_results(stack: Stack, labels: list[str]) -> dict[str, str]:
    """The result of each observation exactly as FROST serves it (the JSON text)."""
    results = {}
    for label in labels:
        response = HTTP.get(f"{stack.url}/Observations({stack.ids[label]})", params={"$select": "result"}, timeout=60)
        response.raise_for_status()
        match = re.fullmatch(r'\{"result":(.*)\}', response.text.strip(), re.S)
        assert match is not None, response.text
        results[label] = match[1]
    return results


def _script(stack: Stack, *args: str) -> int:
    return backfill.main(["--container", stack.db, *args])


def _recorded(ids_file: Path) -> set[int]:
    """Observation IDs in the IDs file, from its first two columns."""
    lines = ids_file.read_text(encoding="utf-8").splitlines()[1:]
    return {int(line.split(",")[1]) for line in lines if line}


def test_read_only_session_cannot_write(fresh: Stack) -> None:
    run_sql = backfill.psql_runner(backfill.psql_argv(container=fresh.db), timeout=120)
    sql = backfill.READ_ONLY_SESSION + backfill.update_sql(fresh.datastreams["A"], [fresh.ids["pa"]])
    with pytest.raises(backfill.PsqlError, match="read-only transaction"):
        run_sql(sql)
    assert _state(fresh)["pa"] == ("100391.86", "100391.86")


def test_dry_run_apply_rerun_and_rollback(fresh: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seeded = _state(fresh)
    # What FROST stored as sent: the RESULT_STRING the rollback must bring back.
    assert seeded["pa_dot_zero"] == ("100199", "100199.0")
    assert seeded["pa_exponent"] == ("100000", "1.0E+5")
    api_before = _api_results(fresh, [*CONVERTED, "hpa"])
    ids_file = tmp_path / "ids.csv"

    # Dry run: counts, nothing written.
    assert _script(fresh) == 0
    out = capsys.readouterr().out
    assert _state(fresh) == seeded
    assert "Skipping 1 datastreams whose unit is not hPa" in out
    assert "2 datastreams: Things" in out  # A and B: not the PM10, LIKE-decoy or other-source ones
    assert re.search(r"pressure readings to convert \(Pa band\)\s+20\n", out)
    assert re.search(r"readings of other properties \(misrouted\)\s+2\n", out)
    assert re.search(r"of those in the Pa band \(not converted\)\s+1\n", out)
    assert re.search(r"pressure readings left as is, not a number\s+2\n", out)

    # --apply: exactly the rows in the band that are pressure readings, recorded with their values.
    assert _script(fresh, "--apply", "--ids-file", str(ids_file), "--batch-rows", "5") == 0
    out = capsys.readouterr().out
    assert "Converted 20 observations (planned 20); 20 rows recorded" in out
    assert "Verification: no rows left in the Pa band, as of now." in out
    after = _state(fresh)
    assert {label: after[label] for label in CONVERTED} == CONVERTED
    assert {k: v for k, v in after.items() if k not in CONVERTED} == {k: v for k, v in seeded.items() if k not in CONVERTED}
    records = backfill.read_ids_file(ids_file)
    assert sorted(r.observation_id for r in records) == sorted(fresh.ids[label] for label in CONVERTED)
    label_of = {obs_id: label for label, obs_id in fresh.ids.items()}
    for record in records:
        assert (record.result_number, record.result_string) == seeded[label_of[record.observation_id]]
    api_after = _api_results(fresh, [*CONVERTED, "hpa"])
    assert api_after["pa"] == "1003.9186"
    assert api_after["pa_dot_zero"] == "1001.99"
    assert api_after["hpa"] == api_before["hpa"]
    filtered = HTTP.get(
        f"{fresh.url}/Datastreams({fresh.datastreams['A']})/Observations",
        params={"$filter": "result gt 2000", "$select": "id"}, timeout=60,
    ).json()["value"]
    assert {o["@iot.id"] for o in filtered} == {fresh.ids[k] for k in ("edge_high", "pm10_tagged")}

    # Re-run: nothing left to convert or to record.
    size = ids_file.stat().st_size
    assert _script(fresh, "--apply", "--ids-file", str(ids_file)) == 0
    assert "Converted 0 observations (planned 0); 0 rows recorded" in capsys.readouterr().out
    assert ids_file.stat().st_size == size

    # Rollback dry run: counts, nothing written.
    edited = fresh.ids["b3"]
    _psql(fresh.db, f'UPDATE "OBSERVATIONS" SET "RESULT_NUMBER" = 1001.5, "RESULT_STRING" = \'1001.5\' '
                    f'WHERE "ID" = {edited};\n')  # changed after the backfill: the rollback must leave it
    converted = _state(fresh)
    assert _script(fresh, "--rollback", "--ids-file", str(ids_file)) == 0
    out = capsys.readouterr().out
    assert _state(fresh) == converted
    assert re.search(r"holding the converted value: to restore\s+19\n", out)
    assert re.search(r"changed since, left alone\s+1\n", out)

    # Rollback: every other recorded row exactly as seeded, RESULT_STRING included.
    assert _script(fresh, "--rollback", "--apply", "--ids-file", str(ids_file), "--batch-rows", "5") == 0
    out = capsys.readouterr().out
    assert "Restored 19 observations (planned 19)." in out
    assert "Verification: no recorded row holds the converted value any more." in out
    restored = _state(fresh)
    assert restored["b3"] == ("1001.5", "1001.5")
    assert {k: v for k, v in restored.items() if k != "b3"} == {k: v for k, v in seeded.items() if k != "b3"}
    api_restored = _api_results(fresh, [*CONVERTED, "hpa"])
    assert {k: v for k, v in api_restored.items() if k != "b3"} == {k: v for k, v in api_before.items() if k != "b3"}
    assert api_restored["pa_dot_zero"] == "100199.0"

    # A second rollback has nothing left to do.
    assert _script(fresh, "--rollback", "--apply", "--ids-file", str(ids_file)) == 0
    assert "Restored 0 observations (planned 0)." in capsys.readouterr().out


class _RowLock:
    """Holds a row lock from another session until released, like a slow concurrent writer."""

    def __init__(self, db: str, obs_id: int) -> None:
        self.db = db
        self.proc = subprocess.Popen(
            ["docker", "exec", "-i", db, "psql", "-X", "-q", "-U", DB_USER, "-d", DB_NAME],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8",
        )
        assert self.proc.stdin is not None
        self.proc.stdin.write(
            "SET application_name = 'sc_test_lock';\nBEGIN;\n"
            f'SELECT 1 FROM "OBSERVATIONS" WHERE "ID" = {obs_id} FOR UPDATE;\n'
        )
        self.proc.stdin.flush()
        held = "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'sc_test_lock' AND state = 'idle in transaction';\n"
        _wait("the row lock", lambda: _psql(db, held) == ["1"], 60)

    def release(self) -> None:
        if self.proc.poll() is None:
            assert self.proc.stdin is not None
            self.proc.stdin.write("COMMIT;\n")
            self.proc.stdin.close()
            self.proc.wait(timeout=60)


def test_interrupted_apply_leaves_no_converted_row_unrecorded(fresh: Stack, tmp_path: Path) -> None:
    """The client gives up mid-session (its timeout; Ctrl-C or a dropped SSH connection alike) while
    psql carries on in the container and finishes the session's statements once a lock is gone."""
    seeded = _state(fresh)
    ids_file = tmp_path / "ids.csv"
    lock = _RowLock(fresh.db, fresh.ids["b0"])  # datastream B's first batch waits for it
    try:
        assert _script(fresh, "--apply", "--ids-file", str(ids_file), "--batch-rows", "5", "--timeout", "4") == 1
    finally:
        lock.release()  # well within the session's lock_timeout: the blocked UPDATE goes on
    _wait_until_no_script_session(fresh.db)

    after = _state(fresh)
    converted = {fresh.ids[label] for label, value in after.items() if value != seeded[label]}
    assert {fresh.ids[label] for label in CONVERTED if label.startswith(("pa", "edge", "no_", "empty"))} <= converted
    assert {fresh.ids[f"b{k}"] for k in range(12)} <= converted  # finished on the server after the client gave up
    assert converted <= _recorded(ids_file)

    # The same command finishes the job; the rollback restores everything as seeded.
    assert _script(fresh, "--apply", "--ids-file", str(ids_file)) == 0
    assert {label: _state(fresh)[label] for label in CONVERTED} == CONVERTED
    assert _script(fresh, "--rollback", "--apply", "--ids-file", str(ids_file)) == 0
    assert _state(fresh) == seeded
