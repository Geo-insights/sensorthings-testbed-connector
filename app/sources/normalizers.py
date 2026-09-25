"""Concrete Normalizer subclasses for each sensor source.

Each normalizer declares Pydantic fields matching the vendor payload shape and
a NAME_TRANSFORM mapping vendor field names to CanonicalDatastream members.

TGV uses a different pattern: each Avro record contains a dynamic
``measurement_id`` resolved through a lookup table, so ``TGVMeasurementNormalizer``
wraps a single-value measurement rather than a fixed-field struct.

Bridge loads pre-formed ``SensorReading`` objects from JSON -- no vendor fields
to normalize, so no normalizer is needed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from app.models import SensorReading
from app.sta.canonical import CanonicalDatastream, resolve
from app.sta.normalizer import Normalizer


class OhnicsNormalizer(Normalizer):
    """Ohnics air quality sensors: P2 = PM2.5, T = temperature."""

    P2: float | None = None
    T: float | None = None

    NAME_TRANSFORM: ClassVar[dict[str, CanonicalDatastream]] = {
        "P2": CanonicalDatastream.PM2_5,
        "T": CanonicalDatastream.AIR_TEMPERATURE,
    }


class LevellogNormalizer(Normalizer):
    """Levellog groundwater sensors: water_level only."""

    water_level: float | None = None

    NAME_TRANSFORM: ClassVar[dict[str, CanonicalDatastream]] = {
        "water_level": CanonicalDatastream.WATER_LEVEL,
    }


class TGVMeasurementNormalizer:
    """Normalize a single TGV Avro measurement via the device mapping table.

    TGV differs from fixed-field normalizers: each Avro record contains a list
    of ``{measurement_id, value, unit}`` entries. The ``measurement_id`` is
    resolved through ``_DEFAULT_DEVICE_MAPPING`` (or an override) to find the
    canonical observed property, thing, and sensor.

    This class wraps that resolution + canonical enforcement so TGV follows the
    same normalize-then-emit pattern as Ohnics/Levellog, even though the lookup
    is dynamic rather than declarative.
    """

    def __init__(self, mapping: dict[str, dict[str, str]]) -> None:
        self._mapping = mapping

    def normalize_measurement(
        self,
        device_id: str,
        measurement_id: str,
        raw_value: Any,
        timestamp: datetime,
        unit_from_avro: str | None = None,
    ) -> SensorReading | None:
        """Resolve one measurement entry to a SensorReading, or None if unmapped."""
        from app.sources.tgv_kafka_mapping import _extract_numeric_value

        numeric_value = _extract_numeric_value(raw_value)
        if numeric_value is None:
            return None

        exact_key = f"{device_id}/{measurement_id}"
        wildcard_key = f"*/{measurement_id}"
        entry = self._mapping.get(exact_key) or self._mapping.get(wildcard_key)
        if entry is None:
            return None

        canonical = resolve(entry["observed_property"])
        if canonical is None:
            return None

        meta = canonical.meta
        return SensorReading(
            sensor_id=entry["sensor_id"],
            sensor_name=entry.get("sensor_name", entry["sensor_id"]),
            observed_property=canonical.value,
            unit=meta.unit,
            value=numeric_value,
            timestamp=timestamp,
            quality="good",
            location="tgv",
            thing_name=entry["thing_name"],
            device_eui=device_id,
            stream_key=measurement_id,
            observed_property_name=meta.display_name,
        )
