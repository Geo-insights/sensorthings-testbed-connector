"""scripts/migrate_knmi_sensor_ids.py against an in-memory FROST.

The FROST state mirrors production (frost.geo-insights.nl, 2026-11-20): KNMI
Things named after their coordinates, Datastreams carrying the "knmi-<index>"
their station had when each was created (some Things mix two), a stale
"Ground temperature" Datastream next to "Ground surface temperature", Sensors
without properties, and knmi_station_id "<index>" on the Things.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest
import requests

from app.frost.odata_fields import odata_fields_for
from app.frost.target import FrostTarget
from app.sources import knmi_weather
from scripts import migrate_knmi_sensor_ids as migrate

BASE = "https://frost.test/FROST-Server/v2.0"
PREFIX = "GEO_"
PATHS = {migrate.THINGS: "/Things", migrate.DATASTREAMS: "/Datastreams", migrate.SENSORS: "/Sensors"}

# (WIGOS id, lon, lat, name)
VLISSINGEN = ("0-20000-0-06310", 3.3417, 52.36, "Station A")
VALKENBURG = ("0-20000-0-06210", 4.4364, 52.1397, "Station B")
IJMOND = ("0-20000-0-06209", 4.5175, 52.4636, "IJmond")
ELSEWHERE = ("0-20000-0-06380", 5.7619, 50.9053, "Station D")

PROPS = ["temperature", "humidity", "wind_speed"]


def _locations_body(*stations: tuple[str, float, float, str]) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
             "properties": {"name": name, "wmoId": sid[-5:]}, "id": sid}
            for sid, lon, lat, name in stations
        ],
    }


class _Resp:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self._body = body

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeFrost:
    """Things with Locations, Datastreams and Sensors; GET /Things pages, PATCH replaces properties."""

    def __init__(self, convention: str = "v1.1", base: str = BASE) -> None:
        self.base = base
        fields = odata_fields_for(convention)
        self.id_key, self.next_key = fields.id, fields.next_link
        self.things: dict[int, dict[str, Any]] = {}
        self.datastreams: dict[int, dict[str, Any]] = {}
        self.sensors: dict[int, dict[str, Any]] = {}
        self.gets: list[dict[str, str]] = []
        self.patches: list[tuple[str, int, dict[str, Any]]] = []
        self.fail_patch: set[tuple[str, int]] = set()
        self.journal: Path | None = None  # when set, every PATCH must find its record there already
        self._next = 1

    def _id(self) -> int:
        self._next += 1
        return self._next

    def add_station(
        self,
        lon: float,
        lat: float,
        knmi_station_id: str | None,
        datastreams: list[tuple[str, str, str | None]],
        *,
        name: str | None = None,
        source: str = "knmi_weather",
        sensor_properties: dict[str, Any] | None = None,
        locations: list[list[float]] | None = None,
    ) -> int:
        """A Thing as the connector registered it; ``datastreams`` is (observed_property, display name, sensor_id)."""
        station = knmi_weather.station_name_for(lat, lon)
        thing_name = name or f"{PREFIX}KNMI {station}"
        thing_props: dict[str, Any] = {"site": "nl", "source": source, "network": "KNMI", "station_name": station}
        if knmi_station_id is not None:
            thing_props["knmi_station_id"] = knmi_station_id
        thing_id = self._id()
        sensor_id = self._id()
        self.sensors[sensor_id] = {"name": f"{PREFIX}KNMI {station} sensor", "properties": sensor_properties}
        ds_ids = []
        for observed_property, display, sid in datastreams:
            ds_id = self._id()
            props: dict[str, Any] = {"site_key": "nl", "thing_name": thing_name, "observed_property": observed_property,
                                     "streamKey": observed_property}
            if sid is not None:
                props["sensor_id"] = sid
            self.datastreams[ds_id] = {"name": f"{PREFIX}KNMI {station} sensor - {display}", "properties": props,
                                       "sensor": sensor_id}
            ds_ids.append(ds_id)
        self.things[thing_id] = {
            "name": thing_name,
            "properties": thing_props,
            "locations": [[lon, lat]] if locations is None else locations,
            "datastreams": ds_ids,
        }
        return thing_id

    # -- FrostHTTPClient surface ---------------------------------------------

    def endpoint(self, path: str) -> str:
        return f"{self.base}{path}"

    def _store(self, collection: str) -> dict[int, dict[str, Any]]:
        return {"Things": self.things, "Datastreams": self.datastreams, "Sensors": self.sensors}[collection]

    def get(self, url: str, params: dict[str, str] | None = None) -> _Resp:
        single = re.fullmatch(rf"{re.escape(self.base)}/(Things|Datastreams|Sensors)\((\d+)\)", url)
        if single:  # one entity, as FROST serves it: its properties are null when it has none
            assert not params
            entity = self._store(single.group(1)).get(int(single.group(2)))
            if entity is None:
                return _Resp(404, {"message": "Nothing found."})
            return _Resp(200, {self.id_key: int(single.group(2)), "name": entity["name"],
                               "properties": json.loads(json.dumps(entity["properties"]))})
        params = dict(params or {})
        self.gets.append(params)
        assert url == f"{self.base}/Things"
        assert params["$expand"] == "Locations,Datastreams($top=1000;$expand=Sensor)"
        prefix = re.fullmatch(r"startswith\(name,'(.*)'\)", params["$filter"]).group(1).replace("''", "'")
        rows = [tid for tid in sorted(self.things) if self.things[tid]["name"].startswith(prefix)]
        skip, top = int(params["$skip"]), int(params["$top"])
        body: dict[str, Any] = {"value": [self._thing(tid) for tid in rows[skip: skip + top]]}
        if skip + top < len(rows):
            body[self.next_key] = f"{self.base}/Things?$skip={skip + top}"
        return _Resp(200, body)

    def patch(self, url: str, payload: dict[str, Any]) -> _Resp:
        match = re.fullmatch(rf"{re.escape(self.base)}/(Things|Datastreams|Sensors)\((\d+)\)", url)
        assert match, url
        collection, entity_id = match.group(1), int(match.group(2))
        assert set(payload) == {"properties"}, payload
        if self.journal is not None:
            recorded = [json.loads(line) for line in self.journal.read_text(encoding="utf-8").splitlines()]
            assert any(r["entity_set"] == collection and r["id"] == str(entity_id)
                       and r["new"] == payload["properties"] for r in recorded), "PATCH sent before its record"
        if (collection, entity_id) in self.fail_patch:
            return _Resp(500, {"message": "Internal error"})
        store = self._store(collection)
        store[entity_id]["properties"] = json.loads(json.dumps(payload["properties"]))
        self.patches.append((collection, entity_id, payload))
        return _Resp(200, {})

    def _thing(self, thing_id: int) -> dict[str, Any]:
        thing = self.things[thing_id]
        datastreams = []
        for ds_id in thing["datastreams"]:
            ds = self.datastreams[ds_id]
            sensor = self.sensors[ds["sensor"]]
            datastreams.append({
                self.id_key: ds_id, "name": ds["name"], "properties": ds["properties"],
                "Sensor": {self.id_key: ds["sensor"], "name": sensor["name"], "properties": sensor["properties"]},
            })
        return {
            self.id_key: thing_id,
            "name": thing["name"],
            "properties": thing["properties"],
            "Locations": [
                {self.id_key: 1000 + thing_id, "location": {"type": "Point", "coordinates": coords}}
                for coords in thing["locations"]
            ],
            "Datastreams": datastreams,
        }

    # -- assertions helpers ----------------------------------------------------

    def sensor_ids(self, thing_id: int) -> list[str | None]:
        return [self.datastreams[ds]["properties"].get("sensor_id") for ds in self.things[thing_id]["datastreams"]]


def _production_like(frost: FakeFrost) -> dict[str, int]:
    """Three stations as production has them: one consistent, one mixing ids, IJmond on an old index."""
    return {
        "vlissingen": frost.add_station(
            VLISSINGEN[1], VLISSINGEN[2], "0",
            [(p, p.title(), "knmi-0") for p in PROPS] + [("ground_temperature", "Ground temperature", "knmi-0"),
                                                       ("ground_temperature", "Ground surface temperature", "knmi-0")],
        ),
        "valkenburg": frost.add_station(
            VALKENBURG[1], VALKENBURG[2], "2",
            [(p, p.title(), "knmi-4") for p in PROPS] + [("ground_temperature", "Ground surface temperature", "knmi-3")],
        ),
        "ijmond": frost.add_station(IJMOND[1], IJMOND[2], "3", [(p, p.title(), "knmi-3") for p in PROPS]),
    }


def _run(frost: FakeFrost, *, apply: bool, stations: Any = None, convention: str = "v1.1",
         journal: Path | None = None) -> int:
    body = stations if stations is not None else _locations_body(VLISSINGEN, VALKENBURG, IJMOND, ELSEWHERE)
    if apply and journal is None:
        journal = Path(tempfile.mkdtemp()) / "journal.jsonl"
    return migrate.run(
        frost,  # type: ignore[arg-type]
        body,
        fields=odata_fields_for(convention),
        entity_prefix=PREFIX,
        paths=PATHS,
        apply=apply,
        journal=migrate.Journal(journal, BASE) if journal is not None else None,
    )


def _rollback(frost: FakeFrost, journal: Path, *, apply: bool, target: str = BASE) -> int:
    return migrate.run_rollback(frost, journal, target_url=target, paths=PATHS, apply=apply)  # type: ignore[arg-type]


def _snapshot(frost: FakeFrost) -> dict[tuple[str, int], Any]:
    return copy.deepcopy({
        **{("Things", i): e["properties"] for i, e in frost.things.items()},
        **{("Datastreams", i): e["properties"] for i, e in frost.datastreams.items()},
        **{("Sensors", i): e["properties"] for i, e in frost.sensors.items()},
    })


# ---------------------------------------------------------------------------
# Dry run, apply, idempotency
# ---------------------------------------------------------------------------


def test_dry_run_plans_every_station_and_writes_nothing(capsys):
    frost = FakeFrost()
    ids = _production_like(frost)

    assert _run(frost, apply=False) == migrate.EXIT_OK

    out = capsys.readouterr().out
    assert not frost.patches
    assert "3 KNMI Things: 3 to migrate (12 Datastream, 0 Sensor and 3 Thing PATCHes)" in out
    assert "Datastreams: 5 of 5 to PATCH (knmi-0 x5) -> knmi-0-20000-0-06310" in out
    assert "Datastreams: 4 of 4 to PATCH (knmi-3 x1, knmi-4 x3) -> knmi-0-20000-0-06210" in out
    assert "Thing: knmi_station_id '3' -> '0-20000-0-06209'" in out
    assert f"Thing {ids['ijmond']:>6} 'GEO_KNMI KNMI-52.46-4.52'" in out
    assert "IJmond" in out
    # The stale and the current ground temperature Datastream end up on one key.
    assert "share the key knmi-0-20000-0-06310::ground_temperature: " in out
    assert "Dry run: nothing was written. Re-run with --apply to PATCH them." in out


def test_apply_moves_datastreams_and_things_to_the_station_id(capsys):
    frost = FakeFrost()
    ids = _production_like(frost)
    before = {ds_id: dict(ds["properties"]) for ds_id, ds in frost.datastreams.items()}

    assert _run(frost, apply=True) == migrate.EXIT_OK

    out = capsys.readouterr().out
    assert "Applied 15 PATCHes, 0 failed." in out
    assert "Read back: every planned entity is on its station id." in out
    assert frost.sensor_ids(ids["vlissingen"]) == ["knmi-0-20000-0-06310"] * 5
    assert frost.sensor_ids(ids["valkenburg"]) == ["knmi-0-20000-0-06210"] * 4
    assert frost.sensor_ids(ids["ijmond"]) == ["knmi-0-20000-0-06209"] * 3
    # Only sensor_id changed: streamKey, observed_property, thing_name, site_key are kept.
    for ds_id, ds in frost.datastreams.items():
        assert {k: v for k, v in ds["properties"].items() if k != "sensor_id"} == {
            k: v for k, v in before[ds_id].items() if k != "sensor_id"
        }
    assert frost.things[ids["ijmond"]]["properties"] == {
        "site": "nl", "source": "knmi_weather", "network": "KNMI", "station_name": "KNMI-52.46-4.52",
        "knmi_station_id": "0-20000-0-06209",
    }
    # Sensors carry no properties on production: nothing to PATCH there.
    assert not [p for p in frost.patches if p[0] == "Sensors"]
    # Datastreams first, the Thing last, per station.
    order = [collection for collection, _id, _body in frost.patches]
    assert order[:6] == ["Datastreams"] * 5 + ["Things"]


def test_rerun_after_apply_finds_nothing(capsys):
    frost = FakeFrost()
    _production_like(frost)
    assert _run(frost, apply=True) == migrate.EXIT_OK
    patches = len(frost.patches)
    capsys.readouterr()

    assert _run(frost, apply=False) == migrate.EXIT_OK
    assert _run(frost, apply=True) == migrate.EXIT_OK

    out = capsys.readouterr().out
    assert len(frost.patches) == patches
    assert "3 KNMI Things: 0 to migrate (0 Datastream, 0 Sensor and 0 Thing PATCHes), 3 already on their station id" in out
    assert ": ok, 5 Datastreams on knmi-0-20000-0-06310" in out
    assert "Applied 0 PATCHes, 0 failed." in out


@pytest.mark.parametrize(
    ("served", "declared"),
    [("v1.1", "v1.1"), ("v2.0", "v2.0"), ("v2.0", "v1.1")],
    ids=["v1.1", "v2.0", "v2.0-served-v1.1-declared"],
)
def test_both_api_conventions(served, declared, monkeypatch):
    frost = FakeFrost(convention=served)
    ids = _production_like(frost)
    monkeypatch.setattr(migrate, "_PAGE_SIZE", 2)  # 3 Things: two pages

    assert _run(frost, apply=True, convention=declared) == migrate.EXIT_OK

    assert len(frost.gets) == 4  # two pages to plan, two to read back
    assert frost.gets[0]["$orderby"] == f"{odata_fields_for(declared).id} asc"
    assert [g["$skip"] for g in frost.gets] == ["0", "2", "0", "2"]
    assert frost.sensor_ids(ids["valkenburg"]) == ["knmi-0-20000-0-06210"] * 4


# ---------------------------------------------------------------------------
# What is left alone
# ---------------------------------------------------------------------------


def test_things_that_do_not_match_are_left_alone(capsys):
    frost = FakeFrost()
    ok = frost.add_station(VLISSINGEN[1], VLISSINGEN[2], "0", [("temperature", "Air temperature", "knmi-0")])
    renamed = frost.add_station(VALKENBURG[1], VALKENBURG[2], "1", [("temperature", "T", "knmi-1")],
                                name=f"{PREFIX}KNMI Valkenburg")
    nowhere = frost.add_station(6.9, 53.4, "5", [("temperature", "T", "knmi-5")])
    no_location = frost.add_station(IJMOND[1], IJMOND[2], "3", [("temperature", "T", "knmi-3")], locations=[])
    other_source = frost.add_station(ELSEWHERE[1], ELSEWHERE[2], "9", [("temperature", "T", "knmi-9")], source="manual")

    assert _run(frost, apply=True) == migrate.EXIT_LEFT

    out = capsys.readouterr().out
    assert frost.sensor_ids(ok) == ["knmi-0-20000-0-06310"]
    for thing_id, old in ((renamed, "knmi-1"), (nowhere, "knmi-5"), (no_location, "knmi-3"), (other_source, "knmi-9")):
        assert frost.sensor_ids(thing_id) == [old]
    assert "names 'GEO_KNMI KNMI-52.14-4.44'" in out
    assert "no KNMI station within 0.0005 degrees of the Location (Location POINT(6.9 53.4))" in out
    assert "not exactly one Location with a Point (0 Location(s))" in out
    assert "properties.source is not knmi_weather (source 'manual')" in out
    assert "5 KNMI Things: 1 to migrate" in out
    assert "4 left alone" in out


def test_ambiguous_and_shared_stations_are_left_alone(capsys):
    frost = FakeFrost()
    a = frost.add_station(VLISSINGEN[1], VLISSINGEN[2], "0", [("temperature", "T", "knmi-0")])
    # A second Thing 30 m away: both match VLISSINGEN.
    b = frost.add_station(VLISSINGEN[1] + 0.0003, VLISSINGEN[2], "1", [("temperature", "T", "knmi-1")])
    c = frost.add_station(VALKENBURG[1], VALKENBURG[2], "2", [("temperature", "T", "knmi-2")])
    twins = (("0-20000-0-00001", VALKENBURG[1], VALKENBURG[2], "x"), ("0-20000-0-00002", VALKENBURG[1], VALKENBURG[2], "y"))

    assert _run(frost, apply=True, stations=_locations_body(VLISSINGEN, *twins)) == migrate.EXIT_LEFT

    out = capsys.readouterr().out
    assert not frost.patches
    assert (frost.sensor_ids(a), frost.sensor_ids(b), frost.sensor_ids(c)) == (["knmi-0"], ["knmi-1"], ["knmi-2"])
    assert out.count("another KNMI Thing matches the same station (station 0-20000-0-06310)") == 2
    assert "several KNMI stations within 0.0005 degrees of the Location" in out


def test_datastream_with_a_foreign_sensor_id_is_left_alone(capsys):
    frost = FakeFrost()
    thing = frost.add_station(VLISSINGEN[1], VLISSINGEN[2], "0",
                              [("temperature", "T", "knmi-0"), ("humidity", "H", "sensor_community-1"),
                               ("wind_speed", "W", None)])

    assert _run(frost, apply=True) == migrate.EXIT_LEFT

    out = capsys.readouterr().out
    # A Datastream without a sensor_id gets the station's, as the connector would have written it.
    assert frost.sensor_ids(thing) == ["knmi-0-20000-0-06310", "sensor_community-1", "knmi-0-20000-0-06310"]
    assert "sensor_id 'sensor_community-1' is not a KNMI one" in out
    assert "(knmi-0 x1, no sensor_id x1)" in out


# ---------------------------------------------------------------------------
# Sensors and other keys that embed the sensor id
# ---------------------------------------------------------------------------


def test_sensor_properties_with_a_knmi_sensor_id_are_patched():
    frost = FakeFrost()
    thing = frost.add_station(VLISSINGEN[1], VLISSINGEN[2], "0", [("temperature", "T", "knmi-0")],
                              sensor_properties={"sensor_id": "knmi-0", "vendor": "KNMI"})

    assert _run(frost, apply=True) == migrate.EXIT_OK

    sensor_id = frost.datastreams[frost.things[thing]["datastreams"][0]]["sensor"]
    assert frost.sensors[sensor_id]["properties"] == {"sensor_id": "knmi-0-20000-0-06310", "vendor": "KNMI"}


def test_values_embedding_the_old_sensor_id_are_rewritten_exactly():
    props = {"sensor_id": "knmi-0", "streamKey": "knmi-0::temperature", "alias": "knmi-0", "other": "knmi-01",
             "observed_property": "temperature", "count": 3}

    updated, changes = migrate.rewrite_properties(props, "knmi-0", "knmi-0-20000-0-06310")

    assert updated == {"sensor_id": "knmi-0-20000-0-06310", "streamKey": "knmi-0-20000-0-06310::temperature",
                       "alias": "knmi-0-20000-0-06310", "other": "knmi-01", "observed_property": "temperature",
                       "count": 3}
    assert [key for key, _old, _new in changes] == ["sensor_id", "streamKey", "alias"]
    assert migrate.rewrite_properties(updated, "knmi-0-20000-0-06310", "knmi-0-20000-0-06310") == (updated, ())


# ---------------------------------------------------------------------------
# Failures and exit codes
# ---------------------------------------------------------------------------


def test_failed_patch_is_reported_and_rerun_finishes(capsys):
    frost = FakeFrost()
    ids = _production_like(frost)
    failing = frost.things[ids["valkenburg"]]["datastreams"][1]
    frost.fail_patch.add(("Datastreams", failing))

    assert _run(frost, apply=True) == migrate.EXIT_FROST_FAILED

    out = capsys.readouterr().out
    assert f"FAILED 0-20000-0-06210 Datastreams({failing}): HTTP 500: Internal error" in out
    assert "Read back: 1 PATCHes still pending; re-run the dry run." in out
    frost.fail_patch.clear()
    assert _run(frost, apply=True) == migrate.EXIT_OK
    assert frost.sensor_ids(ids["valkenburg"]) == ["knmi-0-20000-0-06210"] * 4


def test_an_empty_station_list_plans_nothing(capsys):
    frost = FakeFrost()
    _production_like(frost)

    assert _run(frost, apply=True, stations={"type": "FeatureCollection", "features": []}) == migrate.EXIT_KNMI_FAILED

    assert not frost.patches
    assert not frost.gets
    assert "nothing was planned" in capsys.readouterr().out


class _ApiResp:
    def __init__(self, status: int, body: Any = None) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def test_station_list_is_retried_on_429_5xx_and_timeouts():
    answers: list[Any] = [_ApiResp(429), requests.Timeout("slow"), _ApiResp(503), _ApiResp(200, {"features": []})]
    calls: list[dict[str, Any]] = []
    waits: list[float] = []

    def http_get(url: str, **kwargs: Any) -> Any:
        calls.append({"url": url, **kwargs})
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    body = migrate.fetch_station_list(
        "https://api.test/edr/v1/collections/obs/locations/", "secret-key", http_get=http_get, sleep=waits.append
    )

    assert body == {"features": []}
    assert waits == [2.0, 5.0, 10.0]
    assert calls[0]["url"] == "https://api.test/edr/v1/collections/obs/locations"
    assert calls[0]["headers"]["Authorization"] == "secret-key"


@pytest.mark.parametrize("status", [401, 403, 404])
def test_station_list_client_errors_fail_at_once_without_the_key(status):
    calls: list[str] = []

    def http_get(url: str, **kwargs: Any) -> Any:
        calls.append(url)
        return _ApiResp(status)

    with pytest.raises(migrate.KnmiApiError) as exc_info:
        migrate.fetch_station_list("https://api.test/c", "secret-key", http_get=http_get, sleep=lambda s: None)

    assert len(calls) == 1
    assert f"HTTP {status}" in str(exc_info.value)
    assert "secret-key" not in str(exc_info.value)


def test_station_list_gives_up_after_four_attempts():
    def http_get(url: str, **kwargs: Any) -> Any:
        return _ApiResp(200, ValueError("not json"))

    with pytest.raises(migrate.KnmiApiError, match="after 4 attempts: body is not JSON"):
        migrate.fetch_station_list("https://api.test/c", "", http_get=http_get, sleep=lambda s: None)


# ---------------------------------------------------------------------------
# main(): configuration, nothing secret printed
# ---------------------------------------------------------------------------


def _configure(monkeypatch: pytest.MonkeyPatch, frost: FakeFrost, *, target_url: str = BASE) -> list[dict[str, Any]]:
    built: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> FakeFrost:
        built.append(kwargs)
        return frost

    target = FrostTarget(url=target_url, version="v1.1", auth_username="writer", auth_password="hunter2", label="prod")
    monkeypatch.setattr(migrate, "FrostHTTPClient", factory)
    monkeypatch.setattr(
        migrate,
        "settings",
        dataclasses.replace(migrate.settings, frost_targets=(target,), entity_name_prefix=PREFIX,
                            knmi_api_key="secret-key", things_path="/Things", datastreams_path="/Datastreams",
                            sensors_path="/Sensors"),
    )
    return built


def test_main_reads_a_saved_station_list_and_prints_no_secrets(monkeypatch, tmp_path, capsys):
    frost = FakeFrost()
    ids = _production_like(frost)
    built = _configure(monkeypatch, frost)
    saved = tmp_path / "locations.json"
    saved.write_text(json.dumps(_locations_body(VLISSINGEN, VALKENBURG, IJMOND)), encoding="utf-8")

    assert migrate.main(["--locations-file", str(saved)]) == migrate.EXIT_OK
    assert not frost.patches
    with pytest.raises(SystemExit):  # --apply without a journal
        migrate.main(["--locations-file", str(saved), "--apply"])
    assert not frost.patches
    journal = tmp_path / "journal.jsonl"
    assert migrate.main(["--locations-file", str(saved), "--apply", "--journal", str(journal)]) == migrate.EXIT_OK
    assert len(journal.read_text(encoding="utf-8").splitlines()) == len(frost.patches) == 15

    out = capsys.readouterr().out
    assert built[0]["base_urls"] == [BASE]
    assert built[0]["auth_username"] == "writer"
    assert "hunter2" not in out
    assert "writer" not in out
    assert "secret-key" not in out
    assert "credentials from environment: yes" in out
    assert "note: the URL is a v2.0 endpoint" in out
    assert frost.sensor_ids(ids["ijmond"]) == ["knmi-0-20000-0-06209"] * 3


def test_main_refuses_an_unconfigured_target(monkeypatch):
    _configure(monkeypatch, FakeFrost())
    with pytest.raises(SystemExit, match="not a configured FROST target"):
        migrate.main(["--target-url", "https://elsewhere.test/FROST-Server/v1.1"])


def test_main_stops_when_the_knmi_api_fails(monkeypatch, capsys):
    frost = FakeFrost()
    _production_like(frost)
    _configure(monkeypatch, frost)

    def failing(*args: Any, **kwargs: Any) -> Any:
        raise migrate.KnmiApiError("GET https://api.test/locations failed with HTTP 403")

    monkeypatch.setattr(migrate, "fetch_station_list", failing)

    assert migrate.main(["--apply", "--journal", "unused.jsonl"]) == migrate.EXIT_KNMI_FAILED
    assert not frost.patches
    assert "HTTP 403; nothing was planned." in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The script assigns the id the connector uses
# ---------------------------------------------------------------------------


def test_script_copies_agree_with_the_connector():
    assert migrate.LOCATION_MATCH_TOLERANCE_DEG == knmi_weather.LOCATION_MATCH_TOLERANCE_DEG
    for lat, lon in ((52.36, 3.3417), (52.4636, 4.5175), (50.9053, 5.7619), (52.005, 4.995), (51.0, -0.125)):
        assert migrate.station_name_for(lat, lon) == knmi_weather.station_name_for(lat, lon)
    assert migrate.sensor_id_for("0-20000-0-06260") == knmi_weather.sensor_id_for("0-20000-0-06260")

    body = _locations_body(VLISSINGEN, VALKENBURG, IJMOND, ELSEWHERE)
    script_stations = migrate.parse_stations(body)
    connector_stations = knmi_weather.parse_locations(body)
    assert [(s.station_id, s.lon, s.lat, s.name) for s in script_stations] == [
        (s.station_id, s.lon, s.lat, s.name) for s in connector_stations
    ]
    for dlon, dlat in ((0, 0), (0.0004, -0.0004), (0.0006, 0), (0, 0.0005)):
        point = (IJMOND[1] + dlon, IJMOND[2] + dlat)
        script_match, _ = migrate.match_station(point, script_stations)
        connector_match = knmi_weather.match_location(point[0], point[1], connector_stations)
        assert (script_match.station_id if script_match else None) == (
            connector_match.station_id if connector_match else None
        )


def test_migrated_ids_are_the_ones_the_parser_assigns():
    """For a station the API serves at the Thing's coordinates, the script writes the sensor id
    the fixed connector's readings carry, so the routing check accepts the cached mapping."""
    frost = FakeFrost()
    thing = frost.add_station(IJMOND[1], IJMOND[2], "3", [("temperature", "Air temperature", "knmi-3")])
    _run(frost, apply=True)

    coverage = {
        "type": "Coverage",
        "domain": {"axes": {"x": {"values": [IJMOND[1]]}, "y": {"values": [IJMOND[2]]},
                            "t": {"values": ["2026-11-20T10:00:00Z"]}}},
        "ranges": {"ta": {"values": [7.5]}},
        knmi_weather.EDR_LOCATION_ID: IJMOND[0],
    }
    _, readings = knmi_weather.parse_edr_locations({"coverages": [coverage]})

    assert [r.sensor_id for r in readings] == frost.sensor_ids(thing)
    assert f"{PREFIX}{readings[0].thing_name}" == frost.things[thing]["name"]


