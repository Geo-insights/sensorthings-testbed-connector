"""Routing follow-ups against FROST-Server 2.8 on PostGIS 16, in production shape.

Production's only target is FROST 2.8's /v2.0 URL declared without a version:
the connector speaks v1.1 to it, and FROST answers in v2.0 conventions ("id",
"@nextLink"). Before the fix the warm-up read only "@iot.nextLink" there, took
the first page of 1,000 datastreams for the whole listing, and purged every
cached mapping to a datastream beyond it.

The stack has Basic auth (write/write, no anonymous reads), a serviceRootUrl the
client cannot reach (FROST's next links point there, as behind a proxy), and
more than 2,500 datastreams, so the listing takes several pages. The tests:

* the warm-up lists every datastream, through the configured base URL only,
  and purges only what is stale or crosses properties;
* a reading beyond the first page is pushed into its own datastream;
* a dead-lettered line naming another property's datastream is replayed into
  the reading's own one.

FROST comes from ``ROUTING_FROST_URL`` (a service root such as
``http://127.0.0.1:8080/FROST-Server``, Basic auth write/write), or else from
two throwaway containers (``ROUTING_IT_DOCKER_PREFIX``, default ``routing-it``),
removed afterwards.

    python -m pytest tests/integration/test_routing_followups_frost.py -m integration
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import requests

from app.models import SensorReading
from app.services import sensorthings_client as stc
from app.services.sensorthings_client import SensorThingsClient
from tests.test_sensorthings_client_routing import _settings

pytestmark = pytest.mark.integration

_AUTH = ("write", "write")
_FROST_IMAGE = "fraunhoferiosb/frost-server:2.8"
_POSTGIS_IMAGE = "postgis/postgis:16-3.4-alpine"
_LOCATIONS = 1300  # x 2 properties: 2,600 datastreams of this test, beyond whatever the server already has
_PER_THING = 50
# Own session: the tests record the client's requests.get calls, not these.
HTTP = requests.Session()
HTTP.auth = _AUTH


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=300)


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
    configured = os.getenv("ROUTING_FROST_URL", "").strip().rstrip("/")
    if configured:
        _wait_until_ready(configured)
        yield configured
        return
    if shutil.which("docker") is None:
        pytest.skip("neither ROUTING_FROST_URL nor docker is available")
    name = f"{os.getenv('ROUTING_IT_DOCKER_PREFIX', 'routing-it')}-{uuid.uuid4().hex[:8]}"
    net, db, frost = f"{name}-net", f"{name}-db", f"{name}-frost"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        _docker("network", "create", net)
        _docker(
            "run", "-d", "--name", db, "--network", net,
            "-e", "POSTGRES_DB=sensorthings", "-e", "POSTGRES_USER=sensorthings", "-e", "POSTGRES_PASSWORD=sensorthings",
            _POSTGIS_IMAGE,
        )
        deadline = time.monotonic() + 120
        while _docker("exec", db, "pg_isready", "-h", "127.0.0.1", "-U", "sensorthings", check=False).returncode:
            if time.monotonic() > deadline:
                raise TimeoutError("PostGIS not ready")
            time.sleep(1)
        database = [
            "-e", "persistence_db_driver=org.postgresql.Driver",
            "-e", f"persistence_db_url=jdbc:postgresql://{db}:5432/sensorthings",
            "-e", "persistence_db_username=sensorthings", "-e", "persistence_db_password=sensorthings",
            "-e", "persistence_autoUpdateDatabase=true",
            "-e", "auth_provider=de.fraunhofer.iosb.ilt.frostserver.auth.basic.BasicAuthProvider",
            "-e", "auth_db_driver=org.postgresql.Driver",
            "-e", f"auth_db_url=jdbc:postgresql://{db}:5432/sensorthings",
            "-e", "auth_db_username=sensorthings", "-e", "auth_db_password=sensorthings",
            "-e", "auth_autoUpdateDatabase=true", "-e", "auth_allowAnonymousRead=false",
        ]
        _docker(
            "run", "-d", "--name", frost, "--network", net, "-p", f"127.0.0.1:{port}:8080",
            "-e", "JAVA_OPTS=-Xmx768m",
            # FROST's next links point at its own idea of its root, which the client cannot reach.
            "-e", "serviceRootUrl=http://frost-internal:8080/FROST-Server",
            *database,
            _FROST_IMAGE,
        )
        root = f"http://127.0.0.1:{port}/FROST-Server"  # not localhost: IPv6 first is slow on Windows
        _wait_until_ready(root)
        yield root
    finally:
        _docker("rm", "-f", "-v", frost, db, check=False)  # -v: the database image's anonymous volume
        _docker("network", "rm", net, check=False)


def _post(url: str, body: dict[str, Any]) -> int:
    response = HTTP.post(url, json=body, timeout=60)
    assert response.status_code == 201, response.text
    return int(response.headers["Location"].rsplit("(", 1)[1].rstrip(")"))


def _every_datastream_id(root: str) -> set[str]:
    """All datastream ids, listed independently of the client: v1.1 with its next links, rebased."""
    ids: set[str] = set()
    url: str | None = f"{root}/v1.1/Datastreams"
    params: dict[str, str] | None = {"$select": "id", "$top": "1000", "$orderby": "id asc"}
    while url:
        body = HTTP.get(url, params=params, timeout=60).json()
        ids.update(str(row["@iot.id"]) for row in body["value"])
        next_link = body.get("@iot.nextLink")
        url = f"{root}/v1.1/{next_link.split('/v1.1/', 1)[1]}" if next_link else None
        params = None
    return ids


@pytest.fixture(scope="module")
def seeded(frost_root: str) -> dict[str, Any]:
    """2,600 datastreams of Sensor.Community-like locations (pm10 + pm2_5), labelled as the connector labels them."""
    prefix = f"RT{uuid.uuid4().hex[:6]}"
    v11 = f"{frost_root}/v1.1"
    properties = {
        prop: _post(f"{v11}/ObservedProperties", {"name": f"{prefix} {prop}", "definition": f"urn:{prop}", "description": prop})
        for prop in ("pm10", "pm2_5")
    }
    keys: dict[str, str] = {}
    for first in range(0, _LOCATIONS, _PER_THING):
        thing_name = f"{prefix} Thing {first}"
        sensor = _post(f"{v11}/Sensors", {
            "name": f"{prefix} sensor {first}", "description": "SDS011", "encodingType": "text/plain", "metadata": "",
        })
        datastreams = [
            {
                "name": f"{prefix} sc-{loc} {prop}", "description": prop,
                "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                "unitOfMeasurement": {"name": "ug/m3", "symbol": "ug/m3", "definition": "ucum"},
                "Sensor": {"@iot.id": sensor}, "ObservedProperty": {"@iot.id": properties[prop]},
                "properties": {"sensor_id": f"{prefix}-sc-{loc}", "observed_property": prop, "streamKey": prop},
            }
            for loc in range(first, first + _PER_THING) for prop in ("pm10", "pm2_5")
        ]
        location = {
            "name": f"{prefix} location {first}", "description": "test", "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.36, 52.01]},
        }
        thing = _post(f"{v11}/Things", {
            "name": thing_name, "description": "test", "Locations": [location], "Datastreams": datastreams,
        })
        listed = HTTP.get(
            f"{v11}/Things({thing})/Datastreams", params={"$select": "id,properties", "$top": "1000"}, timeout=60,
        ).json()["value"]
        assert len(listed) == len(datastreams)
        for row in listed:
            label = row["properties"]
            keys[f"{label['sensor_id']}::{label['observed_property']}"] = str(row["@iot.id"])
    return {"prefix": prefix, "keys": keys}


@pytest.fixture
def make_client(frost_root: str, seeded: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A client of ``<root>/<path>`` (no declared version) whose cache holds every seeded key plus two bad ones."""

    def _make(path: str) -> tuple[SensorThingsClient, dict[str, str], list[tuple[str, dict[str, Any]]]]:
        prefix, keys = seeded["prefix"], seeded["keys"]
        bad = {
            f"{prefix}-gone::pm10": "999999999",  # stale: from a previous target
            f"{prefix}-sc-0::humidity": keys[f"{prefix}-sc-0::pm10"],  # crosses properties
        }
        (tmp_path / "registered_entities.json").write_text(json.dumps({"datastreams": {**keys, **bad}}), encoding="utf-8")
        monkeypatch.setattr(stc, "settings", _settings(
            tmp_path, sensorthings_base_url=f"{frost_root}/{path}", auth_password="write", entity_name_prefix="",
            failed_dlq_max_age_hours=48, frost_replay_dedup_probe=False,
        ))
        gets: list[tuple[str, dict[str, Any]]] = []
        real_get = requests.get

        def recording_get(url: str, params: Any = None, **kwargs: Any) -> requests.Response:
            gets.append((url, dict(params or {})))
            return real_get(url, params=params, **kwargs)

        monkeypatch.setattr(stc.requests, "get", recording_get)
        return SensorThingsClient(), bad, gets

    return _make


