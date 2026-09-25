"""Cross-source unit + name normalization tests.

Guards the invariant that every source mapper emits SensorReadings whose
observed_property name and unit symbol come from app.sta.canonical — never
from source-provided strings or upstream Avro payloads. This is the
backend-side enforcement position taken in Geonovum discussion #24.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.sources import levellog, ohnics
from app.sources.climate_adaptation import (
    CLIMATE_ADAPTATION_ENTITY_SETS,
    generate_demo_readings,
)
from app.sources.normalizers import LevellogNormalizer, OhnicsNormalizer
from app.sta.canonical import CanonicalDatastream, resolve
from app.sta.normalizer import Normalizer

_CANONICAL_UNITS = {member.value: member.meta.unit for member in CanonicalDatastream}
_CANONICAL_NAMES = {member.value for member in CanonicalDatastream}


def test_ohnics_emits_canonical_names_and_units():
    sensor_data = {
        "Name": "de-abc123",
        "Lat": 52.0,
        "Long": 4.4,
        "Timestamp": "2026-08-27T12:00:00Z",
        "P2": "12.3",   # PM2.5
        "T": "18.7",    # air_temperature
    }
    readings = ohnics.parse_sensor_readings(sensor_data)
    assert len(readings) == 2
    by_prop = {r.observed_property: r for r in readings}
    # pm25 raw field must resolve to canonical pm2_5
    assert "pm2_5" in by_prop
    assert by_prop["pm2_5"].unit == "ug/m3"
    assert by_prop["pm2_5"].observed_property_name == "PM2.5 concentration"
    # air_temperature stays canonical
    assert "air_temperature" in by_prop
    assert by_prop["air_temperature"].unit == "°C"


def test_ohnics_entity_set_uses_canonical_property_key():
    entity_set = ohnics.build_entity_set("de-xyz", 52.0, 4.4)
    op_keys = set(entity_set["observed_properties"].keys())
    assert op_keys <= _CANONICAL_NAMES
    assert "pm2_5" in op_keys
    # Legacy alias "pm25" must not leak into the entity set.
    assert "pm25" not in op_keys


def test_levellog_emits_canonical_water_level():
    header_row = ["Timestamp", "GrondwaterStand"]
    data_row = ["2026-08-27T09:00:00Z", "1.234"]
    readings = levellog.parse_logdata_readings(
        [header_row, data_row], installation_id="abcdefgh1234", installation_name="TGV-1",
    )
    assert len(readings) == 1
    r = readings[0]
    assert r.observed_property == "water_level"
    assert r.unit == "m"
    assert r.observed_property_name == "Groundwater level"


def test_climate_adaptation_demo_readings_use_canonical_units():
    readings = generate_demo_readings()
    assert readings, "generate_demo_readings should emit at least one reading"
    for r in readings:
        assert r.observed_property in _CANONICAL_NAMES, (
            f"Non-canonical observed_property {r.observed_property!r} in demo readings"
        )
        assert r.unit == _CANONICAL_UNITS[r.observed_property], (
            f"Unit {r.unit!r} for {r.observed_property!r} does not match "
            f"canonical {_CANONICAL_UNITS[r.observed_property]!r}"
        )


def test_entity_sets_observed_property_keys_are_canonical():
    """All observed_property keys in every entity set must resolve to canonical."""
    for entity_set in CLIMATE_ADAPTATION_ENTITY_SETS:
        for key in entity_set["observed_properties"]:
            assert resolve(key) is not None, (
                f"observed_property {key!r} in "
                f"'{entity_set['thing']['name']}' doesn't resolve to canonical"
            )


# ---------------------------------------------------------------------------
# Normalizer pattern tests
# ---------------------------------------------------------------------------

_TS = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


class TestOhnicsNormalizer:
    def test_to_readings_produces_correct_sensor_readings(self):
        norm = OhnicsNormalizer(P2=12.3, T=18.7)
        readings = norm.to_readings(
            sensor_id="ohnics-de-test",
            sensor_name="Ohnics de-test sensor",
            thing_name="Ohnics de-test",
            timestamp=_TS,
            location="delft",
        )
        assert len(readings) == 2
        by_prop = {r.observed_property: r for r in readings}

        pm = by_prop["pm2_5"]
        assert pm.value == 12.3
        assert pm.unit == "ug/m3"
        assert pm.observed_property_name == "PM2.5 concentration"
        assert pm.sensor_id == "ohnics-de-test"

        temp = by_prop["air_temperature"]
        assert temp.value == 18.7
        assert temp.unit == "°C"
        assert temp.observed_property_name == "Air temperature"

    def test_none_fields_are_skipped(self):
        norm = OhnicsNormalizer(P2=None, T=22.0)
        readings = norm.to_readings(
            sensor_id="s", sensor_name="n", thing_name="t", timestamp=_TS,
        )
        assert len(readings) == 1
        assert readings[0].observed_property == "air_temperature"

    def test_all_none_produces_empty(self):
        norm = OhnicsNormalizer()
        readings = norm.to_readings(
            sensor_id="s", sensor_name="n", thing_name="t", timestamp=_TS,
        )
        assert readings == []

    def test_extra_fields_ignored(self):
        """Unknown vendor fields are silently dropped (model_config extra=ignore)."""
        norm = OhnicsNormalizer.model_validate({"P2": 5.0, "unknown_field": 999})
        readings = norm.to_readings(
            sensor_id="s", sensor_name="n", thing_name="t", timestamp=_TS,
        )
        assert len(readings) == 1
        assert readings[0].observed_property == "pm2_5"


class TestLevellogNormalizer:
    def test_to_readings_produces_water_level(self):
        norm = LevellogNormalizer(water_level=1.234)
        readings = norm.to_readings(
            sensor_id="tgv-levellog-abcdefgh",
            sensor_name="Levellog TGV-1 sensor",
            thing_name="Levellog TGV-1",
            timestamp=_TS,
            location="tgv",
        )
        assert len(readings) == 1
        r = readings[0]
        assert r.observed_property == "water_level"
        assert r.value == 1.234
        assert r.unit == "m"
        assert r.observed_property_name == "Groundwater level"

    def test_none_water_level_produces_empty(self):
        norm = LevellogNormalizer(water_level=None)
        assert norm.to_readings(
            sensor_id="s", sensor_name="n", thing_name="t", timestamp=_TS,
        ) == []


class TestNormalizerValidation:
    def test_valid_name_transform_passes(self):
        """Normalizers with valid NAME_TRANSFORM values pass validation."""
        norm = OhnicsNormalizer(P2=1.0)
        assert norm is not None

    def test_transform_callables_applied(self):
        """TRANSFORM callables are applied to values before conversion."""
        from typing import ClassVar

        class ScaledNormalizer(Normalizer):
            raw_val: float | None = None
            NAME_TRANSFORM: ClassVar[dict[str, CanonicalDatastream]] = {
                "raw_val": CanonicalDatastream.TEMPERATURE,
            }
            TRANSFORM: ClassVar[dict[str, object]] = {
                "raw_val": lambda v: v * 0.1,
            }

        norm = ScaledNormalizer(raw_val=220.0)
        readings = norm.to_readings(
            sensor_id="s", sensor_name="n", thing_name="t", timestamp=_TS,
        )
        assert len(readings) == 1
        assert readings[0].value == pytest.approx(22.0)
        assert readings[0].unit == "°C"

    def test_device_eui_and_stream_key_passed_through(self):
        norm = OhnicsNormalizer(P2=5.0)
        readings = norm.to_readings(
            sensor_id="s", sensor_name="n", thing_name="t", timestamp=_TS,
            device_eui="eui-123", stream_key="key-456",
        )
        assert readings[0].device_eui == "eui-123"
        assert readings[0].stream_key == "key-456"

    def test_quality_defaults_to_good(self):
        norm = OhnicsNormalizer(T=20.0)
        readings = norm.to_readings(
            sensor_id="s", sensor_name="n", thing_name="t", timestamp=_TS,
        )
        assert readings[0].quality == "good"