# ---------------------------------------------------------------------------
# The journal and the rollback
# ---------------------------------------------------------------------------


def test_apply_journals_every_patch_before_sending_it(tmp_path, capsys):
    frost = FakeFrost()
    _production_like(frost)
    before = _snapshot(frost)
    journal = tmp_path / "journal.jsonl"
    frost.journal = journal

    assert _run(frost, apply=True, journal=journal) == migrate.EXIT_OK

    records = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
    assert len(records) == len(frost.patches) == 15
    for record, (collection, entity_id, payload) in zip(records, frost.patches, strict=True):
        assert (record["t"], record["target"]) == (migrate.JOURNAL_TAG, BASE)
        assert (record["entity_set"], record["id"]) == (collection, str(entity_id))
        assert record["old"] == before[(collection, entity_id)]
        assert record["new"] == payload["properties"]
    # The same records on stdout, for the operator's tee.
    out = capsys.readouterr().out
    marked = [line[len(migrate.JOURNAL_MARKER):] for line in out.splitlines() if line.startswith(migrate.JOURNAL_MARKER)]
    assert [json.loads(line) for line in marked] == records


def test_a_journal_that_cannot_be_written_stops_the_patches(tmp_path, capsys):
    frost = FakeFrost()
    _production_like(frost)
    before = _snapshot(frost)
    journal = tmp_path / "missing-dir" / "journal.jsonl"

    assert _run(frost, apply=True, journal=journal) == migrate.EXIT_FROST_FAILED

    assert not frost.patches and _snapshot(frost) == before
    assert "STOPPED before" in capsys.readouterr().out