def _persisted(tmp_path: Path) -> dict[str, str]:
    return json.loads((tmp_path / "registered_entities.json").read_text(encoding="utf-8"))["datastreams"]


def _results(root: str, ds_id: str) -> list[tuple[float, str | None]]:
    body = HTTP.get(
        f"{root}/v1.1/Datastreams({ds_id})/Observations", params={"$select": "result,parameters"}, timeout=60,
    ).json()
    return [(row["result"], (row.get("parameters") or {}).get("observed_property")) for row in body["value"]]


@pytest.mark.parametrize("path", ["v2.0", "v1.1"], ids=["v2.0-undeclared", "v1.1"])
def test_warm_up_lists_every_datastream_and_purges_nothing_valid(frost_root, seeded, make_client, tmp_path, path) -> None:
    client, bad, gets = make_client(path)
    on_server = _every_datastream_id(frost_root)
    assert len(on_server) > 2500

    client.warm_datastream_cache()

    assert set(client._datastream_routes) == on_server  # the route guard covers every datastream
    status = client.route_status()
    assert (status["routes_loaded"], status["complete"], status["route_count"]) == (True, True, len(on_server))
    persisted = _persisted(tmp_path)
    assert {key: persisted.get(key) for key in seeded["keys"]} == seeded["keys"]  # nothing valid purged
    assert not set(bad) & set(persisted)  # the stale and the cross-property mapping are
    assert not set(bad) & set(client._datastream_ids)
    # Paged by id from the configured base URL, never at FROST's next link (another host).
    assert len(gets) == len(on_server) // 1000 + 1
    assert {url for url, _params in gets} == {f"{frost_root}/{path}/Datastreams"}
    assert all(params["$orderby"] == "id asc" for _url, params in gets)


