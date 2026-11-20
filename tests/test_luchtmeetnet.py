"""Unit tests for the Luchtmeetnet RIVM air quality source.

Covers:
- parse_measurements() with valid data produces correct canonical names / units
- parse_measurements() with an unknown formula is skipped
- build_entity_set() produces a valid entity-set dict
- LuchtmeetnetPollingSource is a RestPollingSource subclass with
  uses_dynamic_discovery = True
- discovery (mocked HTTP): coordinates and components come from
  GET /stations/{number}; a station without coordinates is skipped, never
  placed at (52, 5); details are cached and failed fetches retried; an empty
  or malformed station list page is a failed read, never the end of the list
- readings are held back (bounded) until discovery is complete, and the first
  fetch afterwards reads back over the held-back cycles
- the 3 h look-back applies from the first cycle; with the ingest loop's
  seeded high-water marks a restart fills the gap without duplicates
- entity sets carry one Datastream per canonical component, and registering
  them where the Thing already exists adds the missing Datastreams
- readings for registered components are pushable into the right Datastream
- scripts/repair_luchtmeetnet_locations.py plans and applies Location and
  generated FeatureOfInterest PATCHes (dry run by default)
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
import pytest
import requests

from app.frost.target import FrostTarget
from app.services import api_client as api_client_module
from app.services import luchtmeetnet_source as lmn_source_module
from app.services.api_client import AsyncAPIClient
from app.services.luchtmeetnet_source import LuchtmeetnetPollingSource
from app.services.rest_polling_source import RestPollingSource
from app.services.sensorthings_client import SensorThingsClient
from app.sources.luchtmeetnet import (
    StationDetail,
    build_entity_set,
    canonical_components,
    parse_measurements,
    parse_station_detail,
)
from app.sta.canonical import CanonicalDatastream
from scripts import repair_luchtmeetnet_locations as repair

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CANONICAL_UNITS = {member.value: member.meta.unit for member in CanonicalDatastream}
_CANONICAL_NAMES = {member.value for member in CanonicalDatastream}


def _make_entry(formula: str, value: float, timestamp: str | None = None) -> dict:
    return {
        "formula": formula,
        "value": value,
        "timestamp_measured": timestamp or "2026-10-07T12:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# parse_measurements — happy paths
# ---------------------------------------------------------------------------


class TestParseMeasurements:
    def test_no2_maps_to_canonical(self):
        data = [_make_entry("NO2", 23.5)]
        readings = parse_measurements(data, "NL49007", "Amsterdam-Einsteinweg")
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.NO2.value  # "no2"
        assert r.unit == _CANONICAL_UNITS["no2"]
        assert r.observed_property_name == CanonicalDatastream.NO2.meta.display_name

    def test_o3_maps_to_canonical(self):
        data = [_make_entry("O3", 41.0)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert len(readings) == 1
        assert readings[0].observed_property == "o3"
        assert readings[0].unit == "ug/m3"

    def test_pm10_maps_to_canonical(self):
        data = [_make_entry("PM10", 18.2)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert len(readings) == 1
        assert readings[0].observed_property == "pm10"
        assert readings[0].unit == "ug/m3"

    def test_pm25_maps_to_canonical_pm2_5(self):
        """PM25 formula must resolve to pm2_5, not pm25."""
        data = [_make_entry("PM25", 9.8)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert len(readings) == 1
        assert readings[0].observed_property == "pm2_5"
        assert readings[0].unit == "ug/m3"

    def test_so2_maps_to_canonical(self):
        data = [_make_entry("SO2", 3.1)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert len(readings) == 1
        assert readings[0].observed_property == "so2"

    def test_co_maps_to_canonical(self):
        data = [_make_entry("CO", 0.25)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert len(readings) == 1
        assert readings[0].observed_property == "co"
        assert readings[0].unit == "mg/m3"

    def test_nh3_maps_to_canonical(self):
        data = [_make_entry("NH3", 7.4)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert len(readings) == 1
        assert readings[0].observed_property == "nh3"

    def test_c6h6_maps_to_benzene(self):
        """C6H6 is the API formula for benzene and must resolve via alias."""
        data = [_make_entry("C6H6", 1.2)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert len(readings) == 1
        assert readings[0].observed_property == "benzene"
        assert readings[0].unit == "ug/m3"

    def test_multiple_formulas_in_one_batch(self):
        data = [
            _make_entry("NO2", 23.5),
            _make_entry("O3", 41.0),
            _make_entry("PM10", 18.2),
        ]
        readings = parse_measurements(data, "NL49007", "Amsterdam-Einsteinweg")
        assert len(readings) == 3
        props = {r.observed_property for r in readings}
        assert props == {"no2", "o3", "pm10"}

    def test_sensor_id_and_thing_name_use_station_fields(self):
        data = [_make_entry("NO2", 10.0)]
        readings = parse_measurements(data, "NL01491", "Rotterdam-Bentinckplein")
        assert len(readings) == 1
        r = readings[0]
        assert r.sensor_id == "luchtmeetnet-NL01491"
        assert r.thing_name == "Luchtmeetnet Rotterdam-Bentinckplein"
        assert r.sensor_name == "Luchtmeetnet Rotterdam-Bentinckplein sensor"

    def test_timestamp_parsed_correctly(self):
        data = [_make_entry("NO2", 10.0, "2026-10-07T08:00:00+00:00")]
        readings = parse_measurements(data, "NL49007", "Test")
        expected_ts = datetime(2026, 10, 7, 8, 0, 0, tzinfo=UTC)
        assert readings[0].timestamp == expected_ts

    def test_unit_is_always_canonical_not_api_provided(self):
        """Unit must come from canonical, never from any API field."""
        # NO2 canonical unit is ug/m3 — check nothing else leaks in.
        data = [_make_entry("NO2", 5.0)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert readings[0].unit == _CANONICAL_UNITS["no2"]

    def test_location_field_set_to_luchtmeetnet(self):
        data = [_make_entry("NO2", 5.0)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert readings[0].location == "luchtmeetnet"

    def test_quality_defaults_to_good(self):
        data = [_make_entry("NO2", 5.0)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert readings[0].quality == "good"


# ---------------------------------------------------------------------------
# parse_measurements — edge / error cases
# ---------------------------------------------------------------------------


class TestParseMeasurementsEdgeCases:
    def test_unknown_formula_is_skipped(self):
        data = [
            _make_entry("UNKNOWN_GAS", 99.0),
            _make_entry("NO2", 15.0),
        ]
        readings = parse_measurements(data, "NL49007", "Test")
        # Only NO2 should make it through; UNKNOWN_GAS is dropped
        assert len(readings) == 1
        assert readings[0].observed_property == "no2"

    def test_all_unknown_formulas_returns_empty(self):
        data = [_make_entry("XYLENE", 5.0), _make_entry("TOLUENE", 3.0)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert readings == []

    def test_missing_value_is_skipped(self):
        data = [{"formula": "NO2", "timestamp_measured": "2026-10-07T12:00:00+00:00"}]
        readings = parse_measurements(data, "NL49007", "Test")
        assert readings == []

    def test_non_numeric_value_is_skipped(self):
        data = [_make_entry("NO2", 0)]  # will be overridden
        data[0]["value"] = "not-a-number"
        readings = parse_measurements(data, "NL49007", "Test")
        assert readings == []

    def test_empty_data_list_returns_empty(self):
        readings = parse_measurements([], "NL49007", "Test")
        assert readings == []

    def test_non_dict_entries_are_skipped(self):
        data = ["garbage", None, 42, _make_entry("NO2", 5.0)]
        readings = parse_measurements(data, "NL49007", "Test")
        assert len(readings) == 1

    def test_missing_timestamp_defaults_gracefully(self):
        data = [{"formula": "NO2", "value": 10.0}]
        readings = parse_measurements(data, "NL49007", "Test")
        # Should produce a reading with a recent-ish timestamp rather than crashing
        assert len(readings) == 1
        assert readings[0].observed_property == "no2"


# ---------------------------------------------------------------------------
# build_entity_set
# ---------------------------------------------------------------------------


class TestBuildEntitySet:
    _DEFAULT_COMPONENTS: ClassVar[list[str]] = ["NO2", "O3", "PM10"]

    def _make_set(
        self,
        number: str = "NL49007",
        name: str = "Amsterdam-Einsteinweg",
        lat: float = 52.35,
        lon: float = 4.95,
        components: list[str] | None = None,
    ) -> dict:
        return build_entity_set(
            station_number=number,
            station_name=name,
            lat=lat,
            lon=lon,
            components=self._DEFAULT_COMPONENTS if components is None else components,
        )

    def test_thing_name_uses_station_name(self):
        es = self._make_set()
        assert es["thing"]["name"] == "Luchtmeetnet Amsterdam-Einsteinweg"

    def test_location_coordinates_order_lon_lat(self):
        """GeoJSON coordinates are [lon, lat]."""
        es = self._make_set(lat=52.35, lon=4.95)
        coords = es["location"]["location"]["coordinates"]
        assert coords == [4.95, 52.35]

    def test_sensor_id_uses_station_number(self):
        es = self._make_set(number="NL49007")
        assert es["sensors"][0]["sensor_id"] == "luchtmeetnet-NL49007"

    def test_observed_properties_keys_are_canonical(self):
        es = self._make_set(components=["NO2", "O3", "PM10", "PM25"])
        op_keys = set(es["observed_properties"].keys())
        assert op_keys <= _CANONICAL_NAMES

    def test_unknown_component_is_excluded_from_observed_properties(self):
        es = self._make_set(components=["NO2", "XYLENE"])
        op_keys = set(es["observed_properties"].keys())
        assert "no2" in op_keys
        assert "xylene" not in op_keys

    def test_sensors_observed_properties_are_canonical(self):
        es = self._make_set(components=["NO2", "O3"])
        props = es["sensors"][0]["observed_properties"]
        for p in props:
            assert p in _CANONICAL_NAMES

    def test_c6h6_resolves_to_benzene_in_entity_set(self):
        es = self._make_set(components=["C6H6"])
        sensor_props = es["sensors"][0]["observed_properties"]
        assert "benzene" in sensor_props

    def test_source_properties_set_correctly(self):
        es = self._make_set()
        assert es["thing"]["properties"]["source"] == "luchtmeetnet"
        assert es["thing"]["properties"]["network"] == "Luchtmeetnet"
        assert es["thing"]["properties"]["operator"] == "RIVM"

    def test_geo_json_encoding_type(self):
        es = self._make_set()
        assert es["location"]["encodingType"] == "application/geo+json"

    def test_empty_components_produces_empty_observed_properties(self):
        es = self._make_set(components=[])
        assert es["observed_properties"] == {}
        assert es["sensors"][0]["observed_properties"] == []


# ---------------------------------------------------------------------------
# LuchtmeetnetPollingSource
# ---------------------------------------------------------------------------


class TestLuchtmeetnetPollingSource:
    def test_is_rest_polling_source_subclass(self):
        assert issubclass(LuchtmeetnetPollingSource, RestPollingSource)

    def test_uses_dynamic_discovery_is_true(self):
        assert LuchtmeetnetPollingSource.uses_dynamic_discovery is True

    def test_source_name(self):
        assert LuchtmeetnetPollingSource.source_name == "luchtmeetnet"

    def test_is_disabled_by_default(self):
        """LUCHTMEETNET_ENABLED defaults to false in tests (env not set)."""
        src = LuchtmeetnetPollingSource()
        # Default config has luchtmeetnet_enabled=False
        assert not src.is_enabled()

    def test_poll_interval_minimum_10(self):
        src = LuchtmeetnetPollingSource()
        assert src.poll_interval() >= 10

    def test_entity_sets_empty_before_discovery(self):
        src = LuchtmeetnetPollingSource()
        assert src.entity_sets() == []

    def test_entity_sets_populated_after_station_injection(self):
        """Simulate post-discovery state by injecting a station directly."""
        src = LuchtmeetnetPollingSource()
        src._discovered_stations["NL49007"] = {
            "name": "Amsterdam-Einsteinweg",
            "lat": 52.35,
            "lon": 4.95,
            "components": ["NO2", "O3"],
        }
        sets = src.entity_sets()
        assert len(sets) == 1
        assert sets[0]["thing"]["name"] == "Luchtmeetnet Amsterdam-Einsteinweg"

    def test_entity_sets_include_all_discovered_stations(self):
        src = LuchtmeetnetPollingSource()
        src._discovered_stations["NL49007"] = {
            "name": "Station A", "lat": 52.0, "lon": 5.0, "components": ["NO2"],
        }
        src._discovered_stations["NL01491"] = {
            "name": "Station B", "lat": 51.9, "lon": 4.5, "components": ["O3"],
        }
        sets = src.entity_sets()
        assert len(sets) == 2
        names = {es["thing"]["name"] for es in sets}
        assert "Luchtmeetnet Station A" in names
        assert "Luchtmeetnet Station B" in names


# ---------------------------------------------------------------------------
# Mocked Luchtmeetnet API (httpx.MockTransport behind the real AsyncAPIClient)
# ---------------------------------------------------------------------------

_API_BASE = "https://lmn.test/open_api"
_REAL_ASYNC_CLIENT = httpx.AsyncClient
_PLACEHOLDER = [5.0, 52.0]

# Shapes and values as served by the live API (Nov 2026). The list endpoint
# only returns number + location (one name with a trailing space, as served).
_STATIONS: list[dict[str, Any]] = [
    {"number": "NL49565", "location": "Oude Meer-Aalsmeerderdijk"},
    {"number": "NL10450", "location": "Den Haag-Neherkade"},
    {"number": "NL50002", "location": "Geleen-Vouershof "},
    {"number": "NL50013", "location": "Meerssen- Beekerweg"},
]


def _detail(lon: float, lat: float, components: list[str]) -> dict[str, Any]:
    return {
        "type": "Regional",
        "components": components,
        "geometry": {"type": "point", "coordinates": [lon, lat]},
        "organisation": "RIVM",
    }


_DETAILS: dict[str, dict[str, Any]] = {
    "NL49565": _detail(4.77082, 52.28013, ["NO", "NO2", "PM10"]),
    "NL10450": _detail(4.318551, 52.062537, ["NO2", "NO", "PM10", "NOx", "O3", "PM2.5", "PM25"]),
    "NL50002": _detail(5.81071, 50.96309, ["NO", "NO2", "O3"]),
    "NL50013": _detail(5.773652, 50.905079, ["PS"]),  # no canonical component
}


def _measurement(number: str, formula: str, value: float, timestamp: str) -> dict[str, Any]:
    return {
        "station_number": number,
        "formula": formula,
        "value": value,
        "timestamp_measured": timestamp,
    }


class _FakeLuchtmeetnetAPI:
    """In-memory Luchtmeetnet open API served through ``httpx.MockTransport``."""

    def __init__(
        self,
        stations: list[dict[str, Any]],
        details: dict[str, dict[str, Any]],
        measurements: dict[str, list[dict[str, Any]]] | None = None,
        *,
        per_page: int = 25,
        detail_status: dict[str, int] | None = None,
        list_status: dict[int, int] | None = None,
        list_bodies: dict[int, Any] | None = None,
        measurement_status: dict[str, int] | None = None,
        filter_window: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.stations = stations
        self.details = details
        self.measurements = measurements or {}
        self.per_page = per_page
        self.detail_status = detail_status or {}
        self.list_status = list_status or {}
        # 200 bodies served instead of the real page (empty or malformed lists)
        self.list_bodies = list_bodies or {}
        self.measurement_status = measurement_status or {}
        # Serve only measurements inside the requested start/end, like the live API
        self.filter_window = filter_window
        self.delay = delay
        self.requests: list[httpx.Request] = []
        self.in_flight = 0
        self.max_in_flight = 0

    def list_pages(self) -> list[str]:
        return [r.url.params.get("page", "") for r in self.requests if r.url.path == "/open_api/stations"]

    def detail_numbers(self) -> list[str]:
        return [
            r.url.path.rsplit("/", 1)[-1] for r in self.requests if r.url.path.startswith("/open_api/stations/")
        ]

    def measurement_params(self) -> list[httpx.QueryParams]:
        return [r.url.params for r in self.requests if r.url.path == "/open_api/measurements"]

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            return self._route(request)
        finally:
            self.in_flight -= 1

    def _route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/open_api")
        params = request.url.params
        if path == "/stations":
            page = int(params.get("page", "1"))
            status = self.list_status.get(page, 200)
            if status != 200:
                return httpx.Response(status, json={"error": "unavailable"})
            if page in self.list_bodies:
                return httpx.Response(200, json=self.list_bodies[page])
            last_page = max(1, -(-len(self.stations) // self.per_page))
            chunk = self.stations[(page - 1) * self.per_page : page * self.per_page]
            return httpx.Response(
                200, json={"pagination": {"current_page": page, "last_page": last_page}, "data": chunk}
            )
        if path.startswith("/stations/"):
            number = path.rsplit("/", 1)[-1]
            status = self.detail_status.get(number, 200)
            if status != 200:
                return httpx.Response(status, json={"error": "unavailable"})
            if number not in self.details:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"data": self.details[number]})
        if path == "/measurements":
            number = params.get("station_number", "")
            status = self.measurement_status.get(number, 200)
            if status != 200:
                return httpx.Response(status, json={"message": "Too Many Requests"})
            data = self.measurements.get(number, [])
            if self.filter_window:
                start = datetime.fromisoformat(params["start"])
                end = datetime.fromisoformat(params["end"])
                data = [m for m in data if start <= datetime.fromisoformat(m["timestamp_measured"]) <= end]
            return httpx.Response(200, json={"pagination": {"last_page": 1}, "data": data})
        return httpx.Response(404, json={"error": f"unexpected path {path}"})


@pytest.fixture
def install_api(monkeypatch):
    """Route every AsyncAPIClient request to a fake Luchtmeetnet API."""

    def _install(api: _FakeLuchtmeetnetAPI) -> _FakeLuchtmeetnetAPI:
        def _client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(api.handler)
            return _REAL_ASYNC_CLIENT(*args, **kwargs)

        monkeypatch.setattr(api_client_module.httpx, "AsyncClient", _client)
        return api

    return _install


async def _no_sleep(*_args: Any, **_kwargs: Any) -> None:
    return None


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now


@pytest.fixture
def clock(monkeypatch):
    """Controls ``datetime.now`` inside the source module; set ``clock.now`` to move time."""
    state = _Clock(datetime(2026, 11, 20, 12, 0, tzinfo=UTC))

    class _ClockDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return state.now if tz is None else state.now.astimezone(tz)

    monkeypatch.setattr(lmn_source_module, "datetime", _ClockDatetime)
    return state


def _iso(moment: datetime) -> str:
    """A time as the source sends it in the start/end measurement parameters."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_source() -> LuchtmeetnetPollingSource:
    src = LuchtmeetnetPollingSource()
    src._base_url = _API_BASE
    return src