def test_run_refuses_apply_without_a_journal():
    frost = FakeFrost()
    _production_like(frost)
    with pytest.raises(ValueError, match="needs a journal"):
        migrate.run(frost, _locations_body(VLISSINGEN), fields=odata_fields_for("v1.1"),  # type: ignore[arg-type]
                    entity_prefix=PREFIX, paths=PATHS, apply=True)
    assert not frost.patches


def test_apply_rollback_round_trip(tmp_path, capsys):
    frost = FakeFrost()
    _production_like(frost)
    before = _snapshot(frost)
    journal = tmp_path / "journal.jsonl"
    assert _run(frost, apply=True, journal=journal) == migrate.EXIT_OK
    migrated = _snapshot(frost)
    patches = len(frost.patches)
    capsys.readouterr()

    # Dry run: the plan, nothing written.
    assert _rollback(frost, journal, apply=False) == migrate.EXIT_OK
    out = capsys.readouterr().out
    assert "To restore: 15 (12 Datastreams, 0 Sensors, 3 Things); already as before: 0; changed since" in out
    assert len(frost.patches) == patches and _snapshot(frost) == migrated

    assert _rollback(frost, journal, apply=True) == migrate.EXIT_OK
    assert _snapshot(frost) == before
    assert "Read back: every restored entity is as before the migration." in capsys.readouterr().out
    # Again: nothing to do. Then migrate again, into the same journal, and roll back once more.
    patches = len(frost.patches)
    assert _rollback(frost, journal, apply=True) == migrate.EXIT_OK
    assert len(frost.patches) == patches
    assert "To restore: 0 (0 Datastreams, 0 Sensors, 0 Things); already as before: 15" in capsys.readouterr().out
    assert _run(frost, apply=True, journal=journal) == migrate.EXIT_OK
    assert _snapshot(frost) == migrated
    assert _rollback(frost, journal, apply=True) == migrate.EXIT_OK
    assert _snapshot(frost) == before


