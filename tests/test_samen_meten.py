"""Tests for the Samen Meten RIVM STA federation source (#13).

Covers:
- parse_things_response() extracts Things with locations and datastreams
- parse_things_response() maps ObservedProperty names to canonical (PM2.5, temperature, humidity)
- parse_observations() produces correct SensorReadings with canonical units
- build_entity_set() produces valid entity set
- @iot.nextLink pagination in Things response is detected
- SamenMetenPollingSource is a RestPollingSource subclass with uses_dynamic_discovery = True
- Unknown ObservedProperty names are skipped
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.rest_polling_source import RestPollingSource
from app.services.samen_meten_source import SamenMetenPollingSource
from app.sources.samen_meten import (
    build_entity_set,
    parse_observations,
    parse_things_response,
)
from app.sta.canonical import resolve

_TS = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_things_response(things: list[dict], next_link: str | None = None) -> dict:
    """Build a minimal STA Things paged response."""
    out: dict = {"value": things}
    if next_link:
        out["@iot.nextLink"] = next_link
    return out


def _make_thing(
    thing_id: int,
    name: str,
    lat: float,
    lon: float,
    datastreams: list[dict] | None = None,
) -> dict:
    """Build a minimal STA Thing dict (as returned by expanded endpoint)."""
    return {
        "@iot.id": thing_id,
        "name": name,
        "Locations": [
            {
                "location": {
                    "type": "Point",
                    "coordinates": [lon, lat],
                }
            }
        ],
        "Datastreams": datastreams or [],
    }


def _make_datastream(ds_id: int, op_name: str) -> dict:
    return {
        "@iot.id": ds_id,
        "ObservedProperty": {"name": op_name},
    }


# ---------------------------------------------------------------------------
# parse_things_response — extraction
# ---------------------------------------------------------------------------


class TestParseThingsResponse:
    def test_extracts_thing_id_and_name(self):
        thing = _make_thing(101, "RIVM-Station-A", 52.1, 4.3)
        result = parse_things_response(_make_things_response([thing]))
        assert len(result) == 1
        assert result[0]["thing_id"] == 101
        assert result[0]["name"] == "RIVM-Station-A"

    def test_extracts_lat_lon(self):
        thing = _make_thing(101, "S", 52.1, 4.3)
        result = parse_things_response(_make_things_response([thing]))
        assert result[0]["lat"] == pytest.approx(52.1)
        assert result[0]["lon"] == pytest.approx(4.3)

    def test_extracts_datastreams(self):
        ds = _make_datastream(201, "pm2_5")
        thing = _make_thing(101, "S", 52.1, 4.3, datastreams=[ds])
        result = parse_things_response(_make_things_response([thing]))
        assert len(result[0]["datastreams"]) == 1
        assert result[0]["datastreams"][0]["ds_id"] == 201
        assert result[0]["datastreams"][0]["observed_property"] == "pm2_5"

    def test_multiple_things_all_extracted(self):
        things = [
            _make_thing(1, "A", 52.0, 4.0),
            _make_thing(2, "B", 52.1, 4.1),
            _make_thing(3, "C", 52.2, 4.2),
        ]
        result = parse_things_response(_make_things_response(things))
        assert len(result) == 3

    def test_thing_without_location_skipped(self):
        thing = {
            "@iot.id": 99,
            "name": "No-location",
            "Locations": [],
            "Datastreams": [],
        }
        result = parse_things_response(_make_things_response([thing]))
        assert result == []

    def test_empty_value_list_returns_empty(self):
        result = parse_things_response({"value": []})
        assert result == []

    def test_non_list_value_returns_empty(self):
        result = parse_things_response({"value": "not-a-list"})
        assert result == []

    def test_missing_thing_id_skipped(self):
        thing = {
            "name": "no-id",
            "Locations": [{"location": {"type": "Point", "coordinates": [4.0, 52.0]}}],
            "Datastreams": [],
        }
        result = parse_things_response(_make_things_response([thing]))
        assert result == []


# ---------------------------------------------------------------------------
# parse_things_response — canonical mapping
# ---------------------------------------------------------------------------


class TestParseThingsResponseCanonicalMapping:
    def test_pm25_full_name_maps_to_canonical(self):
        """'Particulate Matter < 2.5 µm' → pm2_5"""
        ds = _make_datastream(201, "Particulate Matter < 2.5 µm")
        thing = _make_thing(101, "S", 52.1, 4.3, datastreams=[ds])
        result = parse_things_response(_make_things_response([thing]))
        assert len(result) == 1
        ds_out = result[0]["datastreams"]
        assert len(ds_out) == 1
        assert ds_out[0]["observed_property"] == "pm2_5"

    def test_pm10_full_name_maps_to_canonical(self):
        """'Particulate Matter < 10 µm' → pm10"""
        ds = _make_datastream(202, "Particulate Matter < 10 µm")
        thing = _make_thing(101, "S", 52.1, 4.3, datastreams=[ds])
        result = parse_things_response(_make_things_response([thing]))
        ds_out = result[0]["datastreams"]
        assert len(ds_out) == 1
        assert ds_out[0]["observed_property"] == "pm10"

    def test_temperature_name_maps_to_canonical(self):
        ds = _make_datastream(203, "Temperature")
        thing = _make_thing(101, "S", 52.1, 4.3, datastreams=[ds])
        result = parse_things_response(_make_things_response([thing]))
        ds_out = result[0]["datastreams"]
        assert len(ds_out) == 1
        assert ds_out[0]["observed_property"] == "temperature"

    def test_humidity_name_maps_to_canonical(self):
        ds = _make_datastream(204, "Humidity")
        thing = _make_thing(101, "S", 52.1, 4.3, datastreams=[ds])
        result = parse_things_response(_make_things_response([thing]))
        ds_out = result[0]["datastreams"]
        assert len(ds_out) == 1
        assert ds_out[0]["observed_property"] == "humidity"

    def test_unknown_op_name_skipped(self):
        ds_bad = _make_datastream(205, "Unknown Sensor Property XYZ")
        ds_good = _make_datastream(206, "pm2_5")
        thing = _make_thing(101, "S", 52.1, 4.3, datastreams=[ds_bad, ds_good])
        result = parse_things_response(_make_things_response([thing]))
        ds_out = result[0]["datastreams"]
        assert len(ds_out) == 1
        assert ds_out[0]["ds_id"] == 206

    def test_all_unknown_ops_gives_empty_datastreams_list(self):
        ds = _make_datastream(207, "Quantum Entanglement Index")
        thing = _make_thing(101, "S", 52.1, 4.3, datastreams=[ds])
        result = parse_things_response(_make_things_response([thing]))
        assert result[0]["datastreams"] == []

    def test_pm2_5_key_direct_maps_to_canonical(self):
        """Raw key 'pm2_5' passes through resolve() correctly."""
        ds = _make_datastream(208, "pm2_5")
        thing = _make_thing(101, "S", 52.1, 4.3, datastreams=[ds])
        result = parse_things_response(_make_things_response([thing]))
        assert result[0]["datastreams"][0]["observed_property"] == "pm2_5"

    def test_pm10_key_direct_maps_to_canonical(self):
        ds = _make_datastream(209, "pm10")
        thing = _make_thing(101, "S", 52.1, 4.3, datastreams=[ds])
        result = parse_things_response(_make_things_response([thing]))
        assert result[0]["datastreams"][0]["observed_property"] == "pm10"


# ---------------------------------------------------------------------------
# parse_things_response — @iot.nextLink pagination
# ---------------------------------------------------------------------------


class TestParseThingsNextLink:
    def test_next_link_present_in_response_is_detectable(self):
        """Caller detects @iot.nextLink to trigger pagination."""
        thing = _make_thing(1, "A", 52.0, 4.0)
        data = _make_things_response([thing], next_link="https://api-samenmeten.rivm.nl/v1.0/Things?$skip=100")
        assert "@iot.nextLink" in data
        assert data["@iot.nextLink"].endswith("$skip=100")

    def test_no_next_link_when_last_page(self):
        thing = _make_thing(1, "A", 52.0, 4.0)
        data = _make_things_response([thing], next_link=None)
        assert "@iot.nextLink" not in data

    def test_parse_things_response_does_not_follow_next_link(self):
        """parse_things_response only processes 'value'; following links is the source's job."""
        thing = _make_thing(1, "A", 52.0, 4.0)
        data = _make_things_response(
            [thing],
            next_link="https://api-samenmeten.rivm.nl/v1.0/Things?$skip=100",
        )
        result = parse_things_response(data)
        # Only the one thing from the current page
        assert len(result) == 1


