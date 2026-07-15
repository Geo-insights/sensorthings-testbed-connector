from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.models import SensorReading

logger = logging.getLogger(__name__)

# Default mapping: key format is "<device_id>/<measurement_id>" or "*/<measurement_id>" (wildcard device).
# These keys must match the actual measurement_id values arriving on the Kafka topic.
# Override at runtime via KAFKA_TGV_DEVICE_MAPPING_JSON env var without a code change.
_DEFAULT_DEVICE_MAPPING: dict[str, dict[str, str]] = {
    "*/temperature": {
        "thing_name": "TGV Office Lab",
        "sensor_id": "tgv-officelab-climate",
        "observed_property": "temperature",
        "observed_property_name": "Air temperature",
        "unit": "Cel",
    },
    "*/humidity": {
        "thing_name": "TGV Office Lab",
        "sensor_id": "tgv-officelab-climate",
        "observed_property": "humidity",
        "observed_property_name": "Relative humidity",
        "unit": "%",
    },
    "*/co2": {
        "thing_name": "TGV Office Lab",
        "sensor_id": "tgv-officelab-climate",
        "observed_property": "co2",
        "observed_property_name": "CO2 concentration",
        "unit": "ppm",
    },
    "*/pressure": {
        "thing_name": "TGV Office Lab",
        "sensor_id": "tgv-officelab-climate",
        "observed_property": "pressure",
        "observed_property_name": "Air pressure",
        "unit": "hPa",
    },
}


def _extract_numeric_value(raw_value: Any) -> float | None:
    """
    Extract a float from an Avro union value.

    The GreenVillageRecord Avro schema defines value as:
      ["null", "boolean", "int", "long", "float", "double", "string"]

    fastavro may represent union values as {"type_name": actual_value} dicts.
    Returns None for values that cannot be cast to float.
    """
    if raw_value is None:
        return None
    # fastavro dict union representation: {"float": 3.14} or {"int": 5}
    if isinstance(raw_value, dict) and len(raw_value) == 1:
        raw_value = next(iter(raw_value.values()))
    if isinstance(raw_value, bool):
        return float(raw_value)
    if isinstance(raw_value, (int, float)):
        return float(raw_value)
    if isinstance(raw_value, str):
        try:
            return float(raw_value)
        except ValueError:
            return None
    return None


def _get_mapping(
    device_id: str,
    measurement_id: str,
    override_mapping: dict[str, dict[str, str]] | None,
) -> dict[str, str] | None:
    """
    Look up the field mapping for a (device_id, measurement_id) pair.

    Resolution order:
      1. override_mapping (from KAFKA_TGV_DEVICE_MAPPING_JSON) if provided
      2. _DEFAULT_DEVICE_MAPPING

    Within each mapping, tries:
      a. "<device_id>/<measurement_id>"  — exact device match
      b. "*/<measurement_id>"            — wildcard device match
    """
    active = override_mapping if override_mapping is not None else _DEFAULT_DEVICE_MAPPING
    exact_key = f"{device_id}/{measurement_id}"
    wildcard_key = f"*/{measurement_id}"
    return active.get(exact_key) or active.get(wildcard_key)


def avro_record_to_sensor_readings(
    record: dict[str, Any],
    override_mapping: dict[str, dict[str, str]] | None = None,
) -> list[SensorReading]:
    """
    Convert a single deserialized GreenVillageRecord dict to SensorReadings.

    One SensorReading is produced per measurement entry that:
    - Has a resolvable numeric value
    - Has a matching entry in the device mapping

    The measurement_id is used as stream_key so the FROST client can resolve
    the correct Datastream via OData filter on properties/streamKey.
    """
    device_id: str = record.get("device_id", "")
    timestamp_ms: int = record.get("timestamp", 0)
    measurements: list[dict[str, Any]] = record.get("measurements", [])

    try:
        ts = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=UTC)
    except (OSError, ValueError, OverflowError):
        ts = datetime.now(UTC)

    readings: list[SensorReading] = []

    for m in measurements:
        measurement_id: str = m.get("measurement_id", "")
        raw_value = m.get("value")
        unit_from_avro: str | None = m.get("unit") or None

        numeric_value = _extract_numeric_value(raw_value)
        if numeric_value is None:
            logger.debug(
                "Skipping non-numeric measurement: %s/%s = %r",
                device_id, measurement_id, raw_value,
            )
            continue

        mapping = _get_mapping(device_id, measurement_id, override_mapping)
        if mapping is None:
            logger.debug("No mapping for %s/%s — skipping", device_id, measurement_id)
            continue

        readings.append(
            SensorReading(
                sensor_id=mapping["sensor_id"],
                sensor_name=mapping.get("sensor_name", mapping["sensor_id"]),
                observed_property=mapping["observed_property"],
                unit=unit_from_avro or mapping.get("unit", "1"),
                value=numeric_value,
                timestamp=ts,
                quality="good",
                location="tgv",
                thing_name=mapping["thing_name"],
                device_eui=device_id,
                stream_key=measurement_id,
                observed_property_name=mapping.get("observed_property_name"),
            )
        )

    return readings


def avro_batch_to_sensor_readings(
    records: list[dict[str, Any]],
    override_mapping: dict[str, dict[str, str]] | None = None,
) -> list[SensorReading]:
    """
    Convert a batch of deserialized GreenVillageRecord dicts to SensorReadings.

    Uses KAFKA_TGV_DEVICE_MAPPING_JSON from settings if no override is provided.
    Exceptions per record are logged and skipped without crashing the batch.
    """
    if override_mapping is None and settings.kafka_tgv_device_mapping:
        override_mapping = settings.kafka_tgv_device_mapping

    result: list[SensorReading] = []
    for record in records:
        try:
            result.extend(avro_record_to_sensor_readings(record, override_mapping))
        except Exception as exc:
            from app.exceptions import PayloadTransformError

            logger.warning("Failed to map Avro record: %s", exc, exc_info=True)
            if not isinstance(exc, PayloadTransformError):
                logger.debug("Wrapping as PayloadTransformError for traceability")
    return result
