"""KNMI stations keep one sensor id, whatever else is in the EDR response.

The parser used to name each station by its Coverage's position in the /area
response ("knmi-0" ... "knmi-56"). Whenever a station appeared in or dropped out
of a response, the stations after it changed ids, so their readings went into
the Datastreams of the station that held their index before. Production
(frost.geo-insights.nl, 2026-11-20): 57 KNMI Things, most with Datastreams of
two different "knmi-<index>" ids, and 200,300 KNMI rows in another station's
Datastream. Stations are now identified by their WIGOS id.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any

import pytest
import requests

import app.services.knmi_weather_source as knmi_source_module
from app.services.knmi_weather_source import KNMIWeatherPollingSource
from app.sources.knmi_weather import (
    EDR_LOCATION_ID,
    KNMILocation,
    build_entity_set,
    coverage_station_id,
    coverages_without_station_id,
    match_location,
    parse_edr_locations,
    parse_locations,
    sensor_id_for,
    station_name_for,
)

_T = "2026-11-20T10:00:00Z"

# (WIGOS id, lon, lat) as the EDR API serves them. IJmond is the API's own
# /locations example; the others carry made-up WIGOS-style ids at the
# coordinates of production Things.
IJMOND = ("0-20000-0-06209", 4.5175, 52.4636)  # production Thing 2272 'GEO_KNMI KNMI-52.46-4.52'
STATION_A = ("0-20000-0-06310", 3.3417, 52.36)  # 'GEO_KNMI KNMI-52.36-3.34' (Thing 3)
STATION_B = ("0-20000-0-06210", 4.4364, 52.1397)  # 'GEO_KNMI KNMI-52.14-4.44' (Thing 11)
STATION_C = ("0-20000-0-06225", 4.555, 52.4622)  # 'GEO_KNMI KNMI-52.46-4.55' (Thing 14)


def _coverage(
    station: tuple[str, float, float] | None,
    *,
    lon: float | None = None,
    lat: float | None = None,
    ta: float = 10.0,
    rh: float = 80.0,
    location_id: Any = None,
) -> dict[str, Any]:
    """An /area Coverage with temperature and humidity at ``_T``, shaped like the EDR API's."""
    if station is not None:
        location_id = station[0] if location_id is None else location_id
        lon = station[1] if lon is None else lon
        lat = station[2] if lat is None else lat
    nd = {"type": "NdArray", "dataType": "float", "axisNames": ["t", "y", "x"], "shape": [1, 1, 1]}
    cov: dict[str, Any] = {
        "type": "Coverage",
        "domain": {
            "type": "Domain",
            "domainType": "PointSeries",
            "axes": {"x": {"values": [lon]}, "y": {"values": [lat]}, "t": {"values": [_T]}},
        },
        "ranges": {"ta": {**nd, "values": [ta]}, "rh": {**nd, "values": [rh]}},
    }
    if location_id is not None:
        cov[EDR_LOCATION_ID] = location_id
    return cov


def _collection(*coverages: dict[str, Any]) -> dict[str, Any]:
    return {"type": "CoverageCollection", "domainType": "PointSeries", "coverages": list(coverages)}