# ---------------------------------------------------------------------------
# parse_observations
# ---------------------------------------------------------------------------


class TestParseObservations:
    def _make_obs(self, value: float, ts: str = "2026-10-07T12:00:00Z") -> dict:
        return {"phenomenonTime": ts, "result": value}

    def test_basic_reading_fields(self):
        obs = [self._make_obs(15.3)]
        readings = parse_observations(obs, "sm-101", "SamenMeten S sensor", "pm2_5", "SamenMeten S")
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == "pm2_5"
        assert r.unit == "ug/m3"
        assert r.value == pytest.approx(15.3)
        assert r.sensor_id == "sm-101"
        assert r.sensor_name == "SamenMeten S sensor"
        assert r.thing_name == "SamenMeten S"

    def test_pm2_5_unit_is_ug_m3(self):
        readings = parse_observations(
            [self._make_obs(12.0)], "sm-1", "s", "pm2_5", "t"
        )
        assert readings[0].unit == "ug/m3"
        assert readings[0].observed_property_name == "PM2.5 concentration"

    def test_pm10_unit_is_ug_m3(self):
        readings = parse_observations(
            [self._make_obs(20.0)], "sm-1", "s", "pm10", "t"
        )
        assert readings[0].unit == "ug/m3"
        assert readings[0].observed_property_name == "PM10 concentration"

    def test_temperature_unit_is_celsius(self):
        readings = parse_observations(
            [self._make_obs(18.5)], "sm-1", "s", "temperature", "t"
        )
        assert readings[0].unit == "°C"

    def test_humidity_unit_is_percent(self):
        readings = parse_observations(
            [self._make_obs(65.0)], "sm-1", "s", "humidity", "t"
        )
        assert readings[0].unit == "%"

    def test_timestamp_parsed_correctly(self):
        obs = [{"phenomenonTime": "2026-10-07T14:30:00Z", "result": 10.0}]
        readings = parse_observations(obs, "sm-1", "s", "pm2_5", "t")
        ts = readings[0].timestamp
        assert ts.year == 2026
        assert ts.month == 10
        assert ts.day == 7
        assert ts.hour == 14
        assert ts.minute == 30

    def test_multiple_observations_all_parsed(self):
        observations = [
            {"phenomenonTime": "2026-10-07T10:00:00Z", "result": 5.0},
            {"phenomenonTime": "2026-10-07T11:00:00Z", "result": 8.0},
            {"phenomenonTime": "2026-10-07T12:00:00Z", "result": 11.0},
        ]
        readings = parse_observations(observations, "sm-1", "s", "pm2_5", "t")
        assert len(readings) == 3
        values = sorted(r.value for r in readings)
        assert values == pytest.approx([5.0, 8.0, 11.0])

    def test_none_result_skipped(self):
        observations = [
            {"phenomenonTime": "2026-10-07T12:00:00Z", "result": None},
            {"phenomenonTime": "2026-10-07T12:05:00Z", "result": 7.0},
        ]
        readings = parse_observations(observations, "sm-1", "s", "pm2_5", "t")
        assert len(readings) == 1
        assert readings[0].value == pytest.approx(7.0)

    def test_unknown_observed_property_returns_empty(self):
        observations = [{"phenomenonTime": "2026-10-07T12:00:00Z", "result": 5.0}]
        readings = parse_observations(observations, "sm-1", "s", "not_a_real_property_xyz", "t")
        assert readings == []

    def test_empty_observations_returns_empty(self):
        readings = parse_observations([], "sm-1", "s", "pm2_5", "t")
        assert readings == []

    def test_non_dict_observations_skipped(self):
        observations = ["not-a-dict", None, 42]  # type: ignore[list-item]
        readings = parse_observations(observations, "sm-1", "s", "pm2_5", "t")
        assert readings == []

    def test_quality_defaults_to_good(self):
        observations = [{"phenomenonTime": "2026-10-07T12:00:00Z", "result": 9.0}]
        readings = parse_observations(observations, "sm-1", "s", "pm2_5", "t")
        assert readings[0].quality == "good"

    def test_location_is_samen_meten(self):
        observations = [{"phenomenonTime": "2026-10-07T12:00:00Z", "result": 9.0}]
        readings = parse_observations(observations, "sm-1", "s", "pm2_5", "t")
        assert readings[0].location == "samen_meten"