def _sets_by_station(src: LuchtmeetnetPollingSource) -> dict[str, dict[str, Any]]:
    return {es["thing"]["properties"]["station_number"]: es for es in src.entity_sets()}


# ---------------------------------------------------------------------------
# parse_station_detail / canonical_components
# ---------------------------------------------------------------------------


class TestParseStationDetail:
    def test_reads_lon_lat_order_and_components(self):
        detail = parse_station_detail({"data": _DETAILS["NL49565"]})
        assert detail == StationDetail(lat=52.28013, lon=4.77082, components=["NO", "NO2", "PM10"])

    def test_accepts_the_bare_record(self):
        assert parse_station_detail(_DETAILS["NL49565"]) == parse_station_detail({"data": _DETAILS["NL49565"]})

    def test_comma_separated_components_are_split_and_deduplicated(self):
        detail = parse_station_detail({"data": {**_DETAILS["NL49565"], "components": "NO2, PM10,NO2,"}})
        assert detail is not None
        assert detail.components == ["NO2", "PM10"]

    def test_missing_components_give_an_empty_list(self):
        detail = parse_station_detail({"data": {"geometry": {"type": "point", "coordinates": [4.9, 52.3]}}})
        assert detail is not None
        assert detail.components == []

    @pytest.mark.parametrize(
        "geometry",
        [
            None,
            {},
            {"type": "point"},
            {"type": "point", "coordinates": None},
            {"type": "point", "coordinates": []},
            {"type": "point", "coordinates": [4.9]},
            {"type": "point", "coordinates": ["x", "y"]},
            {"type": "point", "coordinates": [float("nan"), 52.0]},
            {"type": "point", "coordinates": [52.0, 95.0]},  # latitude out of range
        ],
    )
    def test_no_usable_coordinates_returns_none(self, geometry):
        assert parse_station_detail({"data": {"components": ["NO2"], "geometry": geometry}}) is None

    @pytest.mark.parametrize("body", [None, [], "oops", {"data": None}, {"data": []}])
    def test_malformed_body_returns_none(self, body):
        assert parse_station_detail(body) is None


