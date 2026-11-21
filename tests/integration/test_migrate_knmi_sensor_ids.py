"""Integration: KNMI stable station ids on FROST-Server 2.8, and the rollout order.

Production (frost.geo-insights.nl) has one target, the v2.0 endpoint declared
without a version, so the connector speaks v1.1 to it; these tests configure the
connector the same way. Each test first replays, in miniature, what the old
image (index-based sensor ids) left on production:

* S1 has a stale '... - Ground temperature' Datastream (created before the
  canonical rename) next to the '... - Ground surface temperature' one the
  connector registers;
* first start: S0, S1, S2 report, in that order (knmi-0, knmi-1, knmi-2); S2 then
  reports only temperature and humidity;
* S3 starts reporting at position 1: in the same process S3, S1 and S2 shift to
  knmi-1, knmi-2 and knmi-3, so S3's readings go into S1's Datastreams and S1's
  into S2's;
* restart: registration under the new positions creates S3 (knmi-1) and S2's
  other Datastreams (knmi-3), so S2 mixes two ids.

The "old image" is the connector's own registration and push code without the
startup warm-up: it routes from the keys registration put in its cache, as the
running main image does. The "new image" is this branch's polling path: cache
warm-up, restart seeding, KNMIWeatherPollingSource against a fake KNMI EDR API,
entity-set registration, deduplication, push.

test_migrate_then_deploy is the recommended order (the migration runs while the
old image still runs) on all three ways of addressing FROST;
test_deploy_then_migrate shows what goes wrong the other way round.

FROST comes from ``KNMI_MIGRATE_FROST_URL`` (a service root with Basic auth
write/write and no anonymous reads) or from docker compose: FROST 2.8
(``KNMI_MIGRATE_FROST_IMAGE`` overrides) and PostGIS 16 under the project name
``lnknmi-it-<random>`` (``KNMI_IT_DOCKER_PREFIX`` replaces ``lnknmi``), the
database on tmpfs, removed afterwards.

    python -m pytest tests/integration/test_migrate_knmi_sensor_ids.py -m integration
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
import requests

import app.main as main
import app.services.knmi_weather_source as knmi_source_module
import app.services.sensorthings_client as client_module
from app import config
from app.frost.target import FrostTarget
from app.models import SensorReading
from app.services.knmi_weather_source import KNMIWeatherPollingSource
from app.services.sensorthings_client import SensorThingsClient
from app.sources.knmi_weather import EDR_LOCATION_ID, build_entity_set, station_name_for
from app.sources.normalizers import KNMIWeatherNormalizer
from scripts import migrate_knmi_sensor_ids as migrate

pytestmark = pytest.mark.integration

_AUTH = ("write", "write")
_API_KEY = "test-key"
_COMPOSE = """
services:
  db:
    image: postgis/postgis:16-3.4-alpine
    environment: {POSTGRES_DB: sensorthings, POSTGRES_USER: sensorthings, POSTGRES_PASSWORD: sensorthings}
    tmpfs: ["/var/lib/postgresql/data"]
    healthcheck: {test: ["CMD-SHELL", "pg_isready -U sensorthings"], interval: 3s, timeout: 3s, retries: 30}
  frost:
    image: IMAGE
    depends_on: {db: {condition: service_healthy}}
    environment:
      JAVA_OPTS: "-Xmx768m"
      serviceRootUrl: http://localhost:PORT/FROST-Server
      persistence_db_driver: org.postgresql.Driver
      persistence_db_url: jdbc:postgresql://db:5432/sensorthings
      persistence_db_username: sensorthings
      persistence_db_password: sensorthings
      persistence_autoUpdateDatabase: "true"
      auth_provider: de.fraunhofer.iosb.ilt.frostserver.auth.basic.BasicAuthProvider
      auth_db_driver: org.postgresql.Driver
      auth_db_url: jdbc:postgresql://db:5432/sensorthings
      auth_db_username: sensorthings
      auth_db_password: sensorthings
      auth_autoUpdateDatabase: "true"
      auth_allowAnonymousRead: "false"
    ports: ["127.0.0.1:PORT:8080"]
