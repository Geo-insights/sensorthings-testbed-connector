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
- scripts/fix_sc_pressure_units.py, the FROST backfill for the Pa history
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
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

# What each of them resolves to today. noise_LA_min/noise_LA_max resolve to nothing (the
# aliases are spelled noise_lamin/noise_lamax), so the mapper drops them: adding aliases
# would register LAmin/LAmax datastreams for every DNMS station, a product decision.
_CD = CanonicalDatastream
_SC_CANONICAL: dict[str, CanonicalDatastream | None] = {
    "P0": None, "P1": _CD.PM10, "P2": _CD.PM2_5, "P4": None,
    "N05": None, "N1": None, "N25": None, "N4": None, "N10": None,
    "TS": None,
    "temperature": _CD.TEMPERATURE, "humidity": _CD.HUMIDITY,
    "pressure": _CD.PRESSURE, "pressure_at_sealevel": None,
    "noise_LAeq": _CD.NOISE_LAEQ, "noise_LA_min": None, "noise_LA_max": None,
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


def _stats(ds_id: int = 16, **overrides: Any) -> backfill.Stats:
    fields: dict[str, Any] = {
        "datastream_id": ds_id,
        "rows": 3952,
        "to_convert": 3950,
        "misrouted": 0,
        "below_band": 1,
        "above_band": 1,
        "in_band_misrouted": 0,
        "text_mismatch": 0,
        "min_value": 97832.84,
        "max_value": 102025.16,
        "min_id": 496,
        "max_id": 21_251_328,
    }
    fields.update(overrides)
    return backfill.Stats(**fields)


class _FakeFrost:
    """Answers the script's SQL the way psql on the FROST database would, by statement tag."""

    def __init__(self, stats: list[backfill.Stats], unit: str = "hPa", convert: bool = True) -> None:
        self.stats = {s.datastream_id: s for s in stats}
        self.unit = unit
        self.convert = convert
        self.sessions: list[str] = []

    def __call__(self, sql: str) -> list[str]:
        self.sessions.append(sql)
        out: list[str] = []
        if "SELECT 'ds'" in sql:
            out += [f"ds|{i}|{self.unit}|GEO_SensorCommunity {i} sensor - Air pressure" for i in self.stats]
        for ds_id in re.findall(r"SELECT 'stat', (\d+),", sql):
            s = self.stats[int(ds_id)]
            fields = [s.datastream_id, s.rows, s.to_convert, s.misrouted, s.below_band, s.above_band,
                      s.in_band_misrouted, s.text_mismatch, s.min_value, s.max_value, s.min_id, s.max_id]
            out.append("stat|" + "|".join("" if f is None else str(f) for f in fields))
        out += ["sample|21251328|16|2026-11-20 10:39:11+00|100199|100199.0|1001.99|1001.99"] * sql.count(
            "SELECT 'sample'"
        )
        for ds_id, lo, hi in re.findall(r"SELECT 'updated', (\d+), (\d+), (\d+)", sql):
            s = self.stats[int(ds_id)]
            n = min(s.to_convert, int(hi) - int(lo) + 1)  # pretend the batch's rows sit at its start
            ids = " ".join(str(int(lo) + k) for k in range(n))
            out.append(f"updated|{ds_id}|{lo}|{hi}|{n}|{ids}")
            if self.convert:
                left = s.to_convert - n
                self.stats[s.datastream_id] = replace(s, to_convert=left, below_band=s.below_band + n)
                if not left:
                    self.stats[s.datastream_id] = replace(
                        self.stats[s.datastream_id], min_value=None, max_value=None, min_id=None, max_id=None
                    )
        return out

    def writes(self) -> list[str]:
        return [sql for sql in self.sessions if _WRITE_SQL.search(sql)]


def test_backfill_band_is_the_mapper_band() -> None:
    assert (backfill.LOW, backfill.HIGH) == PA_BAND
    assert backfill.IN_BAND == '"RESULT_TYPE" = 0 AND "RESULT_NUMBER" > 2000.0 AND "RESULT_NUMBER" < 200000.0'


def test_backfill_selects_sensor_community_air_pressure_datastreams() -> None:
    sql = backfill.datastreams_sql()
    assert """t."NAME" LIKE 'GEO\\_SensorCommunity %'""" in sql
    assert """t."PROPERTIES"->>'source' = 'sensor_community'""" in sql
    assert """op."NAME" = 'Air pressure'""" in sql


def test_backfill_update_statement() -> None:
    sql = backfill.update_sql(16, 496, 21_251_328)
    assert 'UPDATE "OBSERVATIONS"' in sql
    assert '"DATASTREAM_ID" = 16 AND "ID" BETWEEN 496 AND 21251328' in sql
    assert backfill.IN_BAND in sql
    assert '"RESULT_NUMBER" = trim_scale("RESULT_NUMBER"::numeric * 0.01)::double precision' in sql
    assert '"RESULT_STRING" = trim_scale("RESULT_NUMBER"::numeric * 0.01)::text' in sql


def test_backfill_never_converts_rows_tagged_as_another_property() -> None:
    """The pressure datastreams also hold misrouted PM10/PM2.5/humidity readings."""
    assert backfill.IS_PRESSURE == """coalesce("PARAMETERS"->>'observed_property', 'pressure') = 'pressure'"""
    assert backfill.IS_PRESSURE in backfill.update_sql(16, 1, 2)
    assert backfill.IS_PRESSURE in backfill.sample_sql(16, '"ID"', 1)


def test_backfill_read_statements_do_not_write() -> None:
    assert "TRANSACTION READ ONLY" in backfill.READ_ONLY_SESSION
    for sql in (
        backfill.READ_ONLY_SESSION,
        backfill.datastreams_sql(),
        backfill.stats_sql(16),
        backfill.sample_sql(16, '"ID" DESC', 3),
    ):
        assert not _WRITE_SQL.search(sql), sql


def test_backfill_display_conversion_matches_mapper() -> None:
    assert backfill.to_hpa(100391.86) == Decimal("1003.9186")
    assert float(backfill.to_hpa(99954.35)) == to_canonical_unit(CanonicalDatastream.PRESSURE, 99954.35)


@pytest.mark.parametrize(
    ("to_convert", "min_id", "max_id", "batch_rows", "expected"),
    [
        (0, None, None, 5000, []),
        (1, 7, 7, 5000, [(7, 7)]),
        (3950, 496, 21_251_328, 5000, [(496, 21_251_328)]),
        (12_000, 0, 2999, 5000, [(0, 999), (1000, 1999), (2000, 2999)]),
        (10, 5, 6, 1, [(5, 5), (6, 6)]),  # never more batches than IDs
    ],
)
def test_backfill_plan_batches(
    to_convert: int, min_id: int | None, max_id: int | None, batch_rows: int, expected: list[tuple[int, int]]
) -> None:
    stats = _stats(to_convert=to_convert, min_id=min_id, max_id=max_id)
    assert backfill.plan_batches(stats, batch_rows) == expected


def test_backfill_batches_cover_the_id_range_without_gaps() -> None:
    from itertools import pairwise

    batches = backfill.plan_batches(_stats(to_convert=123_457), batch_rows=5000)
    assert len(batches) == 25
    assert batches[0][0] == 496
    assert batches[-1][1] == 21_251_328
    assert all(prev[1] + 1 == nxt[0] for prev, nxt in pairwise(batches))


def test_backfill_parse_stats_handles_nulls() -> None:
    (stats,) = backfill.parse_stats(["stat|16|0|0|0|0|0|0|0||||"])
    assert stats.rows == 0
    assert stats.min_value is None
    assert stats.max_id is None
    assert backfill.plan_batches(stats, 5000) == []


def test_backfill_parse_datastreams_keeps_pipes_in_names() -> None:
    (ds,) = backfill.parse_datastreams(["ds|16|hPa|GEO_SensorCommunity 1 | odd name"])
    assert ds == backfill.Datastream(16, "hPa", "GEO_SensorCommunity 1 | odd name")


def test_backfill_psql_over_ssh_handles_no_credentials() -> None:
    argv = backfill.psql_argv(ssh="root@frost.example", ssh_key="~/.ssh/key")
    assert argv[:4] == ["ssh", "-o", "BatchMode=yes", "-i"]
    assert not argv[4].startswith("~")
    assert argv[5] == "root@frost.example"
    remote = argv[6]
    assert remote.startswith("cd /opt/frost && docker compose exec -T frost-db sh -c 'psql ")
    # Single-quoted on the host: the container's own shell expands its environment.
    assert remote.endswith(' -U "$POSTGRES_USER" -d "$POSTGRES_DB"\'')
    assert "ON_ERROR_STOP=1" in remote
    assert "PASSWORD" not in " ".join(argv).upper()


def test_backfill_psql_in_local_container() -> None:
    argv = backfill.psql_argv(container="frost-db-test")
    assert argv[:6] == ["docker", "exec", "-i", "frost-db-test", "sh", "-c"]


def test_backfill_requires_a_target() -> None:
    with pytest.raises(SystemExit):
        backfill.parse_args([])


def test_backfill_apply_requires_an_ids_file() -> None:
    with pytest.raises(SystemExit):
        backfill.parse_args(["--container", "frost-db-test", "--apply"])


def test_backfill_parse_updated_checks_the_reported_ids() -> None:
    assert backfill.parse_updated(["updated|16|1|9|2|3 7"]) == [(16, [3, 7])]
    assert backfill.parse_updated(["updated|16|1|9|0|"]) == [(16, [])]
    with pytest.raises(ValueError, match="2 rows updated but 1 IDs"):
        backfill.parse_updated(["updated|16|1|9|2|3"])
    with pytest.raises(ValueError, match=r"outside 1\.\.9"):
        backfill.parse_updated(["updated|16|1|9|2|3 70"])


def test_backfill_dry_run_writes_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    hpa_only = _stats(28, to_convert=0, below_band=3952, above_band=0, min_value=None, max_value=None,
                      min_id=None, max_id=None)
    frost = _FakeFrost([_stats(16), hpa_only])
    assert backfill.run(backfill.parse_args(["--container", "frost-db-test"]), frost) == 0
    assert frost.writes() == []
    assert all(sql.startswith(backfill.READ_ONLY_SESSION) for sql in frost.sessions)
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert re.search(r"pressure readings to convert \(Pa band\)\s+3,950\n", out)
    assert "97832.84 .. 102025.16 Pa -> 978.3284 .. 1020.2516 hPa" in out
    assert "100199 ('100199.0') -> 1001.99 ('1001.99')" in out
    assert "Plan: 1 UPDATE statements over 1 datastreams" in out
    assert "Dry run: nothing was written" in out


def _apply_args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    ids_file = str(tmp_path / "ids.csv")
    return backfill.parse_args(["--container", "frost-db-test", "--apply", "--ids-file", ids_file, *extra])


def test_backfill_apply_converts_in_batches_and_verifies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    frost = _FakeFrost([_stats(16), _stats(28, rows=12_002, to_convert=12_000, min_id=0, max_id=29_999)])
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    (write,) = frost.writes()
    assert "READ ONLY" not in write
    assert write.count('UPDATE "OBSERVATIONS"') == 4  # 1 batch for datastream 16, 3 for 28
    out = capsys.readouterr().out
    assert "Converted 15,950 observations (planned 15,950)" in out
    assert "Verification: no rows left in the Pa band." in out
    raw = (tmp_path / "ids.csv").read_bytes()
    assert b"\r" not in raw  # LF on every platform: the file is loaded on the FROST host
    lines = raw.decode("utf-8").splitlines()
    assert lines[0] == "datastream_id,observation_id"
    assert len(lines) == 1 + 15_950
    assert lines[1] == "16,496"


def test_backfill_ids_file_is_appended_across_runs(tmp_path: Path) -> None:
    assert backfill.run(_apply_args(tmp_path, "--datastream", "16"), _FakeFrost([_stats(16), _stats(28)])) == 0
    assert backfill.run(_apply_args(tmp_path, "--datastream", "28"), _FakeFrost([_stats(16), _stats(28)])) == 0
    lines = (tmp_path / "ids.csv").read_text(encoding="utf-8").splitlines()
    assert lines.count("datastream_id,observation_id") == 1
    assert {line.split(",")[0] for line in lines[1:]} == {"16", "28"}


def test_backfill_apply_reports_rows_left_in_band(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    frost = _FakeFrost([_stats(16)], convert=False)  # e.g. Pa rows still arriving
    assert backfill.run(_apply_args(tmp_path), frost) == 1
    assert "3,950 pressure readings still in the Pa band" in capsys.readouterr().out


def test_backfill_canary_touches_only_the_given_datastream(tmp_path: Path) -> None:
    frost = _FakeFrost([_stats(16), _stats(28)])
    assert backfill.run(_apply_args(tmp_path, "--datastream", "28"), frost) == 0
    (write,) = frost.writes()
    assert '"DATASTREAM_ID" = 28 ' in write
    assert '"DATASTREAM_ID" = 16 ' not in write


def test_backfill_rejects_datastreams_outside_the_selection(tmp_path: Path) -> None:
    frost = _FakeFrost([_stats(16)])
    assert backfill.run(_apply_args(tmp_path, "--datastream", "999"), frost) == 1
    assert len(frost.sessions) == 1
    assert frost.writes() == []


def test_backfill_skips_datastreams_not_labelled_hpa(tmp_path: Path) -> None:
    frost = _FakeFrost([_stats(16)], unit="Pa")
    assert backfill.run(_apply_args(tmp_path), frost) == 0
    assert frost.writes() == []


def test_backfill_apply_error_records_progress_and_stops(tmp_path: Path) -> None:
    frost = _FakeFrost([_stats(16)])

    def failing(sql: str) -> list[str]:
        if "SELECT 'updated'" in sql:
            # One statement reported in full, then the session died mid-line.
            partial = ["updated|16|496|9000|3|496 497 498", "updated|16|9001|12000|2|9001 90"]
            raise backfill.PsqlError("psql exited with 3: statement timeout", partial)
        return frost(sql)

    with pytest.raises(backfill.PsqlError, match="rows converted before the error: 3; re-running is safe"):
        backfill.run(_apply_args(tmp_path), failing)
    lines = (tmp_path / "ids.csv").read_text(encoding="utf-8").splitlines()
    assert lines[1:] == ["16,496", "16,497", "16,498"]