class TestCanonicalComponents:
    def test_unknown_formulas_dropped_and_aliases_deduplicated(self):
        components = ["NO2", "NO", "PM10", "NOx", "O3", "PM2.5", "PM25"]
        assert canonical_components(components) == ["no2", "pm10", "o3", "pm2_5"]

    def test_station_without_a_supported_formula(self):
        assert canonical_components(["PS", "FN", "BCWB"]) == []


# ---------------------------------------------------------------------------
# Discovery through the station detail endpoint (mocked HTTP)
# ---------------------------------------------------------------------------


class TestStationDiscovery:
    def test_detail_endpoint_supplies_coordinates_and_components(self, install_api):
        api = install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, per_page=2))
        src = _make_source()
        _run(src._do_fetch_readings())

        stations = src._discovered_stations
        assert set(stations) == {"NL49565", "NL10450", "NL50002", "NL50013"}
        assert stations["NL49565"]["lat"] == 52.28013
        assert stations["NL49565"]["lon"] == 4.77082
        assert stations["NL49565"]["components"] == ["NO", "NO2", "PM10"]
        assert stations["NL10450"]["lat"] == 52.062537
        assert api.list_pages() == ["1", "2"]  # paged list followed
        assert sorted(api.detail_numbers()) == sorted(_DETAILS)  # one detail GET per station

    def test_station_names_are_stripped_as_before(self, install_api):
        """Thing/Location/Sensor names derive from the name; they must match the existing FROST entities."""
        install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS))
        src = _make_source()
        _run(src._do_fetch_readings())

        assert src._discovered_stations["NL50002"]["name"] == "Geleen-Vouershof"
        es = _sets_by_station(src)["NL50002"]
        assert es["thing"]["name"] == "Luchtmeetnet Geleen-Vouershof"
        assert es["location"]["name"] == "Luchtmeetnet Geleen-Vouershof location"
        assert es["sensors"][0]["name"] == "Luchtmeetnet Geleen-Vouershof sensor"

    def test_station_without_coordinates_is_skipped_not_placed_at_52_5(self, install_api, caplog):
        details = {**_DETAILS, "NL50002": {"components": ["NO2", "O3"], "geometry": None}}
        api = install_api(_FakeLuchtmeetnetAPI(_STATIONS, details))
        src = _make_source()
        with caplog.at_level(logging.WARNING, logger="connector.luchtmeetnet"):
            _run(src._do_fetch_readings())

        assert "NL50002" not in src._discovered_stations
        assert any("NL50002" in rec.getMessage() and rec.levelno == logging.WARNING for rec in caplog.records)
        sets = src.entity_sets()
        assert "NL50002" not in _sets_by_station(src)
        assert all(es["location"]["location"]["coordinates"] != _PLACEHOLDER for es in sets)

        # Skipped for the life of the process, not re-fetched every cycle.
        _run(src._do_fetch_readings())
        assert api.detail_numbers().count("NL50002") == 1

    def test_details_are_cached_across_cycles(self, install_api):
        api = install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS))
        src = _make_source()
        _run(src._do_fetch_readings())
        _run(src._do_fetch_readings())

        assert api.list_pages() == ["1"]
        assert sorted(api.detail_numbers()) == sorted(_DETAILS)

    def test_failed_detail_is_retried_next_cycle(self, install_api, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)  # skip AsyncAPIClient's retry backoff
        api = install_api(
            _FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS, detail_status={"NL10450": 503})
        )
        src = _make_source()
        readings = _run(src._do_fetch_readings())

        assert "NL10450" not in src._discovered_stations
        assert api.detail_numbers().count("NL10450") == 3  # the client's own retries
        assert "NL10450" not in _sets_by_station(src)
        # Held back (no measurement requests either) so the first registration is complete.
        assert readings == []
        assert api.measurement_params() == []

        api.detail_status = {}
        readings = _run(src._do_fetch_readings())
        assert src._discovered_stations["NL10450"]["lon"] == 4.318551
        assert api.detail_numbers().count("NL49565") == 1  # resolved stations are not fetched again
        assert {r.sensor_id for r in readings} == {"luchtmeetnet-NL49565", "luchtmeetnet-NL10450"}

    def test_holding_readings_back_is_bounded(self, install_api, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        api = install_api(
            _FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS, detail_status={"NL10450": 503})
        )
        src = _make_source()

        assert _run(src._do_fetch_readings()) == []
        assert _run(src._do_fetch_readings()) == []
        readings = _run(src._do_fetch_readings())  # gives up waiting on the third cycle
        assert {r.sensor_id for r in readings} == {"luchtmeetnet-NL49565"}
        assert api.detail_numbers().count("NL10450") == 9  # still retried every cycle

        # Once released, readings are never held back again.
        assert _run(src._do_fetch_readings())

    def test_station_resolved_after_release_joins_the_entity_sets(self, install_api, monkeypatch):
        """The ingest loop registers entity sets that appear later before pushing their readings."""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        api = install_api(
            _FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS, detail_status={"NL10450": 503})
        )
        src = _make_source()
        for _ in range(lmn_source_module._MAX_HOLD_BACK_CYCLES + 1):
            readings = _run(src._do_fetch_readings())
        assert {r.sensor_id for r in readings} == {"luchtmeetnet-NL49565"}
        assert "NL10450" not in _sets_by_station(src)

        api.detail_status = {}
        readings = _run(src._do_fetch_readings())
        assert _sets_by_station(src)["NL10450"]["sensors"][0]["observed_properties"] == ["no2", "pm10", "o3", "pm2_5"]
        assert {r.sensor_id for r in readings} == {"luchtmeetnet-NL49565", "luchtmeetnet-NL10450"}

    def test_rate_limited_detail_fetches_stop_and_resume_next_cycle(self, install_api, monkeypatch):
        """A persistent 429 means a block: skip the remaining details instead of prolonging it."""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(lmn_source_module, "_DETAIL_CONCURRENCY", 1)
        api = install_api(
            _FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS, detail_status={"NL49565": 429})
        )
        src = _make_source()
        assert _run(src._do_fetch_readings()) == []
        assert api.detail_numbers() == ["NL49565"] * 3  # the client's retries, then nothing more

        api.detail_status = {}
        readings = _run(src._do_fetch_readings())
        assert set(src._discovered_stations) == set(_DETAILS)
        assert readings

    def test_rate_limited_measurements_stop_the_cycle(self, install_api, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        api = install_api(
            _FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS, measurement_status={"NL10450": 429})
        )
        readings = _run(_make_source()._do_fetch_readings())

        requested = [p["station_number"] for p in api.measurement_params()]
        assert requested == ["NL49565"] + ["NL10450"] * 3  # NL50002 is not requested any more
        assert {r.sensor_id for r in readings} == {"luchtmeetnet-NL49565"}

    def test_station_unknown_to_the_detail_endpoint_is_skipped(self, install_api, monkeypatch, caplog):
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        stations = [*_STATIONS, {"number": "NL99999", "location": "Decommissioned"}]
        api = install_api(_FakeLuchtmeetnetAPI(stations, _DETAILS, _MEASUREMENTS))  # no detail: 404
        src = _make_source()
        with caplog.at_level(logging.WARNING, logger="connector.luchtmeetnet"):
            readings = _run(src._do_fetch_readings())

        assert "NL99999" not in src._discovered_stations
        assert any("NL99999" in rec.getMessage() for rec in caplog.records)
        assert readings  # a permanently missing station does not hold readings back

        _run(src._do_fetch_readings())
        assert api.detail_numbers().count("NL99999") == 3  # one cycle of client retries, then skipped

    def test_failed_list_page_is_retried_next_cycle(self, install_api, monkeypatch):
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        api = install_api(
            _FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS, per_page=2, list_status={2: 500})
        )
        src = _make_source()
        readings = _run(src._do_fetch_readings())
        assert set(src._discovered_stations) == {"NL49565", "NL10450"}  # page 1 is kept
        # Every station read so far is resolved, but the list is not: readings are held back.
        assert readings == []
        assert api.measurement_params() == []

        api.list_status = {}
        readings = _run(src._do_fetch_readings())
        assert set(src._discovered_stations) == set(_DETAILS)
        assert sorted(api.detail_numbers()) == sorted(_DETAILS)
        assert {r.sensor_id for r in readings} == {"luchtmeetnet-NL49565", "luchtmeetnet-NL10450"}
        assert {p["station_number"] for p in api.measurement_params()} == {"NL49565", "NL10450", "NL50002"}

    @pytest.mark.parametrize(
        "body",
        [
            {"pagination": {"current_page": 1, "last_page": 1}, "data": []},
            {"message": "Service temporarily unavailable"},
            {"data": [{"number": "NL49565", "location": "Oude Meer-Aalsmeerderdijk"}]},  # no pagination
            {"pagination": {"last_page": 1}, "data": [{"location": "no number"}]},
        ],
        ids=["empty", "no-station-list", "no-pagination", "no-usable-station"],
    )
    def test_empty_or_malformed_first_list_page_is_a_failed_discovery(self, install_api, body):
        api = install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS, list_bodies={1: body}))
        src = _make_source()
        assert _run(src._do_fetch_readings()) == []
        assert not src._station_list_complete
        assert src.entity_sets() == []

        api.list_bodies = {}  # the API recovers: the next cycle reads the list again
        readings = _run(src._do_fetch_readings())
        assert api.list_pages() == ["1", "1"]
        assert set(src._discovered_stations) == set(_DETAILS)
        assert src._station_list_complete
        assert readings

    def test_empty_page_before_the_last_one_leaves_the_list_incomplete(self, install_api):
        empty_page = {"pagination": {"current_page": 2, "last_page": 2}, "data": []}
        api = install_api(
            _FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS, per_page=2, list_bodies={2: empty_page})
        )
        src = _make_source()
        assert _run(src._do_fetch_readings()) == []  # held back: the list was cut short
        assert not src._station_list_complete
        assert api.measurement_params() == []

        api.list_bodies = {}
        readings = _run(src._do_fetch_readings())
        assert src._station_list_complete
        assert set(_sets_by_station(src)) == {"NL49565", "NL10450", "NL50002"}
        assert {r.sensor_id for r in readings} == {"luchtmeetnet-NL49565", "luchtmeetnet-NL10450"}

    def test_detail_fetches_are_concurrent_but_bounded(self, install_api):
        stations = [{"number": f"NL{i:05d}", "location": f"Station {i}"} for i in range(20)]
        details = {s["number"]: _detail(5.1, 52.1, ["NO2"]) for s in stations}
        api = install_api(_FakeLuchtmeetnetAPI(stations, details, delay=0.01))
        src = _make_source()
        _run(src._discover_stations(AsyncAPIClient(base_url=_API_BASE)))

        assert len(src._discovered_stations) == 20
        assert 1 < api.max_in_flight <= lmn_source_module._DETAIL_CONCURRENCY