"""


@dataclasses.dataclass(frozen=True)
class Station:
    wigos: str
    lon: float
    lat: float
    label: str

    @property
    def name(self) -> str:
        return station_name_for(self.lat, self.lon)

    @property
    def sensor_id(self) -> str:
        return f"knmi-{self.wigos}"


S0 = Station("0-20000-0-06310", 3.3417, 52.36, "S0")
S1 = Station("0-20000-0-06210", 4.4364, 52.1397, "S1")  # has a stale 'Ground temperature' Datastream
S2 = Station("0-20000-0-06225", 4.555, 52.4622, "S2")  # gets Datastreams under two indexes
S3 = Station("0-20000-0-06209", 4.5175, 52.4636, "S3")  # IJmond: starts reporting later, at position 1
STATIONS = (S0, S1, S2, S3)
ELSEWHERE = Station("0-20000-0-06380", 5.7619, 50.9053, "S9")  # in /locations, never in FROST
PROPERTIES = ("temperature", "humidity", "ground_temperature")
T0 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
T1, T2, T3, T4, T5, T6 = (T0 + timedelta(minutes=10 * k) for k in range(1, 7))


def _value(station: Station, t: datetime, prop: int) -> float:
    """A value that tells station, time and property apart."""
    return round(10 * (prop + 1) + STATIONS.index(station) + t.minute / 100, 2)


# ---------------------------------------------------------------------------
# FROST and the fake KNMI EDR API
# ---------------------------------------------------------------------------


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(root: str, timeout: float = 300.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ready = all(
                requests.get(f"{root}/{version}/Things", auth=_AUTH, timeout=5).status_code == 200
                for version in ("v1.1", "v2.0")
            )
            if ready:
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise TimeoutError(f"FROST at {root} did not serve v1.1 and v2.0 within {timeout:.0f}s")


@pytest.fixture(scope="module")
def frost_root() -> Iterator[str]:
    configured = os.environ.get("KNMI_MIGRATE_FROST_URL", "").strip().rstrip("/")
    if configured:
        _wait_until_ready(configured)
        yield configured
        return
    try:
        subprocess.run(["docker", "compose", "version"], check=True, capture_output=True, timeout=20)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("neither KNMI_MIGRATE_FROST_URL nor docker compose is available")
    port = _free_port()
    image = os.environ.get("KNMI_MIGRATE_FROST_IMAGE", "fraunhoferiosb/frost-server:2.8")
    compose = _COMPOSE.replace("IMAGE", image).replace("PORT", str(port))
    prefix = os.environ.get("KNMI_IT_DOCKER_PREFIX", "lnknmi")
    project = ["docker", "compose", "-p", f"{prefix}-it-{uuid.uuid4().hex[:8]}", "-f", "-"]
    try:
        subprocess.run([*project, "up", "-d"], input=compose, text=True, check=True, capture_output=True, timeout=600)
        root = f"http://127.0.0.1:{port}/FROST-Server"  # the port is bound to IPv4 only
        _wait_until_ready(root)
        yield root
    finally:
        subprocess.run([*project, "down", "-v", "--remove-orphans"], input=compose, text=True, capture_output=True, timeout=180)


class _FakeEdr(BaseHTTPRequestHandler):
    """GET .../area (the scripted CoverageCollection) and .../locations, behind the API key."""

    area: ClassVar[dict[str, Any]] = {}
    paths: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).paths.append(self.path)
        if self.headers.get("Authorization") != _API_KEY:
            return self._send(401, {"message": "Unauthorized"})
        path = self.path.split("?", 1)[0]
        if path.endswith("/area"):
            return self._send(200, type(self).area)
        if path.endswith("/locations"):
            return self._send(200, _locations())
        return self._send(404, {"message": "not found"})

    def _send(self, status: int, body: Any) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture(scope="module")
def edr_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeEdr)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/edr/v1/collections/10-minute-in-situ-meteorological-observations"
    finally:
        server.shutdown()


def _locations() -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [st.lon, st.lat]},
             "properties": {"name": f"Station {st.label}", "wmoId": st.wigos[-5:]}, "id": st.wigos}
            for st in (*STATIONS, ELSEWHERE)
        ],
    }


def _area(stations: list[Station], t: datetime) -> dict[str, Any]:
    """The /area CoverageCollection the EDR API serves: one Coverage per station, with its WIGOS id."""

    def nd(value: float) -> dict[str, Any]:
        return {"type": "NdArray", "dataType": "float", "axisNames": ["t", "y", "x"], "shape": [1, 1, 1], "values": [value]}

    return {
        "type": "CoverageCollection",
        "domainType": "PointSeries",
        "coverages": [
            {
                "type": "Coverage",
                "domain": {"type": "Domain", "domainType": "PointSeries", "axes": {
                    "x": {"values": [st.lon]}, "y": {"values": [st.lat]},
                    "t": {"values": [t.strftime("%Y-%m-%dT%H:%M:%SZ")]},
                }},
                "ranges": {"ta": nd(_value(st, t, 0)), "rh": nd(_value(st, t, 1)), "tg": nd(_value(st, t, 2))},
                EDR_LOCATION_ID: st.wigos,
            }
            for st in stations
        ],
    }


# ---------------------------------------------------------------------------
# The connector, old and new
# ---------------------------------------------------------------------------


class Env:
    """One test's connector configuration: production-shaped target, its own entity prefix and cache file."""

    def __init__(self, root: str, edr: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.prefix = f"IT{uuid.uuid4().hex[:8]}_"
        self.write = f"{root}/v2.0"  # production: the v2.0 endpoint, no version declared
        self.read = f"{root}/v1.1"
        self.monkeypatch = monkeypatch
        target = {"url": self.write, "label": "lnknmi", "auth_username": "write", "auth_password": "write"}
        monkeypatch.delenv("SENSORTHINGS_BASE_URL", raising=False)
        monkeypatch.setenv("SENSORTHINGS_BASE_URLS", json.dumps([target]))
        self.cache_file = tmp_path / "registered_entities.json"
        self.journal = tmp_path / "knmi_sensor_ids_journal.jsonl"
        self.settings = dataclasses.replace(
            config.settings,
            sensorthings_base_url="",
            sensorthings_base_urls=config._load_base_urls(),
            frost_targets=config._load_frost_targets(),
            auth_username="write",
            auth_password="write",
            auth_token="",
            entity_name_prefix=self.prefix,
            registered_entities_path=str(self.cache_file),
            failed_observations_path=str(tmp_path / "failed_observations.jsonl"),
            datastream_ids={},
            site_project_configs={},
            default_project_name="",
            default_project_id="",
            knmi_weather_enabled=True,
            knmi_api_key=_API_KEY,
            knmi_weather_api_url=f"{edr}/locations",
        )
        for module in (client_module, main, knmi_source_module, migrate):
            monkeypatch.setattr(module, "settings", self.settings)

    def process(self) -> SensorThingsClient:
        """A connector process: a fresh client on the same entity cache file (the persistent disk)."""
        client = SensorThingsClient()
        self.monkeypatch.setattr(main, "client", client)
        return client

    def migrate(self, *args: str, endpoint: str = "v2.0", declared: str | None = None) -> int:
        target = FrostTarget(url=f"{self.root}/{endpoint}", version=declared or "v1.1",
                             auth_username="write", auth_password="write", label="lnknmi")
        self.monkeypatch.setattr(migrate, "settings", dataclasses.replace(self.settings, frost_targets=(target,)))
        return migrate.main(list(args))


@pytest.fixture
def env(frost_root: str, edr_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(frost_root, edr_url, tmp_path, monkeypatch)


def _old_readings(order: list[Station], t: datetime) -> list[SensorReading]:
    """What the old parser made of an /area response: sensor id from the Coverage's position."""
    readings: list[SensorReading] = []
    for index, st in enumerate(order):
        normalizer = KNMIWeatherNormalizer(
            temperature=_value(st, t, 0), humidity=_value(st, t, 1), groundtemperature=_value(st, t, 2)
        )
        readings += normalizer.to_readings(
            sensor_id=f"knmi-{index}", sensor_name=f"KNMI {st.name} sensor", thing_name=f"KNMI {st.name}",
            timestamp=t, location="nl", quality="good",
        )
    return readings


def _register_old(client: SensorThingsClient, order: list[Station], only: dict[str, list[str]] | None = None) -> None:
    """The old image's registration: entity sets keyed by position."""
    for index, st in enumerate(order):
        entity_set = build_entity_set(str(index), st.name, st.lat, st.lon)
        if only and st.label in only:
            entity_set["sensors"][0]["observed_properties"] = only[st.label]
        result = client.register_entity_set(entity_set)
        assert main._registration_complete(result), result


def _post(endpoint: str, collection: str, payload: dict[str, Any]) -> int:
    response = requests.post(f"{endpoint}/{collection}", json=payload, auth=_AUTH, timeout=15)
    assert response.status_code == 201, response.text
    location = response.headers.get("Location", "")
    return int(location.rsplit("(", 1)[1].rstrip(")"))


def _stale_ground_temperature(env: Env, st: Station, index: int) -> int:
    """S1's Thing, Location and Sensor, and the Datastream an older connector named 'Ground temperature'."""
    thing_name = f"{env.prefix}KNMI {st.name}"
    thing = _post(env.read, "Things", {
        "name": thing_name, "description": f"KNMI official weather station: {st.name}.",
        "properties": {"site": "nl", "source": "knmi_weather", "knmi_station_id": str(index),
                       "station_name": st.name, "network": "KNMI"},
    })
    _post(env.read, "Locations", {
        "name": f"{thing_name} location", "description": f"KNMI measurement station at {st.name}.",
        "encodingType": "application/geo+json", "location": {"type": "Point", "coordinates": [st.lon, st.lat]},
        "Things": [{"@iot.id": thing}],
    })
    sensor = _post(env.read, "Sensors", {
        "name": f"{thing_name} sensor", "description": "KNMI official weather sensor.",
        "encodingType": "application/json", "metadata": "https://developer.dataplatform.knmi.nl",
    })
    observed_property = _post(env.read, "ObservedProperties", {
        "name": f"{env.prefix}Ground temperature", "description": "Ground temperature",
        "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#soil_temperature",
    })
    return _post(env.read, "Datastreams", {
        "name": f"{thing_name} sensor - Ground temperature", "description": "Ground temperature observations",
        "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
        "unitOfMeasurement": {"name": "Ground temperature", "symbol": "°C", "definition": "http://unitsofmeasure.org"},
        "Thing": {"@iot.id": thing}, "Sensor": {"@iot.id": sensor}, "ObservedProperty": {"@iot.id": observed_property},
        "properties": {"site_key": "nl", "thing_name": thing_name, "sensor_id": f"knmi-{index}",
                       "observed_property": "ground_temperature", "streamKey": "ground_temperature"},
    })


def _old_image_history(env: Env) -> SensorThingsClient:
    """Replay what the old image did; returns its running process (the one after the restart)."""
    _stale_ground_temperature(env, S1, index=1)
    first = env.process()
    _register_old(first, [S0, S1, S2], only={"S2": ["temperature", "humidity"]})
    result = first.push_observations(_old_readings([S0, S1, S2], T1), source="knmi_weather")
    assert result["total_sent"] == 8  # S2 has no ground temperature Datastream yet
    # S3 reports from now on, at position 1; this process registered positions once, at startup.
    first.push_observations(_old_readings([S0, S3, S1, S2], T2), source="knmi_weather")
    restarted = env.process()
    _register_old(restarted, [S0, S3, S1, S2])
    result = restarted.push_observations(_old_readings([S0, S3, S1, S2], T3), source="knmi_weather")
    assert result["total_sent"] == 12
    return restarted


@dataclasses.dataclass
class NewImage:
    """This branch's connector after a (re)start: warm-up, restart seeding, the KNMI polling loop's steps."""

    client: SensorThingsClient
    source: KNMIWeatherPollingSource
    registry: Any
    marks: dict[str, datetime]

    @classmethod
    def start(cls, env: Env) -> NewImage:
        client = env.process()
        client.warm_datastream_cache()
        marks = main._seed_timestamps_from_frost("knmi_weather")
        return cls(client, KNMIWeatherPollingSource(), main._EntitySetRegistry(), marks)

    def cycle(self, area: dict[str, Any]) -> tuple[list[SensorReading], list[SensorReading], dict[str, Any]]:
        """One polling cycle, as _polling_ingest_loop runs it: fetch, register new sets, dedup, push."""
        _FakeEdr.area = area
        readings = asyncio.run(self.source.fetch_readings())
        assert not asyncio.run(main._register_new_entity_sets(self.source, self.registry))
        new = main._dedup_readings(readings, self.marks)
        result: dict[str, Any] = {"total_sent": 0, "results": []}
        if new:
            result = self.client.push_observations(new, source="knmi_weather")
            main._update_timestamps(new, self.marks)
        return readings, new, result


# ---------------------------------------------------------------------------
# Reading FROST back (through v1.1)
# ---------------------------------------------------------------------------


def _get(url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    response = requests.get(url, params=params, auth=_AUTH, timeout=30)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _things(env: Env) -> dict[str, dict[str, Any]]:
    body = _get(f"{env.read}/Things", {
        "$filter": f"startswith(name,'{env.prefix}KNMI ')",
        "$expand": "Datastreams($top=1000;$expand=Observations($top=1000;$select=phenomenonTime,result,parameters))",
        "$top": "100",
    })
    return {thing["name"]: thing for thing in body["value"]}


def _count(env: Env, collection: str) -> int:
    body = _get(f"{env.read}/{collection}", {"$filter": f"startswith(name,'{env.prefix}')", "$count": "true", "$top": "0"})
    return int(body["@iot.count"])


def _counts(env: Env) -> dict[str, int]:
    return {c: _count(env, c) for c in ("Things", "Locations", "Sensors", "Datastreams")}


def _time(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _rows_at(env: Env, t: datetime) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """(Thing name, Datastream, Observation) of every Observation at ``t``."""
    rows = []
    for name, thing in _things(env).items():
        for ds in thing["Datastreams"]:
            for obs in ds["Observations"]:
                if _time(obs["phenomenonTime"]) == t:
                    rows.append((name, ds, obs))
    return rows


def _misplaced(env: Env, readings: list[SensorReading]) -> list[str]:
    """Readings that are not exactly once in the Datastream the connector registers for them."""
    problems = []
    times = {r.timestamp for r in readings}
    rows = [row for t in times for row in _rows_at(env, t)]
    for r in readings:
        found = [
            (name, ds) for name, ds, obs in rows
            if _time(obs["phenomenonTime"]) == r.timestamp and obs["parameters"]["sensor_id"] == r.sensor_id
            and obs["parameters"]["observed_property"] == r.observed_property
        ]
        expected = f"{env.prefix}{r.sensor_name} - {r.observed_property_name}"
        if [ds["name"] for _name, ds in found] != [expected]:
            problems.append(f"{r.sensor_id} {r.observed_property}: in {[ds['name'] for _n, ds in found]}")
    return problems


def _sensor_ids(env: Env) -> dict[str, list[str]]:
    return {
        name: sorted({ds["properties"]["sensor_id"] for ds in thing["Datastreams"]})
        for name, thing in _things(env).items()
    }


def _properties(env: Env) -> dict[str, Any]:
    """properties of every Thing, Datastream and Sensor of this test's KNMI Things, by entity."""
    body = _get(f"{env.read}/Things", {
        "$filter": f"startswith(name,'{env.prefix}KNMI ')",
        "$expand": "Datastreams($top=1000;$expand=Sensor)",
        "$top": "100",
    })
    found: dict[str, Any] = {}
    for thing in body["value"]:
        found[f"Things({thing['@iot.id']})"] = thing.get("properties")
        for ds in thing["Datastreams"]:
            found[f"Datastreams({ds['@iot.id']})"] = ds.get("properties")
            found[f"Sensors({ds['Sensor']['@iot.id']})"] = ds["Sensor"].get("properties")
    return found


def _thing_name(env: Env, st: Station) -> str:
    return f"{env.prefix}KNMI {st.name}"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "declared"),
    [("v2.0", None), ("v2.0", "v2.0"), ("v1.1", "v1.1")],
    ids=["v2.0-undeclared", "v2.0-declared", "v1.1"],
)
def test_migrate_then_deploy(env, capsys, endpoint, declared):
    old_image = _old_image_history(env)
    assert _sensor_ids(env) == {
        _thing_name(env, S0): ["knmi-0"],
        _thing_name(env, S1): ["knmi-1"],
        _thing_name(env, S2): ["knmi-2", "knmi-3"],
        _thing_name(env, S3): ["knmi-1"],
    }
    entities = _counts(env)
    assert entities == {"Things": 4, "Locations": 4, "Sensors": 4, "Datastreams": 41}

    # 1. Dry run: the plan, nothing written.
    assert env.migrate(endpoint=endpoint, declared=declared) == migrate.EXIT_OK
    out = capsys.readouterr().out
    assert "4 KNMI Things: 4 to migrate (41 Datastream, 0 Sensor and 4 Thing PATCHes)" in out
    assert "(knmi-2 x2, knmi-3 x8) -> knmi-0-20000-0-06225" in out
    assert f"share the key knmi-{S1.wigos}::ground_temperature" in out
    assert _sensor_ids(env)[_thing_name(env, S0)] == ["knmi-0"]

    # 2. --apply while the old image runs; a journal is required.
    original = _properties(env)
    with pytest.raises(SystemExit):
        env.migrate("--apply", endpoint=endpoint, declared=declared)
    journal = ["--journal", str(env.journal)]
    assert env.migrate("--apply", *journal, endpoint=endpoint, declared=declared) == migrate.EXIT_OK
    out = capsys.readouterr().out
    assert "Read back: every planned entity is on its station id." in out
    migrated = _properties(env)
    records = migrate.read_journal(env.journal)
    assert len(records) == 45 == out.count(f"\n{migrate.JOURNAL_MARKER}")
    assert {f"{r.entity_set}({r.entity_id})": r.old for r in records} == {
        key: value for key, value in original.items() if value != migrated[key]
    }

    # The journal undoes it exactly (dry run first), and the migration runs again.
    assert env.migrate("--rollback", *journal, endpoint=endpoint, declared=declared) == migrate.EXIT_OK
    assert "To restore: 45 (41 Datastreams, 0 Sensors, 4 Things)" in capsys.readouterr().out
    assert _properties(env) == migrated
    assert env.migrate("--rollback", "--apply", *journal, endpoint=endpoint, declared=declared) == migrate.EXIT_OK
    assert "Read back: every restored entity is as before the migration." in capsys.readouterr().out
    assert _properties(env) == original
    assert env.migrate("--apply", *journal, endpoint=endpoint, declared=declared) == migrate.EXIT_OK
    capsys.readouterr()
    assert _properties(env) == migrated
    things = _things(env)
    assert _sensor_ids(env) == {_thing_name(env, st): [st.sensor_id] for st in STATIONS}
    assert {things[_thing_name(env, st)]["properties"]["knmi_station_id"] for st in STATIONS} == {
        st.wigos for st in STATIONS
    }
    assert _counts(env) == entities

    # The old image keeps routing as before: its cache maps positions to datastream ids.
    window = _old_readings([S0, S3, S1, S2], T4)
    assert old_image.push_observations(window, source="knmi_weather")["total_sent"] == 12
    assert _misplaced(env, window) == []
    # Were it restarted now, its registration would still find everything by name (and
    # write its position keys to the persistent cache again).
    _register_old(env.process(), [S0, S3, S1, S2])
    assert _counts(env) == entities

    # 3. Deploy: the new image starts.
    new_image = NewImage.start(env)
    assert {f"{st.sensor_id}::{p}" for st in STATIONS for p in PROPERTIES} <= set(new_image.marks)
    assert all(new_image.marks[f"{st.sensor_id}::{p}"] == T4 for st in STATIONS for p in PROPERTIES)
    cached = json.loads(env.cache_file.read_text(encoding="utf-8"))["datastreams"]
    assert not [key for key in cached if re.match(r"knmi-\d+::", key)]  # positions purged

    # Its first cycle sees T4 again: the old image pushed it, so nothing is posted twice.
    readings, new, _ = new_image.cycle(_area([S0, S3, S1, S2], T4))
    assert len(readings) == 12
    assert new == []
    # The next cycle, in another order: every reading in its own Datastream, nothing refused.
    readings, new, result = new_image.cycle(_area([S3, S2, S0, S1], T5))
    assert len(new) == 12
    assert result["total_sent"] == 12
    assert not new_image.client._rejected_routes_logged
    assert _misplaced(env, new) == []
    assert {r.sensor_id for r in new} == {st.sensor_id for st in STATIONS}
    assert _counts(env) == entities
    assert all(path.split("?", 1)[0].endswith("/area") for path in _FakeEdr.paths[-2:])

    # 4. Dry run again: nothing left.
    assert env.migrate(endpoint=endpoint, declared=declared) == migrate.EXIT_OK
    assert "4 KNMI Things: 0 to migrate" in capsys.readouterr().out


def test_deploy_then_migrate(env, capsys):
    old_image = _old_image_history(env)
    assert old_image.push_observations(_old_readings([S0, S3, S1, S2], T4), source="knmi_weather")["total_sent"] == 12
    entities = _counts(env)

    # Deploy without migrating: seeding only knows the positions, every cached mapping is refused.
    new_image = NewImage.start(env)
    assert not {f"{st.sensor_id}::{p}" for st in STATIONS for p in PROPERTIES} & set(new_image.marks)
    readings, new, result = new_image.cycle(_area([S0, S3, S1, S2], T4))
    assert len(new) == len(readings) == 12  # T4 is not recognised as pushed...
    assert result["total_sent"] == 12
    assert len(_rows_at(env, T4)) == 24  # ...so FROST now holds it twice
    refused = {key for key, _ds, _prop in new_image.client._rejected_routes_logged}
    assert refused == {f"{st.sensor_id}::{p}" for st in STATIONS for p in PROPERTIES}
    # Delivered by live lookups on Thing name and streamKey. S1 has two Datastreams with
    # streamKey ground_temperature (on production every KNMI Thing has such a pair), and the
    # lookup takes FROST's first match of a $top=1 query without $orderby. PostgreSQL returns
    # those in physical row order, not by id: on a fresh database that is the stale one (lower
    # id), but once earlier tests in this module have updated rows (their migrations PATCH
    # Datastreams), the registered one can come first. Neither branch's code decides it; the
    # integration release (fix/seeding-followups + fix/routing-followups) misplaces it too when
    # this test runs alone on a fresh FROST. So S1's ground temperature is in one of its two
    # ground temperature Datastreams, and every other reading is where it belongs.
    stale = f"{_thing_name(env, S1)} sensor - Ground temperature"
    assert _misplaced(env, new) in ([], [f"{S1.sensor_id} ground_temperature: in ['{stale}']"])
    assert _counts(env) == entities

    # Migrating now does not help the running process: its routes hold the old ids until a restart.
    original = _properties(env)
    assert env.migrate("--apply", "--journal", str(env.journal)) == migrate.EXIT_OK
    capsys.readouterr()
    new_image.client._rejected_routes_logged.clear()
    _, new, result = new_image.cycle(_area([S0, S3, S1, S2], T5))
    assert result["total_sent"] == 12
    assert new_image.client._rejected_routes_logged

    # After a restart the new image routes from the migrated properties.
    restarted = NewImage.start(env)
    _, new, result = restarted.cycle(_area([S0, S3, S1, S2], T6))
    assert result["total_sent"] == 12
    assert not restarted.client._rejected_routes_logged
    assert _misplaced(env, new) == []
    assert Counter(name for name, _ds, _obs in _rows_at(env, T6)) == {_thing_name(env, st): 3 for st in STATIONS}

    # The rollback leaves an entity changed since the migration alone and restores the rest.
    migrated = _properties(env)
    s0_temperature = next(
        ds for ds in _things(env)[_thing_name(env, S0)]["Datastreams"] if ds["properties"]["observed_property"] == "temperature"
    )
    edited = {**s0_temperature["properties"], "note": "edited after the migration"}
    response = requests.patch(f"{env.read}/Datastreams({s0_temperature['@iot.id']})", json={"properties": edited},
                              auth=_AUTH, timeout=30)
    assert response.status_code == 200, response.text
    capsys.readouterr()
    assert env.migrate("--rollback", "--apply", "--journal", str(env.journal)) == migrate.EXIT_LEFT
    out = capsys.readouterr().out
    assert f"changed since the migration, left alone: Datastreams({s0_temperature['@iot.id']})" in out
    after = _properties(env)
    key = f"Datastreams({s0_temperature['@iot.id']})"
    assert after[key] == edited
    assert {k: v for k, v in after.items() if k != key} == {k: v for k, v in original.items() if k != key}
    assert migrated != original
