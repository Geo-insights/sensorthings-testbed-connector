"""Unit tests for the Sensor.Community source (#12).

Covers:
- parse_sensor_data() with PM sensor (SDS011) produces PM10 and PM2.5 readings
- parse_sensor_data() with noise sensor (DNMS) produces NOISE_LAEQ/LAMIN/LAMAX
- parse_sensor_data() with temperature/humidity sensor (BME280) produces correct readings
- build_entity_set() produces a valid entity set
- Unknown value_types are silently skipped
- SensorCommunityPollingSource is a RestPollingSource subclass
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.rest_polling_source import RestPollingSource
from app.sources.sensor_community import build_entity_set, parse_sensor_data

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _sds011_entry(location_id: int = 456) -> dict[str, Any]:
    """SDS011 particulate matter sensor entry."""
    return {
        "id": 123456,
        "sensor": {"id": 789, "sensor_type": {"name": "SDS011"}},
        "location": {"id": location_id, "latitude": "52.01", "longitude": "4.36"},
        "sensordatavalues": [
            {"value_type": "P1", "value": "15.3"},
            {"value_type": "P2", "value": "8.1"},
        ],
        "timestamp": "2026-10-07 12:00:00",
    }


def _dnms_entry(location_id: int = 456) -> dict[str, Any]:
    """DNMS noise sensor entry at the same location."""
    return {
        "id": 123457,
        "sensor": {"id": 790, "sensor_type": {"name": "DNMS"}},
        "location": {"id": location_id, "latitude": "52.01", "longitude": "4.36"},
        "sensordatavalues": [
            {"value_type": "noise_LAeq", "value": "52.4"},
            {"value_type": "noise_LAmin", "value": "44.1"},
            {"value_type": "noise_LAmax", "value": "65.8"},
        ],
        "timestamp": "2026-10-07 12:00:00",
    }


def _bme280_entry(location_id: int = 456) -> dict[str, Any]:
    """BME280 temperature/humidity/pressure sensor entry."""
    return {
        "id": 123458,
        "sensor": {"id": 791, "sensor_type": {"name": "BME280"}},
        "location": {"id": location_id, "latitude": "52.01", "longitude": "4.36"},
        "sensordatavalues": [
            {"value_type": "temperature", "value": "18.5"},
            {"value_type": "humidity", "value": "72.3"},
            {"value_type": "pressure", "value": "1013.25"},
        ],
        "timestamp": "2026-10-07 12:00:00",
    }


# ---------------------------------------------------------------------------
# parse_sensor_data — PM sensor (SDS011)
# ---------------------------------------------------------------------------


def test_parse_pm_sensor_produces_pm10_and_pm25() -> None:
    readings = parse_sensor_data([_sds011_entry()])
    assert len(readings) == 2
    by_prop = {r.observed_property: r for r in readings}
    assert "pm10" in by_prop
    assert "pm2_5" in by_prop


def test_parse_pm_sensor_pm10_value_and_unit() -> None:
    readings = parse_sensor_data([_sds011_entry()])
    by_prop = {r.observed_property: r for r in readings}
    pm10 = by_prop["pm10"]
    assert pm10.value == pytest.approx(15.3)
    assert pm10.unit == "ug/m3"
    assert pm10.observed_property_name == "PM10 concentration"


def test_parse_pm_sensor_pm25_value_and_unit() -> None:
    readings = parse_sensor_data([_sds011_entry()])
    by_prop = {r.observed_property: r for r in readings}
    pm25 = by_prop["pm2_5"]
    assert pm25.value == pytest.approx(8.1)
    assert pm25.unit == "ug/m3"
    assert pm25.observed_property_name == "PM2.5 concentration"


def test_parse_pm_sensor_thing_name_includes_location_id() -> None:
    readings = parse_sensor_data([_sds011_entry(location_id=789)])
    assert all(r.thing_name == "SensorCommunity 789" for r in readings)


def test_parse_pm_sensor_location_matches_location_id() -> None:
    readings = parse_sensor_data([_sds011_entry(location_id=789)])
    assert all(r.location == "789" for r in readings)


def test_parse_pm_sensor_timestamp_parsed() -> None:
    readings = parse_sensor_data([_sds011_entry()])
    ts = readings[0].timestamp
    assert ts.year == 2026
    assert ts.month == 10
    assert ts.day == 7
    assert ts.hour == 12
    assert ts.tzinfo is not None


# ---------------------------------------------------------------------------
# parse_sensor_data — noise sensor (DNMS)
# ---------------------------------------------------------------------------


def test_parse_noise_sensor_produces_three_readings() -> None:
    readings = parse_sensor_data([_dnms_entry()])
    by_prop = {r.observed_property: r for r in readings}
    assert "noise_laeq" in by_prop
    assert "noise_lamin" in by_prop
    assert "noise_lamax" in by_prop


def test_parse_noise_sensor_laeq_value_and_unit() -> None:
    readings = parse_sensor_data([_dnms_entry()])
    by_prop = {r.observed_property: r for r in readings}
    laeq = by_prop["noise_laeq"]
    assert laeq.value == pytest.approx(52.4)
    assert laeq.unit == "dB(A)"
    assert "LAeq" in laeq.observed_property_name


def test_parse_noise_sensor_lamin_value() -> None:
    readings = parse_sensor_data([_dnms_entry()])
    by_prop = {r.observed_property: r for r in readings}
    assert by_prop["noise_lamin"].value == pytest.approx(44.1)


def test_parse_noise_sensor_lamax_value() -> None:
    readings = parse_sensor_data([_dnms_entry()])
    by_prop = {r.observed_property: r for r in readings}
    assert by_prop["noise_lamax"].value == pytest.approx(65.8)


# ---------------------------------------------------------------------------
# parse_sensor_data — temperature/humidity/pressure sensor (BME280)
# ---------------------------------------------------------------------------


def test_parse_bme280_produces_temperature_humidity_pressure() -> None:
    readings = parse_sensor_data([_bme280_entry()])
    by_prop = {r.observed_property: r for r in readings}
    assert "temperature" in by_prop
    assert "humidity" in by_prop
    assert "pressure" in by_prop


def test_parse_bme280_temperature_value_and_unit() -> None:
    readings = parse_sensor_data([_bme280_entry()])
    by_prop = {r.observed_property: r for r in readings}
    temp = by_prop["temperature"]
    assert temp.value == pytest.approx(18.5)
    assert temp.unit == "°C"


def test_parse_bme280_humidity_value_and_unit() -> None:
    readings = parse_sensor_data([_bme280_entry()])
    by_prop = {r.observed_property: r for r in readings}
    hum = by_prop["humidity"]
    assert hum.value == pytest.approx(72.3)
    assert hum.unit == "%"


def test_parse_bme280_pressure_value_and_unit() -> None:
    readings = parse_sensor_data([_bme280_entry()])
    by_prop = {r.observed_property: r for r in readings}
    pres = by_prop["pressure"]
    assert pres.value == pytest.approx(1013.25)
    assert pres.unit == "hPa"


# ---------------------------------------------------------------------------
# parse_sensor_data — multi-sensor at same location
# ---------------------------------------------------------------------------


def test_parse_multiple_sensor_types_at_same_location() -> None:
    entries = [_sds011_entry(456), _dnms_entry(456), _bme280_entry(456)]
    readings = parse_sensor_data(entries)
    # SDS011: 2, DNMS: 3, BME280: 3 → 8 total
    assert len(readings) == 8
    props = {r.observed_property for r in readings}
    assert "pm10" in props
    assert "pm2_5" in props
    assert "noise_laeq" in props
    assert "temperature" in props


# ---------------------------------------------------------------------------
# parse_sensor_data — unknown value_types silently skipped
# ---------------------------------------------------------------------------


def test_unknown_value_type_is_skipped() -> None:
    entry: dict[str, Any] = {
        "id": 999,
        "sensor": {"id": 1, "sensor_type": {"name": "UNKNOWN"}},
        "location": {"id": 100, "latitude": "52.0", "longitude": "4.0"},
        "sensordatavalues": [
            {"value_type": "totally_unknown_field", "value": "42.0"},
            {"value_type": "another_garbage_type", "value": "1.0"},
        ],
        "timestamp": "2026-10-07 10:00:00",
    }
    readings = parse_sensor_data([entry])
    assert readings == []


def test_mixed_known_and_unknown_value_types() -> None:
    """Known types are kept; unknown types are silently dropped."""
    entry: dict[str, Any] = {
        "id": 999,
        "sensor": {"id": 1, "sensor_type": {"name": "SDS011"}},
        "location": {"id": 100, "latitude": "52.0", "longitude": "4.0"},
        "sensordatavalues": [
            {"value_type": "P1", "value": "5.0"},          # known → pm10
            {"value_type": "UNKNOWN_FIELD", "value": "1.0"},  # unknown → skip
        ],
        "timestamp": "2026-10-07 10:00:00",
    }
    readings = parse_sensor_data([entry])
    assert len(readings) == 1
    assert readings[0].observed_property == "pm10"


def test_non_numeric_value_is_skipped() -> None:
    entry: dict[str, Any] = {
        "id": 999,
        "sensor": {"id": 1, "sensor_type": {"name": "SDS011"}},
        "location": {"id": 100, "latitude": "52.0", "longitude": "4.0"},
        "sensordatavalues": [
            {"value_type": "P1", "value": "not_a_number"},
        ],
        "timestamp": "2026-10-07 10:00:00",
    }
    readings = parse_sensor_data([entry])
    assert readings == []


def test_empty_input_returns_empty_list() -> None:
    readings = parse_sensor_data([])
    assert readings == []


def test_entry_without_location_id_is_skipped() -> None:
    entry: dict[str, Any] = {
        "id": 999,
        "sensor": {"id": 1, "sensor_type": {"name": "SDS011"}},
        "location": {"latitude": "52.0", "longitude": "4.0"},  # no id
        "sensordatavalues": [{"value_type": "P1", "value": "5.0"}],
        "timestamp": "2026-10-07 10:00:00",
    }
    readings = parse_sensor_data([entry])
    assert readings == []


# ---------------------------------------------------------------------------
# build_entity_set — valid structure
# ---------------------------------------------------------------------------


def test_build_entity_set_basic_structure() -> None:
    es = build_entity_set(456, 52.01, 4.36)
    assert "thing" in es
    assert "location" in es
    assert "sensors" in es
    assert "observed_properties" in es


def test_build_entity_set_thing_name_includes_location_id() -> None:
    es = build_entity_set(456, 52.01, 4.36)
    assert es["thing"]["name"] == "SensorCommunity 456"


def test_build_entity_set_location_coordinates() -> None:
    es = build_entity_set(456, 52.01, 4.36)
    coords = es["location"]["location"]["coordinates"]
    assert coords == [4.36, 52.01]  # [lon, lat] — GeoJSON order


def test_build_entity_set_default_observed_properties() -> None:
    es = build_entity_set(456, 52.01, 4.36)
    assert "pm10" in es["observed_properties"]
    assert "pm2_5" in es["observed_properties"]


def test_build_entity_set_custom_observed_properties() -> None:
    es = build_entity_set(456, 52.01, 4.36, ["noise_laeq", "noise_lamin", "noise_lamax"])
    assert "noise_laeq" in es["observed_properties"]
    assert "noise_lamin" in es["observed_properties"]
    assert "noise_lamax" in es["observed_properties"]
    assert "pm10" not in es["observed_properties"]


def test_build_entity_set_sensor_has_correct_sensor_id() -> None:
    es = build_entity_set(456, 52.01, 4.36)
    sensors = es["sensors"]
    assert len(sensors) == 1
    assert sensors[0]["sensor_id"] == "sensor_community-456"


def test_build_entity_set_site_key_includes_location_id() -> None:
    es = build_entity_set(789, 52.0, 4.3)
    assert es["site_key"] == "sensor_community_789"


def test_build_entity_set_source_property() -> None:
    es = build_entity_set(456, 52.01, 4.36)
    assert es["thing"]["properties"]["source"] == "sensor_community"


# ---------------------------------------------------------------------------
# SensorCommunityPollingSource — class hierarchy and interface
# ---------------------------------------------------------------------------


def test_sensor_community_source_is_rest_polling_source() -> None:
    from app.services.sensor_community_source import SensorCommunityPollingSource

    assert issubclass(SensorCommunityPollingSource, RestPollingSource)


def test_sensor_community_source_has_dynamic_discovery() -> None:
    from app.services.sensor_community_source import SensorCommunityPollingSource

    assert SensorCommunityPollingSource.uses_dynamic_discovery is True


def test_sensor_community_source_name() -> None:
    from app.services.sensor_community_source import SensorCommunityPollingSource

    assert SensorCommunityPollingSource.source_name == "sensor_community"


def test_sensor_community_source_disabled_by_default() -> None:
    from app.services.sensor_community_source import SensorCommunityPollingSource

    source = SensorCommunityPollingSource()
    # Default env: SENSOR_COMMUNITY_ENABLED=false
    assert not source.is_enabled()


def test_sensor_community_source_poll_interval_at_least_10() -> None:
    from app.services.sensor_community_source import SensorCommunityPollingSource

    source = SensorCommunityPollingSource()
    assert source.poll_interval() >= 10


def test_sensor_community_source_entity_sets_empty_initially() -> None:
    from app.services.sensor_community_source import SensorCommunityPollingSource

    source = SensorCommunityPollingSource()
    assert source.entity_sets() == []