def test_rollback_leaves_entities_changed_since(tmp_path, capsys):
    frost = FakeFrost()
    ids = _production_like(frost)
    before = _snapshot(frost)
    journal = tmp_path / "journal.jsonl"
    assert _run(frost, apply=True, journal=journal) == migrate.EXIT_OK
    changed = frost.things[ids["ijmond"]]["datastreams"][0]
    frost.datastreams[changed]["properties"]["note"] = "edited by hand"
    edited = json.loads(json.dumps(frost.datastreams[changed]["properties"]))
    deleted = frost.things[ids["ijmond"]]["datastreams"][1]
    del frost.datastreams[deleted]
    frost.things[ids["ijmond"]]["datastreams"].remove(deleted)
    capsys.readouterr()

    assert _rollback(frost, journal, apply=True) == migrate.EXIT_LEFT

    out = capsys.readouterr().out
    assert f"changed since the migration, left alone: Datastreams({changed})" in out
    assert f"deleted since the migration: Datastreams({deleted})" in out
    assert "To restore: 13 (10 Datastreams, 0 Sensors, 3 Things)" in out
    assert frost.datastreams[changed]["properties"] == edited
    assert {k: v for k, v in _snapshot(frost).items() if k not in {("Datastreams", changed)}} == {
        k: v for k, v in before.items() if k not in {("Datastreams", changed), ("Datastreams", deleted)}
    }