# ---------------------------------------------------------------------------
# build_entity_set
# ---------------------------------------------------------------------------


class TestBuildEntitySet:
    def test_returns_required_keys(self):
        es = build_entity_set("RIVM-101", 52.1, 4.3, ["pm2_5", "pm10"])
        for key in ("site_key", "site_name", "thing", "location", "sensors", "observed_properties"):
            assert key in es, f"missing key: {key}"

    def test_site_key_is_samen_meten(self):
        es = build_entity_set("RIVM-101", 52.1, 4.3, ["pm2_5"])
        assert es["site_key"] == "samen_meten"

    def test_thing_name_prefixed_with_samenmeten(self):
        es = build_entity_set("RIVM-101", 52.1, 4.3, ["pm2_5"])
        assert es["thing"]["name"] == "SamenMeten RIVM-101"

    def test_location_coordinates_are_lon_lat(self):
        es = build_entity_set("S", 52.1, 4.3, ["pm2_5"])
        coords = es["location"]["location"]["coordinates"]
        assert coords == [4.3, 52.1]  # [lon, lat] per GeoJSON

    def test_source_property_is_samen_meten(self):
        es = build_entity_set("RIVM-101", 52.1, 4.3, ["pm2_5"])
        assert es["thing"]["properties"]["source"] == "samen_meten"

    def test_observed_properties_subset(self):
        es = build_entity_set("S", 52.1, 4.3, ["pm2_5", "temperature"])
        obs_props = set(es["observed_properties"].keys())
        assert obs_props == {"pm2_5", "temperature"}

    def test_unknown_observed_properties_excluded(self):
        es = build_entity_set("S", 52.1, 4.3, ["pm2_5", "not_real_xyz"])
        obs_props = set(es["observed_properties"].keys())
        assert "not_real_xyz" not in obs_props
        assert "pm2_5" in obs_props

    def test_observed_properties_are_canonical(self):
        es = build_entity_set("S", 52.1, 4.3, ["pm2_5", "pm10", "temperature", "humidity"])
        for key in es["observed_properties"]:
            assert resolve(key) is not None, f"observed_property {key!r} not canonical"

    def test_sensors_list_has_one_entry(self):
        es = build_entity_set("RIVM-101", 52.1, 4.3, ["pm2_5"])
        assert len(es["sensors"]) == 1

    def test_empty_observed_properties_list(self):
        es = build_entity_set("RIVM-101", 52.1, 4.3, [])
        assert es["observed_properties"] == {}


