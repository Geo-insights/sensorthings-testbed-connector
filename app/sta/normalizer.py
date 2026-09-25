"""Formal Normalizer pattern for source-specific field normalization.

Subclasses declare NAME_TRANSFORM (vendor field -> CanonicalDatastream) and
optional TRANSFORM (per-field value conversion callables). The base class
validates transforms against the canonical enum and produces SensorReadings
with canonical names, units, and display names.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, model_validator

from app.models import SensorReading
from app.sta.canonical import CanonicalDatastream


class Normalizer(BaseModel):
    """Base class for source-specific field normalization.

    Subclasses declare:
    - NAME_TRANSFORM: maps vendor field names to CanonicalDatastream enum values
    - TRANSFORM: optional per-field value conversion callables
    - Pydantic fields matching the vendor payload shape (for validation)
    """

    NAME_TRANSFORM: ClassVar[dict[str, CanonicalDatastream]]
    TRANSFORM: ClassVar[dict[str, Callable[..., Any]]] = {}

    model_config = {"extra": "ignore"}  # silently drop unknown vendor fields

    @model_validator(mode="after")
    def _validate_transforms(self) -> Normalizer:
        """Ensure all NAME_TRANSFORM values are valid CanonicalDatastream members."""
        canonical_values = {m.value for m in CanonicalDatastream}
        invalid = {ds.value for ds in self.NAME_TRANSFORM.values()} - canonical_values
        if invalid:
            raise ValueError(f"NAME_TRANSFORM maps to non-canonical names: {invalid}")
        return self

    def to_readings(
        self,
        sensor_id: str,
        sensor_name: str,
        thing_name: str,
        timestamp: datetime,
        location: str = "",
        quality: str = "good",
        device_eui: str | None = None,
        stream_key: str | None = None,
    ) -> list[SensorReading]:
        """Apply transforms, map vendor names to canonical, output SensorReadings."""
        readings: list[SensorReading] = []

        for vendor_field, canonical in self.NAME_TRANSFORM.items():
            value = getattr(self, vendor_field, None)
            if value is None:
                continue

            # Apply value transform if defined
            if vendor_field in self.TRANSFORM:
                value = self.TRANSFORM[vendor_field](value)

            try:
                float_val = float(value)
            except (ValueError, TypeError):
                continue

            meta = canonical.meta
            readings.append(
                SensorReading(
                    sensor_id=sensor_id,
                    sensor_name=sensor_name,
                    observed_property=canonical.value,
                    unit=meta.unit,
                    value=float_val,
                    timestamp=timestamp,
                    quality=quality,
                    location=location,
                    thing_name=thing_name,
                    observed_property_name=meta.display_name,
                    device_eui=device_eui,
                    stream_key=stream_key,
                )
            )

        return readings
