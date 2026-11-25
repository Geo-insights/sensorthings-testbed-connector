"""scripts/fix_samen_meten_calibrated.py against the docker test FROST (docker-compose.test.yaml), RIVM stubbed.

The script's real SQL runs through its real transport (docker exec + psql) on the compose stack's database;
RIVM is an injected fake client, so no network. Seeded through the FROST API like the connector's Samen Meten
entities: rows for all three rules, a row above --max-id, a datastream whose RIVM Thing name differs, one whose
Thing has no _kal series. Dry run, apply, verify, idempotent re-run, then --revert of the updates plus
cleanup_frost_observations.py --restore of the archive, which must bring every row back byte for byte.

Start the stack first (the test skips when it is not reachable); the port and compose project are read from
FROST_PORT (default 18080) and COMPOSE_PROJECT_NAME, as docker compose itself does:

    FROST_PORT=18082 docker compose -p sm-kal-it -f docker-compose.test.yaml up -d --wait
    FROST_PORT=18082 COMPOSE_PROJECT_NAME=sm-kal-it python -m pytest tests/ -q -m integration -k samen_meten_calibrated
    FROST_PORT=18082 docker compose -p sm-kal-it -f docker-compose.test.yaml down -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import requests

from scripts import cleanup_frost_observations as cleanup
from scripts import fix_samen_meten_calibrated as fix

pytestmark = pytest.mark.integration

COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.test.yaml"
HTTP = requests.Session()
DAY = "2026-11-25"

# RIVM Things of the fake client: 9001 matches, 9002 has another name, 9003 has no _kal series.
KAL25, KAL10 = 43002, 43006

# label -> (datastream key, hour, result, sensor_id)
SEED: dict[str, tuple[str, int, str, str]] = {
    # rule 1: a row equals _kal; the lowest-ID matching row stays
    "h06_raw": ("a25", 6, "17.77", "sm-9001"),
    "h06_kal": ("a25", 6, "17.234945", "sm-9001"),
    "h06_raw2": ("a25", 6, "17.77", "sm-9001"),
    "h07_kal": ("a25", 7, "17.819597", "sm-9001"),
    "h07_kal2": ("a25", 7, "17.819597", "sm-9001"),
    # rule 2: no row equals _kal; the lowest-ID row is set to it
    "h08_a": ("a25", 8, "20.0", "sm-9001"),
    "h08_b": ("a25", 8, "21.0", "sm-9001"),
    "h09_single": ("a25", 9, "10.000", "sm-9001"),
    # rule 3: no _kal value for the hour
    "h10_a": ("a25", 10, "5.0", "sm-9001"),
    "h10_b": ("a25", 10, "6.0", "sm-9001"),
    # within tolerance: kept as it is
    "h11_close": ("a25", 11, "12.0", "sm-9001"),
    "h12_kal": ("a25", 12, "13.5", "sm-9001"),
    # pm10 of the same Thing
    "p10_raw": ("a10", 6, "30.1", "sm-9001"),
    "p10_kal": ("a10", 6, "28.4", "sm-9001"),
    # skipped datastreams: RIVM name differs; Thing without _kal
    "b_raw": ("b25", 6, "17.77", "sm-9002"),
    "b_kal": ("b25", 6, "17.2", "sm-9002"),
    "c_raw": ("c25", 6, "17.77", "sm-9003"),
    "c_raw2": ("c25", 6, "17.70", "sm-9003"),
    # not Samen Meten: never in scope
    "sc": ("sc25", 6, "4.4", "sensor_community-1"),
}
ABOVE = ("a25", 12, "99.0", "sm-9001")  # posted after --max-id: never touched

KAL: dict[int, dict[int, float]] = {
    KAL25: {6: 17.234945, 7: 17.819597, 8: 19.5, 9: 9.25, 11: 12.0000004, 12: 13.5},
    KAL10: {6: 28.4},
}
DELETED = {"h06_raw", "h06_raw2", "h07_kal2", "h08_b", "h10_a", "h10_b", "p10_raw"}
UPDATED = {"h08_a": 19.5, "h09_single": 9.25}


class FakeRivm:
    """Answers like api-samenmeten.rivm.nl for the seeded Things; records the windows asked for."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.windows: list[tuple[int, datetime, datetime]] = []

    def thing(self, thing_id: int) -> fix.RivmThing:
        name = f"LUC_{self.tag}"
        if thing_id == 9001:
            return fix.RivmThing(9001, name, ((43001, f"{name}-12-pm25"), (KAL25, f"{name}-12-pm25_kal"),
                                              (43005, f"{name}-12-pm10"), (KAL10, f"{name}-12-pm10_kal")))
        if thing_id == 9002:
            return fix.RivmThing(9002, "SOMEONE_ELSE", ((43101, "SOMEONE_ELSE-12-pm25_kal"),))
        if thing_id == 9003:
            return fix.RivmThing(9003, f"NOKAL_{self.tag}", ((43201, f"NOKAL_{self.tag}-12-pm25"),))
        raise fix.RivmNotFound(f"Things({thing_id})")

    def observations(self, ds_id: int, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        self.windows.append((ds_id, start, end))
        out = []
        for hour, value in KAL.get(ds_id, {}).items():
            when = fix.parse_time(f"{DAY}T{hour:02d}:00:00Z")
            if start <= when < end:
                out.append((when, value))
        return out


@dataclass
class Stack:
    db: str
    url: str
    tag: str
    datastreams: dict[str, int]
    ids: dict[str, int]
    above: int
    max_id: int


def _docker(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], input=input, capture_output=True, text=True, encoding="utf-8",
                          timeout=180, check=check)