def _locations(*stations: tuple[str, float, float], names: dict[str, str] | None = None) -> dict[str, Any]:
    """A /locations FeatureCollection, shaped like the EDR API's."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"name": (names or {}).get(sid, f"Station {sid[-5:]}"), "wmoId": sid[-5:]},
                "id": sid,
            }
            for sid, lon, lat in stations
        ],
    }


def _ids_by_thing(readings: list[Any]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for reading in readings:
        out.setdefault(reading.thing_name, set()).add(reading.sensor_id)
    return out


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class TestStableStationIds:
    def test_sensor_id_is_the_wigos_id_and_names_stay_coordinate_based(self):
        stations, readings = parse_edr_locations(_collection(_coverage(IJMOND)))

        assert stations == [{"id": "0-20000-0-06209", "name": "KNMI-52.46-4.52", "lat": 52.4636, "lon": 4.5175}]
        assert {r.sensor_id for r in readings} == {"knmi-0-20000-0-06209"}
        # Production's Thing is 'GEO_KNMI KNMI-52.46-4.52': the readings still name it.
        assert {r.thing_name for r in readings} == {"KNMI KNMI-52.46-4.52"}
        assert {r.sensor_name for r in readings} == {"KNMI KNMI-52.46-4.52 sensor"}

    def test_ids_do_not_depend_on_position_in_the_response(self):
        _, first = parse_edr_locations(_collection(_coverage(STATION_A), _coverage(STATION_B), _coverage(STATION_C)))
        # Next cycle: A is missing (no data in the window) and the order changed.
        # Index-based ids made B "knmi-0" and C "knmi-1" here.
        _, second = parse_edr_locations(_collection(_coverage(STATION_C), _coverage(STATION_B)))

        assert _ids_by_thing(first) == {
            "KNMI KNMI-52.36-3.34": {"knmi-0-20000-0-06310"},
            "KNMI KNMI-52.14-4.44": {"knmi-0-20000-0-06210"},
            "KNMI KNMI-52.46-4.55": {"knmi-0-20000-0-06225"},
        }
        assert _ids_by_thing(second) == {
            "KNMI KNMI-52.14-4.44": {"knmi-0-20000-0-06210"},
            "KNMI KNMI-52.46-4.55": {"knmi-0-20000-0-06225"},
        }

    def test_two_stations_never_share_an_id(self):
        # 57 stations, as on production, a few km apart.
        stations_in = [(f"0-20000-0-06{k:03d}", 3.4 + 0.06 * k, 50.8 + 0.045 * k) for k in range(57)]
        stations, readings = parse_edr_locations(_collection(*(_coverage(s) for s in stations_in)))

        assert len({st["id"] for st in stations}) == 57
        by_thing = _ids_by_thing(readings)
        assert len(by_thing) == 57
        assert all(len(ids) == 1 for ids in by_thing.values())
        assert len({next(iter(ids)) for ids in by_thing.values()}) == 57

    def test_a_second_coverage_of_the_same_station_is_skipped(self, caplog):
        caplog.set_level(logging.WARNING)
        duplicate = _coverage(STATION_A, lon=5.0, lat=52.0, ta=99.0)

        stations, readings = parse_edr_locations(_collection(_coverage(STATION_A), duplicate))

        assert [st["id"] for st in stations] == ["0-20000-0-06310"]
        assert {r.thing_name for r in readings} == {"KNMI KNMI-52.36-3.34"}
        assert 99.0 not in {r.value for r in readings}
        assert "already in the response: 0-20000-0-06310" in caplog.text

    def test_integer_location_id_is_accepted(self):
        stations, readings = parse_edr_locations(_collection(_coverage(STATION_A, location_id=6310)))
        assert stations[0]["id"] == "6310"
        assert {r.sensor_id for r in readings} == {"knmi-6310"}

    @pytest.mark.parametrize("bad", ["", "   ", True, {"id": "x"}, ["0-20000-0-06310"]])
    def test_unusable_location_id_counts_as_missing(self, bad):
        cov = _coverage(STATION_A, location_id=bad)
        assert coverages_without_station_id(_collection(cov)) == 1
        stations, readings = parse_edr_locations(_collection(cov))
        assert stations == []
        assert readings == []


class TestCoverageWithoutLocationId:
    def test_identified_by_its_coordinates_in_locations(self):
        known = parse_locations(_locations(STATION_A, STATION_B))
        cov = _coverage(None, lon=STATION_B[1], lat=STATION_B[2])

        stations, readings = parse_edr_locations(_collection(cov), known_locations=known)

        assert [st["id"] for st in stations] == ["0-20000-0-06210"]
        assert {r.sensor_id for r in readings} == {"knmi-0-20000-0-06210"}

    def test_small_coordinate_differences_still_match(self):
        known = parse_locations(_locations(STATION_B))
        assert coverage_station_id({}, STATION_B[1] + 0.0004, STATION_B[2] - 0.0004, known) == "0-20000-0-06210"
        assert coverage_station_id({}, STATION_B[1] + 0.0006, STATION_B[2], known) is None

    def test_skipped_without_a_matching_station(self, caplog):
        caplog.set_level(logging.WARNING)
        cov = _coverage(None, lon=6.0, lat=53.0)

        stations, readings = parse_edr_locations(_collection(cov), known_locations=parse_locations(_locations(STATION_A)))

        assert stations == []
        assert readings == []
        assert "skipped 1 coverage(s) without a station id" in caplog.text

    def test_two_stations_at_the_coordinates_are_ambiguous(self):
        known = [KNMILocation("0-20000-0-00001", 5.0, 52.0), KNMILocation("0-20000-0-00002", 5.0001, 52.0)]
        assert match_location(5.0, 52.0, known) is None

    def test_the_coverage_member_wins_over_coordinates(self):
        known = parse_locations(_locations(("0-20000-0-09999", STATION_A[1], STATION_A[2])))
        assert coverage_station_id(_coverage(STATION_A), STATION_A[1], STATION_A[2], known) == "0-20000-0-06310"

    def test_station_names_from_locations_never_rename_things(self):
        known = parse_locations(_locations(STATION_A, names={"0-20000-0-06310": "Vlissingen"}))
        assert known[0].name == "Vlissingen"
        for cov in (_coverage(STATION_A), _coverage(None, lon=STATION_A[1], lat=STATION_A[2])):
            stations, readings = parse_edr_locations(_collection(cov), known_locations=known)
            assert stations[0]["name"] == "KNMI-52.36-3.34"
            assert {r.thing_name for r in readings} == {"KNMI KNMI-52.36-3.34"}


class TestParseLocations:
    def test_reads_id_point_and_name(self):
        body = _locations(IJMOND, names={"0-20000-0-06209": "IJmond"})
        assert parse_locations(body) == [KNMILocation("0-20000-0-06209", 4.5175, 52.4636, "IJmond")]

    def test_skips_features_without_id_or_point(self):
        body = _locations(STATION_A)
        body["features"] += [
            {"type": "Feature", "geometry": {"type": "Point", "coordinates": [5.0, 52.0]}, "properties": {}},
            {"type": "Feature", "id": "x", "geometry": None},
            {"type": "Feature", "id": "y", "geometry": {"type": "Point", "coordinates": ["a", "b"]}},
            {"type": "Feature", "id": "z", "geometry": {"type": "Point", "coordinates": [float("nan"), 52.0]}},
            "not a feature",
        ]
        assert [loc.station_id for loc in parse_locations(body)] == ["0-20000-0-06310"]

    @pytest.mark.parametrize("body", [None, [], {"features": "x"}, {}])
    def test_unusable_bodies_give_no_stations(self, body):
        assert parse_locations(body) == []


class TestEntitySet:
    def test_wigos_id_in_sensor_id_and_thing_properties(self):
        entity_set = build_entity_set("0-20000-0-06310", station_name_for(52.36, 3.3417), 52.36, 3.3417)

        assert entity_set["thing"]["name"] == "KNMI KNMI-52.36-3.34"
        assert entity_set["location"]["name"] == "KNMI KNMI-52.36-3.34 location"
        assert entity_set["sensors"][0]["name"] == "KNMI KNMI-52.36-3.34 sensor"
        assert entity_set["sensors"][0]["sensor_id"] == "knmi-0-20000-0-06310"
        assert entity_set["thing"]["properties"]["knmi_station_id"] == "0-20000-0-06310"

    def test_registration_keys_of_two_stations_never_overlap(self):
        from app.main import _entity_set_datastream_keys

        a = build_entity_set(STATION_A[0], station_name_for(STATION_A[2], STATION_A[1]), STATION_A[2], STATION_A[1])
        b = build_entity_set(STATION_B[0], station_name_for(STATION_B[2], STATION_B[1]), STATION_B[2], STATION_B[1])
        keys_a, keys_b = _entity_set_datastream_keys(a), _entity_set_datastream_keys(b)
        assert keys_a
        assert keys_b
        assert not keys_a & keys_b
        assert "knmi-0-20000-0-06310::temperature" in keys_a

    def test_sensor_id_format(self):
        assert sensor_id_for("0-20000-0-06260") == "knmi-0-20000-0-06260"
        assert station_name_for(52.4636, 4.5175) == "KNMI-52.46-4.52"


# ---------------------------------------------------------------------------
# Polling source
# ---------------------------------------------------------------------------


class _EdrResponse:
    def __init__(self, body: Any) -> None:
        self._body = body
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._body


@pytest.fixture
def edr(monkeypatch):
    """Serve scripted /area bodies to the source and count /locations requests."""
    state: dict[str, Any] = {"area": [], "locations": None, "location_calls": 0, "urls": []}

    def fake_send(self: requests.Session, prepared: Any, **kwargs: Any) -> _EdrResponse:
        state["urls"].append(prepared.url)
        return _EdrResponse(state["area"].pop(0))

    monkeypatch.setattr(requests.Session, "send", fake_send)
    monkeypatch.setattr(
        knmi_source_module,
        "settings",
        dataclasses.replace(
            knmi_source_module.settings,
            knmi_weather_enabled=True,
            knmi_api_key="test-key",
            knmi_weather_api_url="https://edr.test/edr/v1/collections/obs/locations",
        ),
    )

    def make_source() -> KNMIWeatherPollingSource:
        source = KNMIWeatherPollingSource()

        async def fake_get(url: str, params: Any = None) -> Any:
            state["location_calls"] += 1
            assert url == "https://edr.test/edr/v1/collections/obs/locations"
            if isinstance(state["locations"], Exception):
                raise state["locations"]
            return state["locations"]

        source._client.get = fake_get  # type: ignore[method-assign]
        return source

    state["make_source"] = make_source
    return state


def _poll(source: KNMIWeatherPollingSource) -> list[Any]:
    return asyncio.run(source.fetch_readings())


class TestPollingSource:
    def test_station_ids_survive_order_changes_and_gaps(self, edr):
        edr["area"] = [
            _collection(_coverage(STATION_A), _coverage(STATION_B), _coverage(STATION_C)),
            _collection(_coverage(STATION_C), _coverage(STATION_B)),
            _collection(_coverage(IJMOND), _coverage(STATION_B), _coverage(STATION_A), _coverage(STATION_C)),
        ]
        source = edr["make_source"]()

        cycles = [_poll(source) for _ in range(3)]

        expected = {
            "KNMI KNMI-52.36-3.34": {"knmi-0-20000-0-06310"},
            "KNMI KNMI-52.14-4.44": {"knmi-0-20000-0-06210"},
            "KNMI KNMI-52.46-4.55": {"knmi-0-20000-0-06225"},
            "KNMI KNMI-52.46-4.52": {"knmi-0-20000-0-06209"},
        }
        assert _ids_by_thing([r for cycle in cycles for r in cycle]) == expected
        # Each entity set's sensor id belongs to that set's Thing, also after the gaps.
        sets = {es["thing"]["name"]: es["sensors"][0]["sensor_id"] for es in source.entity_sets()}
        assert sets == {name: next(iter(ids)) for name, ids in expected.items()}
        assert edr["location_calls"] == 0  # every Coverage named its station
        assert all("/area?coords=POLYGON" in url for url in edr["urls"])

    def test_locations_loaded_once_when_a_coverage_lacks_its_id(self, edr):
        edr["locations"] = _locations(STATION_A, STATION_B)
        no_id = _coverage(None, lon=STATION_B[1], lat=STATION_B[2])
        edr["area"] = [_collection(_coverage(STATION_A), no_id), _collection(no_id, _coverage(STATION_A))]
        source = edr["make_source"]()

        first, second = _poll(source), _poll(source)

        expected = {"KNMI KNMI-52.36-3.34": {"knmi-0-20000-0-06310"}, "KNMI KNMI-52.14-4.44": {"knmi-0-20000-0-06210"}}
        assert _ids_by_thing(first) == expected
        assert _ids_by_thing(second) == expected
        assert edr["location_calls"] == 1

    def test_unreachable_locations_skip_only_the_unidentified_coverage(self, edr, caplog):
        caplog.set_level(logging.WARNING)
        edr["locations"] = RuntimeError("KNMI down")
        no_id = _coverage(None, lon=STATION_B[1], lat=STATION_B[2])
        edr["area"] = [_collection(_coverage(STATION_A), no_id)]
        source = edr["make_source"]()

        readings = _poll(source)

        assert _ids_by_thing(readings) == {"KNMI KNMI-52.36-3.34": {"knmi-0-20000-0-06310"}}
        assert [es["sensors"][0]["sensor_id"] for es in source.entity_sets()] == ["knmi-0-20000-0-06310"]
        assert "could not load /locations" in caplog.text
