"""Integration: scripts/repair_luchtmeetnet_locations.py main() against FROST-Server 2.8.

FROST serves SensorThings v1.1 and v2.0 from one database. Production's only
connector target is the v2.0 endpoint, declared without a version (so the
connector speaks v1.1 to it). The tests build the state the connector left
before the station-detail fix (Things whose Location sits at POINT(5 52)),
run main() through one endpoint, configured the way the connector is
configured (SENSORTHINGS_BASE_URLS JSON with per-target Basic auth), and read
the result back through the other endpoint.

FROST comes from ``LMN_REPAIR_FROST_URL`` (a service root such as
``http://frost:8080/FROST-Server`` with Basic auth write/write and no anonymous
reads) or, when that is unset, from docker compose: fraunhoferiosb/frost-server:2.8
(``LMN_REPAIR_FROST_IMAGE`` overrides) and PostGIS under a unique project name,
removed afterwards. Luchtmeetnet is a local fake HTTP server.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar

import pytest
import requests

from app.config import _load_frost_targets
from scripts import repair_luchtmeetnet_locations as repair

pytestmark = pytest.mark.integration

_AUTH = ("write", "write")
_PLACEHOLDER = [5.0, 52.0]
_STATIONS = {  # number: (name, [lon, lat]) as Luchtmeetnet serves them
    "NL49565": ("Oude Meer-Aalsmeerderdijk", [4.77082, 52.28013]),
    "NL10450": ("Den Haag-Neherkade", [4.318551, 52.062537]),
    "NL50002": ("Geleen-Vouershof", [5.81071, 50.96309]),
    "NL01491": ("Rotterdam-Ridderkerk", [4.4307, 51.93858]),
}
_COMPOSE = """
services:
  db:
    image: postgis/postgis:16-3.4-alpine
    environment: {POSTGRES_DB: sensorthings, POSTGRES_USER: sensorthings, POSTGRES_PASSWORD: sensorthings}
    healthcheck: {test: ["CMD-SHELL", "pg_isready -U sensorthings"], interval: 3s, timeout: 3s, retries: 30}
  frost:
    image: IMAGE
    depends_on: {db: {condition: service_healthy}}
    environment:
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


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_ready(root: str, timeout: float = 240.0) -> None:
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
    configured = os.environ.get("LMN_REPAIR_FROST_URL", "").strip().rstrip("/")
    if configured:
        _wait_until_ready(configured)
        yield configured
        return
    try:
        subprocess.run(["docker", "compose", "version"], check=True, capture_output=True, timeout=20)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("neither LMN_REPAIR_FROST_URL nor docker compose is available")
    port = _free_port()
    image = os.environ.get("LMN_REPAIR_FROST_IMAGE", "fraunhoferiosb/frost-server:2.8")
    compose = _COMPOSE.replace("IMAGE", image).replace("PORT", str(port))
    project = ["docker", "compose", "-p", f"lmn-repair-it-{uuid.uuid4().hex[:8]}", "-f", "-"]
    try:
        subprocess.run([*project, "up", "-d"], input=compose, text=True, check=True, capture_output=True, timeout=600)
        root = f"http://127.0.0.1:{port}/FROST-Server"  # the port is bound to IPv4 only
        _wait_until_ready(root)
        yield root
    finally:
        subprocess.run([*project, "down", "-v", "--remove-orphans"], input=compose, text=True, capture_output=True, timeout=180)


class _FakeLuchtmeetnet(BaseHTTPRequestHandler):
    """GET /open_api/stations and /open_api/stations/{number}; scripted failures per station."""

    failures: ClassVar[dict[str, list[int]]] = {}  # number -> statuses to answer before the record
    paths: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0].removeprefix("/open_api")
        type(self).paths.append(path)
        if path == "/stations":
            items = [{"number": n, "location": name} for n, (name, _) in _STATIONS.items()]
            return self._send(200, {"pagination": {"last_page": 1}, "data": items})
        number = path.rsplit("/", 1)[-1]
        pending = type(self).failures.get(number)
        if pending:
            status = pending.pop(0)
            return self._send(status, {"message": "Too Many Requests"}, {"Retry-After": "0"} if status == 429 else {})
        if number not in _STATIONS:
            return self._send(404, {"message": "not found"})
        coordinates = _STATIONS[number][1]
        return self._send(200, {"data": {"components": ["NO2"], "geometry": {"type": "point", "coordinates": coordinates}}})

    def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture(scope="module")
def api_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLuchtmeetnet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/open_api"
    finally:
        server.shutdown()


@pytest.fixture(autouse=True)
def _reset_fake_api() -> None:
    _FakeLuchtmeetnet.failures = {}
    _FakeLuchtmeetnet.paths = []


# ---------------------------------------------------------------------------
# FROST helpers (connector-shaped payloads, as the connector created them)
# ---------------------------------------------------------------------------


def _id_of(entity: dict[str, Any]) -> int:
    value = entity.get("@iot.id", entity.get("id"))
    assert value is not None, entity
    return int(value)


