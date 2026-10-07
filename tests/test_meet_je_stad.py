"""Tests for the Meet je Stad source (#15).

Covers:
- MeetJeStadNormalizer canonical names and units
- parse_data_readings() with valid data
- parse_data_readings() maps "pm2.5" field correctly
- None fields produce empty readings
- build_entity_set() produces valid entity set
- MeetJeStadPollingSource is a RestPollingSource subclass
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.meet_je_stad_source import MeetJeStadPollingSource
from app.services.rest_polling_source import RestPollingSource
from app.sources.meet_je_stad import build_entity_set, parse_data_readings
from app.sources.normalizers import MeetJeStadNormalizer
from app.sta.canonical import CanonicalDatastream

_TS = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# MeetJeStadNormalizer — canonical names and units
# ---------------------------------------------------------------------------


class TestMeetJeStadNormalizer:
    def test_temperature_produces_canonical_reading(self):
        norm = MeetJeStadNormalizer(temperature=18.5)
        readings = norm.to_readings(
            sensor_id="mjs-123",
            sensor_name="MeetJeStad 123 sensor",
            thing_name="MeetJeStad Amersfoort 123",
            timestamp=_TS,
            location="amersfoort",
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == "temperature"
        assert r.unit == "°C"
        assert r.observed_property_name == "Air temperature"
        assert r.value == pytest.approx(18.5)

    def test_humidity_produces_canonical_reading(self):
        norm = MeetJeStadNormalizer(humidity=65.0)
        readings = norm.to_readings(
            sensor_id="mjs-123",
            sensor_name="MeetJeStad 123 sensor",
            thing_name="MeetJeStad Amersfoort 123",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == "humidity"
        assert r.unit == "%"
        assert r.observed_property_name == "Relative humidity"

    def test_soil_moisture_canonical(self):
        norm = MeetJeStadNormalizer(soil_moisture=0.35)
        readings = norm.to_readings(
            sensor_id="mjs-123",
            sensor_name="MeetJeStad 123 sensor",
            thing_name="MeetJeStad Amersfoort 123",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.SOIL_MOISTURE.value
        assert r.unit == "m3/m3"
        assert r.observed_property_name == "Soil moisture content"

    def test_soil_temperature_canonical(self):
        norm = MeetJeStadNormalizer(soil_temperature=14.2)
        readings = norm.to_readings(
            sensor_id="mjs-123",
            sensor_name="MeetJeStad 123 sensor",
            thing_name="MeetJeStad Amersfoort 123",
            timestamp=_TS,
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == CanonicalDatastream.SOIL_TEMPERATURE.value
        assert r.unit == "°C"
        assert r.observed_property_name == "Soil temperature"

    def test_pm2_5_canonical(self):
        norm = MeetJeStadNormalizer(pm2_5=12.3)
        readings = norm.to_readings(
            sensor_id="mjs-123",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        assert readings[0].observed_property == "pm2_5"
        assert readings[0].unit == "ug/m3"

    def test_pm10_canonical(self):
        norm = MeetJeStadNormalizer(pm10=25.0)
        readings = norm.to_readings(
            sensor_id="mjs-123",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        assert readings[0].observed_property == "pm10"
        assert readings[0].unit == "ug/m3"

    def test_all_fields_populated(self):
        norm = MeetJeStadNormalizer(
            temperature=18.5,
            humidity=65.0,
            pm2_5=12.3,
            pm10=25.0,
            soil_moisture=0.35,
            soil_temperature=14.2,
        )
        readings = norm.to_readings(
            sensor_id="mjs-123",
            sensor_name="MeetJeStad 123 sensor",
            thing_name="MeetJeStad Amersfoort 123",
            timestamp=_TS,
        )
        assert len(readings) == 6
        props = {r.observed_property for r in readings}
        assert "temperature" in props
        assert "humidity" in props
        assert "pm2_5" in props
        assert "pm10" in props
        assert "soil_moisture" in props
        assert "soil_temperature" in props

    def test_none_fields_produce_empty_readings(self):
        norm = MeetJeStadNormalizer(
            temperature=None,
            humidity=None,
            pm2_5=None,
            pm10=None,
            soil_moisture=None,
            soil_temperature=None,
        )
        readings = norm.to_readings(
            sensor_id="mjs-123",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert readings == []

    def test_extra_fields_ignored(self):
        norm = MeetJeStadNormalizer.model_validate({
            "temperature": 20.0,
            "battery_voltage": 3.7,   # governance: device telemetry excluded
            "rssi": -85,
        })
        readings = norm.to_readings(
            sensor_id="mjs-123",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        assert readings[0].observed_property == "temperature"

    def test_partial_none_produces_only_set_readings(self):
        norm = MeetJeStadNormalizer(temperature=21.0, soil_moisture=None)
        readings = norm.to_readings(
            sensor_id="mjs-42",
            sensor_name="s",
            thing_name="t",
            timestamp=_TS,
        )
        assert len(readings) == 1
        assert readings[0].observed_property == "temperature"


# ---------------------------------------------------------------------------
# parse_data_readings
# ---------------------------------------------------------------------------


class TestParseDataReadings:
    def test_valid_entry_produces_readings(self):
        entries = [
            {
                "id": "123",
                "timestamp": "2026-10-07T12:00:00",
                "temperature": 18.5,
                "humidity": 65.0,
                "soil_moisture": 0.35,
                "soil_temperature": 14.2,
            }
        ]
        readings = parse_data_readings(entries, "123", "Amersfoort 123")
        assert len(readings) == 4
        props = {r.observed_property for r in readings}
        assert "temperature" in props
        assert "humidity" in props
        assert "soil_moisture" in props
        assert "soil_temperature" in props

    def test_pm25_dot_key_mapped_correctly(self):
        """API field 'pm2.5' (dot notation) must map to canonical pm2_5."""
        entries = [
            {
                "id": "123",
                "timestamp": "2026-10-07T12:00:00",
                "pm2.5": 12.3,
            }
        ]
        readings = parse_data_readings(entries, "123", "Amersfoort 123")
        assert len(readings) == 1
        assert readings[0].observed_property == "pm2_5"
        assert readings[0].unit == "ug/m3"
        assert readings[0].value == pytest.approx(12.3)

    def test_empty_entries_produce_no_readings(self):
        readings = parse_data_readings([], "123", "Amersfoort 123")
        assert readings == []

    def test_non_dict_entries_are_skipped(self):
        entries = ["not_a_dict", None, 42]  # type: ignore[list-item]
        readings = parse_data_readings(entries, "123", "Amersfoort 123")
        assert readings == []

    def test_none_values_produce_no_readings(self):
        entries = [
            {
                "id": "123",
                "timestamp": "2026-10-07T12:00:00",
                "temperature": None,
                "humidity": None,
            }
        ]
        readings = parse_data_readings(entries, "123", "Amersfoort 123")
        assert readings == []

    def test_timestamp_iso_parsed(self):
        entries = [
            {
                "id": "123",
                "timestamp": "2026-10-07T12:30:00Z",
                "temperature": 20.0,
            }
        ]
        readings = parse_data_readings(entries, "123", "Utrecht 123")
        assert len(readings) == 1
        ts = readings[0].timestamp
        assert ts.year == 2026
        assert ts.month == 10
        assert ts.day == 7
        assert ts.hour == 12
        assert ts.minute == 30

    def test_sensor_id_prefixed_with_mjs(self):
        entries = [{"id": "42", "timestamp": "2026-10-07T12:00:00", "temperature": 15.0}]
        readings = parse_data_readings(entries, "42", "Leiden 42")
        assert len(readings) == 1
        assert readings[0].sensor_id == "mjs-42"

    def test_multiple_entries_all_parsed(self):
        entries = [
            {"id": "99", "timestamp": "2026-10-07T10:00:00", "temperature": 14.0},
            {"id": "99", "timestamp": "2026-10-07T11:00:00", "temperature": 15.0},
            {"id": "99", "timestamp": "2026-10-07T12:00:00", "temperature": 16.0},
        ]
        readings = parse_data_readings(entries, "99", "Delft 99")
        assert len(readings) == 3
        values = sorted(r.value for r in readings)
        assert values == [14.0, 15.0, 16.0]


# ---------------------------------------------------------------------------
# build_entity_set
# ---------------------------------------------------------------------------


class TestBuildEntitySet:
    def test_returns_required_keys(self):
        entity_set = build_entity_set("123", "Amersfoort", 52.15, 5.38)
        assert "site_key" in entity_set
        assert "site_name" in entity_set
        assert "thing" in entity_set
        assert "location" in entity_set
        assert "sensors" in entity_set
        assert "observed_properties" in entity_set

    def test_thing_name_format(self):
        entity_set = build_entity_set("123", "Amersfoort", 52.15, 5.38)
        assert entity_set["thing"]["name"] == "MeetJeStad Amersfoort 123"

    def test_site_key_is_lowercased_city(self):
        entity_set = build_entity_set("123", "Den Haag", 52.08, 4.31)
        assert entity_set["site_key"] == "den_haag"

    def test_location_coordinates(self):
        entity_set = build_entity_set("42", "Utrecht", 52.09, 5.12)
        coords = entity_set["location"]["location"]["coordinates"]
        assert coords == [5.12, 52.09]  # [lon, lat] per GeoJSON

    def test_sensor_id_prefixed_with_mjs(self):
        entity_set = build_entity_set("777", "Leiden", 52.16, 4.49)
        sensor = entity_set["sensors"][0]
        assert sensor["sensor_id"] == "mjs-777"

    def test_source_property_is_meet_je_stad(self):
        entity_set = build_entity_set("1", "Enschede", 52.22, 6.89)
        assert entity_set["thing"]["properties"]["source"] == "meet_je_stad"

    def test_observed_properties_are_canonical(self):
        from app.sta.canonical import resolve

        entity_set = build_entity_set("1", "Groningen", 53.22, 6.57)
        for key in entity_set["observed_properties"]:
            assert resolve(key) is not None, f"observed_property {key!r} not canonical"

    def test_custom_properties_subset(self):
        entity_set = build_entity_set(
            "1", "Rotterdam", 51.92, 4.48, properties=["temperature", "soil_moisture"]
        )
        obs_props = set(entity_set["observed_properties"].keys())
        assert obs_props == {"temperature", "soil_moisture"}


# ---------------------------------------------------------------------------
# MeetJeStadPollingSource
# ---------------------------------------------------------------------------


class TestMeetJeStadPollingSource:
    def test_is_rest_polling_source_subclass(self):
        assert issubclass(MeetJeStadPollingSource, RestPollingSource)

    def test_source_name(self):
        source = MeetJeStadPollingSource()
        assert source.source_name == "meet_je_stad"

    def test_uses_dynamic_discovery(self):
        source = MeetJeStadPollingSource()
        assert source.uses_dynamic_discovery is True

    def test_is_disabled_by_default(self):
        """MEET_JE_STAD_ENABLED defaults to false (env not set in tests)."""
        source = MeetJeStadPollingSource()
        assert not source.is_enabled()

    def test_poll_interval_at_least_10(self):
        """poll_interval() enforces minimum of 10 seconds."""
        source = MeetJeStadPollingSource()
        assert source.poll_interval() >= 10

    def test_poll_interval_default(self):
        """Default poll interval is 900s (15 min) per config default."""
        source = MeetJeStadPollingSource()
        assert source.poll_interval() == 900

    def test_entity_sets_empty_before_discovery(self):
        source = MeetJeStadPollingSource()
        assert source.entity_sets() == []

    def test_entity_sets_populated_after_discovery(self):
        source = MeetJeStadPollingSource()
        source._discovered_sensors["123"] = {"city": "Amersfoort", "lat": 52.15, "lon": 5.38}
        source._discovered_sensors["456"] = {"city": "Utrecht", "lat": 52.09, "lon": 5.12}
        sets = source.entity_sets()
        assert len(sets) == 2
        names = {s["thing"]["name"] for s in sets}
        assert "MeetJeStad Amersfoort 123" in names
        assert "MeetJeStad Utrecht 456" in names