def _psql(db: str, sql: str) -> list[str]:
    proc = _docker("exec", "-i", db, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", "sensorthings",
                   "-d", "sensorthings", input="SET TimeZone = 'UTC';\nSET extra_float_digits = 3;\n" + sql)
    return proc.stdout.splitlines()


def _post(url: str, path: str, body: dict[str, Any] | str) -> int:
    data = body if isinstance(body, str) else json.dumps(body)
    response = HTTP.post(f"{url}/{path}", data=data, headers={"Content-Type": "application/json"}, timeout=60)
    assert response.status_code == 201, response.text
    match = re.search(r"\((\d+)\)$", response.headers["Location"])
    assert match is not None
    return int(match[1])


def _thing(url: str, name: str, source: str, sensor_id: str, keys: dict[str, str]) -> dict[str, int]:
    """A Thing with one datastream per key -> observed_property, as the connector registers them."""
    thing = _post(url, "Things", {
        "name": name, "description": "-", "properties": {"source": source, "site": source},
        "Locations": [{"name": f"{name} location", "description": "-", "encodingType": "application/geo+json",
                       "location": {"type": "Point", "coordinates": [4.48, 51.92]}}],
        "Datastreams": [{
            "name": f"{name} sensor - {prop}", "description": "-",
            "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
            "unitOfMeasurement": {"name": prop, "symbol": "ug/m3", "definition": "-"},
            "properties": {"site_key": source, "thing_name": name, "sensor_id": sensor_id,
                           "observed_property": prop, "streamKey": prop},
            "Sensor": {"name": f"{name} {prop} sensor", "description": "-", "encodingType": "text/plain",
                       "metadata": "-"},
            "ObservedProperty": {"name": f"{prop} {name}", "definition": "-", "description": "-"},
        } for prop in keys.values()],
    })
    listing = HTTP.get(f"{url}/Things({thing})/Datastreams", params={"$select": "id,properties"}, timeout=60).json()
    by_prop = {ds["properties"]["observed_property"]: ds["@iot.id"] for ds in listing["value"]}
    return {key: by_prop[prop] for key, prop in keys.items()}