# ---------------------------------------------------------------------------
# Entity sets built from discovered stations
# ---------------------------------------------------------------------------


class TestEntitySetsFromDiscovery:
    def test_entity_sets_carry_a_datastream_per_canonical_component(self, install_api):
        install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS))
        src = _make_source()
        _run(src._do_fetch_readings())

        es = _sets_by_station(src)["NL10450"]
        assert es["sensors"][0]["sensor_id"] == "luchtmeetnet-NL10450"
        assert es["sensors"][0]["observed_properties"] == ["no2", "pm10", "o3", "pm2_5"]
        assert set(es["observed_properties"]) == {"no2", "pm10", "o3", "pm2_5"}
        assert es["location"]["location"]["coordinates"] == [4.318551, 52.062537]

    def test_station_without_a_supported_component_gets_no_entity_set(self, install_api):
        install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS))
        src = _make_source()
        _run(src._do_fetch_readings())

        assert set(_sets_by_station(src)) == {"NL49565", "NL10450", "NL50002"}

    def test_injected_station_without_coordinates_is_not_registered(self):
        src = LuchtmeetnetPollingSource()
        src._discovered_stations["NL49007"] = {"name": "Amsterdam-Einsteinweg", "components": ["NO2"]}
        assert src.entity_sets() == []


