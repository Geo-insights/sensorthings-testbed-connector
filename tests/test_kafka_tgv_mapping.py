"""
Unit tests for the TGV Kafka → SensorReading mapping layer.
No Kafka connection required.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.sources.tgv_kafka_mapping import (
    _DEFAULT_DEVICE_MAPPING,
    _extract_numeric_value,
    _get_mapping,
    avro_batch_to_sensor_readings,
    avro_record_to_sensor_readings,
)


# ---------------------------------------------------------------------------
# _extract_numeric_value
# ---------------------------------------------------------------------------

def test_extract_numeric_value_plain_float():
    assert _extract_numeric_value(21.5) == 21.5


def test_extract_numeric_value_plain_int():
    assert _extract_numeric_value(100) == 100.0


def test_extract_numeric_value_plain_long():
    assert _extract_numeric_value(9_999_999_999) == 9_999_999_999.0


def test_extract_numeric_value_fastavro_dict_float():
    assert _extract_numeric_value({"float": 3.14}) == pytest.approx(3.14)


def test_extract_numeric_value_fastavro_dict_int():
    assert _extract_numeric_value({"int": 42}) == 42.0


def test_extract_numeric_value_fastavro_dict_double():
    assert _extract_numeric_value({"double": 1.23456789}) == pytest.approx(1.23456789)


def test_extract_numeric_value_null_returns_none():
    assert _extract_numeric_value(None) is None


def test_extract_numeric_value_bool_true():
    assert _extract_numeric_value(True) == 1.0


def test_extract_numeric_value_bool_false():
    assert _extract_numeric_value(False) == 0.0


def test_extract_numeric_value_numeric_string():
    assert _extract_numeric_value("23.5") == 23.5


def test_extract_numeric_value_non_numeric_string_returns_none():
    assert _extract_numeric_value("abc") is None


def test_extract_numeric_value_empty_string_returns_none():
    assert _extract_numeric_value("") is None


# ---------------------------------------------------------------------------
# _get_mapping
# ---------------------------------------------------------------------------

def test_get_mapping_wildcard_match():
    result = _get_mapping("any-device", "temperature", None)
    assert result is not None
    assert result["observed_property"] == "temperature"


def test_get_mapping_exact_device_preferred_over_wildcard():
    custom = {
        "*/temperature": {"thing_name": "Generic", "sensor_id": "generic", "observed_property": "temperature", "unit": "Cel"},
        "dev1/temperature": {"thing_name": "Specific", "sensor_id": "specific", "observed_property": "temperature", "unit": "K"},
    }
    result = _get_mapping("dev1", "temperature", custom)
    assert result is not None
    assert result["thing_name"] == "Specific"


def test_get_mapping_override_beats_default():
    override = {
        "*/temperature": {"thing_name": "Override Lab", "sensor_id": "override-sensor", "observed_property": "temperature", "unit": "Cel"},
    }
    result = _get_mapping("any-device", "temperature", override)
    assert result is not None
    assert result["thing_name"] == "Override Lab"


def test_get_mapping_unknown_measurement_returns_none():
    assert _get_mapping("dev", "unknown_measurement_xyz", None) is None


def test_get_mapping_no_override_uses_default():
    result = _get_mapping("dev", "humidity", None)
    assert result is not None
    assert result["observed_property"] == "humidity"


# ---------------------------------------------------------------------------
# avro_record_to_sensor_readings
# ---------------------------------------------------------------------------

def _make_record(**kwargs) -> dict:
    base = {
        "project_id": "proj1",
        "application_id": "app1",
        "device_id": "device-abc",
        "timestamp": 1_700_000_000_000,
        "measurements": [],
    }
    base.update(kwargs)
    return base


def test_avro_record_maps_temperature():
    record = _make_record(measurements=[
        {"measurement_id": "temperature", "value": 22.0, "unit": "Cel", "measurement_description": None}
    ])
    readings = avro_record_to_sensor_readings(record)
    assert len(readings) == 1
    r = readings[0]
    assert r.observed_property == "temperature"
    assert r.value == 22.0
    assert r.unit == "°C"
    assert r.thing_name == "TGV Office Lab"


def test_avro_record_stream_key_equals_measurement_id():
    record = _make_record(measurements=[
        {"measurement_id": "humidity", "value": 55.0, "unit": "%", "measurement_description": None}
    ])
    readings = avro_record_to_sensor_readings(record)
    assert readings[0].stream_key == "humidity"


def test_avro_record_device_eui_equals_device_id():
    record = _make_record(device_id="my-device-123", measurements=[
        {"measurement_id": "temperature", "value": 20.0, "unit": "Cel", "measurement_description": None}
    ])
    readings = avro_record_to_sensor_readings(record)
    assert readings[0].device_eui == "my-device-123"


def test_avro_record_location_is_tgv():
    record = _make_record(measurements=[
        {"measurement_id": "temperature", "value": 20.0, "unit": "Cel", "measurement_description": None}
    ])
    readings = avro_record_to_sensor_readings(record)
    assert readings[0].location == "tgv"


def test_avro_record_null_value_skipped():
    record = _make_record(measurements=[
        {"measurement_id": "temperature", "value": None, "unit": "Cel", "measurement_description": None}
    ])
    assert avro_record_to_sensor_readings(record) == []


def test_avro_record_unknown_measurement_id_skipped():
    record = _make_record(measurements=[
        {"measurement_id": "unknown_xyz", "value": 1.0, "unit": "1", "measurement_description": None}
    ])
    assert avro_record_to_sensor_readings(record) == []


def test_avro_record_canonical_unit_overrides_avro_unit():
    """Canonical unit always wins over an Avro-supplied unit string; the
    connector normalizes at the backend (per Geonovum discussion #24)."""
    record = _make_record(measurements=[
        {"measurement_id": "temperature", "value": 295.15, "unit": "K", "measurement_description": None}
    ])
    readings = avro_record_to_sensor_readings(record)
    assert readings[0].unit == "°C"


def test_avro_record_uses_canonical_unit_when_avro_unit_is_none():
    record = _make_record(measurements=[
        {"measurement_id": "temperature", "value": 22.0, "unit": None, "measurement_description": None}
    ])
    readings = avro_record_to_sensor_readings(record)
    assert readings[0].unit == "°C"


def test_avro_record_timestamp_ms_to_utc_datetime():
    # 1_700_000_000_000 ms = 2023-11-14T22:13:20Z
    record = _make_record(
        timestamp=1_700_000_000_000,
        measurements=[
            {"measurement_id": "temperature", "value": 20.0, "unit": "Cel", "measurement_description": None}
        ],
    )
    readings = avro_record_to_sensor_readings(record)
    ts = readings[0].timestamp
    assert ts.tzinfo is not None
    assert ts == datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)