def _observe(url: str, ds: int, hour: int, raw: str, sensor_id: str) -> int:
    body = {
        "phenomenonTime": f"{DAY}T{hour:02d}:00:00Z", "result": "__RESULT__", "Datastream": {"@iot.id": ds},
        "parameters": {"sensor_id": sensor_id, "sensor_name": "SamenMeten sensor", "observed_property": "pm2_5",
                       "thing_name": "SamenMeten x", "location": "samen_meten"},
    }
    return _post(url, "Observations", json.dumps(body).replace('"__RESULT__"', raw))


def _compose_db() -> str:
    explicit = os.environ.get("FROST_DB_CONTAINER")
    if explicit:
        return explicit
    proc = _docker("compose", "-f", str(COMPOSE_FILE), "ps", "-q", "frost-db", check=False)
    return proc.stdout.strip().splitlines()[0] if proc.returncode == 0 and proc.stdout.strip() else ""


@pytest.fixture(scope="module")
def stack() -> Iterator[Stack]:
    port = os.environ.get("FROST_PORT", "18080")
    url = f"http://127.0.0.1:{port}/FROST-Server/v1.1"
    try:
        if HTTP.get(f"{url}/Things", params={"$top": "1"}, timeout=10).status_code != 200:
            pytest.skip(f"FROST at {url} does not answer; start docker-compose.test.yaml (see the module docstring)")
        db = _compose_db()
    except (requests.RequestException, FileNotFoundError, subprocess.SubprocessError):
        pytest.skip(f"FROST at {url} or docker is not available; see the module docstring")
    if not db:
        pytest.skip("no frost-db container of the compose project (set COMPOSE_PROJECT_NAME or FROST_DB_CONTAINER)")
    tag = uuid.uuid4().hex[:8].upper()
    datastreams: dict[str, int] = {}
    datastreams |= _thing(url, f"GEO_SamenMeten LUC_{tag}", "samen_meten", f"sm-luc_{tag.lower()}",
                          {"a25": "pm2_5", "a10": "pm10"})
    datastreams |= _thing(url, f"GEO_SamenMeten MISMATCH_{tag}", "samen_meten", "sm-x", {"b25": "pm2_5"})
    datastreams |= _thing(url, f"GEO_SamenMeten NOKAL_{tag}", "samen_meten", "sm-y", {"c25": "pm2_5"})
    datastreams |= _thing(url, f"GEO_SensorCommunity {tag}", "sensor_community", "sc-1", {"sc25": "pm2_5"})
    ids = {label: _observe(url, datastreams[key], hour, raw, sid) for label, (key, hour, raw, sid) in SEED.items()}
    max_id = max(ids.values())
    key, hour, raw, sid = ABOVE
    above = _observe(url, datastreams[key], hour, raw, sid)
    yield Stack(db, url, tag, datastreams, ids, above, max_id)


def _ours(stack: Stack) -> list[int]:
    return sorted(stack.datastreams[k] for k in ("a25", "a10", "b25", "c25"))


def _rows(stack: Stack) -> dict[int, str]:
    """row_to_json of every observation of the seeded datastreams, by ID."""
    ids = ",".join(str(d) for d in stack.datastreams.values())
    lines = _psql(stack.db, f'SELECT "ID", row_to_json(o)::text FROM "OBSERVATIONS" o WHERE "DATASTREAM_ID" IN ({ids}) '
                            "ORDER BY 1;\n")
    return {int(i): row for i, row in (line.split("|", 1) for line in lines)}


def _script(stack: Stack, fake: FakeRivm, *args: str, datastreams: list[int] | None = None) -> int:
    scope = ",".join(str(d) for d in (datastreams or _ours(stack)))
    return fix.main(["--container", stack.db, "--max-id", str(stack.max_id), "--datastreams", scope, *args], api=fake)


