"""scripts/fix_sc_misrouted_observations.py --sensor-type-split (#40) end to end on a docker FROST.

A Sensor.Community location seeded the way the pre-#39 connector left it: its temperature
datastream (sensor_id ``sensor_community-{loc}``) holds BME280 rows (the owner) and DHT22 rows,
its humidity datastream the same but without a DHT22 humidity datastream yet; then the #39
connector registers the DHT22 temperature datastream (``sensor_community-{loc}-dht22``) and
writes to it. The script's real SQL runs through its real transport (docker exec + psql):
dry run, --apply, re-run, the default mode's label check, --rollback.

FROST comes from ``SC_TYPE_SPLIT_FROST_URL`` (a service root such as
``http://127.0.0.1:18081/FROST-Server``, anonymous writes) with ``SC_TYPE_SPLIT_FROST_DB_CONTAINER``
(its database container), e.g. the compose test stack:

    docker compose -p sc-split-it -f docker-compose.test.yaml up -d --wait     (FROST_PORT=18081)
    SC_TYPE_SPLIT_FROST_URL=http://127.0.0.1:18081/FROST-Server \\
    SC_TYPE_SPLIT_FROST_DB_CONTAINER=sc-split-it-frost-db-1 \\
        python -m pytest tests/ -q -m integration -k sc_type_split
    docker compose -p sc-split-it -f docker-compose.test.yaml down -v

Without them it starts fraunhoferiosb/frost-server:2.8 (production's version) and PostGIS as
containers named ``<SC_TYPE_SPLIT_DOCKER_PREFIX, default sc-split-it>-<random>-*`` and removes
them afterwards. Each run seeds locations with fresh ids, so a shared stack's other rows are
left alone (and only this run's datastreams are asserted on).
"""

from __future__ import annotations

import csv
import json
import os
import random
import shutil
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

from scripts import fix_sc_misrouted_observations as fix

pytestmark = pytest.mark.integration

HTTP = requests.Session()


def _docker(*args: str, check: bool = True, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout)


