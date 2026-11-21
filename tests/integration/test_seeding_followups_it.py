"""Integration: restart seeding and high-water marks against FROST-Server 2.8 on PostgreSQL 16 (PostGIS).

The target is configured as production's: the FROST v2.0 endpoint without a declared version (the
connector speaks v1.1 to it, FROST answers with "id" and "@nextLink"), Basic auth with anonymous
reads off, the projects plugin, an entity name prefix, and more than 1,000 Datastreams of one
source, so the seed pages. The real polling loop runs with the real client; only the upstream APIs
are fakes. Each "connector run" builds a new client from the entity cache on disk, as a restart does.

* A restart posts nothing again for Samen Meten (Datastreams keyed on the remote Thing's name, its
  readings on the Thing's id) or Ohnics (Datastreams registered as "pm25", readings "pm2_5").
* A foreign newest row (a TGV reading in an Ohnics Datastream) does not drop an Ohnics reading.
* Readings of entity sets whose registration failed or was deferred are pushed once registered.
* Sets FROST refuses for good do not starve sets discovered later, all with one site_key.

FROST (``SEED_IT_FROST_IMAGE``, default fraunhoferiosb/frost-server:2.8) and PostGIS run under a
docker compose project named ``fxseed-<random>``, the database on tmpfs, FROST on 127.0.0.1 only;
``down -v`` removes everything.

    python -m pytest tests/integration/test_seeding_followups_it.py -m integration
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import socket
import subprocess
import time
import types
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests

import app.main as main
from app.config import _load_frost_targets
from app.config import settings as base_settings
from app.models import SensorReading
from app.services import sensorthings_client as stc
from app.services.ohnics_source import OhnicsPollingSource
from app.services.samen_meten_source import SamenMetenPollingSource
from app.services.sensorthings_client import SensorThingsClient

pytestmark = pytest.mark.integration

_AUTH = ("write", "write")
_T9 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
_COMPOSE = """
services:
  db:
    image: postgis/postgis:16-3.4-alpine
    environment: {POSTGRES_DB: sensorthings, POSTGRES_USER: sensorthings, POSTGRES_PASSWORD: sensorthings}
    tmpfs: [/var/lib/postgresql/data]
    healthcheck: {test: ["CMD-SHELL", "pg_isready -h 127.0.0.1 -U sensorthings"], interval: 3s, timeout: 3s, retries: 40}
  frost:
    image: IMAGE
    depends_on: {db: {condition: service_healthy}}
    environment:
      JAVA_OPTS: -Xmx768m
      serviceRootUrl: http://127.0.0.1:PORT/FROST-Server
      persistence_db_driver: org.postgresql.Driver
      persistence_db_url: jdbc:postgresql://db:5432/sensorthings
      persistence_db_username: sensorthings
      persistence_db_password: sensorthings
      persistence_autoUpdateDatabase: "true"
      persistence_idGenerationMode: ServerAndClientGenerated
      plugins_projects_enable: "true"
      plugins_projects_enableDefaultRules: "true"
      plugins_actuation_enable: "true"
      plugins_openCitySense_enable: "true"
      plugins_modelLoader_enable: "true"
      auth_provider: de.fraunhofer.iosb.ilt.frostserver.auth.basic.BasicAuthProvider
      auth_db_driver: org.postgresql.Driver
      auth_db_url: jdbc:postgresql://db:5432/sensorthings
      auth_db_username: sensorthings
      auth_db_password: sensorthings
      auth_autoUpdateDatabase: "true"
      auth_allowAnonymousRead: "false"
    ports: ["127.0.0.1:PORT:8080"]
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(root: str, timeout: float = 300.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if all(
                requests.get(f"{root}/{version}/Things", auth=_AUTH, timeout=5).status_code == 200
                for version in ("v1.1", "v2.0")
            ):
                assert requests.get(f"{root}/v2.0/Things", timeout=5).status_code == 401  # no anonymous reads
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise TimeoutError(f"FROST at {root} did not serve v1.1 and v2.0 within {timeout:.0f}s")


@pytest.fixture(scope="module")
def frost_root() -> Iterator[str]:
    try:
        subprocess.run(["docker", "compose", "version"], check=True, capture_output=True, timeout=20)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("docker compose is not available")
    port = _free_port()
    image = os.environ.get("SEED_IT_FROST_IMAGE", "fraunhoferiosb/frost-server:2.8")
    compose = _COMPOSE.replace("IMAGE", image).replace("PORT", str(port))
    project = ["docker", "compose", "-p", f"fxseed-{uuid.uuid4().hex[:8]}", "-f", "-"]
    try:
        subprocess.run([*project, "up", "-d"], input=compose, text=True, check=True, capture_output=True, timeout=600)
        root = f"http://127.0.0.1:{port}/FROST-Server"  # not localhost: IPv6 first is slow on Windows
        _wait_until_ready(root)
        yield root
    finally:
        subprocess.run([*project, "down", "-v", "--remove-orphans"], input=compose, text=True, capture_output=True, timeout=180)


# ---------------------------------------------------------------------------
# FROST helpers (v1.1 endpoint, Basic auth)
# ---------------------------------------------------------------------------


def _post(root: str, path: str, payload: dict[str, Any]) -> int:
    response = requests.post(f"{root}/v1.1/{path}", json=payload, auth=_AUTH, timeout=60)
    assert response.status_code == 201, response.text
    return int(response.headers["Location"].rsplit("(", 1)[1].rstrip(")"))


def _add_fillers(root: str, prefix: str, source: str, count: int) -> None:
    """``count`` Datastreams without Observations on one Thing of ``source``, ahead of the real ones."""
    observed_property = _post(root, "ObservedProperties", {"name": f"{prefix}filler", "definition": "x", "description": "d"})
    sensor = _post(root, "Sensors", {"name": f"{prefix}filler", "description": "d", "encodingType": "application/json", "metadata": "m"})
    thing = _post(root, "Things", {"name": f"{prefix}filler {source}", "description": "d", "properties": {"source": source}})
    for start in range(0, count, 200):
        batch = [{"id": f"r{i}", "method": "post", "url": "Datastreams", "body": {
            "name": f"{prefix}filler {source} {i}", "description": "d",
            "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
            "unitOfMeasurement": {"name": "c", "symbol": "1", "definition": "x"},
            "Thing": {"@iot.id": thing}, "Sensor": {"@iot.id": sensor}, "ObservedProperty": {"@iot.id": observed_property},
            "properties": {"sensor_id": f"filler-{i}", "observed_property": "pm10", "streamKey": "pm10"},
        }} for i in range(start, min(count, start + 200))]
        response = requests.post(f"{root}/v1.1/$batch", json={"requests": batch}, auth=_AUTH, timeout=120)
        response.raise_for_status()
        assert {r["status"] for r in response.json()["responses"]} == {201}


def _stored(root: str, prefix: str, sensor_id: str) -> dict[str, list[tuple[str, str]]]:
    """Per observed property of the sensor's Datastreams: (HH:MM, labelled sensor_id) of each Observation."""
    response = requests.get(f"{root}/v1.1/Datastreams", params={
        "$filter": f"startswith(name,'{prefix}') and properties/sensor_id eq '{sensor_id}'",
        "$select": "properties",
        "$expand": "Observations($select=phenomenonTime,parameters;$orderby=phenomenonTime asc;$top=1000)",
    }, auth=_AUTH, timeout=60)
    response.raise_for_status()
    return {
        row["properties"]["observed_property"]: [
            (datetime.fromisoformat(o["phenomenonTime"].replace("Z", "+00:00")).strftime("%H:%M"),
             (o.get("parameters") or {}).get("sensor_id"))
            for o in row["Observations"]
        ]
        for row in response.json()["value"]
    }


# ---------------------------------------------------------------------------
# The connector, configured as on production
# ---------------------------------------------------------------------------


class _Stop(BaseException):
    """Ends a polling loop after its scripted cycles."""


class _Connector:
    """Starts connector processes (settings, client, startup warm-up) against the FROST under test."""

    def __init__(self, root: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prefix: str) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.prefix = prefix
        self.posted: dict[str, int] = {}
        self.seed_lines: list[str] = []

    def start(self) -> SensorThingsClient:
        """A new process: settings from the environment, a client built from the entity cache on disk."""
        target = {"url": f"{self.root}/v2.0", "label": "frost-local", "auth_username": "write", "auth_password": "write"}
        for name in ("SENSORTHINGS_BASE_URL", "SENSORTHINGS_AUTH_USERNAME", "SENSORTHINGS_AUTH_PASSWORD", "SENSORTHINGS_AUTH_TOKEN"):
            self.monkeypatch.delenv(name, raising=False)
        self.monkeypatch.setenv("SENSORTHINGS_BASE_URLS", json.dumps([target]))
        targets = _load_frost_targets()
        assert [(t.url, t.version) for t in targets] == [(f"{self.root}/v2.0", "v1.1")]  # undeclared: v1.1 payloads
        configured = dataclasses.replace(
            base_settings,
            sensorthings_base_url="",
            sensorthings_base_urls=(target["url"],),
            frost_targets=targets,
            auth_username="write",
            auth_password="write",
            auth_token="",
            entity_name_prefix=self.prefix,
            registered_entities_path=str(self.tmp_path / "registered_entities.json"),
            failed_observations_path=str(self.tmp_path / "failed_observations.jsonl"),
            datastream_ids={},
            frost_async_push_enabled=False,
        )
        self.monkeypatch.setattr(stc, "settings", configured)
        self.monkeypatch.setattr(main, "settings", configured)
        client = SensorThingsClient()
        self.monkeypatch.setattr(stc, "client", client)
        self.monkeypatch.setattr(main, "client", client)
        self.monkeypatch.setattr("app.services.alerting.send_alert", lambda *_a, **_kw: False)
        client.warm_datastream_cache()  # as lifespan() does at startup
        self.posted = {}
        push = client.push_observations

        def counting_push(readings: list[SensorReading], *, source: str = "unknown") -> dict[str, Any]:
            result = push(readings, source=source)
            self.posted[source] = self.posted.get(source, 0) + int(result.get("total_sent", 0))
            return result

        self.monkeypatch.setattr(client, "push_observations", counting_push)
        return client

    def run(self, source: Any, cycles: int = 1) -> None:
        """Run the real polling loop for ``cycles`` fetches; its sleeps return at once."""
        fetch = source.fetch_readings
        fetched = {"n": 0}

        async def fetch_scripted() -> list[SensorReading]:
            if fetched["n"] >= cycles:
                raise _Stop
            fetched["n"] += 1
            return list(await fetch())

        async def no_sleep(_seconds: float) -> None:
            return None

        source.fetch_readings = fetch_scripted
        self.monkeypatch.setattr(main, "asyncio", types.SimpleNamespace(sleep=no_sleep))
        handler = _SeedLines(self.seed_lines)
        events = logging.getLogger("connector.events")
        events.addHandler(handler)
        try:
            with pytest.raises(_Stop):
                asyncio.run(main._polling_ingest_loop(source))
        finally:
            events.removeHandler(handler)


class _SeedLines(logging.Handler):
    def __init__(self, lines: list[str]) -> None:
        super().__init__(logging.INFO)
        self.lines = lines

    def emit(self, record: logging.LogRecord) -> None:
        if record.getMessage().startswith("Seeded"):
            self.lines.append(record.getMessage())


@pytest.fixture
def connector(frost_root: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Connector:
    return _Connector(frost_root, monkeypatch, tmp_path, f"IT{uuid.uuid4().hex[:8]}_")


# ---------------------------------------------------------------------------
# Upstream fakes
# ---------------------------------------------------------------------------


class _SamenMetenAPI:
    """Two remote Things with a PM2.5 and a PM10 Datastream; every fetch serves a 1 h look-back of 3 Observations."""

    def __init__(self, first_id: int) -> None:
        self.first_id = first_id

    async def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if path.startswith("/Things"):
            return {"value": [
                {"@iot.id": self.first_id + k, "name": f"OHN_gm-{self.first_id + k}",
                 "Locations": [{"location": {"type": "Point", "coordinates": [4.3 + k / 100, 52.0]}}],
                 "Datastreams": [
                     {"@iot.id": 10 * k + 1, "name": "pm25", "ObservedProperty": {"name": "pm25"}},
                     {"@iot.id": 10 * k + 2, "name": "pm10", "ObservedProperty": {"name": "pm10"}},
                 ]}
                for k in range(2)
            ]}
        return {"value": [{"phenomenonTime": f"2026-11-20T09:{m:02d}:00Z", "result": 5.0 + m / 10} for m in (0, 10, 20)]}


class _OhnicsAPI:
    def __init__(self, at: str = "10:00") -> None:
        self.at = at

    async def get(self, url: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        return [{"Name": f"de-{k}", "Lat": 52.0, "Long": 4.36 + k / 100, "P2": 3.0 + k, "T": 9.5,
                 "Timestamp": f"2026-11-20T{self.at}:00Z"} for k in range(3)]


def _samen_meten(first_id: int) -> SamenMetenPollingSource:
    source = SamenMetenPollingSource()
    source._client = _SamenMetenAPI(first_id)
    return source


def _ohnics(at: str = "10:00") -> OhnicsPollingSource:
    source = OhnicsPollingSource()
    source._client = _OhnicsAPI(at)
    source._prefix = "de-"
    return source


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_a_restart_posts_nothing_again_for_samen_meten_and_ohnics(connector: _Connector) -> None:
    root, prefix = connector.root, connector.prefix
    _add_fillers(root, prefix, "samen_meten", 1100)
    first_id = 4000 + int(prefix[2:10], 16) % 1000 * 10
    posted = []
    for _run in range(2):
        connector.start()
        connector.run(_samen_meten(first_id))
        connector.run(_ohnics())
        posted.append(dict(connector.posted))

    assert posted == [{"samen_meten": 12, "ohnics": 6}, {}]
    for k in range(2):
        stored = _stored(root, prefix, f"sm-ohn_gm-{first_id + k}")
        assert stored == {prop: [(f"09:{m:02d}", f"sm-{first_id + k}") for m in (0, 10, 20)] for prop in ("pm2_5", "pm10")}
    for k in range(3):
        assert _stored(root, prefix, f"ohnics-de-{k}") == {
            "pm25": [("10:00", f"ohnics-de-{k}")], "air_temperature": [("10:00", f"ohnics-de-{k}")]
        }
    # The restart's seeds paged through the 1,100 fillers and marked the readings' own keys.
    restart_samen_meten = connector.seed_lines[-2]
    assert restart_samen_meten.startswith("Seeded 4 timestamps from FROST for samen_meten (1104 datastreams in 3 page(s); ")
    assert "labelled with the datastream's key: 0, with another key: 4, unlabelled: 0, none: 1100;" in restart_samen_meten
    assert connector.seed_lines[-1].startswith("Seeded 6 timestamps from FROST for ohnics (6 datastreams in 1 page(s); "
                                               "newest Observation labelled with the datastream's key: 6, ")


def test_a_foreign_newest_row_drops_no_reading(connector: _Connector) -> None:
    root = connector.root
    connector.start()
    connector.run(_ohnics("10:50"))
    datastream = requests.get(f"{root}/v1.1/Datastreams", params={
        "$filter": "properties/sensor_id eq 'ohnics-de-1' and properties/observed_property eq 'air_temperature' "
                   f"and startswith(name,'{connector.prefix}')",
        "$select": "id",
    }, auth=_AUTH, timeout=30).json()["value"][0]["@iot.id"]
    # Before the routing fix, a TGV reading could land in this Datastream: now its newest row.
    _post(root, "Observations", {
        "phenomenonTime": "2026-11-20T10:57:00Z", "result": 4.2, "Datastream": {"@iot.id": datastream},
        "parameters": {"sensor_id": "tgv-weatherstation-1", "observed_property": "air_temperature"},
    })

    connector.start()
    connector.run(_ohnics("10:55"))

    assert connector.posted == {"ohnics": 6}
    assert _stored(root, connector.prefix, "ohnics-de-1")["air_temperature"] == [
        ("10:50", "ohnics-de-1"), ("10:55", "ohnics-de-1"), ("10:57", "tgv-weatherstation-1"),
    ]
    assert "with another key: 1," in connector.seed_lines[-1]


def _station(prefix: str, number: str, *, refused: bool = False) -> dict[str, Any]:
    """A Luchtmeetnet entity set (every station has site_key "luchtmeetnet")."""
    name = f"Station {prefix}{number}"
    return {
        "site_key": "luchtmeetnet",
        "thing": {"name": f"Luchtmeetnet {name}", "description": f"Station {number}",
                  "properties": {"site": "luchtmeetnet", "source": "luchtmeetnet", "station_number": number}},
        "location": {
            "name": f"Luchtmeetnet {name} location", "description": f"Station {number}",
            # FROST answers 400 to a Location without an encodingType: this set never registers.
            "encodingType": None if refused else "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.5, 52.0]}, "properties": {},
        },
        "sensors": [{"sensor_id": f"luchtmeetnet-{prefix}{number}", "name": f"Luchtmeetnet {name} sensor",
                     "description": "NO2", "encodingType": "application/json", "metadata": "https://www.luchtmeetnet.nl",
                     "observed_properties": ["no2"], "properties": {}}],
        "observed_properties": {"no2": "Nitrogen dioxide in ambient air."},
    }


class _Luchtmeetnet:
    """Discovers its stations with the first fetch; serves a look-back: each cycle repeats the earlier hours."""

    source_name = "luchtmeetnet"

    def __init__(self, prefix: str, stations: dict[int, list[tuple[str, bool]]]) -> None:
        self.prefix = prefix
        self.stations = stations  # cycle -> [(number, refused)] known from that cycle on
        self.cycle = 0

    def poll_interval(self) -> int:
        return 3600

    def known(self) -> list[tuple[str, bool]]:
        return [station for cycle, stations in self.stations.items() if cycle <= self.cycle for station in stations]

    def entity_sets(self) -> list[dict[str, Any]]:
        return [_station(self.prefix, number, refused=refused) for number, refused in self.known()]

    async def fetch_readings(self) -> list[SensorReading]:
        self.cycle += 1
        return [
            SensorReading(
                sensor_id=f"luchtmeetnet-{self.prefix}{number}", sensor_name=f"Luchtmeetnet Station {number} sensor",
                observed_property="no2", observed_property_name="Nitrogen dioxide", unit="ug/m3", value=20.0,
                timestamp=_T9 + timedelta(hours=hour), thing_name=f"Luchtmeetnet Station {self.prefix}{number}",
                location="luchtmeetnet",
            )
            for number, _refused in self.known() for hour in range(self.cycle)
        ]


def test_deferred_sets_lose_nothing_and_refused_sets_starve_none(connector: _Connector) -> None:
    root, prefix = connector.root, connector.prefix
    client = connector.start()
    register = client.register_entity_set
    attempts: list[list[str]] = []
    transient = {"NL02": 1, "NL03": 1, "NL04": 1}  # FROST hiccups: each fails once

    def flaky_register(entity_set: dict[str, Any]) -> dict[str, Any]:
        number = entity_set["thing"]["properties"]["station_number"]
        attempts[-1].append(number)
        if transient.get(number):
            transient[number] -= 1
            return {"mode": "live", "primary_ok": False, "primary_error": "Primary target unreachable (see logs)."}
        return register(entity_set)

    connector.monkeypatch.setattr(client, "register_entity_set", flaky_register)
    source = _Luchtmeetnet(prefix, {
        1: [(f"NL0{n}", False) for n in range(1, 7)] + [(f"NL{n}", True) for n in (71, 72, 73)],
        2: [("NL99", False)],
    })
    fetch = source.fetch_readings

    async def fetch_and_count() -> list[SensorReading]:
        attempts.append([])
        return await fetch()

    source.fetch_readings = fetch_and_count  # type: ignore[method-assign]

    connector.run(source, cycles=3)

    # Cycle 1: NL02-NL04 fail in a row and defer the rest. Cycle 2: the sets not tried yet go first;
    # the three FROST refuses defer NL99 once. Cycle 3: NL99 first, then the rest.
    assert attempts == [
        ["NL01", "NL02", "NL03", "NL04"],
        ["NL05", "NL06", "NL71", "NL72", "NL73"],
        ["NL99", "NL02", "NL03", "NL04", "NL71", "NL72", "NL73"],
    ]
    # The look-back serves every earlier hour again: each reading is in FROST exactly once, none lost.
    for number in ["NL01", "NL02", "NL03", "NL04", "NL05", "NL06", "NL99"]:
        assert _stored(root, prefix, f"luchtmeetnet-{prefix}{number}") == {
            "no2": [(at, f"luchtmeetnet-{prefix}{number}") for at in ("09:00", "10:00", "11:00")]
        }, number
