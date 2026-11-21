"""Integration: TGV Kafka batches against FROST-Server 2.8 on PostgreSQL 16 (PostGIS).

No Kafka: synthetic GreenVillageRecords go through a fake consumer into
app.main._consume_and_push_once, which maps them, registers the TGV entity sets
(entity_configs/tgv_*.yaml) on the primary target and pushes. The target is
configured as production's: the FROST v2.0 endpoint without a declared version
(the connector speaks v1.1 to it), Basic auth, the projects plugin, an entity
name prefix, and a SENSORTHINGS_DATASTREAM_IDS_JSON whose TGV keys point at
other sources' datastreams.

FROST (``TGV_IT_FROST_IMAGE``, default fraunhoferiosb/frost-server:2.8) and
PostGIS run under a docker compose project named ``lntgv-<random>``, the
database on tmpfs, FROST on 127.0.0.1 only; ``down -v`` removes everything.

    python -m pytest tests/integration/test_tgv_primary_registration.py -m integration
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import time
import uuid
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import requests

import app.main as main
from app.config import _load_frost_targets
from app.config import settings as base_settings
from app.pipeline import kafka_tgv
from app.pipeline.kafka_tgv import TGVPrimaryRegistration, TGVRegistrationPending
from app.services import sensorthings_client as stc
from app.services.sensorthings_client import SensorThingsClient
from app.sources.tgv_kafka_mapping import _DEFAULT_DEVICE_MAPPING

pytestmark = pytest.mark.integration

_AUTH = ("write", "write")
_DEVICE = "Weather Station"
_T0 = datetime(2026, 11, 20, 22, 0, tzinfo=UTC)
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
      serviceRootUrl: http://localhost:PORT/FROST-Server
      persistence_db_driver: org.postgresql.Driver
      persistence_db_url: jdbc:postgresql://db:5432/sensorthings
      persistence_db_username: sensorthings
      persistence_db_password: sensorthings
      persistence_autoUpdateDatabase: "true"
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

# Other sources' datastreams, created first as the connector creates them; the
# stale mapping points the TGV keys at them, at the same property where one exists.
_OTHER_SOURCES = {
    "sensor_community-81543": ["temperature", "pressure", "humidity"],
    "knmi-7": ["wind_direction", "wind_gust", "air_pressure", "solar_radiation", "precipitation", "humidity"],
    "ohnics-de-4073": ["air_temperature"],
}
_STALE_TARGETS = {  # TGV key -> (other sensor, property) whose datastream id the stale mapping names
    "tgv-officelab-climate::temperature": ("sensor_community-81543", "temperature"),
    "tgv-officelab-climate::humidity": ("sensor_community-81543", "humidity"),
    "tgv-officelab-climate::pressure": ("sensor_community-81543", "pressure"),
    "tgv-climate-davis-weather-station::air_temperature": ("ohnics-de-4073", "air_temperature"),
    "tgv-climate-davis-weather-station::wind_direction": ("knmi-7", "wind_direction"),
    "tgv-climate-davis-weather-station::wind_gust": ("knmi-7", "wind_gust"),
    "tgv-climate-davis-weather-station::precipitation": ("knmi-7", "precipitation"),
    "tgv-weather-climatics-rooftop-station::air_pressure": ("knmi-7", "air_pressure"),
    "tgv-hitteplein-climate-station::solar_radiation": ("knmi-7", "solar_radiation"),
    "tgv-hitteplein-climate-station::relative_humidity": ("knmi-7", "humidity"),
    "tgv-officelab-climate": ("sensor_community-81543", "temperature"),
    "air_pressure": ("knmi-7", "air_pressure"),
    "precipitation": ("knmi-7", "precipitation"),
}


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
    image = os.environ.get("TGV_IT_FROST_IMAGE", "fraunhoferiosb/frost-server:2.8")
    compose = _COMPOSE.replace("IMAGE", image).replace("PORT", str(port))
    project = ["docker", "compose", "-p", f"lntgv-{uuid.uuid4().hex[:8]}", "-f", "-"]
    try:
        subprocess.run([*project, "up", "-d"], input=compose, text=True, check=True, capture_output=True, timeout=600)
        root = f"http://127.0.0.1:{port}/FROST-Server"  # not localhost: IPv6 first is slow on Windows
        _wait_until_ready(root)
        yield root
    finally:
        subprocess.run([*project, "down", "-v", "--remove-orphans"], input=compose, text=True, capture_output=True, timeout=180)


# ---------------------------------------------------------------------------
# FROST helpers
# ---------------------------------------------------------------------------


def _get(root: str, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    response = requests.get(f"{root}/v1.1/{path}", params=params, auth=_AUTH, timeout=30)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _all(root: str, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
    body = _get(root, path, {**params, "$top": "1000"})
    assert "@iot.nextLink" not in body
    return list(body["value"])


def _post(root: str, collection: str, payload: dict[str, Any]) -> int:
    response = requests.post(f"{root}/v1.1/{collection}", json=payload, auth=_AUTH, timeout=30)
    assert response.status_code == 201, response.text
    return int(response.headers["Location"].rsplit("(", 1)[1].rstrip(")"))


def _seed_other_sources(root: str, prefix: str) -> dict[tuple[str, str], int]:
    """Things, Sensors and Datastreams of other sources, shaped as the connector registers them."""
    properties = {}
    for display, definition in [
        ("Air temperature", "air_temperature"), ("Relative humidity", "relative_humidity"), ("Air pressure", "air_pressure"),
        ("Wind direction", "wind_from_direction"), ("Wind gust speed", "wind_speed_of_gust"),
        ("Solar radiation", "surface_downwelling_shortwave_flux_in_air"), ("Precipitation", "precipitation_amount"),
    ]:
        found = _get(root, "ObservedProperties", {"$filter": f"name eq '{display}'"})["value"]
        properties[display] = int(found[0]["@iot.id"]) if found else _post(root, "ObservedProperties", {
            "name": display, "description": display,
            "definition": f"https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#{definition}",
        })
    display_of = {
        "temperature": "Air temperature", "air_temperature": "Air temperature", "humidity": "Relative humidity",
        "pressure": "Air pressure", "air_pressure": "Air pressure", "wind_direction": "Wind direction",
        "wind_gust": "Wind gust speed", "solar_radiation": "Solar radiation", "precipitation": "Precipitation",
    }
    ids: dict[tuple[str, str], int] = {}
    for sensor_id, observed_properties in _OTHER_SOURCES.items():
        source = sensor_id.split("-", 1)[0]
        thing = _post(root, "Things", {
            "name": f"{prefix}{sensor_id}", "description": "another source",
            "properties": {"site": source, "source": source},
        })
        sensor = _post(root, "Sensors", {
            "name": f"{prefix}{sensor_id} sensor", "description": "another source", "encodingType": "application/json", "metadata": "",
        })
        for observed_property in observed_properties:
            display = display_of[observed_property]
            ids[(sensor_id, observed_property)] = _post(root, "Datastreams", {
                "name": f"{prefix}{sensor_id} sensor - {display}",
                "description": f"{display} observations",
                "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                "unitOfMeasurement": {"name": display, "symbol": "1", "definition": "https://example.org/unit"},
                "Thing": {"@iot.id": thing},
                "Sensor": {"@iot.id": sensor},
                "ObservedProperty": {"@iot.id": properties[display]},
                "properties": {"sensor_id": sensor_id, "observed_property": observed_property, "streamKey": observed_property},
            })
    return ids


def _tgv_things(root: str, prefix: str) -> list[dict[str, Any]]:
    return [t for t in _all(root, "Things", {"$filter": f"startswith(name,'{prefix}')"}) if t["properties"].get("site") == "tgv"]


def _tgv_datastreams(root: str, prefix: str) -> dict[str, dict[str, Any]]:
    """Datastreams of the TGV Things by sensor_id::observed_property; each key once."""
    datastreams = _all(root, "Datastreams", {
        "$filter": f"startswith(Thing/name,'{prefix}') and Thing/properties/site eq 'tgv'",
        "$expand": "Thing($select=name,properties)",
    })
    keys = [f"{d['properties']['sensor_id']}::{d['properties']['observed_property']}" for d in datastreams]
    assert len(keys) == len(set(keys)), Counter(keys).most_common(3)
    return dict(zip(keys, datastreams, strict=True))


def _observations(root: str, datastream_id: int) -> list[dict[str, Any]]:
    return _all(root, f"Datastreams({datastream_id})/Observations", {"$select": "phenomenonTime,parameters"})


# ---------------------------------------------------------------------------
# The connector, configured as on production, fed by a fake Kafka consumer
# ---------------------------------------------------------------------------


def _record(minutes: int) -> dict[str, Any]:
    return {
        "device_id": _DEVICE,
        "timestamp": int((_T0 + timedelta(minutes=minutes)).timestamp() * 1000),
        "measurements": [
            {"measurement_id": key.split("/", 1)[1], "value": {"double": 10.0 + n + minutes / 100}, "unit": None, "measurement_description": None}
            for n, key in enumerate(_DEFAULT_DEVICE_MAPPING)
        ],
    }


class _Consumer:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.commits = 0

    def consume_batch(self, max_messages: int = 100, timeout: float = 5.0) -> list[dict[str, Any]]:
        batch, self.records = self.records[:max_messages], self.records[max_messages:]
        return batch

    def commit(self) -> None:
        self.commits += 1


class _Connector:
    """Starts connector processes (settings, client, startup warm-up) against the FROST under test."""

    def __init__(self, root: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, prefix: str) -> None:
        self.root = root
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.prefix = prefix

    def start(self, *, stale: dict[str, str], password: str = "write") -> SensorThingsClient:
        target = {"url": f"{self.root}/v2.0", "label": "frost-local", "auth_username": "write", "auth_password": password}
        for name in ("SENSORTHINGS_BASE_URL", "SENSORTHINGS_AUTH_USERNAME", "SENSORTHINGS_AUTH_PASSWORD", "SENSORTHINGS_AUTH_TOKEN"):
            self.monkeypatch.delenv(name, raising=False)
        self.monkeypatch.setenv("SENSORTHINGS_BASE_URLS", json.dumps([target]))
        targets = _load_frost_targets()
        assert [(t.url, t.version) for t in targets] == [(f"{self.root}/v2.0", "v1.1")]  # undeclared: v1.1 payloads
        self.monkeypatch.setattr(stc, "settings", dataclasses.replace(
            base_settings,
            sensorthings_base_url="",
            sensorthings_base_urls=(target["url"],),
            frost_targets=targets,
            auth_username="write",
            auth_password=password,
            auth_token="",
            entity_name_prefix=self.prefix,
            registered_entities_path=str(self.tmp_path / "registered_entities.json"),
            failed_observations_path=str(self.tmp_path / "failed_observations.jsonl"),
            datastream_ids=dict(stale),
            default_project_name="GI-Fieldlab",
        ))
        client = SensorThingsClient()
        self.monkeypatch.setattr(stc, "client", client)
        self.monkeypatch.setattr(main, "client", client)
        self.monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, frost_async_push_enabled=False))
        self.monkeypatch.setattr(kafka_tgv, "tgv_primary", TGVPrimaryRegistration())  # resolves stc.client
        self.monkeypatch.setattr("app.services.alerting.send_alert", lambda *_a, **_kw: False)
        client.warm_datastream_cache()  # as at startup
        return client

    def forget_cache(self) -> None:
        for path in self.tmp_path.glob("*.json"):
            path.unlink()


@pytest.fixture
def connector(frost_root: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Connector:
    return _Connector(frost_root, monkeypatch, tmp_path, f"IT{uuid.uuid4().hex[:8]}_")


def test_kafka_batches_register_tgv_once_and_land_in_tgv_datastreams(connector: _Connector) -> None:
    root, prefix = connector.root, connector.prefix
    others = _seed_other_sources(root, prefix)
    stale = {key: str(others[target]) for key, target in _STALE_TARGETS.items()}

    client = connector.start(stale=stale)
    # The warm-up listed the other datastreams: stale full keys are gone, bare ones remain.
    assert not any("::" in key and key.startswith("tgv-") for key in client._datastream_ids)
    consumer = _Consumer([_record(0), _record(1)])
    summary = main._consume_and_push_once(consumer)

    assert summary["pulled"] == 2 and summary["mapped"] == 34 and summary["pushed"] == 34
    assert consumer.commits == 1
    things = _tgv_things(root, prefix)
    assert sorted(t["name"] for t in things) == sorted(
        f"{prefix}{name}" for name in ("Climate Davis", "Hitteplein", "TGV Office Lab", "Weather Climatics")
    )
    assert all(t["properties"]["source"] == "kafka" for t in things)
    datastreams = _tgv_datastreams(root, prefix)
    assert len(datastreams) == 19
    routes = kafka_tgv.tgv_primary.datastream_ids()
    assert {key: str(ds["@iot.id"]) for key, ds in datastreams.items()} == routes

    def check_observations(expected_per_stream: int) -> None:
        for key, datastream in datastreams.items():
            observations = _observations(root, datastream["@iot.id"])
            streamed = key not in ("tgv-officelab-climate::co2", "tgv-officelab-climate::pressure")
            assert len(observations) == (expected_per_stream if streamed else 0), key
            for observation in observations:
                parameters = observation["parameters"]
                assert f"{parameters['sensor_id']}::{parameters['observed_property']}" == key
                assert parameters["location"] == "tgv" and parameters["device_eui"] == _DEVICE
        for ds_id in others.values():
            assert _observations(root, ds_id) == []

    check_observations(2)

    # A restart: the entity cache is there, the stale mapping still set.
    client = connector.start(stale=stale)
    assert main._consume_and_push_once(_Consumer([_record(2)]))["pushed"] == 17
    assert len(_tgv_things(root, prefix)) == 4
    assert _tgv_datastreams(root, prefix).keys() == datastreams.keys()
    assert kafka_tgv.tgv_primary.datastream_ids() == routes
    check_observations(3)

    # A restart that lost the entity cache finds everything by name.
    connector.forget_cache()
    connector.start(stale=stale)
    assert main._consume_and_push_once(_Consumer([_record(3)]))["pushed"] == 17
    assert len(_tgv_things(root, prefix)) == 4
    assert kafka_tgv.tgv_primary.datastream_ids() == routes
    check_observations(4)
    for display in ("Air temperature", "Relative humidity", "Dew point temperature"):
        assert len(_all(root, "ObservedProperties", {"$filter": f"name eq '{display}'"})) == 1, display


def test_a_batch_is_held_while_frost_refuses_the_registration(connector: _Connector) -> None:
    connector.start(stale={}, password="not-the-password")
    consumer = _Consumer([_record(0)])

    with pytest.raises(TGVRegistrationPending):
        main._consume_and_push_once(consumer)

    assert consumer.commits == 0
    assert _tgv_things(connector.root, connector.prefix) == []