# ---------------------------------------------------------------------------
# Registration against FROST where the Things already exist (fake FROST)
# ---------------------------------------------------------------------------

_FROST_BASE = "https://lmn-frost.test/FROST-Server/v1.1"


def _frost_settings(tmp_path: Any) -> SimpleNamespace:
    return SimpleNamespace(
        registered_entities_path=str(tmp_path / "registered_entities.json"),
        failed_observations_path=str(tmp_path / "failed_observations.jsonl"),
        datastream_ids={},
        sensorthings_base_url=_FROST_BASE,
        sensorthings_base_urls=(_FROST_BASE,),
        auth_username="",
        auth_password="",
        auth_token="",
        entity_name_prefix="GEO_",
        connector_name="UrbanAdapt SensorThings Connector",
        things_path="/Things",
        locations_path="/Locations",
        sensors_path="/Sensors",
        observed_properties_path="/ObservedProperties",
        datastreams_path="/Datastreams",
        observations_path="/Observations",
        projects_path="/Projects",
        actuators_path="/Actuators",
        tasking_capabilities_path="/TaskingCapabilities",
        tasks_path="/Tasks",
        default_project_name="",
        default_project_id="",
        default_project_description="",
        default_project_public=True,
        site_project_configs={},
        site_tasking_configs={},
        tasking_allowed_commands=set(),
        request_timeout_seconds=5.0,
        frost_targets=(),
        frost_batch_push_enabled=False,
        frost_v2_batch_push_enabled=False,
        frost_batch_max_observations=500,
        frost_batch_timeout_seconds=60,
        frost_batch_max_concurrency=4,
        frost_cb_failure_threshold=3,
        frost_cb_cooldown_seconds=600,
        failed_dlq_max_bytes=50_000_000,
    )


