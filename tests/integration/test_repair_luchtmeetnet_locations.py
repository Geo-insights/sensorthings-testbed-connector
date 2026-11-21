"""Integration: scripts/repair_luchtmeetnet_locations.py main() against FROST-Server 2.8.

FROST serves SensorThings v1.1 and v2.0 from one database. Production's only
connector target is the v2.0 endpoint, declared without a version (so the
connector speaks v1.1 to it). The tests build the state the connector left
before the station-detail fix (Things whose Location sits at POINT(5 52)),
run main() through one endpoint, configured the way the connector is
configured (SENSORTHINGS_BASE_URLS JSON with per-target Basic auth), and read
the result back through the other endpoint.

The observedArea SQL that ``--print-sql`` prints runs through psql in the
database container, as an operator runs it, and is checked against what FROST's
own insert trigger computes for the same Observations.

FROST comes from ``LMN_REPAIR_FROST_URL`` (a service root such as
``http://frost:8080/FROST-Server`` with Basic auth write/write and no anonymous
reads; the SQL test also needs ``LMN_REPAIR_FROST_DB_CONTAINER``, the database
container for ``docker exec ... psql``) or, when that is unset, from Docker:
fraunhoferiosb/frost-server:2.8 (``LMN_REPAIR_FROST_IMAGE`` overrides) and
postgis/postgis:16-3.4-alpine, as containers and a network named
``<LMN_REPAIR_DOCKER_PREFIX, default lmn-repair-it>-<random>-*``. The database
lives in a tmpfs; everything is removed afterwards. FROST listens on 127.0.0.1
only. Luchtmeetnet is a local fake HTTP server.

    python -m pytest tests/integration/test_repair_luchtmeetnet_locations.py -m integration
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
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
_DB = "sensorthings"  # database, user and password of the throwaway PostGIS
# FROST as configured in production: Basic auth, no anonymous reads, plugins on.
_FROST_ENV = {
    "persistence_db_driver": "org.postgresql.Driver",
    "persistence_db_url": "jdbc:postgresql://{db}:5432/sensorthings",
    "persistence_db_username": _DB,
    "persistence_db_password": _DB,
    "persistence_autoUpdateDatabase": "true",
    "plugins_projects_enable": "true",
    "plugins_projects_enableDefaultRules": "true",
    "plugins_actuation_enable": "true",
    "plugins_openCitySense_enable": "true",
    "plugins_modelLoader_enable": "true",
    "auth_provider": "de.fraunhofer.iosb.ilt.frostserver.auth.basic.BasicAuthProvider",
    "auth_db_driver": "org.postgresql.Driver",
    "auth_db_url": "jdbc:postgresql://{db}:5432/sensorthings",
    "auth_db_username": _DB,
    "auth_db_password": _DB,
    "auth_autoUpdateDatabase": "true",
    "auth_allowAnonymousRead": "false",
}


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


@dataclass(frozen=True)
class _Stack:
    root: str  # FROST service root
    db: str | None  # database container for psql, None when unknown


def _docker(*args: str, input: str | None = None, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=check
    )


def _wait_for(what: str, ready: Callable[[], bool], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(1)
    raise TimeoutError(f"{what} not ready within {timeout:.0f}s")


@pytest.fixture(scope="module")
def frost_stack() -> Iterator[_Stack]:
    configured = os.environ.get("LMN_REPAIR_FROST_URL", "").strip().rstrip("/")
    if configured:
        _wait_until_ready(configured)
        yield _Stack(configured, os.environ.get("LMN_REPAIR_FROST_DB_CONTAINER", "").strip() or None)
        return
    try:
        _docker("version", timeout=30)
    except (FileNotFoundError, subprocess.SubprocessError):
        pytest.skip("neither LMN_REPAIR_FROST_URL nor Docker is available")
    name = f"{os.environ.get('LMN_REPAIR_DOCKER_PREFIX', 'lmn-repair-it')}-{uuid.uuid4().hex[:8]}"
    db, frost, network = f"{name}-db", f"{name}-frost", f"{name}-net"
    port = _free_port()
    try:
        _docker("network", "create", network)
        _docker(
            "run", "-d", "--name", db, "--network", network, "--tmpfs", "/var/lib/postgresql/data",
            "-e", f"POSTGRES_DB={_DB}", "-e", f"POSTGRES_USER={_DB}", "-e", f"POSTGRES_PASSWORD={_DB}",
            "postgis/postgis:16-3.4-alpine",
        )
        # Over TCP: the image's init phase runs a socket-only server first.
        _wait_for(
            "PostGIS",
            lambda: _docker("exec", db, "pg_isready", "-h", "127.0.0.1", "-U", _DB, check=False).returncode == 0,
            120,
        )
        env = [arg for key, value in _FROST_ENV.items() for arg in ("-e", f"{key}={value.format(db=db)}")]
        _docker(
            "run", "-d", "--name", frost, "--network", network, "-p", f"127.0.0.1:{port}:8080",
            "-e", f"serviceRootUrl=http://127.0.0.1:{port}/FROST-Server", *env,
            os.environ.get("LMN_REPAIR_FROST_IMAGE", "fraunhoferiosb/frost-server:2.8"),
        )
        root = f"http://127.0.0.1:{port}/FROST-Server"  # the port is bound to IPv4 only
        _wait_until_ready(root)
        yield _Stack(root, db)
    finally:
        _docker("rm", "-f", "-v", frost, db, check=False, timeout=180)
        _docker("network", "rm", network, check=False, timeout=60)


@pytest.fixture(scope="module")
def frost_root(frost_stack: _Stack) -> str:
    return frost_stack.root


@pytest.fixture(scope="module")
def frost_db(frost_stack: _Stack) -> str:
    if frost_stack.db is None:
        pytest.skip("set LMN_REPAIR_FROST_DB_CONTAINER (the FROST database container) with LMN_REPAIR_FROST_URL")
    return frost_stack.db


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


def _observe(endpoint: str, datastream_id: int, hour: int, feature: int | None = None) -> int:
    """An Observation; without *feature* as the connector pushes them (FROST generates the FoI)."""
    body: dict[str, Any] = {
        "phenomenonTime": f"2026-11-20T{hour:02d}:00:00Z", "result": 12.5, "resultQuality": ["good"],
        "parameters": {"sensor_id": "luchtmeetnet"}, "Datastream": {"@iot.id": datastream_id},
    }
    if feature is not None:
        body["FeatureOfInterest"] = {"@iot.id": feature}
    return _post(endpoint, "Observations", body)


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


# ---------------------------------------------------------------------------
# observedArea: the SQL that --print-sql prints, run with psql
# ---------------------------------------------------------------------------


def _query(db: str, sql: str) -> list[str]:
    """Rows of a query in the database container, ``|``-separated."""
    command = ["exec", "-i", db, "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-U", _DB, "-d", _DB]
    return _docker(*command, input=sql, timeout=120).stdout.splitlines()


def _run_sql_script(db: str, script: str) -> str:
    """Run a psql script as an operator does (command tags printed); its output."""
    proc = _docker("exec", "-i", db, "psql", "-X", "-U", _DB, "-d", _DB, input=script, check=False, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _datastream_rows(db: str) -> dict[int, tuple[str, str]]:
    """``{id: (OBSERVED_AREA as WKT, LAST_FOI_ID)}`` of every Datastream in the database."""
    rows = _query(
        db,
        'SELECT "ID", coalesce(ST_AsText("OBSERVED_AREA"), \'NULL\'), coalesce("LAST_FOI_ID"::text, \'NULL\') '
        'FROM "DATASTREAMS";\n',
    )
    return {int(row.split("|")[0]): (row.split("|")[1], row.split("|")[2]) for row in rows}


def _same_as_trigger(db: str, datastream: int, reference: int) -> bool:
    """Whether two Datastreams have the same LAST_FOI_ID and the same OBSERVED_AREA (type, SRID, geometry)."""
    (row,) = _query(
        db,
        'SELECT a."LAST_FOI_ID" = b."LAST_FOI_ID" AND GeometryType(a."OBSERVED_AREA") = GeometryType(b."OBSERVED_AREA") '
        'AND ST_SRID(a."OBSERVED_AREA") = ST_SRID(b."OBSERVED_AREA") '
        'AND ST_AsText(ST_Normalize(a."OBSERVED_AREA")) = ST_AsText(ST_Normalize(b."OBSERVED_AREA")) '
        f'FROM "DATASTREAMS" a, "DATASTREAMS" b WHERE a."ID" = {int(datastream)} AND b."ID" = {int(reference)};\n',
    )
    return row == "t"


def _observed_area(endpoint: str, datastream_id: int) -> Any:
    return _get(endpoint, f"Datastreams({datastream_id})", {"$select": "observedArea"})["observedArea"]


def _point(coordinates: list[float]) -> dict[str, Any]:
    return {"type": "Point", "coordinates": coordinates}


def _printed_sql(capsys: pytest.CaptureFixture[str], *args: str) -> str:
    assert repair.main(["--print-sql", *args]) == repair.EXIT_OK
    return capsys.readouterr().out


def test_observed_area_sql_recomputes_what_the_api_cannot_move(frost_root, frost_db, api_url, configure, capsys):
    prefix = _prefix()
    write, read = f"{frost_root}/v2.0", f"{frost_root}/v1.1"
    station, elsewhere = _STATIONS["NL10450"][1], [4.4, 52.1]
    ids = _create_station(write, prefix, "NL10450", coordinates=_PLACEHOLDER)
    # Observations before the repair: FROST generates their FeatureOfInterest from the placeholder
    # Location, and its insert trigger sets each Datastream's observedArea from it.
    _observe(write, ids["ds0"], 7)
    generated, point = _feature_point(read, _observe(write, ids["ds0"], 8))
    assert point == _PLACEHOLDER
    _observe(write, ids["ds1"], 7)
    other = _post(write, "FeaturesOfInterest", {
        "name": f"{prefix}elsewhere", "description": "another FoI", "encodingType": "application/geo+json",
        "feature": _point(elsewhere),
    })
    _observe(write, ids["ds1"], 8, feature=other)
    hull = _observed_area(read, ids["ds1"])
    assert hull["type"] == "LineString" and sorted(hull["coordinates"]) == sorted([_PLACEHOLDER, elsewhere])
    # A Luchtmeetnet Datastream without Observations, and a stale Datastream of another Thing.
    _create_station(write, prefix, "NL01491", coordinates=_PLACEHOLDER, datastreams=1)
    stranger = _create_station(write, f"{prefix}x", "NL50002", coordinates=_PLACEHOLDER, datastreams=1)
    stranger_feature, _ = _feature_point(read, _observe(write, stranger["ds0"], 7))
    response = requests.patch(
        f"{write}/FeaturesOfInterest({stranger_feature})", json={"feature": _point(elsewhere)}, auth=_AUTH, timeout=15
    )
    assert response.status_code == 200, response.text
    configure(_target(frost_root, "v2.0", None), prefix)

    assert repair.main(["--api-url", api_url, "--apply", "--allow-observations"]) == repair.EXIT_OK
    capsys.readouterr()
    assert _feature_point(read, _observe(write, ids["ds0"], 9)) == (generated, station)  # moved, and reused
    # FROST leaves observedArea where it was, and ignores it in a PATCH: the API cannot fix it.
    assert _observed_area(read, ids["ds0"]) == _point(_PLACEHOLDER)
    response = requests.patch(f"{write}/Datastreams({ids['ds0']})", json={"observedArea": _point(station)}, auth=_AUTH, timeout=15)
    assert response.status_code == 200, response.text
    assert _observed_area(read, ids["ds0"]) == _point(_PLACEHOLDER)

    # The check after the repair: nothing left to move, so exit 0; the observedAreas are reported.
    assert repair.main(["--api-url", api_url]) == repair.EXIT_OK
    out = capsys.readouterr().out
    assert "--apply is refused" not in out
    assert "2 Luchtmeetnet Datastreams have an observedArea with a corner at POINT(5 52)" in out

    before = _datastream_rows(frost_db)
    preview = _run_sql_script(frost_db, _printed_sql(capsys))
    assert "UPDATE 2" in preview and preview.rstrip().endswith("ROLLBACK")
    assert _datastream_rows(frost_db) == before  # the preview writes nothing

    applied = _run_sql_script(frost_db, _printed_sql(capsys, "--apply"))
    assert "UPDATE 2" in applied and applied.rstrip().endswith("COMMIT")
    after = _datastream_rows(frost_db)
    assert {ds for ds in after if after[ds] != before[ds]} == {ids["ds0"], ids["ds1"]}  # nothing else, other prefixes included
    for endpoint in (read, write):
        assert _observed_area(endpoint, ids["ds0"]) == _point(station)
        assert _observed_area(endpoint, ids["ds1"])["type"] == "LineString"
    assert after[ids["ds0"]][1] == str(generated)
    assert after[ids["ds1"]][1] == str(other)
    assert after[stranger["ds0"]] == before[stranger["ds0"]] == ("POINT(5 52)", str(stranger_feature))

    # What FROST's insert trigger computes for the same Observations, with the FeaturesOfInterest
    # where they are now, on Datastreams of a Thing the SQL does not touch.
    reference = _create_station(write, f"{prefix}ref", "NL49565", coordinates=station)
    for hour in (7, 8, 9):
        _observe(write, reference["ds0"], hour, feature=generated)
    _observe(write, reference["ds1"], 7, feature=generated)
    _observe(write, reference["ds1"], 8, feature=other)
    assert _same_as_trigger(frost_db, ids["ds0"], reference["ds0"])
    assert _same_as_trigger(frost_db, ids["ds1"], reference["ds1"])

    # Later Observations keep it right, and running the SQL again writes nothing.
    _observe(write, ids["ds0"], 10)
    assert _datastream_rows(frost_db)[ids["ds0"]] == after[ids["ds0"]]
    assert "UPDATE 0" in _run_sql_script(frost_db, _printed_sql(capsys, "--apply"))
    assert repair.main(["--api-url", api_url]) == repair.EXIT_OK
    assert "observedArea with a corner" not in capsys.readouterr().out