def test_dry_run_apply_verify_revert_restore(stack: Stack, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """protects=#41 rules 1-3, skips, --max-id and the full undo path on real PostgreSQL + FROST;
    fails_when=apply deletes/updates other rows than planned, FROST still serves the raw value, a re-run plans
    anything, or revert + restore does not bring every row back byte for byte;
    no_claim=RIVM's real API shape (stubbed) or production data volumes/timeouts"""
    fake = FakeRivm(stack.tag)
    before = _rows(stack)
    archive, rollback = tmp_path / "sm.jsonl", tmp_path / "sm.csv"

    # A datastream outside the scope is refused.
    assert _script(stack, fake, datastreams=[stack.datastreams["sc25"]]) == fix.EXIT_ERROR

    # Dry run: the plan, nothing written.
    capsys.readouterr()
    assert _script(stack, fake) == fix.EXIT_OK
    out = capsys.readouterr().out
    assert _rows(stack) == before
    assert not archive.exists() and not rollback.exists()
    assert re.search(r"to update to _kal \(rule 2\) +2\n", out)
    assert re.search(r"to keep unchanged \(equal to _kal\) +5\n", out)
    assert re.search(r"to delete +7\n", out)
    assert re.search(r"no _kal value for the hour \(rule 3\) +2\n", out)
    assert re.search(re.escape(fix.SKIP_NAME) + r" +1\n", out)
    assert re.search(re.escape(fix.SKIP_NO_KAL) + r" +1\n", out)
    assert re.search(r"hours that also hold rows above --max-id.* +1\n", out)
    # RIVM was asked for [first hour, last hour + 1 h) of our rows only.
    assert (KAL25, fix.parse_time(f"{DAY}T06:00:00Z"), fix.parse_time(f"{DAY}T13:00:00Z")) in fake.windows

    # Apply.
    assert _script(stack, fake, "--apply", "--archive", str(archive), "--rollback-csv", str(rollback)) == fix.EXIT_OK
    after = _rows(stack)
    gone = {label for label, obs_id in stack.ids.items() if obs_id not in after}
    assert gone == DELETED
    assert stack.above in after
    for label, value in UPDATED.items():
        row = json.loads(after[stack.ids[label]])
        assert row["RESULT_NUMBER"] == value and float(row["RESULT_STRING"]) == value
        served = HTTP.get(f"{stack.url}/Observations({stack.ids[label]})", timeout=30).json()["result"]
        assert served == value  # FROST serves RESULT_STRING
    unchanged = set(stack.ids) - DELETED - set(UPDATED)
    assert all(after[stack.ids[label]] == before[stack.ids[label]] for label in unchanged)
    archived = {cleanup.parse_row_line(line).id for line in archive.read_text(encoding="utf-8").splitlines()
                if line.startswith('{"sel":')}
    assert archived == {stack.ids[label] for label in DELETED}
    records = fix.read_rollback(rollback)
    assert {(u.id, u.old, u.new) for u in records} == {
        (stack.ids["h08_a"], 20.0, 19.5), (stack.ids["h09_single"], 10.0, 9.25)}
    assert {u.old_string for u in records} == {"20.0", "10.000"}

    # Idempotent: a re-run plans and writes nothing; --verify passes.
    csv_before = rollback.read_text(encoding="utf-8")
    assert _script(stack, fake, "--apply", "--archive", str(archive), "--rollback-csv", str(rollback)) == fix.EXIT_OK
    assert _rows(stack) == after
    assert rollback.read_text(encoding="utf-8") == csv_before
    assert _script(stack, fake, "--verify", "--sample", "50",
                   datastreams=[stack.datastreams["a25"], stack.datastreams["a10"]]) == fix.EXIT_OK

    # Undo: revert the updates, restore the archive (cleanup_frost_observations.py).
    assert fix.main(["--container", stack.db, "--rollback-csv", str(rollback), "--revert"]) == fix.EXIT_OK
    assert _rows(stack) == after  # a dry run
    assert fix.main(["--container", stack.db, "--rollback-csv", str(rollback), "--revert", "--apply"]) == fix.EXIT_OK
    assert cleanup.main(["--container", stack.db, "--restore", "--archive", str(archive), "--apply"]) == cleanup.EXIT_OK
    assert _rows(stack) == before