class _FakeFrostResponse:
    def __init__(self, status_code: int, body: Any = None) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._body = body
        self.headers: dict[str, str] = {}
        self.text = "" if body is None else json.dumps(body)

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("empty body")
        return self._body


def _collection_of(url: str) -> tuple[str, int | None]:
    last = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    match = re.fullmatch(r"(\w+)(?:\((\d+)\))?", last)
    assert match, url
    return match.group(1), int(match.group(2)) if match.group(2) else None


class _FakeFrost:
    """In-memory FROST v1.1 behind the get/post/patch surface of ``requests.Session``."""

    def __init__(self) -> None:
        self.entities: dict[str, dict[int, dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, Any]] = []
        self.headers: dict[str, str] = {}
        self._ids = itertools.count(100)

    def add(self, collection: str, **fields: Any) -> int:
        entity_id = next(self._ids)
        self.entities.setdefault(collection, {})[entity_id] = {"@iot.id": entity_id, **fields}
        return entity_id

    def posted(self, collection: str) -> list[dict[str, Any]]:
        return [
            payload for method, url, payload in self.calls if method == "POST" and _collection_of(url)[0] == collection
        ]

    def get(self, url: str, params: dict[str, str] | None = None, **_: Any) -> _FakeFrostResponse:
        self.calls.append(("GET", url, params))
        collection, entity_id = _collection_of(url)
        store = self.entities.get(collection, {})
        if entity_id is not None:
            entity = store.get(entity_id)
            return _FakeFrostResponse(200, entity) if entity else _FakeFrostResponse(404, {"message": "Nothing found."})
        odata_filter = (params or {}).get("$filter")
        if odata_filter is None:
            return _FakeFrostResponse(200, {"value": list(store.values())})
        match = re.fullmatch(r"name eq '(.*)'", odata_filter)
        if match is None:  # filters this fake does not model never match
            return _FakeFrostResponse(200, {"value": []})
        name = match.group(1).replace("''", "'")
        return _FakeFrostResponse(200, {"value": [e for e in store.values() if e.get("name") == name]})

    def post(self, url: str, **kwargs: Any) -> _FakeFrostResponse:
        payload = kwargs.get("json") or {}
        self.calls.append(("POST", url, payload))
        collection, _ = _collection_of(url)
        entity_id = self.add(collection, **payload)
        return _FakeFrostResponse(201, self.entities[collection][entity_id])

    def patch(self, url: str, **kwargs: Any) -> _FakeFrostResponse:
        payload = kwargs.get("json") or {}
        self.calls.append(("PATCH", url, payload))
        collection, entity_id = _collection_of(url)
        entity = self.entities.get(collection, {}).get(entity_id or -1)
        if entity is None:
            return _FakeFrostResponse(404, {"message": "Nothing found."})
        entity.update(payload)
        return _FakeFrostResponse(200)


def _client_on(frost: _FakeFrost, monkeypatch: Any, tmp_path: Any) -> SensorThingsClient:
    monkeypatch.setattr("app.services.sensorthings_client.settings", _frost_settings(tmp_path))
    client = SensorThingsClient()
    client._http._session = frost
    return client


class TestRegistrationOnExistingThings:
    def test_existing_thing_gets_its_missing_datastreams(self, install_api, monkeypatch, tmp_path):
        install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS))
        src = _make_source()
        _run(src._do_fetch_readings())
        entity_set = _sets_by_station(src)["NL10450"]

        # Production state: Thing, Location (at the placeholder) and Sensor exist, no Datastreams.
        frost = _FakeFrost()
        thing_id = frost.add(
            "Things", name="GEO_Luchtmeetnet Den Haag-Neherkade", properties={"station_number": "NL10450"}
        )
        location_id = frost.add(
            "Locations",
            name="GEO_Luchtmeetnet Den Haag-Neherkade location",
            encodingType="application/geo+json",
            location={"type": "Point", "coordinates": _PLACEHOLDER},
        )
        frost.add("Sensors", name="GEO_Luchtmeetnet Den Haag-Neherkade sensor")
        client = _client_on(frost, monkeypatch, tmp_path)

        result = client.register_entity_set(entity_set)

        assert result["thing_id"] == str(thing_id)
        assert result["thing_status"] == "existing"
        created = frost.posted("Datastreams")
        assert sorted(ds["properties"]["observed_property"] for ds in created) == ["no2", "o3", "pm10", "pm2_5"]
        assert all(ds["Thing"] == {"@iot.id": thing_id} for ds in created)
        assert not frost.posted("Things")
        assert not frost.posted("Locations")
        assert not frost.posted("Sensors")
        # Registration never touches an existing Location: the placeholder needs the repair script.
        assert not [call for call in frost.calls if call[0] == "PATCH"]
        assert frost.entities["Locations"][location_id]["location"]["coordinates"] == _PLACEHOLDER

        # Idempotent: registering again (e.g. after a restart) creates nothing new.
        client.register_entity_set(entity_set)
        assert len(frost.posted("Datastreams")) == 4


# ---------------------------------------------------------------------------
# Measurements → readings → push
# ---------------------------------------------------------------------------

_T09 = "2026-11-20T09:00:00+00:00"
_T10 = "2026-11-20T10:00:00+00:00"

_MEASUREMENTS: dict[str, list[dict[str, Any]]] = {
    "NL49565": [
        _measurement("NL49565", "NO2", 12.9, _T10),
        _measurement("NL49565", "NO", 3.3, _T10),  # not canonical
        _measurement("NL49565", "PM10", 9.9, _T10),
        _measurement("NL49565", "O3", 20.0, _T10),  # canonical, but not one of this station's components
        _measurement("NL49565", "NO2", 14.1, _T09),
    ],
    "NL10450": [
        _measurement("NL10450", "NO2", 24.57, _T10),
        _measurement("NL10450", "NOx", 56.93, _T10),
        _measurement("NL10450", "O3", 22.91, _T10),
        _measurement("NL10450", "PM10", 12.14, _T10),
        _measurement("NL10450", "PM25", 8.99, _T10),
        _measurement("NL10450", "PM2.5", 9.01, _T10),  # same property and hour as PM25
    ],
}


