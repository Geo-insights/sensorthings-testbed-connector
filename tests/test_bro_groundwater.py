"""Tests for the BRO groundwater level polling source (#16).

Covers:
- discover_wells() with mock response returns well IDs with coordinates
- discover_wells() respects max_wells cap (>500 wells → capped at configured limit)
- parse_observations() produces WATER_LEVEL SensorReadings with correct unit "m"
- build_entity_set() produces valid entity set
- BROGroundwaterPollingSource is a RestPollingSource subclass
- BROGroundwaterPollingSource has uses_dynamic_discovery = True
- Empty well search returns empty list
- Various location parsing shapes
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.services.bro_groundwater_source import BROGroundwaterPollingSource
from app.services.rest_polling_source import RestPollingSource
from app.sources.bro_groundwater import (
    build_entity_set,
    discover_wells,
    parse_observations,
)
from app.sta.canonical import CanonicalDatastream

_TS = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# discover_wells
# ---------------------------------------------------------------------------


class TestDiscoverWells:
    def _make_response(self, n: int) -> dict:
        """Build a synthetic /gmw/v1/objects/search response with n wells."""
        return {
            "broObjects": [
                {
                    "broId": f"GMW{i:015d}",
                    "deliveredLocation": {
                        "location": {"pos": f"{52.0 + i * 0.001} {4.0 + i * 0.001}"}
                    },
                }
                for i in range(n)
            ]
        }

    def test_returns_expected_well_ids(self):
        resp = self._make_response(3)
        wells = discover_wells(resp)
        assert len(wells) == 3
        ids = {w["well_id"] for w in wells}
        assert "GMW000000000000000" in ids
        assert "GMW000000000000001" in ids
        assert "GMW000000000000002" in ids

    def test_returns_lat_lon(self):
        resp = {
            "broObjects": [
                {
                    "broId": "GMW000000012345",
                    "deliveredLocation": {
                        "location": {"pos": "52.0 4.0"}
                    },
                }
            ]
        }
        wells = discover_wells(resp)
        assert len(wells) == 1
        assert wells[0]["lat"] == pytest.approx(52.0)
        assert wells[0]["lon"] == pytest.approx(4.0)

    def test_respects_max_wells_cap(self):
        """500 wells in response but cap=10 → returns exactly 10 with a warning."""
        resp = self._make_response(500)
        import logging
        with patch.object(logging.getLogger("app.sources.bro_groundwater"), "warning") as mock_warn:
            wells = discover_wells(resp, max_wells=10)
        assert len(wells) == 10
        # Warning must mention the overage
        assert mock_warn.called
        warning_msg = mock_warn.call_args_list[0][0][0]
        assert "500" in warning_msg or "%d" in warning_msg  # either interpolated or format string

    def test_max_wells_warns_and_truncates(self, caplog):
        """Confirm the log WARNING is emitted when wells exceed cap."""
        resp = self._make_response(50)
        import logging
        with caplog.at_level(logging.WARNING, logger="app.sources.bro_groundwater"):
            wells = discover_wells(resp, max_wells=5)
        assert len(wells) == 5
        assert any("50" in r.message or "capping" in r.message.lower() for r in caplog.records)

    def test_empty_response_returns_empty_list(self):
        wells = discover_wells({"broObjects": []})
        assert wells == []

    def test_missing_bro_objects_key_returns_empty(self):
        wells = discover_wells({})
        assert wells == []

    def test_non_list_bro_objects_returns_empty(self):
        wells = discover_wells({"broObjects": "not_a_list"})
        assert wells == []

    def test_object_without_bro_id_is_skipped(self):
        resp = {
            "broObjects": [
                {"deliveredLocation": {"location": {"pos": "52.0 4.0"}}},
            ]
        }
        wells = discover_wells(resp)
        assert wells == []

    def test_object_with_unparseable_location_is_skipped(self):
        resp = {
            "broObjects": [
                {"broId": "GMW000000012345"},  # no location at all
            ]
        }
        wells = discover_wells(resp)
        assert wells == []

    def test_location_via_flat_lat_lon_keys(self):
        """Flat lat/lon keys on the object (test-fixture friendly)."""
        resp = {
            "broObjects": [
                {"broId": "GMW000000099999", "lat": 51.9, "lon": 4.3},
            ]
        }
        wells = discover_wells(resp)
        assert len(wells) == 1
        assert wells[0]["lat"] == pytest.approx(51.9)
        assert wells[0]["lon"] == pytest.approx(4.3)

    def test_location_via_delivered_coordinates_geojson(self):
        """GeoJSON [lon, lat] shape under deliveredLocation.coordinates."""
        resp = {
            "broObjects": [
                {
                    "broId": "GMW000000077777",
                    "deliveredLocation": {"coordinates": [4.4, 52.1]},
                }
            ]
        }
        wells = discover_wells(resp)
        assert len(wells) == 1
        assert wells[0]["lat"] == pytest.approx(52.1)
        assert wells[0]["lon"] == pytest.approx(4.4)


# ---------------------------------------------------------------------------
# parse_observations
# ---------------------------------------------------------------------------


class TestParseObservations:
    def _make_gld_response(self, obs: list[dict]) -> dict:
        return {"observations": obs}

    def test_produces_water_level_readings(self):
        obs = [{"timestamp": "2026-10-07T12:00:00Z", "value": -1.5}]
        readings = parse_observations(self._make_gld_response(obs), "GMW000000012345")
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.WATER_LEVEL.value
        assert r.unit == "m"
        assert r.value == pytest.approx(-1.5)

    def test_sensor_id_uses_bro_prefix(self):
        obs = [{"timestamp": "2026-10-07T12:00:00Z", "value": 0.0}]
        readings = parse_observations(self._make_gld_response(obs), "GMW000000099999")
        assert readings[0].sensor_id == "bro-GMW000000099999"

    def test_thing_name_uses_bro_prefix(self):
        obs = [{"timestamp": "2026-10-07T12:00:00Z", "value": 0.5}]
        readings = parse_observations(self._make_gld_response(obs), "GMW000000012345")
        assert readings[0].thing_name == "BRO GMW000000012345"

    def test_unit_is_metres(self):
        obs = [{"timestamp": "2026-10-07T12:00:00Z", "value": -0.75}]
        readings = parse_observations(self._make_gld_response(obs), "GMW000000012345")
        assert readings[0].unit == "m"

    def test_observed_property_name_is_groundwater_level(self):
        obs = [{"timestamp": "2026-10-07T12:00:00Z", "value": -1.0}]
        readings = parse_observations(self._make_gld_response(obs), "GMW000000012345")
        assert readings[0].observed_property_name == "Groundwater level"

    def test_timestamp_is_parsed(self):
        obs = [{"timestamp": "2026-10-07T08:30:00Z", "value": -1.2}]
        readings = parse_observations(self._make_gld_response(obs), "GMW000000012345")
        ts = readings[0].timestamp
        assert ts.year == 2026
        assert ts.month == 10
        assert ts.day == 7
        assert ts.hour == 8
        assert ts.minute == 30

    def test_phenomenon_time_field_name_accepted(self):
        """phenomenonTime (STA-style) is also accepted."""
        obs = [{"phenomenonTime": "2026-10-07T09:00:00+00:00", "result": -0.5}]
        readings = parse_observations(self._make_gld_response(obs), "GMW000000012345")
        assert len(readings) == 1
        assert readings[0].value == pytest.approx(-0.5)

    def test_multiple_observations_all_parsed(self):
        obs = [
            {"timestamp": "2026-10-07T10:00:00Z", "value": -1.0},
            {"timestamp": "2026-10-07T11:00:00Z", "value": -1.1},
            {"timestamp": "2026-10-07T12:00:00Z", "value": -1.2},
        ]
        readings = parse_observations(self._make_gld_response(obs), "GMW000000012345")
        assert len(readings) == 3

    def test_empty_observations_returns_empty_list(self):
        readings = parse_observations({"observations": []}, "GMW000000012345")
        assert readings == []

    def test_missing_observations_key_returns_empty_list(self):
        readings = parse_observations({}, "GMW000000012345")
        assert readings == []

    def test_non_dict_observations_are_skipped(self):
        readings = parse_observations({"observations": ["bad", None, 42]}, "GMW000000012345")
        assert readings == []

    def test_observation_without_timestamp_is_skipped(self):
        obs = [{"value": -1.0}]  # no timestamp
        readings = parse_observations(self._make_gld_response(obs), "GMW000000012345")
        assert readings == []

    def test_observation_without_value_is_skipped(self):
        obs = [{"timestamp": "2026-10-07T12:00:00Z"}]  # no value
        readings = parse_observations(self._make_gld_response(obs), "GMW000000012345")
        assert readings == []

    def test_measured_values_key_also_accepted(self):
        """measuredValues is an alternative field name."""
        obs = [{"timestamp": "2026-10-07T12:00:00Z", "value": -2.0}]
        readings = parse_observations({"measuredValues": obs}, "GMW000000012345")
        assert len(readings) == 1

    def test_negative_water_level_preserved(self):
        """Below-datum levels are negative — must not be coerced to zero."""
        obs = [{"timestamp": "2026-10-07T12:00:00Z", "value": -3.45}]
        readings = parse_observations(self._make_gld_response(obs), "GMW000000099999")
        assert readings[0].value == pytest.approx(-3.45)


# ---------------------------------------------------------------------------
# build_entity_set
# ---------------------------------------------------------------------------


class TestBuildEntitySet:
    def test_returns_required_keys(self):
        es = build_entity_set("GMW000000012345", 52.0, 4.0)
        assert "site_key" in es
        assert "site_name" in es
        assert "thing" in es
        assert "location" in es
        assert "sensors" in es
        assert "observed_properties" in es

    def test_site_key_is_bro(self):
        es = build_entity_set("GMW000000012345", 52.0, 4.0)
        assert es["site_key"] == "bro"

    def test_thing_name_format(self):
        es = build_entity_set("GMW000000012345", 52.0, 4.0)
        assert es["thing"]["name"] == "BRO GMW000000012345"

    def test_source_property_is_bro_groundwater(self):
        es = build_entity_set("GMW000000012345", 52.0, 4.0)
        assert es["thing"]["properties"]["source"] == "bro_groundwater"

    def test_bro_well_id_in_properties(self):
        es = build_entity_set("GMW000000012345", 52.0, 4.0)
        assert es["thing"]["properties"]["bro_well_id"] == "GMW000000012345"

    def test_location_coordinates_geojson_lon_lat_order(self):
        """GeoJSON coordinates must be [lon, lat]."""
        es = build_entity_set("GMW000000012345", 52.1, 4.3)
        coords = es["location"]["location"]["coordinates"]
        assert coords == [4.3, 52.1]

    def test_sensor_id_uses_bro_prefix(self):
        es = build_entity_set("GMW000000012345", 52.0, 4.0)
        sensor = es["sensors"][0]
        assert sensor["sensor_id"] == "bro-GMW000000012345"

    def test_observed_properties_contains_water_level(self):
        es = build_entity_set("GMW000000012345", 52.0, 4.0)
        assert "water_level" in es["observed_properties"]

    def test_observed_property_is_canonical(self):
        from app.sta.canonical import resolve

        es = build_entity_set("GMW000000012345", 52.0, 4.0)
        for key in es["observed_properties"]:
            assert resolve(key) is not None, f"observed_property {key!r} not in canonical"

    def test_sensor_observed_properties_lists_water_level(self):
        es = build_entity_set("GMW000000012345", 52.0, 4.0)
        assert "water_level" in es["sensors"][0]["observed_properties"]

    def test_encoding_type_is_geojson(self):
        es = build_entity_set("GMW000000012345", 52.0, 4.0)
        assert es["location"]["encodingType"] == "application/geo+json"

    def test_geometry_type_is_point(self):
        es = build_entity_set("GMW000000012345", 52.0, 4.0)
        assert es["location"]["location"]["type"] == "Point"


# ---------------------------------------------------------------------------
# BROGroundwaterPollingSource — class-level properties
# ---------------------------------------------------------------------------


class TestBROGroundwaterPollingSource:
    def test_is_rest_polling_source_subclass(self):
        assert issubclass(BROGroundwaterPollingSource, RestPollingSource)

    def test_source_name(self):
        src = BROGroundwaterPollingSource()
        assert src.source_name == "bro_groundwater"

    def test_uses_dynamic_discovery_is_true(self):
        src = BROGroundwaterPollingSource()
        assert src.uses_dynamic_discovery is True

    def test_is_disabled_by_default(self):
        """BRO_ENABLED defaults to false in tests."""
        src = BROGroundwaterPollingSource()
        assert not src.is_enabled()

    def test_poll_interval_at_least_10(self):
        src = BROGroundwaterPollingSource()
        assert src.poll_interval() >= 10

    def test_poll_interval_default_is_3600(self):
        """Default BRO_POLL_SECONDS is 3600."""
        src = BROGroundwaterPollingSource()
        assert src.poll_interval() == 3600

    def test_entity_sets_empty_before_discovery(self):
        src = BROGroundwaterPollingSource()
        assert src.entity_sets() == []

    def test_entity_sets_populated_after_injecting_wells(self):
        src = BROGroundwaterPollingSource()
        src._discovered_wells["GMW000000001111"] = {"lat": 52.0, "lon": 4.0}
        src._discovered_wells["GMW000000002222"] = {"lat": 52.1, "lon": 4.1}
        sets = src.entity_sets()
        assert len(sets) == 2
        names = {s["thing"]["name"] for s in sets}
        assert "BRO GMW000000001111" in names
        assert "BRO GMW000000002222" in names

    def test_entity_set_site_key_is_bro(self):
        src = BROGroundwaterPollingSource()
        src._discovered_wells["GMW000000009999"] = {"lat": 51.9, "lon": 4.3}
        sets = src.entity_sets()
        assert sets[0]["site_key"] == "bro"

    def test_last_fetch_initially_none(self):
        src = BROGroundwaterPollingSource()
        assert src._last_fetch is None