def _post(endpoint: str, collection: str, payload: dict[str, Any]) -> int:
    response = requests.post(f"{endpoint}/{collection}", json=payload, auth=_AUTH, timeout=15)
    assert response.status_code == 201, response.text
    # v1.1 answers with an empty body; the new id is in the Location header.
    location = response.headers.get("Location", "")
    if location.endswith(")") and "(" in location:
        return int(location.rsplit("(", 1)[1].rstrip(")"))
    return _id_of(response.json())


def _create_station(endpoint: str, prefix: str, number: str, *, coordinates: list[float], datastreams: int = 2) -> dict[str, int]:
    """Thing + Location + Sensor + Datastreams with the connector's v1.1 payloads; returns their ids."""
    name, _ = _STATIONS[number]
    thing = _post(endpoint, "Things", {
        "name": f"{prefix}Luchtmeetnet {name}",
        "description": f"Luchtmeetnet RIVM air quality monitoring station {name} (station number {number}).",
        "properties": {"site": "luchtmeetnet", "source": "luchtmeetnet", "station_number": number},
    })
    location = _post(endpoint, "Locations", {
        "name": f"{prefix}Luchtmeetnet {name} location",
        "description": f"Air quality measurement point {name} ({number}).",
        "encodingType": "application/geo+json",
        "location": {"type": "Point", "coordinates": coordinates},
        "Things": [{"@iot.id": thing}],
    })
    sensor = _post(endpoint, "Sensors", {
        "name": f"{prefix}Luchtmeetnet {name} sensor",
        "description": "Luchtmeetnet RIVM ambient air quality sensor.",
        "encodingType": "application/json",
        "metadata": "https://www.luchtmeetnet.nl",
    })
    ids = {"thing": thing, "location": location}
    for index, prop in enumerate(["NO2", "PM10"][:datastreams]):
        observed_property = _post(endpoint, "ObservedProperties", {
            "name": f"{prefix}{prop} {uuid.uuid4().hex[:6]}", "definition": "https://example.org/def", "description": prop,
        })
        ids[f"ds{index}"] = _post(endpoint, "Datastreams", {
            "name": f"{prefix}Luchtmeetnet {name} sensor - {prop}",
            "description": f"{prop} observations",
            "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
            "unitOfMeasurement": {"name": prop, "symbol": "ug/m3", "definition": "https://example.org/unit"},
            "Thing": {"@iot.id": thing},
            "Sensor": {"@iot.id": sensor},
            "ObservedProperty": {"@iot.id": observed_property},
            "properties": {"sensor_id": f"luchtmeetnet-{number}", "observed_property": prop.lower()},
        })
    return ids


def _observe(endpoint: str, datastream_id: int, hour: int) -> int:
    """An Observation without FeatureOfInterest, as the connector pushes them (FROST generates the FoI)."""
    return _post(endpoint, "Observations", {
        "phenomenonTime": f"2026-11-20T{hour:02d}:00:00Z", "result": 12.5, "resultQuality": ["good"],
        "parameters": {"sensor_id": "luchtmeetnet"}, "Datastream": {"@iot.id": datastream_id},
    })