class TestReadingsArePushable:
    def test_measurements_are_requested_without_a_formula_filter(self, install_api):
        """The API honours only the first formula of a comma list; a filter would limit stations to NO2."""
        api = install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS))
        _run(_make_source()._do_fetch_readings())

        params = api.measurement_params()
        assert params
        assert all("formula" not in p for p in params)
        assert {p["station_number"] for p in params} == {"NL49565", "NL10450", "NL50002"}  # not the PS-only station

    def test_look_back_is_three_hours_from_the_first_cycle_on(self, install_api, clock):
        """Late-published hours are caught next cycle; after a restart the window also fills the gap."""
        api = install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS))
        src = _make_source()
        t0 = clock.now

        _run(src._do_fetch_readings())
        assert {(p["start"], p["end"]) for p in api.measurement_params()} == {(_iso(t0 - timedelta(hours=3)), _iso(t0))}

        clock.now = t0 + timedelta(hours=1)
        _run(src._do_fetch_readings())
        last = api.measurement_params()[-1]
        assert (last["start"], last["end"]) == (_iso(t0 - timedelta(hours=2)), _iso(t0 + timedelta(hours=1)))

    def test_restart_fills_the_gap_without_pushing_anything_twice(self, install_api, clock):
        """The ingest loop's seeded high-water marks drop what the previous process already pushed."""
        from app.main import _dedup_readings

        hours = [datetime(2026, 11, 20, h, 0, tzinfo=UTC) for h in (7, 8, 9, 10)]
        measurements = {
            "NL49565": [_measurement("NL49565", f, 10.0 + i, _iso(h)) for f in ("NO2", "PM10") for i, h in enumerate(hours)]
        }
        install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, measurements, filter_window=True))
        # The previous process pushed NO2 up to 10:00, but PM10 only up to 08:00: its
        # 09:00 and 10:00 values were published after that process's last cycle.
        seeded = {"luchtmeetnet-NL49565::no2": hours[3], "luchtmeetnet-NL49565::pm10": hours[1]}
        clock.now = datetime(2026, 11, 20, 10, 50, tzinfo=UTC)  # restart

        readings = _run(_make_source()._do_fetch_readings())
        new = _dedup_readings(readings, seeded)

        assert [(r.observed_property, r.timestamp.hour) for r in new] == [("pm10", 9), ("pm10", 10)]

    def test_first_fetch_after_holding_back_reads_back_over_the_held_back_cycles(
        self, install_api, monkeypatch, clock
    ):
        """Holding readings back delays them; it must not drop the hours the held-back cycles would have read."""
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        api = install_api(
            _FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS, detail_status={"NL10450": 503})
        )
        src = _make_source()
        t0 = clock.now

        assert _run(src._do_fetch_readings()) == []
        clock.now = t0 + timedelta(hours=1, minutes=1)
        assert _run(src._do_fetch_readings()) == []
        clock.now = t0 + timedelta(hours=2, minutes=2)
        assert _run(src._do_fetch_readings())  # released: reads back to the first cycle's window start
        released = api.measurement_params()
        assert {(p["start"], p["end"]) for p in released} == {(_iso(t0 - timedelta(hours=3)), _iso(clock.now))}

        clock.now = t0 + timedelta(hours=3, minutes=3)  # back to the plain look-back
        _run(src._do_fetch_readings())
        last = api.measurement_params()[-1]
        assert (last["start"], last["end"]) == (_iso(clock.now - timedelta(hours=3)), _iso(clock.now))

    def test_catch_up_after_holding_back_is_bounded(self, install_api, monkeypatch, clock):
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(lmn_source_module, "_MAX_HOLD_BACK_CYCLES", 1)
        api = install_api(
            _FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS, detail_status={"NL10450": 503})
        )
        src = _make_source()
        t0 = clock.now
        assert _run(src._do_fetch_readings()) == []

        clock.now = t0 + timedelta(hours=30)  # a very long poll interval
        _run(src._do_fetch_readings())
        last = api.measurement_params()[-1]
        assert (last["start"], last["end"]) == (_iso(clock.now - timedelta(hours=24)), _iso(clock.now))

    def test_cycle_without_stations_is_caught_up_too(self, install_api, monkeypatch, clock):
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        api = install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS, list_status={1: 404}))
        src = _make_source()
        t0 = clock.now
        assert _run(src._do_fetch_readings()) == []  # no station known yet

        api.list_status = {}
        clock.now = t0 + timedelta(hours=1)
        _run(src._do_fetch_readings())
        last = api.measurement_params()[-1]
        assert (last["start"], last["end"]) == (_iso(t0 - timedelta(hours=3)), _iso(clock.now))

    def test_only_registered_components_are_emitted_oldest_first(self, install_api):
        install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS))
        readings = _run(_make_source()._do_fetch_readings())

        station = [r for r in readings if r.sensor_id == "luchtmeetnet-NL49565"]
        assert [(r.observed_property, r.timestamp.hour) for r in station] == [("no2", 9), ("no2", 10), ("pm10", 10)]
        assert [r.timestamp for r in readings] == sorted(r.timestamp for r in readings)

    def test_alias_duplicates_collapse_to_one_reading(self, install_api):
        install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS))
        readings = _run(_make_source()._do_fetch_readings())

        pm25 = [r for r in readings if r.sensor_id == "luchtmeetnet-NL10450" and r.observed_property == "pm2_5"]
        assert [r.value for r in pm25] == [8.99]

    def test_readings_for_known_components_are_pushable(self, install_api, monkeypatch, tmp_path):
        install_api(_FakeLuchtmeetnetAPI(_STATIONS, _DETAILS, _MEASUREMENTS))
        src = _make_source()
        readings = _run(src._do_fetch_readings())
        assert readings

        frost = _FakeFrost()
        client = _client_on(frost, monkeypatch, tmp_path)
        for entity_set in src.entity_sets():
            client.register_entity_set(entity_set)

        registered = {
            f"{sensor['sensor_id']}::{prop}"
            for es in src.entity_sets()
            for sensor in es["sensors"]
            for prop in sensor["observed_properties"]
        }
        assert {f"{r.sensor_id}::{r.observed_property}" for r in readings} <= registered

        result = client.push_observations(readings, source="luchtmeetnet")

        assert result["sent"] == len(readings)
        observations = frost.posted("Observations")
        assert len(observations) == len(readings)
        datastreams = frost.entities["Datastreams"]
        for obs in observations:
            ds = datastreams[obs["Datastream"]["@iot.id"]]
            assert ds["properties"]["sensor_id"] == obs["parameters"]["sensor_id"]
            assert ds["properties"]["observed_property"] == obs["parameters"]["observed_property"]


# ---------------------------------------------------------------------------
# scripts/repair_luchtmeetnet_locations.py
# ---------------------------------------------------------------------------


def _frost_thing(
    thing_id: int,
    number: str,
    name: str,
    *,
    location_id: int,
    coordinates: list[float] | None = None,
    with_station_number: bool = True,
    feature: tuple[int, list[float]] | None = None,
) -> dict[str, Any]:
    """A Thing as returned with the script's ``$expand``; *feature* adds an Observation's FoI."""
    datastreams: list[dict[str, Any]] = []
    if feature is not None:
        feature_id, feature_coordinates = feature
        foi = {
            "@iot.id": feature_id,
            "name": f"GEO_Luchtmeetnet {name} location",
            "encodingType": "application/geo+json",
            "feature": {"type": "Point", "coordinates": feature_coordinates},
        }
        datastreams = [{"@iot.id": 7, "Observations": [{"@iot.id": 70, "FeatureOfInterest": foi}]}]
    return {
        "@iot.id": thing_id,
        "name": f"GEO_Luchtmeetnet {name}",
        "properties": {"station_number": number, "source": "luchtmeetnet"} if with_station_number else {},
        "Locations": [
            {
                "@iot.id": location_id,
                "name": f"GEO_Luchtmeetnet {name} location",
                "encodingType": "application/geo+json",
                "location": {"type": "Point", "coordinates": coordinates or _PLACEHOLDER},
            }
        ],
        "Datastreams": datastreams,
    }


