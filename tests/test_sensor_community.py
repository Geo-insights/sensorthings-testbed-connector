"""Unit tests for the Sensor.Community source (#12).

Covers:
- parse_sensor_data() with PM sensor (SDS011) produces PM10 and PM2.5 readings
- parse_sensor_data() with noise sensor (DNMS) produces NOISE_LAEQ/LAMIN/LAMAX
- parse_sensor_data() with temperature/humidity sensor (BME280) produces correct readings
- Pressure is converted from Pa (as Sensor.Community reports it) to canonical hPa,
  for every value_type that resolves to an hPa datastream
- build_entity_set() produces a valid entity set
- Unknown value_types are silently skipped
- SensorCommunityPollingSource is a RestPollingSource subclass
- scripts/fix_sc_pressure_units.py, the FROST backfill for the Pa history: its record,
  rollback and dead-letter-queue check, on an in-memory stand-in for the database
  (tests/integration/test_sc_pressure_backfill.py runs the real SQL)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.services.rest_polling_source import RestPollingSource
from app.sources.sensor_community import (
    OWNER_RANK,
    PA_BAND,
    UNIT_SCALES,
    build_entity_set,
    choose_owner,
    parse_sensor_data,
    sensor_type_slug,
    to_canonical_unit,
)
from app.sta.canonical import CanonicalDatastream, resolve
from scripts import fix_sc_pressure_units as backfill

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


def test_parse_noise_sensor_live_api_value_types() -> None:
    """The data API sends DNMS min/max as noise_LA_min / noise_LA_max (seen 2026-11-24 for all 881 NL DNMS
    sensors, sensor_type 'DNMS (Laerm)'); they must land on LAmin/LAmax, not be skipped as unknown."""
    entry = _dnms_entry()
    entry["sensor"]["sensor_type"]["name"] = "DNMS (Laerm)"
    entry["sensordatavalues"] = [
        {"value_type": "noise_LAeq", "value": "52.4"},
        {"value_type": "noise_LA_min", "value": "44.1"},
        {"value_type": "noise_LA_max", "value": "65.8"},
    ]
    by_prop = {r.observed_property: r for r in parse_sensor_data([entry])}
    assert set(by_prop) == {"noise_laeq", "noise_lamin", "noise_lamax"}
    assert by_prop["noise_lamin"].value == pytest.approx(44.1)
    assert by_prop["noise_lamax"].value == pytest.approx(65.8)
    assert by_prop["noise_lamin"].unit == by_prop["noise_lamax"].unit == "dB(A)"


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

# What each of them resolves to today. noise_LA_min/noise_LA_max resolve to LAmin/LAmax
# (aliases added 2026-11-24 on the owner's decision; this registers LAmin/LAmax datastreams
# for every DNMS station).
_CD = CanonicalDatastream
_SC_CANONICAL: dict[str, CanonicalDatastream | None] = {
    "P0": None, "P1": _CD.PM10, "P2": _CD.PM2_5, "P4": None,
    "N05": None, "N1": None, "N25": None, "N4": None, "N10": None,
    "TS": None,
    "temperature": _CD.TEMPERATURE, "humidity": _CD.HUMIDITY,
    "pressure": _CD.PRESSURE, "pressure_at_sealevel": None,
    "noise_LAeq": _CD.NOISE_LAEQ, "noise_LA_min": _CD.NOISE_LAMIN, "noise_LA_max": _CD.NOISE_LAMAX,
    "co2_ppm": None,
    "counts_per_minute": None, "hv_pulses": None, "counts": None, "sample_time_ms": None,
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


@pytest.mark.parametrize(
    "value_type",
    ["pressure", "Pressure", " PRESSURE ", "air_pressure", "airpressure", "Air Pressure", "air-pressure", "AIR_PRESSURE"],
)
def test_every_spelling_of_an_hpa_datastream_is_converted(value_type: str) -> None:
    """The scale follows the datastream resolve() picks, not the raw value_type."""
    assert resolve(value_type) in (CanonicalDatastream.PRESSURE, CanonicalDatastream.AIR_PRESSURE)
    assert repr(_value(value_type, "100391.86")) == "1003.9186"
    assert _value(value_type, "1013.25") == 1013.25  # already hPa


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
    once = to_canonical_unit(CanonicalDatastream.PRESSURE, raw)
    assert to_canonical_unit(CanonicalDatastream.PRESSURE, once) == once


def test_every_scaled_value_lands_below_its_band() -> None:
    """What makes the conversion idempotent; the FROST backfill relies on it too."""
    for scale in UNIT_SCALES.values():
        low, high = scale.band
        assert Decimal(repr(high)) * scale.factor <= Decimal(repr(low))


def test_live_value_types_reach_frost_in_canonical_units() -> None:
    """Each value_type that resolves is either reported in the canonical unit or scaled.

    Guards against a new alias mapping a value_type onto a canonical datastream
    with a different unit, e.g. "pressure_at_sealevel" (Pa) onto hPa, unscaled:
    any change in what a live value_type resolves to fails the first assert.
    """
    assert {value_type: resolve(value_type) for value_type in _SC_UNITS} == _SC_CANONICAL
    for value_type, sc_unit in _SC_UNITS.items():
        canonical = resolve(value_type)
        if canonical is None:
            continue  # the mapper skips it
        scale = UNIT_SCALES.get(canonical)
        if scale is None:
            assert sc_unit == canonical.meta.unit, value_type
        else:
            assert scale.source_unit == sc_unit, value_type


def test_every_hpa_datastream_is_scaled_from_pa() -> None:
    hpa = {member for member in CanonicalDatastream if member.meta.unit == "hPa"}
    assert hpa == {CanonicalDatastream.PRESSURE, CanonicalDatastream.AIR_PRESSURE}
    assert set(UNIT_SCALES) == hpa
    assert all(scale.source_unit == "Pa" and scale.band == PA_BAND for scale in UNIT_SCALES.values())


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
# One datastream per sensor type (#39): slug, owner rank, sensor ids, secondary sensors
# ---------------------------------------------------------------------------


def _dht22_entry(location_id: int = 456) -> dict[str, Any]:
    """DHT22 temperature/humidity sensor next to the BME280 of ``_bme280_entry``."""
    return {
        "id": 123459,
        "sensor": {"id": 792, "sensor_type": {"name": "DHT22"}},
        "location": {"id": location_id, "latitude": "52.01", "longitude": "4.36"},
        "sensordatavalues": [
            {"value_type": "temperature", "value": "18.9"},
            {"value_type": "humidity", "value": "70.0"},
        ],
        "timestamp": "2026-10-07 12:00:00",
    }


@pytest.mark.parametrize(
    ("sensor_type", "slug"),
    [("DHT22", "dht22"), ("DNMS (Laerm)", "dnms-laerm"), ("SEN5x", "sen5x"), (" Radiation SBM-19 ", "radiation-sbm-19")],
)
def test_sensor_type_slug(sensor_type: str, slug: str) -> None:
    """protects=the sensor_id suffix of a type's own datastream (sensor_community-{loc}-{slug});
    fails_when=case, punctuation runs or edge separators leak into the slug;
    no_claim=two types never share a slug."""
    assert sensor_type_slug(sensor_type) == slug


@pytest.mark.parametrize(
    ("observed_property", "sensor_types", "owner"),
    [
        ("temperature", {"DHT22", "BME280"}, "BME280"),
        ("temperature", {"DHT22", "BMP280"}, "BMP280"),
        ("temperature", {"BMP180", "SHT31"}, "SHT31"),
        ("humidity", {"DHT22", "SCD30", "BME280"}, "BME280"),
        ("pressure", {"BMP180", "BME280"}, "BME280"),
        ("pm2_5", {"SDS011", "SPS30"}, "SPS30"),
        ("pm10", {"SDS011", "SEN5x"}, "SEN5x"),
        ("temperature", {"DHT22", "Unlisted9"}, "DHT22"),  # unlisted types rank after every listed one
        ("temperature", {"Zeta", "Alpha"}, "Alpha"),  # unlisted ties: alphabetical
        ("noise_laeq", {"DNMS (Laerm)", "ABC"}, "ABC"),  # property without a rank: alphabetical
    ],
)
def test_choose_owner_follows_the_rank(observed_property: str, sensor_types: set[str], owner: str) -> None:
    """protects=the owner of a (location, property) is the best-ranked reporting type, independent of order;
    fails_when=OWNER_RANK order, the unlisted-after-listed rule or the alphabetical tie-break is not applied;
    no_claim=the rank reflects real instrument quality."""
    assert choose_owner(observed_property, sensor_types) == owner
    assert choose_owner(observed_property, sorted(sensor_types, reverse=True)) == owner


def test_owner_rank_keys_are_the_canonical_values_resolve_returns() -> None:
    """protects=OWNER_RANK is looked up with the canonical keys readings carry;
    fails_when=a rank key is no longer a canonical value (e.g. after a canonical rename), silently ranking alphabetically;
    no_claim=the canonical names themselves."""
    for raw, key in [("temperature", "temperature"), ("humidity", "humidity"), ("pressure", "pressure"),
                     ("P2", "pm2_5"), ("P1", "pm10")]:
        canonical = resolve(raw)
        assert canonical is not None and canonical.value == key
    assert set(OWNER_RANK) == {"temperature", "humidity", "pressure", "pm2_5", "pm10"}


def test_parse_sensor_ids_follow_the_owner_map() -> None:
    """protects=a non-owner type's readings get its own sensor_id, the owner's keep the location id;
    fails_when=the owner map is ignored, or a non-owner reading still uses sensor_community-{loc};
    no_claim=how owners are chosen (see the source tests)."""
    entries = [_dht22_entry(), _bme280_entry()]
    owners = {"456": {"temperature": "BME280", "humidity": "BME280", "pressure": "BME280"}}

    readings = parse_sensor_data(entries, owners=owners)

    ids = {(r.sensor_name, r.observed_property): r.sensor_id for r in readings}
    assert ids == {
        ("SensorCommunity 456 DHT22 sensor", "temperature"): "sensor_community-456-dht22",
        ("SensorCommunity 456 DHT22 sensor", "humidity"): "sensor_community-456-dht22",
        ("SensorCommunity 456 BME280 sensor", "temperature"): "sensor_community-456",
        ("SensorCommunity 456 BME280 sensor", "humidity"): "sensor_community-456",
        ("SensorCommunity 456 BME280 sensor", "pressure"): "sensor_community-456",
    }
    assert {(r.thing_name, r.location) for r in readings} == {("SensorCommunity 456", "456")}


def test_parse_without_owner_map_keeps_the_location_sensor_id() -> None:
    """protects=today's behaviour for callers that pass no owner map;
    fails_when=parse_sensor_data(entries) assigns type sensor_ids by itself;
    no_claim=those readings avoid shared datastreams."""
    readings = parse_sensor_data([_dht22_entry(), _bme280_entry()])

    assert {r.sensor_id for r in readings} == {"sensor_community-456"}


def test_build_entity_set_adds_a_sensor_per_secondary_type() -> None:
    """protects=a type that reports properties it does not own gets its own Sensor + datastreams;
    fails_when=the secondary sensor is missing, has the wrong sensor_id/name/properties, or alters the location sensor;
    no_claim=how FROST names the datastreams (see the integration test)."""
    plain = build_entity_set(456, 52.01, 4.36, ["temperature", "humidity", "pressure"])
    es = build_entity_set(
        456, 52.01, 4.36, ["temperature", "humidity", "pressure"], secondary={"DHT22": ["temperature", "humidity"]},
    )

    location_sensor, dht22 = es["sensors"]
    assert location_sensor == plain["sensors"][0]
    assert dht22["sensor_id"] == "sensor_community-456-dht22"
    assert dht22["name"] == "SensorCommunity 456 DHT22 sensor"
    assert dht22["observed_properties"] == ["temperature", "humidity"]
    assert dht22["properties"] == {"sensor_type": "DHT22"}
    assert {k: v for k, v in es.items() if k != "sensors"} == {k: v for k, v in plain.items() if k != "sensors"}
    assert build_entity_set(456, 52.01, 4.36, ["pm10"], secondary={"SDS011": []}) == build_entity_set(
        456, 52.01, 4.36, ["pm10"]
    )


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


# ---------------------------------------------------------------------------
# scripts/fix_sc_pressure_units.py — FROST backfill of the Pa history
# ---------------------------------------------------------------------------

_WRITE_SQL = re.compile(r"\b(UPDATE|INSERT|DELETE|TRUNCATE|ALTER|DROP|CREATE|GRANT)\b", re.IGNORECASE)
_IDS = re.compile(r"ANY\('\{([0-9,]*)\}'::bigint\[\]\)")
_JSON_ROWS = re.compile(r"json_to_recordset\('((?:[^']|'')*)'::json\)")
_DS = re.compile(r'"DATASTREAM_ID" = (\d+)')


@dataclass
class _Row:
    """One OBSERVATIONS row, as far as the backfill cares."""

    ds: int
    number: float | None
    text: str | None
    prop: str | None = "pressure"  # PARAMETERS->>'observed_property'; None: no PARAMETERS
    result_type: int = 0


def _hpa_text(number: float) -> str:
    """trim_scale(number::numeric * 0.01)::text, for the values used here."""
    text = format(Decimal(repr(number)) * Decimal("0.01"), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _is_pressure(row: _Row) -> bool:
    return row.prop in (None, "pressure")


def _to_convert(row: _Row) -> bool:
    return (
        row.result_type == 0
        and row.number is not None
        and backfill.LOW < row.number < backfill.HIGH
        and _is_pressure(row)
    )


class _FakeFrost:
    """psql on the FROST database, in memory: answers the script's statements by their tag.

    It does not evaluate SQL (tests/integration/test_sc_pressure_backfill.py runs the real
    SQL); it keeps rows, so the script's sequencing, record and rollback can be checked end
    to end. crash_after_updates makes a write session fail after that many UPDATE statements,
    as a timeout or a dropped connection would; with server_keeps_running, the statements
    after it still change rows, unreported, as psql does on the server once the client is gone.
    """

    def __init__(self, rows: dict[int, _Row], units: dict[int, str] | None = None) -> None:
        self.rows = rows
        self.units = units or {ds: "hPa" for ds in sorted({r.ds for r in rows.values()})}
        self.sessions: list[str] = []
        self.busy: list[str] = []
        self.crash_after_updates: int | None = None
        self.server_keeps_running = False
        self.before_session: Callable[[str], None] | None = None
        self.after_session: Callable[[str], None] | None = None

    def __call__(self, sql: str) -> list[str]:
        self.sessions.append(sql)
        if self.before_session:
            self.before_session(sql)
        out: list[str] = []
        updates = 0
        crashed = False
        for statement in sql.split(";\n"):
            lines = self._run(statement)
            if not crashed:
                out += lines
            if "SELECT 'updated'" in statement:
                updates += 1
                if updates == self.crash_after_updates:
                    crashed = True
                    if not self.server_keeps_running:
                        break
        if self.after_session:
            self.after_session(sql)
        if crashed:
            raise backfill.PsqlError("psql session did not finish within 5 s", out)
        return out

    def _ds_rows(self, ds: int) -> list[tuple[int, _Row]]:
        return sorted(((i, r) for i, r in self.rows.items() if r.ds == ds), key=lambda item: item[0])

    def _run(self, sql: str) -> list[str]:
        if "SELECT 'ds'" in sql:
            return [f"ds|{ds}|{unit}|GEO_SensorCommunity {ds} sensor - Air pressure" for ds, unit in self.units.items()]
        if m := re.search(r"SELECT 'stat', (\d+),", sql):
            return [self._stat(int(m[1]))]
        if "SELECT 'sample'" in sql:
            ds = int(_DS.findall(sql)[0])
            return [
                f"sample|{i}|{ds}|2026-11-20 10:39:11+00|{r.number!r}|{r.text}|{float(_hpa_text(r.number))!r}|"
                f"{_hpa_text(r.number)}"
                for i, r in self._ds_rows(ds)
                if _to_convert(r) and r.number is not None
            ][:1]
        if "SELECT 'target'" in sql:
            ds = int(_DS.findall(sql)[0])
            return [f"target|{ds}|{i}|{r.number!r}|{json.dumps(r.text)}" for i, r in self._ds_rows(ds) if _to_convert(r)]
        if m := re.search(r"SELECT 'updated', (\d+), (\d+)", sql):
            ds, listed = int(m[1]), int(m[2])
            match = _IDS.search(sql)
            assert match is not None
            ids = [int(i) for i in match[1].split(",") if i]
            assert len(ids) == listed
            done = 0
            for i in ids:
                row = self.rows.get(i)
                if row is not None and row.ds == ds and _to_convert(row) and row.number is not None:
                    row.text = _hpa_text(row.number)
                    row.number = float(row.text)
                    done += 1
            return [f"updated|{ds}|{listed}|{done}|end"]
        if "SELECT 'busy'" in sql:
            return list(self.busy)
        if m := re.search(r"SELECT '(rbstate|restored)', (\d+)", sql):
            return [self._rollback(m[1], int(m[2]), sql)]
        return []

    def _stat(self, ds: int) -> str:
        rows = [r for _, r in self._ds_rows(ds)]
        conv = [r for r in rows if _to_convert(r)]
        conv_numbers = [r.number for r in conv if r.number is not None]
        pressure_numbers = [r.number for r in rows if _is_pressure(r) and r.result_type == 0 and r.number is not None]
        fields: list[object] = [
            ds,
            len(rows),
            len(conv),
            sum(1 for r in rows if not _is_pressure(r)),
            sum(1 for n in pressure_numbers if n <= backfill.LOW),
            sum(1 for n in pressure_numbers if n >= backfill.HIGH),
            sum(
                1
                for r in rows
                if not _is_pressure(r) and r.result_type == 0 and r.number is not None
                and backfill.LOW < r.number < backfill.HIGH
            ),
            sum(1 for r in conv if r.text is None or float(r.text) != r.number),
            min(conv_numbers, default=""),
            max(conv_numbers, default=""),
        ]
        return "stat|" + "|".join(str(f) for f in fields)

    def _rollback(self, tag: str, ds: int, sql: str) -> str:
        match = _JSON_ROWS.search(sql)
        assert match is not None
        records = json.loads(match[1].replace("''", "'"))
        # As the statement joins: on the observation ID alone, or (an older form) only in the recorded datastream.
        recorded_datastream_only = re.search(rf'o\."DATASTREAM_ID" = {ds}\b', sql) is not None
        found = as_converted = as_recorded = elsewhere = elsewhere_converted = 0
        for record in records:
            row = self.rows.get(record["id"])
            if row is None or (recorded_datastream_only and row.ds != ds):
                continue
            found += 1
            moved = row.ds != ds
            elsewhere += moved
            original = float(record["num"])
            if row.result_type == 0 and row.number == float(_hpa_text(original)) and row.text == _hpa_text(original):
                as_converted += 1
                elsewhere_converted += moved
                if tag == "restored":
                    row.number, row.text = original, record["str"]
            elif row.number == original and row.text == record["str"]:
                as_recorded += 1
        if tag == "restored":
            return f"restored|{ds}|{len(records)}|{as_converted}|end"
        if recorded_datastream_only:
            return f"rbstate|{ds}|{len(records)}|{found}|{as_converted}|{as_recorded}|end"
        return f"rbstate|{ds}|{len(records)}|{found}|{as_converted}|{as_recorded}|{elsewhere}|{elsewhere_converted}|end"


def _rows() -> dict[int, _Row]:
    """Datastream 16: Pa readings and all that must stay as is; datastream 28: Pa readings only."""
    rows = {
        1: _Row(16, 100391.86, "100391.86"),
        2: _Row(16, 99954.35, "99954.35"),
        3: _Row(16, 100199.0, "100199.0"),  # FROST keeps the text of the number it was sent
        4: _Row(16, 100000.0, "1.0E+5"),
        5: _Row(16, 1013.25, "1013.25"),  # already hPa
        6: _Row(16, 2000.0, "2000"),  # on the band edge
        7: _Row(16, 2500.0, "2500.0", prop="pm10"),  # a misrouted PM10 reading
        8: _Row(16, None, "n/a", result_type=3),  # a string result
    }
    rows.update({100 + k: _Row(28, 100000.0 + k, f"{100000 + k}.0", prop=None) for k in range(12)})
    return rows


_CONVERTED = {1: "1003.9186", 2: "999.5435", 3: "1001.99", 4: "1000"} | {
    100 + k: _hpa_text(100000.0 + k) for k in range(12)
}


def _state(frost: _FakeFrost) -> dict[int, tuple[float | None, str | None]]:
    return {i: (r.number, r.text) for i, r in frost.rows.items()}


def _args(*extra: str) -> argparse.Namespace:
    return backfill.parse_args(["--container", "frost-db-test", *extra])


def _apply_args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    return _args("--apply", "--ids-file", str(tmp_path / "ids.csv"), *extra)


def _rollback_args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    return _args("--rollback", "--ids-file", str(tmp_path / "ids.csv"), *extra)


def _recorded(tmp_path: Path) -> list[backfill.Record]:
    return backfill.read_ids_file(tmp_path / "ids.csv")


def _writes(frost: _FakeFrost) -> list[str]:
    return [sql for sql in frost.sessions if sql.startswith(backfill.WRITE_SESSION)]


# --- SQL -------------------------------------------------------------------


def test_backfill_band_is_the_mapper_band() -> None:
    assert (backfill.LOW, backfill.HIGH) == PA_BAND
    assert backfill.IN_BAND == '"RESULT_TYPE" = 0 AND "RESULT_NUMBER" > 2000.0 AND "RESULT_NUMBER" < 200000.0'


def test_backfill_selects_sensor_community_air_pressure_datastreams() -> None:
    sql = backfill.datastreams_sql()
    assert """t."NAME" LIKE 'GEO\\_SensorCommunity %'""" in sql
    assert """t."PROPERTIES"->>'source' = 'sensor_community'""" in sql
    assert """op."NAME" = 'Air pressure'""" in sql


def test_backfill_update_converts_only_listed_rows_still_in_the_band() -> None:
    sql = backfill.update_sql(16, [3, 7])
    assert 'UPDATE "OBSERVATIONS"' in sql
    assert """"DATASTREAM_ID" = 16 AND "ID" = ANY('{3,7}'::bigint[])""" in sql
    assert backfill.TO_CONVERT in sql
    assert '"RESULT_NUMBER" = trim_scale("RESULT_NUMBER"::numeric * 0.01)::double precision' in sql
    assert '"RESULT_STRING" = trim_scale("RESULT_NUMBER"::numeric * 0.01)::text' in sql


def test_backfill_rollback_restores_only_rows_holding_the_converted_value() -> None:
    sql = backfill.rollback_sql(16, [backfill.Record(16, 3, "100199", "100199.0")])
    assert backfill.IS_CONVERTED in sql
    # The rollback recomputes the converted value with the conversion's own expression.
    assert backfill.converted('"RESULT_NUMBER"') == backfill.NEW_VALUE
    assert f'o."RESULT_STRING" = {backfill.converted("v.num")}::text' in backfill.IS_CONVERTED
    assert '"RESULT_NUMBER" = v.num' in sql
    assert '"RESULT_STRING" = v.str' in sql
    assert """'[{"id":3,"num":"100199","str":"100199.0"}]'::json""" in sql


def test_backfill_rollback_finds_rows_by_id_in_any_datastream() -> None:
    """scripts/fix_sc_misrouted_observations.py moves rows, converted ones too: the rollback must
    not look for a row only in the datastream it was recorded under. IS_CONVERTED proves the row."""
    records = [backfill.Record(16, 3, "100199", "100199.0")]
    restore, state = backfill.rollback_sql(16, records), backfill.rollback_state_sql(16, records)
    assert 'o."DATASTREAM_ID" = 16' not in restore
    assert 'WHERE o."ID" = v.id\n' in restore
    assert backfill.IS_CONVERTED in restore
    assert 'o."DATASTREAM_ID" = 16' not in state
    assert 'LEFT JOIN "OBSERVATIONS" o ON o."ID" = v.id;' in state
    assert 'count(o."ID") FILTER (WHERE o."DATASTREAM_ID" IS DISTINCT FROM 16)' in state


def test_backfill_rollback_sql_quotes_any_recorded_text() -> None:
    sql = backfill.rollback_sql(16, [backfill.Record(16, 3, "100199", "it's \\ odd")])
    assert """'[{"id":3,"num":"100199","str":"it''s \\\\ odd"}]'::json""" in sql


def test_backfill_never_converts_rows_tagged_as_another_property() -> None:
    """The pressure datastreams also hold misrouted PM10/PM2.5/humidity readings."""
    for sql in (backfill.update_sql(16, [1, 2]), backfill.targets_sql(16), backfill.sample_sql(16, '"ID"', 1)):
        assert backfill.IS_PRESSURE in sql


def test_backfill_read_statements_do_not_write() -> None:
    assert "TRANSACTION READ ONLY" in backfill.READ_ONLY_SESSION
    for sql in (
        backfill.READ_ONLY_SESSION,
        backfill.datastreams_sql(),
        backfill.stats_sql(16),
        backfill.sample_sql(16, '"ID" DESC', 3),
        backfill.targets_sql(16),
        backfill.busy_sql(),
        backfill.rollback_state_sql(16, [backfill.Record(16, 3, "100199", "100199.0")]),
    ):
        assert not _WRITE_SQL.search(sql), sql


def test_backfill_every_session_sets_its_timeouts() -> None:
    for session in (backfill.READ_ONLY_SESSION, backfill.WRITE_SESSION):
        assert "SET statement_timeout = '50s';" in session
        assert "SET lock_timeout = '10s';" in session
        assert "SET idle_in_transaction_session_timeout = '60s';" in session
        assert "SET extra_float_digits = 3;" in session
        assert "SET standard_conforming_strings = on;" in session
    assert "READ ONLY" not in backfill.WRITE_SESSION


def test_backfill_display_conversion_matches_mapper() -> None:
    assert backfill.to_hpa(100391.86) == Decimal("1003.9186")
    assert float(backfill.to_hpa(99954.35)) == to_canonical_unit(CanonicalDatastream.PRESSURE, 99954.35)


# --- parsing psql output -----------------------------------------------------


def test_backfill_parse_stats_handles_nulls() -> None:
    (stats,) = backfill.parse_stats(["stat|16|0|0|0|0|0|0|0||"])
    assert stats.rows == 0
    assert stats.min_value is None
    assert stats.max_value is None


def test_backfill_parse_datastreams_keeps_pipes_in_names() -> None:
    (ds,) = backfill.parse_datastreams(["ds|16|hPa|GEO_SensorCommunity 1 | odd name"])
    assert ds == backfill.Datastream(16, "hPa", "GEO_SensorCommunity 1 | odd name")


def test_backfill_parse_targets_reads_the_stored_text_exactly() -> None:
    lines = [
        'target|16|3|100199|"100199.0"',
        'target|16|4|100000|"1.0E+5"',
        "target|16|5|100391.86|null",
        'target|16|6|100391.86|"a|b \\"c\\""',
        "updated|16|1|1|end",
    ]
    assert backfill.parse_targets(lines) == [
        backfill.Record(16, 3, "100199", "100199.0"),
        backfill.Record(16, 4, "100000", "1.0E+5"),
        backfill.Record(16, 5, "100391.86", None),
        backfill.Record(16, 6, "100391.86", 'a|b "c"'),
    ]


@pytest.mark.parametrize(
    "line",
    [
        'target|16|3|100199|"100199.',  # cut off inside the text
        "target|16|3|100199|",  # cut off before it
        "target|16|3|1001",  # cut off inside the number
        "target|16|3|100199|42",  # not a string
        'target|16|3|1013.25|"1013.25"',  # not in the Pa band
        'target|16|3|100199|"a\\nb"',  # a line break
    ],
)
def test_backfill_parse_targets_rejects_what_it_cannot_record_exactly(line: str) -> None:
    with pytest.raises(ValueError):
        backfill.parse_targets([line])


def test_backfill_parse_counts_rejects_cut_off_lines() -> None:
    assert backfill.parse_counts(["updated|16|5|4|end"], "updated") == [(16, 5, 4)]
    for line in ("updated|16|5|4|en", "updated|16|5|4", "updated|16|5|6|end"):
        with pytest.raises(ValueError):
            backfill.parse_counts([line], "updated")


# --- the IDs file --------------------------------------------------------------


@pytest.mark.parametrize("text", ["100391.86", "100199.0", "1.0E+5", "", 'a "quote", a comma', None])
def test_backfill_record_line_round_trips(text: str | None) -> None:
    record = backfill.Record(16, 496, "100391.86", text)
    line = backfill.format_record(record)
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert backfill.parse_record(line[:-1]) == record


def test_backfill_record_line_is_postgresql_csv() -> None:
    """COPY ... CSV HEADER loads it as is: an unquoted empty field is NULL, a quoted one ''."""
    assert backfill.format_record(backfill.Record(16, 1, "100391.86", None)) == "16,1,100391.86,\n"
    assert backfill.format_record(backfill.Record(16, 1, "100391.86", "")) == '16,1,100391.86,""\n'
    assert backfill.format_record(backfill.Record(16, 1, "100199", "100199.0")) == '16,1,100199,"100199.0"\n'


def test_backfill_ids_file_is_created_with_its_header(tmp_path: Path) -> None:
    path = tmp_path / "ids.csv"
    backfill.prepare_ids_file(path)
    assert path.read_bytes() == (backfill.IDS_HEADER + "\n").encode()
    backfill.prepare_ids_file(path)  # an existing, complete file is left as is
    assert path.read_bytes() == (backfill.IDS_HEADER + "\n").encode()


def test_backfill_refuses_an_ids_file_of_another_format(tmp_path: Path) -> None:
    path = tmp_path / "ids.csv"
    path.write_text("datastream_id,observation_id\n16,496\n", encoding="utf-8")
    with pytest.raises(backfill.IdsFileError, match="not an IDs file"):
        backfill.prepare_ids_file(path)
    with pytest.raises(backfill.IdsFileError, match="not an IDs file"):
        backfill.read_ids_file(path)


def test_backfill_drops_a_line_cut_off_by_an_interrupted_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "ids.csv"
    complete = backfill.IDS_HEADER + "\n" + '16,1,100391.86,"100391.86"\n'
    path.write_bytes((complete + '16,2,99954.35,"9995').encode())
    assert [r.observation_id for r in backfill.read_ids_file(path)] == [1]  # skipped when read
    backfill.prepare_ids_file(path)  # dropped before anything is appended
    assert path.read_bytes() == complete.encode()
    assert "cut-off" in capsys.readouterr().err


def test_backfill_read_ids_file_names_a_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "ids.csv"
    path.write_text(backfill.IDS_HEADER + "\n16,1,100391.86,\nnot,a,record\n", encoding="utf-8")
    with pytest.raises(backfill.IdsFileError, match="line 3"):
        backfill.read_ids_file(path)


def test_backfill_group_records_takes_a_row_recorded_under_two_datastreams_as_one() -> None:
    """An interrupted run records a row, the move script moves it, the re-run records it again
    under its new datastream: the same values, so one row, not a conflict."""
    first = backfill.Record(28, 7, "100391.86", "100391.86")
    again = first._replace(datastream_id=16)
    assert backfill.group_records([first, again]) == {28: [first]}
    assert backfill.group_records([first, again], {16}) == {16: [again]}
    assert backfill.group_records([first, again], {28}) == {28: [first]}
    for different in (again._replace(result_number="100391.87"), again._replace(result_string="100391.860")):
        with pytest.raises(backfill.IdsFileError, match="different values"):
            backfill.group_records([first, different])


def test_backfill_group_records_dedupes_and_refuses_conflicts() -> None:
    a = backfill.Record(16, 1, "100391.86", "100391.86")
    b = backfill.Record(28, 2, "100000", "100000.0")
    assert backfill.group_records([b, a, a]) == {16: [a], 28: [b]}
    assert backfill.group_records([b, a], {28}) == {28: [b]}
    with pytest.raises(backfill.IdsFileError, match="different values"):
        backfill.group_records([a, a._replace(result_string="100391.860")])


# --- transport -------------------------------------------------------------------


def test_backfill_psql_over_ssh_handles_no_credentials() -> None:
    argv = backfill.psql_argv(ssh="root@frost.example", ssh_key="~/.ssh/key")
    assert argv[:7] == ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    assert argv[7] == "-i"
    assert not argv[8].startswith("~")
    assert argv[9] == "root@frost.example"
    remote = argv[10]
    assert remote.startswith("cd /opt/frost && docker compose exec -T frost-db sh -c 'psql ")
    # Single-quoted on the host: the container's own shell expands its environment.
    assert remote.endswith(' -U "$POSTGRES_USER" -d "$POSTGRES_DB"\'')
    assert "ON_ERROR_STOP=1" in remote
    assert "PASSWORD" not in " ".join(argv).upper()


def test_backfill_psql_in_local_container() -> None:
    argv = backfill.psql_argv(container="frost-db-test")
    assert argv[:6] == ["docker", "exec", "-i", "frost-db-test", "sh", "-c"]


def _child(code: str) -> list[str]:
    """A stand-in for the psql command: a Python child process."""
    return [sys.executable, "-c", code]


def test_backfill_psql_runner_returns_the_output_lines() -> None:
    run = backfill.psql_runner(_child("import sys; sys.stdout.write(sys.stdin.read().upper())"), timeout=60)
    assert run("a|1\nb|2\n") == ["A|1", "B|2"]


def test_backfill_psql_runner_keeps_the_complete_lines_after_an_error() -> None:
    code = "import sys; sys.stdout.write('updated|16|5|5|end\\nupdated|16|5|'); sys.exit(3)"
    with pytest.raises(backfill.PsqlError, match="psql exited with 3") as info:
        backfill.psql_runner(_child(code), timeout=60)("")
    assert info.value.lines == ["updated|16|5|5|end"]


def test_backfill_psql_runner_keeps_the_complete_lines_after_the_timeout() -> None:
    code = (
        "import sys, time; sys.stdout.write('updated|16|5|5|end\\nupdated|16|5|'); sys.stdout.flush(); "
        "time.sleep(60)"
    )
    with pytest.raises(backfill.PsqlError, match="did not finish within 3 s") as info:
        backfill.psql_runner(_child(code), timeout=3)("")
    assert info.value.lines == ["updated|16|5|5|end"]


def test_backfill_psql_runner_turns_ctrl_c_into_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(*args: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(backfill.subprocess, "run", interrupted)
    with pytest.raises(backfill.PsqlError, match="interrupted"):
        backfill.psql_runner(["psql"], timeout=1)("SELECT 1;")


# --- arguments -------------------------------------------------------------------


def test_backfill_requires_a_target() -> None:
    with pytest.raises(SystemExit):
        backfill.parse_args([])


def test_backfill_apply_requires_an_ids_file() -> None:
    with pytest.raises(SystemExit):
        backfill.parse_args(["--container", "frost-db-test", "--apply"])


def test_backfill_rollback_requires_an_existing_ids_file(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        backfill.parse_args(["--container", "frost-db-test", "--rollback"])
    with pytest.raises(SystemExit):
        backfill.parse_args(["--container", "frost-db-test", "--rollback", "--ids-file", str(tmp_path / "none.csv")])


def test_backfill_check_dlq_takes_no_database_arguments() -> None:
    args = backfill.parse_args(["--check-dlq", "dlq.jsonl"])
    assert args.ssh is None
    assert args.container is None
    for extra in (["--apply"], ["--rollback"], ["--container", "frost-db-test"]):
        with pytest.raises(SystemExit):
            backfill.parse_args(["--check-dlq", "dlq.jsonl", *extra])


# --- dry run and --apply ---------------------------------------------------------


def test_backfill_dry_run_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    frost = _FakeFrost(_rows())
    before = _state(frost)
    assert backfill.run(_args(), frost) == 0
    assert _state(frost) == before
    assert all(sql.startswith(backfill.READ_ONLY_SESSION) for sql in frost.sessions)
    assert not [sql for sql in frost.sessions if _WRITE_SQL.search(sql)]
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert re.search(r"pressure readings to convert \(Pa band\)\s+16\n", out)
    assert "99954.35 .. 100391.86 Pa -> 999.5435 .. 1003.9186 hPa" in out
    assert "Samples: before -> after" in out
    assert "Plan: 2 UPDATE statements over 2 datastreams" in out
    assert "Dry run: nothing was written" in out


def test_backfill_apply_records_every_row_before_it_is_converted(tmp_path: Path) -> None:
    frost = _FakeFrost(_rows())
    original = _state(frost)
    ids_file = tmp_path / "ids.csv"
    listed_in_writes: list[int] = []

    def recorded_first(sql: str) -> None:
        if sql.startswith(backfill.WRITE_SESSION):
            on_disk = {r.observation_id for r in backfill.read_ids_file(ids_file)}
            listed = {int(i) for group in _IDS.findall(sql) for i in group.split(",")}
            assert listed <= on_disk, "an UPDATE was sent for rows not yet in the IDs file"
            listed_in_writes.append(len(listed))

    frost.before_session = recorded_first
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    assert listed_in_writes == [16]
    records = _recorded(tmp_path)
    assert {r.observation_id for r in records} == set(_CONVERTED)  # a clean run records what it converts
    for record in records:  # with the values from before the conversion
        assert (float(record.result_number), record.result_string) == original[record.observation_id]
    after = _state(frost)
    assert {i: after[i][1] for i in _CONVERTED} == _CONVERTED
    assert {i: v for i, v in after.items() if i not in _CONVERTED} == {
        i: v for i, v in original.items() if i not in _CONVERTED
    }


def test_backfill_apply_converts_in_batches_and_verifies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    frost = _FakeFrost(_rows())
    assert backfill.run(_apply_args(tmp_path, "--batch-rows", "5"), frost) == 0
    (write,) = _writes(frost)
    assert write.count('UPDATE "OBSERVATIONS"') == 4  # 4 rows of datastream 16, 12 of 28 in 3 batches
    out = capsys.readouterr().out
    assert "Converted 16 observations (planned 16); 16 rows recorded" in out
    assert "Verification: no rows left in the Pa band, as of now." in out
    assert "dead-letter queue can still bring Pa rows back until 48 h after" in out
    raw = (tmp_path / "ids.csv").read_bytes()
    assert b"\r" not in raw  # LF on every platform
    assert raw.decode("utf-8").splitlines()[0] == backfill.IDS_HEADER


@pytest.mark.parametrize("server_keeps_running", [False, True])
def test_backfill_interrupted_apply_never_leaves_a_converted_row_unrecorded(
    tmp_path: Path, server_keeps_running: bool
) -> None:
    """A timeout, Ctrl-C or a dropped connection mid-session; psql may still finish on the server."""
    frost = _FakeFrost(_rows())
    original = _state(frost)
    frost.crash_after_updates = 1
    frost.server_keeps_running = server_keeps_running
    with pytest.raises(backfill.PsqlError, match="is already in") as info:
        backfill.run(_apply_args(tmp_path, "--batch-rows", "5"), frost)
    assert "as far as psql reported: 4." in str(info.value)  # the first batch: datastream 16
    changed = {i for i, v in _state(frost).items() if v != original[i]}
    assert changed <= {r.observation_id for r in _recorded(tmp_path)}
    assert changed == (set(_CONVERTED) if server_keeps_running else {1, 2, 3, 4})

    # Re-running finishes the job; the rollback then restores every row exactly.
    frost.crash_after_updates = None
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    assert {i: _state(frost)[i][1] for i in _CONVERTED} == _CONVERTED
    assert backfill.run(_rollback_args(tmp_path, "--apply"), frost) == 0
    assert _state(frost) == original


def test_backfill_ids_file_is_appended_across_runs(tmp_path: Path) -> None:
    frost = _FakeFrost(_rows())
    assert backfill.run(_apply_args(tmp_path, "--datastream", "16"), frost) == 0
    assert backfill.run(_apply_args(tmp_path, "--datastream", "28"), frost) == 0
    lines = (tmp_path / "ids.csv").read_text(encoding="utf-8").splitlines()
    assert lines.count(backfill.IDS_HEADER) == 1
    assert {r.observation_id for r in _recorded(tmp_path)} == set(_CONVERTED)


def test_backfill_rerun_converts_and_records_nothing_more(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    frost = _FakeFrost(_rows())
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    size = (tmp_path / "ids.csv").stat().st_size
    capsys.readouterr()
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    assert (tmp_path / "ids.csv").stat().st_size == size
    assert len(_writes(frost)) == 1
    assert "Converted 0 observations (planned 0); 0 rows recorded" in capsys.readouterr().out


def test_backfill_apply_reports_rows_left_in_band(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    frost = _FakeFrost(_rows())

    def replayed(sql: str) -> None:  # a Pa reading arrives from the connector's dead-letter queue mid-run
        if sql.startswith(backfill.WRITE_SESSION):
            frost.rows[999] = _Row(28, 100500.0, "100500.0")

    frost.after_session = replayed
    assert backfill.run(_apply_args(tmp_path), frost) == 1
    assert "1 pressure readings still in the Pa band" in capsys.readouterr().out


def test_backfill_canary_touches_only_the_given_datastream(tmp_path: Path) -> None:
    frost = _FakeFrost(_rows())
    original = _state(frost)
    assert backfill.run(_apply_args(tmp_path, "--datastream", "28"), frost) == 0
    (write,) = _writes(frost)
    assert '"DATASTREAM_ID" = 28 ' in write
    assert '"DATASTREAM_ID" = 16 ' not in write
    assert all(_state(frost)[i] == original[i] for i, r in frost.rows.items() if r.ds == 16)


def test_backfill_rejects_datastreams_outside_the_selection(tmp_path: Path) -> None:
    frost = _FakeFrost(_rows())
    assert backfill.run(_apply_args(tmp_path, "--datastream", "999"), frost) == 1
    assert len(frost.sessions) == 1
    assert _writes(frost) == []


def test_backfill_skips_datastreams_not_labelled_hpa(tmp_path: Path) -> None:
    frost = _FakeFrost(_rows(), units={16: "Pa", 28: "hPa"})
    original = _state(frost)
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    assert all(_state(frost)[i] == original[i] for i, r in frost.rows.items() if r.ds == 16)


def test_backfill_refuses_to_write_while_another_write_session_is_active(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    frost = _FakeFrost(_rows())
    frost.busy = ["busy|4242|active|00:00:12|end"]
    original = _state(frost)
    assert backfill.run(_apply_args(tmp_path), frost) == 1
    assert _state(frost) == original
    assert _writes(frost) == []
    assert "pid 4242: active" in capsys.readouterr().out


def test_backfill_checks_the_ids_file_before_touching_the_database(tmp_path: Path) -> None:
    (tmp_path / "ids.csv").write_text("datastream_id,observation_id\n", encoding="utf-8")
    frost = _FakeFrost(_rows())
    with pytest.raises(backfill.IdsFileError):
        backfill.run(_apply_args(tmp_path), frost)
    assert frost.sessions == []


# --- --rollback ------------------------------------------------------------------


def test_backfill_rollback_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    frost = _FakeFrost(_rows())
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    converted = _state(frost)
    sessions = len(frost.sessions)
    capsys.readouterr()
    assert backfill.run(_rollback_args(tmp_path), frost) == 0
    assert _state(frost) == converted
    assert frost.sessions[sessions:]
    assert all(sql.startswith(backfill.READ_ONLY_SESSION) for sql in frost.sessions[sessions:])
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert re.search(r"holding the converted value: to restore\s+16\n", out)
    assert "Dry run: nothing was written. Re-run with --apply to restore." in out


def test_backfill_rollback_restores_the_recorded_values_exactly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    frost = _FakeFrost(_rows())
    original = _state(frost)
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    assert backfill.run(_rollback_args(tmp_path, "--apply", "--batch-rows", "5"), frost) == 0
    assert _state(frost) == original  # RESULT_STRING too: '100199.0', '1.0E+5'
    out = capsys.readouterr().out
    assert "Restored 16 observations (planned 16)." in out
    assert "Verification: no recorded row holds the converted value any more, in any datastream." in out
    assert sum(sql.count("SELECT 'restored'") for sql in _writes(frost)) == 4  # in batches of 5
    assert backfill.run(_rollback_args(tmp_path, "--apply"), frost) == 0  # again: nothing to do
    assert "Restored 0 observations (planned 0)." in capsys.readouterr().out


def test_backfill_rollback_leaves_rows_changed_since_alone(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    frost = _FakeFrost(_rows())
    original = _state(frost)
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    frost.rows[2].number, frost.rows[2].text = 1001.5, "1001.5"  # edited after the backfill
    capsys.readouterr()
    assert backfill.run(_rollback_args(tmp_path, "--apply"), frost) == 0
    after = _state(frost)
    assert after[2] == (1001.5, "1001.5")
    assert {i: v for i, v in after.items() if i != 2} == {i: v for i, v in original.items() if i != 2}
    assert re.search(r"changed since, left alone\s+1\n", capsys.readouterr().out)


def test_backfill_rollback_can_be_limited_to_a_datastream(tmp_path: Path) -> None:
    frost = _FakeFrost(_rows())
    original = _state(frost)
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    converted = _state(frost)
    assert backfill.run(_rollback_args(tmp_path, "--apply", "--datastream", "16"), frost) == 0
    after = _state(frost)
    assert all(after[i] == (original[i] if r.ds == 16 else converted[i]) for i, r in frost.rows.items())


def test_backfill_rollback_restores_converted_rows_moved_to_another_datastream(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The backfill converts (b), the move script moves converted rows (d): the rollback alone restores them."""
    frost = _FakeFrost(_rows())
    original = _state(frost)
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    frost.rows[1].ds = 28  # to another Air pressure datastream
    frost.rows[2].ds = 77  # out of the Air pressure datastreams, e.g. back to a humidity one
    capsys.readouterr()

    assert backfill.run(_rollback_args(tmp_path), frost) == 0  # dry run
    out = capsys.readouterr().out
    assert re.search(r"not found \(deleted\)\s+0\n", out)
    assert re.search(r"in another datastream than recorded \(moved since\)\s+2\n", out)
    assert re.search(r"holding the converted value: to restore\s+16\n", out)
    assert re.search(r"of those in another datastream than recorded\s+2\n", out)

    assert backfill.run(_rollback_args(tmp_path, "--apply"), frost) == 0
    assert _state(frost) == original
    out = capsys.readouterr().out
    assert "Restored 16 observations (planned 16)." in out
    assert "Verification: no recorded row holds the converted value any more, in any datastream." in out


