"""Unit tests for the Luchtmeetnet RIVM air quality source.

Covers:
- parse_measurements() with valid data produces correct canonical names / units
- parse_measurements() with an unknown formula is skipped
- build_entity_set() produces a valid entity-set dict
- LuchtmeetnetPollingSource is a RestPollingSource subclass with
  uses_dynamic_discovery = True
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from app.services.luchtmeetnet_source import LuchtmeetnetPollingSource
from app.services.rest_polling_source import RestPollingSource
from app.sources.luchtmeetnet import build_entity_set, parse_measurements
from app.sta.canonical import CanonicalDatastream

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