def test_avro_record_multiple_measurements_in_one_record():
    record = _make_record(measurements=[
        {"measurement_id": "temperature", "value": 22.0, "unit": "Cel", "measurement_description": None},
        {"measurement_id": "humidity", "value": 60.0, "unit": "%", "measurement_description": None},
    ])
    readings = avro_record_to_sensor_readings(record)
    assert len(readings) == 2
    props = {r.observed_property for r in readings}
    assert props == {"temperature", "humidity"}


def test_avro_record_fastavro_dict_union_value():
    record = _make_record(measurements=[
        {"measurement_id": "temperature", "value": {"float": 19.5}, "unit": "Cel", "measurement_description": None}
    ])
    readings = avro_record_to_sensor_readings(record)
    assert readings[0].value == pytest.approx(19.5)


def test_avro_record_quality_is_good():
    record = _make_record(measurements=[
        {"measurement_id": "co2", "value": 420.0, "unit": "ppm", "measurement_description": None}
    ])
    readings = avro_record_to_sensor_readings(record)
    assert readings[0].quality == "good"


def test_avro_record_override_mapping_applied():
    """Override mapping controls thing/sensor routing; unit still comes from
    canonical (the ``unit`` field on the mapping is ignored)."""
    custom_mapping = {
        "*/temperature": {
            "thing_name": "Custom Thing",
            "sensor_id": "custom-sensor",
            "observed_property": "temperature",
            "unit": "K",  # ignored — canonical wins
        }
    }
    record = _make_record(measurements=[
        {"measurement_id": "temperature", "value": 295.0, "unit": None, "measurement_description": None}
    ])
    readings = avro_record_to_sensor_readings(record, override_mapping=custom_mapping)
    assert readings[0].thing_name == "Custom Thing"
    assert readings[0].unit == "°C"


# ---------------------------------------------------------------------------
# avro_batch_to_sensor_readings
# ---------------------------------------------------------------------------

def test_avro_batch_aggregates_multiple_records():
    records = [
        _make_record(measurements=[
            {"measurement_id": "temperature", "value": 21.0, "unit": "Cel", "measurement_description": None}
        ]),
        _make_record(measurements=[
            {"measurement_id": "humidity", "value": 50.0, "unit": "%", "measurement_description": None}
        ]),
    ]
    readings = avro_batch_to_sensor_readings(records, override_mapping=_DEFAULT_DEVICE_MAPPING)
    assert len(readings) == 2


def test_avro_batch_bad_record_does_not_crash_batch():
    records = [
        None,  # will cause AttributeError on .get()
        _make_record(measurements=[
            {"measurement_id": "temperature", "value": 22.0, "unit": "Cel", "measurement_description": None}
        ]),
    ]
    readings = avro_batch_to_sensor_readings(records, override_mapping=_DEFAULT_DEVICE_MAPPING)
    # Bad record is skipped; good record is mapped
    assert len(readings) == 1


def test_avro_batch_empty_list_returns_empty():
    assert avro_batch_to_sensor_readings([]) == []


def test_avro_batch_override_mapping_propagated():
    custom_mapping = {
        "*/temperature": {
            "thing_name": "Batch Thing",
            "sensor_id": "batch-sensor",
            "observed_property": "temperature",
            "unit": "Cel",
        }
    }
    records = [
        _make_record(measurements=[
            {"measurement_id": "temperature", "value": 20.0, "unit": "Cel", "measurement_description": None}
        ])
    ]
    readings = avro_batch_to_sensor_readings(records, override_mapping=custom_mapping)
    assert readings[0].thing_name == "Batch Thing"