# ---------------------------------------------------------------------------
# SamenMetenPollingSource
# ---------------------------------------------------------------------------


class TestSamenMetenPollingSource:
    def test_is_rest_polling_source_subclass(self):
        assert issubclass(SamenMetenPollingSource, RestPollingSource)

    def test_uses_dynamic_discovery_is_true(self):
        source = SamenMetenPollingSource()
        assert source.uses_dynamic_discovery is True

    def test_source_name(self):
        source = SamenMetenPollingSource()
        assert source.source_name == "samen_meten"

    def test_is_disabled_by_default(self):
        """SAMEN_METEN_ENABLED defaults to false (env not set in tests)."""
        source = SamenMetenPollingSource()
        assert not source.is_enabled()

    def test_poll_interval_at_least_10(self):
        source = SamenMetenPollingSource()
        assert source.poll_interval() >= 10

    def test_poll_interval_default(self):
        """Default poll interval is 600s per config default."""
        source = SamenMetenPollingSource()
        assert source.poll_interval() == 600

    def test_entity_sets_empty_before_discovery(self):
        source = SamenMetenPollingSource()
        assert source.entity_sets() == []

    def test_entity_sets_populated_after_discovery(self):
        source = SamenMetenPollingSource()
        source._discovered_things[101] = {
            "name": "RIVM-Station-A",
            "lat": 52.1,
            "lon": 4.3,
            "datastreams": [{"ds_id": 201, "observed_property": "pm2_5"}],
        }
        source._discovered_things[102] = {
            "name": "RIVM-Station-B",
            "lat": 52.2,
            "lon": 4.4,
            "datastreams": [{"ds_id": 202, "observed_property": "pm10"}],
        }
        sets = source.entity_sets()
        assert len(sets) == 2
        names = {s["thing"]["name"] for s in sets}
        assert "SamenMeten RIVM-Station-A" in names
        assert "SamenMeten RIVM-Station-B" in names

    def test_entity_sets_include_observed_properties(self):
        source = SamenMetenPollingSource()
        source._discovered_things[101] = {
            "name": "S",
            "lat": 52.1,
            "lon": 4.3,
            "datastreams": [
                {"ds_id": 201, "observed_property": "pm2_5"},
                {"ds_id": 202, "observed_property": "pm10"},
            ],
        }
        sets = source.entity_sets()
        assert len(sets) == 1
        props = set(sets[0]["observed_properties"].keys())
        assert "pm2_5" in props
        assert "pm10" in props