def test_rollback_reads_a_saved_stdout_and_walks_records_back(tmp_path, capsys):
    """A tee'd stdout works as the journal; a PATCH recorded but never applied (it failed) and a re-run
    recording the entity again both roll back to the properties before the first record."""
    frost = FakeFrost()
    ids = _production_like(frost)
    before = _snapshot(frost)
    failing = frost.things[ids["valkenburg"]]["datastreams"][1]
    frost.fail_patch.add(("Datastreams", failing))
    journal = tmp_path / "journal.jsonl"
    assert _run(frost, apply=True, journal=journal) == migrate.EXIT_FROST_FAILED
    frost.fail_patch.clear()
    assert _run(frost, apply=True, journal=journal) == migrate.EXIT_OK
    saved = tmp_path / "apply.log"
    saved.write_text(capsys.readouterr().out + migrate.JOURNAL_MARKER + '{"t": "knmi-sensor-id-mig', encoding="utf-8")
    assert len(migrate.read_journal(saved)) == len(migrate.read_journal(journal)) == 16

    assert _rollback(frost, saved, apply=True) == migrate.EXIT_OK
    assert "cut-off last record skipped" in capsys.readouterr().out
    assert _snapshot(frost) == before


def test_rollback_refuses_a_journal_of_another_target(tmp_path, capsys):
    frost = FakeFrost()
    _production_like(frost)
    journal = tmp_path / "journal.jsonl"
    assert _run(frost, apply=True, journal=journal) == migrate.EXIT_OK
    migrated = _snapshot(frost)
    assert _rollback(frost, journal, apply=True, target="https://other.test/FROST-Server/v1.1") \
        == migrate.EXIT_FROST_FAILED
    assert _snapshot(frost) == migrated
    assert "another FROST target" in capsys.readouterr().out