def test_backfill_rollback_after_an_interrupted_run_a_move_and_a_rerun(tmp_path: Path) -> None:
    """b stops after its first batch; the move script moves rows b recorded but did not convert; the
    re-run converts them and records them again under their new datastream. One rollback undoes it all."""
    frost = _FakeFrost(_rows())
    original = _state(frost)
    frost.crash_after_updates = 1  # datastream 16 converted; 28's rows recorded, not converted
    with pytest.raises(backfill.PsqlError):
        backfill.run(_apply_args(tmp_path, "--batch-rows", "5"), frost)
    frost.crash_after_updates = None
    for i in (100, 101, 102):  # still in Pa: moved from 28 to 16
        frost.rows[i].ds = 16

    assert backfill.run(_apply_args(tmp_path), frost) == 0
    recorded = {(r.datastream_id, r.observation_id) for r in _recorded(tmp_path)}
    assert {(28, 100), (16, 100)} <= recorded  # one row, recorded under both datastreams
    assert {i: _state(frost)[i][1] for i in _CONVERTED} == _CONVERTED

    assert backfill.run(_rollback_args(tmp_path, "--apply"), frost) == 0
    assert _state(frost) == original


def test_backfill_rollback_refuses_while_another_write_session_is_active(tmp_path: Path) -> None:
    frost = _FakeFrost(_rows())
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    converted = _state(frost)
    frost.busy = ["busy|4242|idle in transaction|00:00:12|end"]
    assert backfill.run(_rollback_args(tmp_path, "--apply"), frost) == 1
    assert _state(frost) == converted