def _get(endpoint: str, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    response = requests.get(f"{endpoint}/{path}", params=params, auth=_AUTH, timeout=15)
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def _location_point(endpoint: str, location_id: int) -> list[float]:
    return list(_get(endpoint, f"Locations({location_id})")["location"]["coordinates"])


def _feature_point(endpoint: str, observation_id: int) -> tuple[int, list[float]]:
    feature = _get(endpoint, f"Observations({observation_id})/FeatureOfInterest")
    return _id_of(feature), list(feature["feature"]["coordinates"])


@pytest.fixture
def configure(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point repair.settings at one target, parsed from SENSORTHINGS_BASE_URLS like the connector does."""

    def _configure(target: dict[str, Any], prefix: str) -> None:
        for name in ("SENSORTHINGS_BASE_URL", "SENSORTHINGS_AUTH_USERNAME", "SENSORTHINGS_AUTH_PASSWORD", "SENSORTHINGS_AUTH_TOKEN"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("SENSORTHINGS_BASE_URLS", json.dumps([target]))
        targets = _load_frost_targets()
        assert len(targets) == 1
        monkeypatch.setattr(
            repair, "settings", dataclasses.replace(repair.settings, frost_targets=targets, entity_name_prefix=prefix)
        )
        monkeypatch.setattr(repair, "_sleep", lambda _seconds: None)

    return _configure


def _target(frost_root: str, endpoint_version: str, declared: str | None, password: str = "write") -> dict[str, Any]:
    target: dict[str, Any] = {
        "url": f"{frost_root}/{endpoint_version}",
        "label": "frost-local",
        "auth_username": "write",
        "auth_password": password,
    }
    if declared:
        target["version"] = declared
    return target


def _prefix() -> str:
    return f"IT{uuid.uuid4().hex[:8]}_"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint_version", "declared", "read_back"),
    [
        ("v2.0", None, "v1.1"),  # production: the v2.0 endpoint, no declared version
        ("v2.0", "v2.0", "v1.1"),
        ("v1.1", "v1.1", "v2.0"),
    ],
    ids=["v2.0-undeclared", "v2.0-declared", "v1.1"],
)
def test_main_moves_placeholder_locations(frost_root, api_url, configure, monkeypatch, capsys, endpoint_version, declared, read_back):
    prefix = _prefix()
    write = f"{frost_root}/{endpoint_version}"
    read = f"{frost_root}/{read_back}"
    stale = {n: _create_station(write, prefix, n, coordinates=_PLACEHOLDER) for n in ("NL49565", "NL10450", "NL01491")}
    fine = _create_station(write, prefix, "NL50002", coordinates=_STATIONS["NL50002"][1])
    configure(_target(frost_root, endpoint_version, declared), prefix)
    monkeypatch.setattr(repair, "_PAGE_SIZE", 2)  # 4 Things: two pages

    assert repair.main(["--api-url", api_url]) == repair.EXIT_OK
    out = capsys.readouterr().out
    assert "3 Locations and 0 FeaturesOfInterest to move off POINT(5 52), 1 Things already elsewhere" in out
    assert all(_location_point(read, ids["location"]) == _PLACEHOLDER for ids in stale.values())  # dry run

    assert repair.main(["--api-url", api_url, "--apply"]) == repair.EXIT_OK
    assert "Applied 3 PATCHes, 0 failed." in capsys.readouterr().out
    for number, ids in stale.items():
        assert _location_point(read, ids["location"]) == _STATIONS[number][1]
    assert _location_point(read, fine["location"]) == _STATIONS["NL50002"][1]

    assert repair.main(["--api-url", api_url]) == repair.EXIT_OK
    assert "0 Locations and 0 FeaturesOfInterest to move" in capsys.readouterr().out

    # The fixed connector's first Observation now gets a FeatureOfInterest at the station.
    observation = _observe(write, stale["NL49565"]["ds1"], 9)
    assert _feature_point(read, observation)[1] == _STATIONS["NL49565"][1]


def test_existing_observations_refuse_apply_until_allowed(frost_root, api_url, configure, capsys):
    prefix = _prefix()
    write = f"{frost_root}/v2.0"
    read = f"{frost_root}/v1.1"
    ids = _create_station(write, prefix, "NL10450", coordinates=_PLACEHOLDER)
    # Only the second Datastream has data: FROST generated its FoI from the placeholder Location.
    first = _observe(write, ids["ds1"], 8)
    feature_id, point = _feature_point(read, first)
    assert point == _PLACEHOLDER
    configure(_target(frost_root, "v2.0", None), prefix)

    assert repair.main(["--api-url", api_url]) == repair.EXIT_OBSERVATIONS_EXIST
    assert "1 Luchtmeetnet Datastreams on 1 Things already have Observations" in capsys.readouterr().out

    assert repair.main(["--api-url", api_url, "--apply"]) == repair.EXIT_OBSERVATIONS_EXIST
    assert _location_point(read, ids["location"]) == _PLACEHOLDER  # nothing written

    assert repair.main(["--api-url", api_url, "--apply", "--allow-observations"]) == repair.EXIT_OK
    real = _STATIONS["NL10450"][1]
    assert _location_point(read, ids["location"]) == real
    assert _feature_point(read, first) == (feature_id, real)
    # FROST keeps reusing that FeatureOfInterest, now at the station.
    assert _feature_point(read, _observe(write, ids["ds0"], 9)) == (feature_id, real)


def test_luchtmeetnet_failures_are_retried_reported_and_rerun(frost_root, api_url, configure, capsys):
    prefix = _prefix()
    write = f"{frost_root}/v2.0"
    ids = {n: _create_station(write, prefix, n, coordinates=_PLACEHOLDER, datastreams=0) for n in ("NL49565", "NL10450")}
    configure(_target(frost_root, "v2.0", None), prefix)
    _FakeLuchtmeetnet.failures = {"NL49565": [429, 429], "NL10450": [503] * 4}

    assert repair.main(["--api-url", api_url, "--apply"]) == repair.EXIT_API_FAILED
    out = capsys.readouterr().out
    assert _FakeLuchtmeetnet.paths.count("/stations/NL49565") == 3  # two 429s, then the record
    assert _location_point(write, ids["NL49565"]["location"]) == _STATIONS["NL49565"][1]
    assert "left at the placeholder: NL10450" in out and "server error after retries (HTTP 503)" in out
    assert "[server_error: re-run in a few minutes]" in out
    assert _location_point(write, ids["NL10450"]["location"]) == _PLACEHOLDER

    assert repair.main(["--api-url", api_url, "--apply"]) == repair.EXIT_OK  # the API recovered
    assert _location_point(write, ids["NL10450"]["location"]) == _STATIONS["NL10450"][1]


def test_wrong_credentials_stop_before_anything_is_planned(frost_root, api_url, configure):
    configure(_target(frost_root, "v2.0", None, password="not-the-password"), _prefix())

    with pytest.raises(SystemExit) as exc_info:
        repair.main(["--api-url", api_url, "--apply"])
    assert "HTTP 401" in str(exc_info.value)
    assert "not-the-password" not in str(exc_info.value)
    assert _FakeLuchtmeetnet.paths == []