def _wait_for_frost(root: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if HTTP.get(f"{root}/v1.1/Things", timeout=5).status_code == 200:
                return
        except requests.RequestException:
            pass
        if time.monotonic() > deadline:
            raise TimeoutError(f"FROST at {root} not ready")
        time.sleep(2)


@pytest.fixture(scope="module")
def frost() -> Iterator[tuple[str, str]]:
    """(v1.1 base URL, database container)."""
    configured = os.environ.get("SC_TYPE_SPLIT_FROST_URL", "").strip().rstrip("/")
    if configured:
        db = os.environ.get("SC_TYPE_SPLIT_FROST_DB_CONTAINER", "").strip()
        if not db:
            pytest.skip("SC_TYPE_SPLIT_FROST_URL needs SC_TYPE_SPLIT_FROST_DB_CONTAINER (the database container)")
        _wait_for_frost(configured, 240)
        yield f"{configured}/v1.1", db
        return
    if shutil.which("docker") is None:
        pytest.skip("neither SC_TYPE_SPLIT_FROST_URL nor docker is available")
    name = f"{os.environ.get('SC_TYPE_SPLIT_DOCKER_PREFIX') or 'sc-split-it'}-{uuid.uuid4().hex[:8]}"
    net, db, server = f"{name}-net", f"{name}-db", f"{name}-frost"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = f"http://127.0.0.1:{port}/FROST-Server"  # not localhost: IPv6 first is slow on Windows
    try:
        _docker("network", "create", net)
        _docker("run", "-d", "--name", db, "--network", net, "--tmpfs", "/var/lib/postgresql/data",
                "-e", "POSTGRES_DB=sensorthings", "-e", "POSTGRES_USER=sensorthings",
                "-e", "POSTGRES_PASSWORD=sensorthings", "postgis/postgis:16-3.4-alpine")
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
        _wait_for_frost(root, 240)
        yield f"{root}/v1.1", db
    finally:
        _docker("rm", "-f", "-v", server, db, check=False)
        _docker("network", "rm", net, check=False)


# --- seeding through the FROST API, shaped like the connector's entities and observations ---------------


def _datastream(loc: str, sensor_id: str, sensor_name: str, prop: str) -> dict[str, Any]:
    return {
        "name": f"GEO_{sensor_name} - {prop}", "description": "-",
        "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
        "unitOfMeasurement": {"name": prop, "symbol": "1", "definition": "-"},
        "properties": {"sensor_id": sensor_id, "observed_property": prop, "streamKey": prop},
        "Sensor": {"name": f"GEO_{sensor_name} {prop} {uuid.uuid4().hex[:6]}", "description": "-",
                   "encodingType": "text/plain", "metadata": "-"},
        "ObservedProperty": {"name": f"SensorCommunity {loc} {prop}", "definition": "-", "description": "-"},
    }


def _thing(base: str, loc: str, props: list[str]) -> tuple[int, dict[str, int]]:
    """A location's Thing with its location datastreams: (Thing id, {property: datastream id})."""
    sensor_id = f"sensor_community-{loc}"
    body = {
        "name": f"GEO_SensorCommunity {loc}", "description": "-",
        "properties": {"source": "sensor_community", "location_id": loc},
        "Locations": [{"name": f"GEO_SensorCommunity {loc} location", "description": "-",
                       "encodingType": "application/geo+json",
                       "location": {"type": "Point", "coordinates": [4.0 + random.random(), 52.0]}}],
        "Datastreams": [_datastream(loc, sensor_id, f"SensorCommunity {loc} sensor", p) for p in props],
    }
    response = HTTP.post(f"{base}/Things", json=body, timeout=15)
    response.raise_for_status()
    thing_id = int(response.headers["Location"].rsplit("(", 1)[1].rstrip(")"))
    listing = HTTP.get(f"{base}/Things({thing_id})/Datastreams", params={"$select": "id,properties"}, timeout=15).json()
    return thing_id, {ds["properties"]["observed_property"]: ds["@iot.id"] for ds in listing["value"]}


def _type_datastream(base: str, thing_id: int, loc: str, sensor_type: str, prop: str) -> int:
    """What the #39 connector registers for a non-owner type at an existing location."""
    sensor_id = fix.type_sensor_id(loc, sensor_type)
    body = _datastream(loc, sensor_id, f"SensorCommunity {loc} {sensor_type} sensor", prop) | {"Thing": {"@iot.id": thing_id}}
    response = HTTP.post(f"{base}/Datastreams", json=body, timeout=15)
    response.raise_for_status()
    return int(response.headers["Location"].rsplit("(", 1)[1].rstrip(")"))


def _observe(base: str, ds_id: int, minute: int, value: float, loc: str, sensor_id: str, sensor_name: str,
             prop: str) -> int:
    body = {
        "phenomenonTime": f"2026-11-20T10:{minute:02d}:00Z", "result": value, "Datastream": {"@iot.id": ds_id},
        "parameters": {"sensor_id": sensor_id, "sensor_name": sensor_name, "observed_property": prop,
                       "thing_name": f"SensorCommunity {loc}", "location": loc, "stream_key": prop},
    }
    response = HTTP.post(f"{base}/Observations", json=body, timeout=15)
    response.raise_for_status()
    return int(response.headers["Location"].rsplit("(", 1)[1].rstrip(")"))


def _psql(db: str, sql: str) -> list[str]:
    proc = subprocess.run(["docker", "exec", "-i", db, "sh", "-c", fix.CONTAINER_PSQL], input=sql,
                          capture_output=True, text=True, encoding="utf-8", check=True, timeout=120)
    return proc.stdout.splitlines()


@dataclass
class Seed:
    loc: str
    temp: int  # the location's temperature datastream (owner BME280)
    hum: int  # its humidity datastream (owner BME280; no DHT22 humidity datastream)
    dht22_temp: int  # the DHT22 temperature datastream the #39 connector registered
    orphan: int  # another location's temperature datastream, without an owner
    dht22_rows: list[int]  # DHT22 rows in temp, ID <= max_id: to move
    late_row: int  # a DHT22 row in temp after max_id: stays
    own_row: int  # the row the #39 connector wrote to dht22_temp
    max_id: int
    owners: Path

    @property
    def mine(self) -> tuple[int, ...]:
        return (self.temp, self.hum, self.dht22_temp, self.orphan)


def _seed(base: str, db: str, tmp_path: Path) -> Seed:
    loc, orphan_loc = (str(random.randint(100_000_000, 999_999_999)) for _ in range(2))
    sid = f"sensor_community-{loc}"
    bme, dht = f"SensorCommunity {loc} BME280 sensor", f"SensorCommunity {loc} DHT22 sensor"
    thing_id, ds = _thing(base, loc, ["temperature", "humidity"])
    _, orphan_ds = _thing(base, orphan_loc, ["temperature"])

    dht22_rows = []
    for minute in range(4):  # both sensors report every poll: one BME280 and one DHT22 row per phenomenonTime
        _observe(base, ds["temperature"], minute, 20.0 + minute, loc, sid, bme, "temperature")
        if minute < 3:
            dht22_rows.append(_observe(base, ds["temperature"], minute, 21.5 + minute, loc, sid, dht, "temperature"))
        _observe(base, ds["humidity"], minute, 60.0, loc, sid, bme, "humidity")
    for minute in range(2):
        _observe(base, ds["humidity"], minute, 70.0 + minute, loc, sid, dht, "humidity")  # needs-datastream
    _observe(base, ds["temperature"], 30, 19.0, loc, sid, "airrohr-esp8266 sensor", "temperature")  # unparsable
    _observe(base, orphan_ds["temperature"], 5, 18.0, orphan_loc, f"sensor_community-{orphan_loc}",
             f"SensorCommunity {orphan_loc} DHT22 sensor", "temperature")  # no owner
    max_id = int(_psql(db, 'SELECT max("ID") FROM "OBSERVATIONS";')[0])

    # The #39 deploy: the DHT22 gets its own datastream and writes there; a late DHT22 row in the location
    # datastream (ID > max_id) stands for anything written after the watermark.
    dht22_temp = _type_datastream(base, thing_id, loc, "DHT22", "temperature")
    own_row = _observe(base, dht22_temp, 40, 23.0, loc, fix.type_sensor_id(loc, "DHT22"), dht, "temperature")
    late_row = _observe(base, ds["temperature"], 41, 23.5, loc, sid, dht, "temperature")
    assert late_row > max_id

    owners = tmp_path / "sc_sensor_owners.json"
    owners.write_text(json.dumps({"version": 1, "owners": {loc: {"temperature": "BME280", "humidity": "BME280"}}}),
                      encoding="utf-8")
    return Seed(loc, ds["temperature"], ds["humidity"], dht22_temp, orphan_ds["temperature"], dht22_rows, late_row,
                own_row, max_id, owners)


def _rows(db: str, seed: Seed) -> dict[int, tuple[str, ...]]:
    """ID -> (datastream, FoI, PARAMETERS.sensor_id, sensor_name) of every row in this run's datastreams."""
    ids = ", ".join(str(i) for i in seed.mine)
    lines = _psql(db, f"""SELECT "ID", "DATASTREAM_ID", "FEATURE_ID", "PARAMETERS"->>'sensor_id', "PARAMETERS"->>'sensor_name'
                            FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" IN ({ids}) ORDER BY 1;""")
    return {int(line.split("|")[0]): tuple(line.split("|")[1:]) for line in lines}


def _times(db: str, seed: Seed) -> dict[int, tuple[str, ...]]:
    ids = ", ".join(str(i) for i in seed.mine)
    lines = _psql(db, f"""SELECT "ID", "PHENOMENON_TIME_START", "PHENOMENON_TIME_END", "RESULT_TIME_START", "RESULT_TIME_END"
                            FROM "DATASTREAMS" WHERE "ID" IN ({ids}) ORDER BY 1;""")
    return {int(line.split("|")[0]): tuple(line.split("|")[1:]) for line in lines}


def _plan(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as src:
        return list(csv.DictReader(src))


def test_sc_type_split_dry_run_apply_and_rollback_on_frost(frost, tmp_path, capsys) -> None:
    """protects=#40 acceptance 1-4 on a real FROST: the dry run lists the DHT22 rows of the mixed temperature
    datastream as a move to the DHT22 datastream, the DHT22 humidity rows as needs-datastream, the ownerless and
    unparsable rows as reported; --apply moves exactly those rows (not the row after --max-id), relabels
    PARAMETERS.sensor_id so the default mode finds nothing misrouted, refreshes both datastreams' times and
    leaves 0 non-owner rows to move; --rollback restores datastream, FoI, label and times;
    fails_when=the SQL (pattern, jsonb_set, owner comparison, max-id cut, refresh) misbehaves on PostgreSQL/FROST;
    no_claim=production's volume or timing, FROST versions other than the one run."""
    base, db = frost
    seed = _seed(base, db, tmp_path)
    sid, dht22_sid = f"sensor_community-{seed.loc}", fix.type_sensor_id(seed.loc, "DHT22")
    script = ["--container", db, "--range-ids", "7", "--chunk", "3"]
    split = [*script, "--sensor-type-split", "--owners", str(seed.owners), "--max-id", str(seed.max_id)]

    # Dry run: read-only, the plan per datastream.
    before_rows, before_times = _rows(db, seed), _times(db, seed)
    plan_file = tmp_path / "split.csv"
    assert fix.main([*split, "--plan-file", str(plan_file)]) == 0
    assert _rows(db, seed) == before_rows
    plan = {(r["action"], int(r["source_datastream"]), r["sensor_type"]): r for r in _plan(plan_file)
            if int(r["source_datastream"]) in seed.mine}
    assert set(plan) == {
        (fix.MOVE, seed.temp, "DHT22"), (fix.NEEDS_DATASTREAM, seed.hum, "DHT22"),
        (fix.UNPARSABLE, seed.temp, ""), (fix.OWNER_MISSING, seed.orphan, "DHT22"),
    }
    move = plan[(fix.MOVE, seed.temp, "DHT22")]
    assert (move["rows"], move["target_datastream"], move["target_sensor_id"]) == ("3", str(seed.dht22_temp), dht22_sid)
    assert plan[(fix.NEEDS_DATASTREAM, seed.hum, "DHT22")]["rows"] == "2"
    out = capsys.readouterr().out
    assert "Totals by sensor type" in out and "Dry run: nothing was written." in out

    # Apply: exactly the planned rows move, relabelled, FoI kept (same Thing); both datastreams refreshed.
    ids_file = tmp_path / "split.jsonl"
    assert fix.main([*split, "--apply", "--ids-file", str(ids_file), "--batch-rows", "2"]) == 0
    out = capsys.readouterr().out
    assert "Moved 3 observations (0 left in place" in out
    assert "to move 0, kept as duplicates 0" in out
    after = _rows(db, seed)
    changed = {i for i in after if after[i] != before_rows[i]}
    assert changed == set(seed.dht22_rows)
    for i in seed.dht22_rows:
        assert after[i] == (str(seed.dht22_temp), before_rows[i][1], dht22_sid, before_rows[i][3])
    assert after[seed.late_row][:3] == (str(seed.temp), before_rows[seed.late_row][1], sid)  # after --max-id: stays
    non_owner_left = _psql(db, f"""SELECT count(*) FROM "OBSERVATIONS" WHERE "DATASTREAM_ID" = {seed.temp}
                                     AND "ID" <= {seed.max_id} AND "PARAMETERS"->>'sensor_name' LIKE '% DHT22 sensor';""")
    assert non_owner_left == ["0"]
    served = HTTP.get(f"{base}/Datastreams({seed.dht22_temp})/Observations",
                      params={"$select": "id,parameters", "$top": 100}, timeout=15).json()["value"]
    assert sorted(o["@iot.id"] for o in served) == sorted([*seed.dht22_rows, seed.own_row])
    assert {o["parameters"]["sensor_id"] for o in served} == {dht22_sid}
    times = _times(db, seed)
    assert times[seed.dht22_temp][:2] == ("2026-11-20 10:00:00+00", "2026-11-20 10:40:00+00")
    assert times[seed.temp][:2] == before_times[seed.temp][:2]  # BME280 rows still span 10:00 to the late row

    records = [json.loads(line) for line in ids_file.read_text(encoding="utf-8").splitlines()]
    moves = [r for r in records if r["op"] == "move"]
    assert {(r["from_ds"], r["to_ds"], r["from_sensor_id"], r["to_sensor_id"]) for r in moves} == {
        (seed.temp, seed.dht22_temp, sid, dht22_sid)}
    assert sorted(row[0] for r in moves for row in r["rows"]) == sorted(seed.dht22_rows)

    # The default mode's label checks: nothing misrouted in this run's datastreams.
    default_plan = tmp_path / "misrouted.csv"
    assert fix.main([*script, "--sc-datastreams-only", "--no-duplicate-check", "--plan-file", str(default_plan)]) == 0
    assert [r for r in _plan(default_plan) if int(r["source_datastream"]) in seed.mine] == []

    # A re-run finds nothing left to move.
    capsys.readouterr()
    assert fix.main([*split, "--apply", "--ids-file", str(ids_file)]) == 0
    assert "Moved 0 observations" in capsys.readouterr().out

    # Rollback: a dry run first, then back to the seeded state (datastream, FoI, label, times).
    assert fix.main([*script, "--rollback", "--ids-file", str(ids_file)]) == 0
    assert "Re-run with --apply to move 3 rows back." in capsys.readouterr().out
    assert fix.main([*script, "--rollback", "--apply", "--ids-file", str(ids_file)]) == 0
    assert "Verification: no recorded row is left where the script put it." in capsys.readouterr().out
    assert _rows(db, seed) == before_rows
    assert _times(db, seed) == before_times