# --- --check-dlq -----------------------------------------------------------------


def _dlq_entry(
    result: Any,
    *,
    prop: str = "pressure",
    sensor: str = "sensor_community-1001",
    host: str = "frost.geo-insights.nl",
    stamp: str = "2026-11-20T09:00:00+00:00",
) -> str:
    """A dead-letter queue line as the connector writes it (SensorThingsClient._write_failed_observation)."""
    thing = "SensorCommunity 1001" if sensor.startswith("sensor_community-") else "KNMI 260"
    payload = {
        "phenomenonTime": "2026-11-20T08:55:00Z",
        "result": result,
        "resultQuality": ["good"],
        "Datastream": {"@iot.id": 16},
        "parameters": {"sensor_id": sensor, "observed_property": prop, "unit": "hPa", "thing_name": thing},
    }
    return json.dumps({
        "timestamp": stamp,
        "datastream_id": "16",
        "endpoint": f"https://{host}/FROST-Server/v1.1/Observations",
        "payload": payload,
        "status_code": None,
        "error": "circuit_open",
    })


def test_backfill_check_dlq_counts_pre_fix_pressure_in_the_pa_band(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "dlq.jsonl"
    entries = [
        _dlq_entry(100391.86),
        _dlq_entry(99954.35, stamp="2026-11-20T10:30:00+00:00"),
        _dlq_entry(100199.0, host="v2.example"),
        _dlq_entry(1003.9186),  # converted by the fixed mapper: hPa
        _dlq_entry(True),  # not a number
        _dlq_entry(15.3, prop="pm10"),
        _dlq_entry(100391.86, sensor="knmi-260"),  # not Sensor.Community
        "{not json",
    ]
    path.write_text("\n".join(entries) + "\n", encoding="utf-8")
    now = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    assert backfill.check_dlq(path, now) == 1
    out = capsys.readouterr().out
    assert "entries: 8 (1 unreadable)" in out
    assert "in the Pa band: 3" in out
    assert "frost.geo-insights.nl: 2, written 2026-11-20T09:00:00+00:00 .. 2026-11-20T10:30:00+00:00" in out
    assert "v2.example: 1, written" in out
    assert "outside the band, left as is: 2" in out
    assert "The last one expires by 2026-11-22T10:30:00+00:00 (1 day, 22:30:00 from now)." in out


def test_backfill_check_dlq_without_pa_readings_returns_0(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "dlq.jsonl"
    path.write_text(_dlq_entry(1003.9186) + "\n\n", encoding="utf-8")
    assert backfill.check_dlq(path, datetime(2026, 11, 20, tzinfo=UTC)) == 0
    assert "None: this queue cannot bring Pa readings back" in capsys.readouterr().out


def test_backfill_and_dlq_check_read_what_the_connector_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backfill selects on PARAMETERS->>'observed_property'; --check-dlq reads the dead-letter
    entries SensorThingsClient writes. Both as the client builds them, not as a test assumes."""
    from dataclasses import replace as with_overrides

    import requests

    from app.config import settings
    from app.services import sensorthings_client as client_module

    dlq = tmp_path / "failed_observations.jsonl"
    monkeypatch.setattr(
        client_module,
        "settings",
        with_overrides(
            settings,
            sensorthings_base_url="https://sc-backfill-test.invalid/v1.1",
            frost_targets=(),
            datastream_ids={},
            registered_entities_path=str(tmp_path / "registered_entities.json"),
            failed_observations_path=str(dlq),
            frost_batch_push_enabled=False,
        ),
    )
    client = client_module.SensorThingsClient()
    posted: list[dict[str, Any]] = []

    def unreachable(endpoint: str, payload: dict[str, Any], datastream_id: str, base_url: str | None = None) -> None:
        posted.append(payload)
        raise requests.ConnectionError("FROST unreachable")  # so the client dead-letters it

    monkeypatch.setattr(client, "_resolve_datastream_id_live", lambda base_url, reading: "16")
    monkeypatch.setattr(client, "_post_observation", unreachable)

    (fixed,) = parse_sensor_data([_entry("pressure", "100391.86")])
    pre_fix = fixed.model_copy(update={"value": 100391.86})  # what the mapper sent before the fix
    client.push_observations([fixed, pre_fix])

    assert sorted(p["result"] for p in posted) == [1003.9186, 100391.86]
    assert all(p["parameters"]["observed_property"] == "pressure" for p in posted)
    assert """"PARAMETERS"->>'observed_property'""" in backfill.IS_PRESSURE
    assert len(dlq.read_text(encoding="utf-8").splitlines()) == 2
    assert backfill.check_dlq(dlq, datetime.now(UTC)) == 1  # only the pre-fix one is in the Pa band


def test_backfill_main_checks_a_dlq_copy_without_a_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "dlq.jsonl"
    path.write_text(_dlq_entry(100391.86) + "\n", encoding="utf-8")

    def no_database(*args: Any, **kwargs: Any) -> None:
        pytest.fail("the DLQ check needs no database")

    monkeypatch.setattr(backfill, "psql_runner", no_database)
    assert backfill.main(["--check-dlq", str(path)]) == 1


def test_backfill_main_reports_psql_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def failing(argv: list[str], timeout: float) -> Callable[[str], list[str]]:
        def run(sql: str) -> list[str]:
            raise backfill.PsqlError("psql exited with 2: connection refused")

        return run

    monkeypatch.setattr(backfill, "psql_runner", failing)
    assert backfill.main(["--container", "frost-db-test"]) == 1
    assert "ERROR: psql exited with 2: connection refused" in capsys.readouterr().err