@pytest.mark.parametrize("path", ["v2.0", "v1.1"], ids=["v2.0-undeclared", "v1.1"])
def test_push_and_replay_beyond_the_first_page(frost_root, seeded, make_client, tmp_path, path) -> None:
    client, _bad, _gets = make_client(path)
    client.warm_datastream_cache()
    prefix, keys = seeded["prefix"], seeded["keys"]
    sensor_id = f"{prefix}-sc-{_LOCATIONS - 1}"  # the last seeded location: far beyond the first page
    pm10, pm2_5 = keys[f"{sensor_id}::pm10"], keys[f"{sensor_id}::pm2_5"]
    assert int(pm10) > 1000

    def reading(prop: str, value: float, minute: int) -> SensorReading:
        return SensorReading(
            sensor_id=sensor_id, sensor_name="SDS011", observed_property=prop, unit="ug/m3", value=value,
            timestamp=datetime(2026, 11, 20, 12, minute, tzinfo=UTC), thing_name=f"{prefix} Thing",
        )

    marker = float(int(uuid.uuid4().int % 10_000))
    result = client.push_observations([reading("pm10", marker, 0)], source="sensor_community")
    assert result["results"][0]["ok"] is True
    assert (marker, "pm10") in _results(frost_root, pm10)

    # A dead-lettered pm2_5 reading in the location's pm10 datastream, as the pre-fix routing wrote it.
    payload = {
        "phenomenonTime": "2026-11-20T12:05:00Z", "resultTime": "2026-11-20T12:05:00Z", "result": marker + 0.5,
        "resultQuality": ["good"], "Datastream": {"@iot.id": int(pm10)},
        "parameters": {"sensor_id": sensor_id, "observed_property": "pm2_5", "sensor_name": "SDS011"},
    }
    line = {
        "timestamp": datetime.now(UTC).isoformat(), "datastream_id": pm10,
        "endpoint": f"{frost_root}/{path}/Observations", "payload": payload, "status_code": None, "error": "circuit_open",
    }
    (tmp_path / "failed_observations.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

    summary = client.replay_failed_observations()

    assert (summary["replayed"], summary["route_rewritten"], summary["remaining"]) == (1, 1, 0)
    assert (marker + 0.5, "pm2_5") in _results(frost_root, pm2_5)
    assert (marker + 0.5, "pm2_5") not in _results(frost_root, pm10)
