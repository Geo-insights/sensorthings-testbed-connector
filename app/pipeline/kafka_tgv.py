"""Concrete pipeline components for TGV Kafka Avro data source."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading
from app.pipeline.base import Decapsulator, Deserializer, Parser
from app.sta.canonical import resolve

logger = logging.getLogger("connector.debug")


class AvroUnionDeserializer(Deserializer):
    """Unwrap fastavro union dicts like ``{"float": 3.14}`` to plain values."""

    def deserialize(self, record: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in record.items():
            if isinstance(value, dict) and len(value) == 1:
                value = next(iter(value.values()))
            out[key] = value
        return out


class TGVDecapsulator(Decapsulator):
    """Extract individual measurement dicts from a GreenVillageRecord envelope."""

    def decapsulate(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        device_id = record.get("device_id", "")
        timestamp = record.get("timestamp", 0)
        measurements: list[dict[str, Any]] = record.get("measurements", [])
        return [
            {**m, "device_id": device_id, "timestamp": timestamp}
            for m in measurements
        ]


class TGVMeasurementParser(Parser):
    """Parse a single TGV measurement dict into a SensorReading.

    Requires a device mapping dict to resolve sensor/thing metadata.
    """

    def __init__(self, device_mapping: dict[str, dict[str, str]]) -> None:
        self._mapping = device_mapping

    def parse(self, raw: dict[str, Any]) -> SensorReading:
        device_id = raw.get("device_id", "")
        measurement_id = raw.get("measurement_id", "")
        timestamp_ms = raw.get("timestamp", 0)

        # Resolve mapping
        exact_key = f"{device_id}/{measurement_id}"
        wildcard_key = f"*/{measurement_id}"
        mapping = self._mapping.get(exact_key) or self._mapping.get(wildcard_key)
        if mapping is None:
            raise ValueError(f"No mapping for {device_id}/{measurement_id}")

        # Extract numeric value
        raw_value = raw.get("value")
        if isinstance(raw_value, dict) and len(raw_value) == 1:
            raw_value = next(iter(raw_value.values()))
        if raw_value is None or isinstance(raw_value, bool):
            value = float(raw_value) if raw_value is not None else 0.0
        elif isinstance(raw_value, (int, float)):
            value = float(raw_value)
        elif isinstance(raw_value, str):
            value = float(raw_value)
        else:
            raise ValueError(f"Cannot convert {raw_value!r} to float")

        try:
            ts = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=UTC)
        except (OSError, ValueError, OverflowError):
            ts = datetime.now(UTC)

        canonical = resolve(mapping["observed_property"])
        if canonical is None:
            raise ValueError(
                f"Mapping for {device_id}/{measurement_id} uses non-canonical "
                f"observed_property {mapping['observed_property']!r}"
            )
        meta = canonical.meta
        unit_from_avro = raw.get("unit") or None
        if unit_from_avro and unit_from_avro != meta.unit:
            logger.info(
                "Avro unit %r for %s/%s disagrees with canonical %r — using canonical",
                unit_from_avro, device_id, measurement_id, meta.unit,
            )

        return SensorReading(
            sensor_id=mapping["sensor_id"],
            sensor_name=mapping.get("sensor_name", mapping["sensor_id"]),
            observed_property=canonical.value,
            unit=meta.unit,
            value=value,
            timestamp=ts,
            quality="good",
            location="tgv",
            thing_name=mapping["thing_name"],
            device_eui=device_id,
            stream_key=measurement_id,
            observed_property_name=meta.display_name,
        )