def test_a_corrupt_journal_line_is_an_error(tmp_path):
    journal = tmp_path / "journal.jsonl"
    journal.write_text('{"t": "knmi-sensor-id-migration", "entity_set": "Things"}\n{}\n', encoding="utf-8")
    with pytest.raises(migrate.JournalError, match="line 1"):
        migrate.read_journal(journal)


def test_main_rollback_needs_an_existing_journal(monkeypatch, tmp_path, capsys):
    frost = FakeFrost()
    _production_like(frost)
    _configure(monkeypatch, frost)
    saved = tmp_path / "locations.json"
    saved.write_text(json.dumps(_locations_body(VLISSINGEN, VALKENBURG, IJMOND)), encoding="utf-8")
    before = _snapshot(frost)
    with pytest.raises(SystemExit):
        migrate.main(["--rollback"])
    with pytest.raises(SystemExit):
        migrate.main(["--rollback", "--journal", str(tmp_path / "nope.jsonl")])
    journal = tmp_path / "journal.jsonl"
    assert migrate.main(["--locations-file", str(saved), "--apply", "--journal", str(journal)]) == migrate.EXIT_OK
    assert migrate.main(["--rollback", "--journal", str(journal)]) == migrate.EXIT_OK
    assert _snapshot(frost) != before
    assert migrate.main(["--rollback", "--journal", str(journal), "--apply"]) == migrate.EXIT_OK
    assert _snapshot(frost) == before
    assert "Mode: ROLLBACK APPLY" in capsys.readouterr().out