class _FakeFrostHTTP:
    """FrostHTTPClient stand-in for the repair script: paged Things, recorded PATCHes."""

    def __init__(self, things: list[dict[str, Any]], patch_status: int = 200) -> None:
        self.things = things
        self.patch_status = patch_status
        self.gets: list[dict[str, str]] = []
        self.patches: list[tuple[str, dict[str, Any]]] = []

    def endpoint(self, path: str) -> str:
        return f"{_FROST_BASE}{path}"

    def get(self, url: str, params: dict[str, str] | None = None) -> _FakeFrostResponse:
        params = dict(params or {})
        self.gets.append(params)
        top, skip = int(params["$top"]), int(params["$skip"])
        body: dict[str, Any] = {"value": self.things[skip : skip + top]}
        if skip + top < len(self.things):
            body["@iot.nextLink"] = f"{url}?$top={top}&$skip={skip + top}"
        return _FakeFrostResponse(200, body)

    def patch(self, url: str, payload: dict[str, Any]) -> _FakeFrostResponse:
        self.patches.append((url, payload))
        return _FakeFrostResponse(self.patch_status)


def _api_get_from(details: dict[str, dict[str, Any]]) -> Any:
    def _get(path: str, params: dict[str, str] | None) -> Any:
        if path == "/stations":
            return {"pagination": {"last_page": 1}, "data": _STATIONS}
        number = path.rsplit("/", 1)[-1]
        if number not in details:
            raise requests.HTTPError(f"404 for {path}")
        return {"data": details[number]}

    return _get


def _repair_things() -> list[dict[str, Any]]:
    return [
        _frost_thing(195, "NL49565", "Oude Meer-Aalsmeerderdijk", location_id=108),
        # No station_number in properties: matched through the station list by name.
        _frost_thing(197, "NL10450", "Den Haag-Neherkade", location_id=110, with_station_number=False),
        # Already at its real position: left alone.
        _frost_thing(199, "NL50002", "Geleen-Vouershof", location_id=111, coordinates=[5.81071, 50.96309]),
    ]


def _run_repair(http: _FakeFrostHTTP, details: dict[str, dict[str, Any]], *, apply: bool) -> int:
    return repair.run(
        http,
        _api_get_from(details),
        things_path="/Things",
        locations_path="/Locations",
        entity_prefix="GEO_",
        apply=apply,
    )


class TestRepairLuchtmeetnetLocations:
    def test_dry_run_prints_the_plan_and_writes_nothing(self, capsys):
        http = _FakeFrostHTTP(_repair_things())
        assert _run_repair(http, _DETAILS, apply=False) == 0

        assert http.patches == []
        assert http.gets[0]["$filter"] == "startswith(name,'GEO_Luchtmeetnet ')"
        out = capsys.readouterr().out
        assert re.search(r"NL49565 +195 +Locations\(108\) +POINT\(5 52\) +-> POINT\(4\.77082 52\.28013\)", out)
        assert re.search(r"NL10450 +197 +Locations\(110\) +POINT\(5 52\) +-> POINT\(4\.318551 52\.062537\)", out)
        assert "NL50002" not in out  # nothing to do for it
        assert "2 Locations and 0 FeaturesOfInterest" in out
        assert "Dry run" in out

    def test_apply_patches_only_placeholder_locations(self):
        http = _FakeFrostHTTP(_repair_things())
        assert _run_repair(http, _DETAILS, apply=True) == 0

        assert http.patches == [
            (
                f"{_FROST_BASE}/Locations(108)",
                {"encodingType": "application/geo+json", "location": {"type": "Point", "coordinates": [4.77082, 52.28013]}},
            ),
            (
                f"{_FROST_BASE}/Locations(110)",
                {"encodingType": "application/geo+json", "location": {"type": "Point", "coordinates": [4.318551, 52.062537]}},
            ),
        ]

    def test_failed_patch_gives_a_non_zero_exit(self):
        http = _FakeFrostHTTP(_repair_things(), patch_status=401)
        assert _run_repair(http, _DETAILS, apply=True) == 1

    def test_station_without_upstream_coordinates_is_skipped(self, capsys):
        details = {**_DETAILS, "NL49565": {"components": ["NO2"], "geometry": None}}
        http = _FakeFrostHTTP(_repair_things())
        _run_repair(http, details, apply=True)

        assert [url for url, _ in http.patches] == [f"{_FROST_BASE}/Locations(110)"]
        assert "skipped: NL49565" in capsys.readouterr().out

    def test_things_are_read_page_by_page(self, monkeypatch):
        monkeypatch.setattr(repair, "_PAGE_SIZE", 1)
        http = _FakeFrostHTTP(_repair_things())
        _run_repair(http, _DETAILS, apply=False)

        assert [p["$skip"] for p in http.gets] == ["0", "1", "2"]

    def test_generated_feature_of_interest_at_the_placeholder_is_patched_too(self):
        """FROST keeps reusing a FoI generated before the repair, even after its Location moved."""
        things = [
            # Location already repaired by an earlier run, FoI still at the placeholder.
            _frost_thing(
                195, "NL49565", "Oude Meer-Aalsmeerderdijk", location_id=108,
                coordinates=[4.77082, 52.28013], feature=(31, _PLACEHOLDER),
            ),
            # Observations exist, but their FoI was generated from a correct Location.
            _frost_thing(
                199, "NL50002", "Geleen-Vouershof", location_id=111,
                coordinates=[5.81071, 50.96309], feature=(32, [5.81071, 50.96309]),
            ),
        ]
        http = _FakeFrostHTTP(things)
        assert _run_repair(http, _DETAILS, apply=True) == 0

        assert http.patches == [
            (
                f"{_FROST_BASE}/FeaturesOfInterest(31)",
                {"encodingType": "application/geo+json", "feature": {"type": "Point", "coordinates": [4.77082, 52.28013]}},
            ),
        ]
        assert "Datastreams($top=1;$expand=Observations($top=1;$expand=FeatureOfInterest))" in http.gets[0]["$expand"]

    def test_target_selection_never_sends_credentials_to_an_unconfigured_url(self):
        targets = [FrostTarget(url="https://frost.example/FROST-Server/v1.1", auth_username="w", auth_password="p")]
        assert repair.select_target(targets, None) is targets[0]
        assert repair.select_target(targets, "https://frost.example/FROST-Server/v1.1/") is targets[0]
        with pytest.raises(SystemExit):
            repair.select_target(targets, "https://elsewhere.example/v1.1")
