"""Unit tests for the Sensor.Community source (#12).

Covers:
- parse_sensor_data() with PM sensor (SDS011) produces PM10 and PM2.5 readings
- parse_sensor_data() with noise sensor (DNMS) produces NOISE_LAEQ/LAMIN/LAMAX
- parse_sensor_data() with temperature/humidity sensor (BME280) produces correct readings
- Pressure is converted from Pa (as Sensor.Community reports it) to canonical hPa
- build_entity_set() produces a valid entity set
- Unknown value_types are silently skipped
- SensorCommunityPollingSource is a RestPollingSource subclass
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

import pytest

from app.services.rest_polling_source import RestPollingSource
from app.sources.sensor_community import (
    PA_BAND,
    UNIT_SCALES,
    build_entity_set,
    parse_sensor_data,
    to_canonical_unit,
)
from app.sta.canonical import CanonicalDatastream, resolve

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
    """BME280 temperature/humidity/pressure sensor entry; the API reports pressure in Pa."""
    return {
        "id": 123458,
        "sensor": {"id": 791, "sensor_type": {"name": "BME280"}},
        "location": {"id": location_id, "latitude": "52.01", "longitude": "4.36"},
        "sensordatavalues": [
            {"value_type": "temperature", "value": "18.5"},
            {"value_type": "humidity", "value": "72.3"},
            {"value_type": "pressure", "value": "100391.86"},
        ],
        "timestamp": "2026-10-07 12:00:00",
    }


def _entry(value_type: str, value: Any) -> dict[str, Any]:
    """A single-value entry, for unit-conversion tests."""
    return {
        "id": 1,
        "sensor": {"id": 2, "sensor_type": {"name": "BME280"}},
        "location": {"id": 100, "latitude": "52.0", "longitude": "4.0"},
        "sensordatavalues": [{"value_type": value_type, "value": value}],
        "timestamp": "2026-11-20 10:00:00",
    }


def _value(value_type: str, value: Any) -> float:
    readings = parse_sensor_data([_entry(value_type, value)])
    assert len(readings) == 1
    return readings[0].value


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
    assert pres.value == pytest.approx(1003.9186)  # 100391.86 Pa
    assert pres.unit == "hPa"
    assert pres.observed_property_name == "Air pressure"


# ---------------------------------------------------------------------------
# parse_sensor_data — pressure unit conversion (Pa -> canonical hPa)
# ---------------------------------------------------------------------------

# Every value_type in the live NL feed (data.sensor.community, 2026-11-20), with the
# unit Sensor.Community reports it in (airrohr firmware / data API).
_SC_UNITS = {
    "P0": "ug/m3", "P1": "ug/m3", "P2": "ug/m3", "P4": "ug/m3",
    "N05": "#/cm3", "N1": "#/cm3", "N25": "#/cm3", "N4": "#/cm3", "N10": "#/cm3",
    "TS": "um",
    "temperature": "°C", "humidity": "%",
    "pressure": "Pa", "pressure_at_sealevel": "Pa",
    "noise_LAeq": "dB(A)", "noise_LA_min": "dB(A)", "noise_LA_max": "dB(A)",
    "co2_ppm": "ppm",
    "counts_per_minute": "1/min", "hv_pulses": "1", "counts": "1", "sample_time_ms": "ms",
}  # fmt: skip


def test_pressure_pa_converted_to_hpa() -> None:
    readings = parse_sensor_data([_entry("pressure", "100391.86")])
    assert len(readings) == 1
    assert readings[0].observed_property == "pressure"
    assert readings[0].unit == "hPa"
    assert repr(readings[0].value) == "1003.9186"


def test_pressure_conversion_is_exact_decimal() -> None:
    """Plain float division would give 999.5435000000001, which FROST would store verbatim."""
    assert 99954.35 / 100 != 999.5435
    assert repr(_value("pressure", "99954.35")) == "999.5435"
    assert repr(_value("pressure", "100199.0")) == "1001.99"


def test_pressure_json_number_converted() -> None:
    assert _value("pressure", 101325) == 1013.25


@pytest.mark.parametrize("raw", ["1013.25", "1001.1", "1002.97998046875", "870.0", "1085"])
def test_pressure_already_in_hpa_left_unchanged(raw: str) -> None:
    """Some stations (other firmware) already report hPa: they must not be divided again."""
    assert _value("pressure", raw) == float(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2000", 2000.0),  # band is open: the bound itself is not plainly Pa
        ("2000.01", 20.0001),
        ("62417.13", 624.1713),  # implausible for NL, but plainly Pa
        ("199999.99", 1999.9999),
        ("200000", 200000.0),  # implausible in either unit: left as is
        ("217706", 217706.0),
        ("-21526.13", -21526.13),  # broken sensor: left as is
        ("0", 0.0),
    ],
)
def test_pressure_band_edges(raw: str, expected: float) -> None:
    assert _value("pressure", raw) == expected


def test_pressure_value_type_matched_case_insensitively() -> None:
    assert _value("Pressure", "100391.86") == pytest.approx(1003.9186)


def test_pressure_outside_band_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="app.sources.sensor_community"):
        assert _value("pressure", "1013.25") == 1013.25
    assert any("not scaled" in r.getMessage() and r.levelno == logging.DEBUG for r in caplog.records)


@pytest.mark.parametrize(
    ("value_type", "raw"),
    [
        ("P1", "2500.0"),  # above the Pa threshold, but scaling is per value_type
        ("P2", "2100.5"),
        ("temperature", "18.5"),
        ("humidity", "72.3"),
        ("noise_LAeq", "52.4"),
    ],
)
def test_other_value_types_not_scaled(value_type: str, raw: str) -> None:
    assert _value(value_type, raw) == float(raw)


@pytest.mark.parametrize("raw", ["not_a_number", "", "nan", "NaN", "inf", "-inf", "Infinity"])
def test_pressure_non_numeric_and_non_finite_still_skipped(raw: str) -> None:
    assert parse_sensor_data([_entry("pressure", raw)]) == []


@pytest.mark.parametrize("raw", [-21526.13, 0.0, 1013.25, 2000.0, 2000.01, 100391.86, 199999.99, 200000.0, 217706.0])
def test_pressure_conversion_is_idempotent(raw: float) -> None:
    once = to_canonical_unit("pressure", raw)
    assert to_canonical_unit("pressure", once) == once


def test_every_scaled_value_lands_below_its_band() -> None:
    """What makes the conversion idempotent; the FROST backfill relies on it too."""
    for scale in UNIT_SCALES.values():
        low, high = scale.band
        assert Decimal(repr(high)) * scale.factor <= Decimal(repr(low))


def test_live_value_types_reach_frost_in_canonical_units() -> None:
    """Each value_type that resolves is either reported in the canonical unit or scaled.

    Guards against a new alias mapping a value_type onto a canonical datastream
    with a different unit, e.g. "pressure_at_sealevel" (Pa) onto hPa, unscaled.
    """
    for value_type, sc_unit in _SC_UNITS.items():
        canonical = resolve(value_type)
        if canonical is None:
            continue  # the mapper skips it
        scale = UNIT_SCALES.get(value_type.lower())
        if scale is None:
            assert sc_unit == canonical.meta.unit, value_type
        else:
            assert scale.source_unit == sc_unit, value_type


def test_only_pressure_is_scaled() -> None:
    assert set(UNIT_SCALES) == {"pressure"}
    assert resolve("pressure") is CanonicalDatastream.PRESSURE
    assert CanonicalDatastream.PRESSURE.meta.unit == "hPa"
    assert UNIT_SCALES["pressure"].band == PA_BAND


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
