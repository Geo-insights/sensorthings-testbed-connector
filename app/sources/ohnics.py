"""Ohnics air quality sensor entity definitions and response mapping.

Ohnics sensors in Delft are identified by a "de-" prefix in the Name field.
The 5min.json endpoint returns the latest 5-minute readings for all sensors.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading
from app.sources.normalizers import OhnicsNormalizer

logger = logging.getLogger(__name__)

# Source-specific descriptions per canonical observed property. Unit + display
# name + CF definition all come from canonical.py at read time.
_DESCRIPTIONS: dict[str, str] = {
    "pm2_5": "Particulate matter concentration (PM2.5) in ambient air.",
    "air_temperature": "Outdoor air temperature at sensor location.",
}

# Mapping from Ohnics JSON field names to canonical observed property keys.
# Retained for backward compatibility (used by external callers).
FIELD_TO_PROPERTY: dict[str, str] = {
    "P2": "pm2_5",
    "T": "air_temperature",
}


def build_entity_set(
    sensor_name: str,
    lat: float,
    lon: float,
    measured_properties: list[str] | None = None,
) -> dict[str, Any]:
    """Build a CLIMATE_ADAPTATION_ENTITY_SETS-format dict for one Ohnics sensor."""
    props = measured_properties or ["pm2_5", "air_temperature"]
    return {
        "site_key": "delft",
        "site_name": "Delft",
        "thing": {
            "name": f"Ohnics {sensor_name}",
            "description": f"Ohnics outdoor air quality sensor in Delft ({sensor_name}).",
            "properties": {
                "site": "delft",
                "source": "ohnics",
                "ohnics_name": sensor_name,
                "network": "SamenMeten",
            },
        },
        "location": {
            "name": f"Ohnics {sensor_name} location",
            "description": f"Outdoor air quality measurement point {sensor_name} in Delft.",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {},
        },
        "sensors": [
            {
                "sensor_id": f"ohnics-{sensor_name}",
                "name": f"Ohnics {sensor_name} sensor",
                "description": f"Ohnics particulate matter sensor ({sensor_name}).",
                "encodingType": "application/json",
                "metadata": "https://ohnics.online",
                "observed_properties": props,
                "properties": {},
            }
        ],
        "observed_properties": {k: v for k, v in _DESCRIPTIONS.items() if k in props},
    }


def parse_sensor_readings(sensor_data: dict[str, Any]) -> list[SensorReading]:
    """Parse a single Ohnics sensor JSON object into SensorReadings.

    Uses :class:`OhnicsNormalizer` to map vendor fields (P2, T) to canonical
    observed properties. The exact field names depend on the API response shape;
    this function handles several known variants for sensor name and timestamp.
    """
    # Extract sensor name
    name = str(sensor_data.get("Name", sensor_data.get("name", "")))
    if not name:
        return []

    # Extract coordinates (optional — used for entity registration, not readings)
    _lat = sensor_data.get("Lat", sensor_data.get("lat", 0.0))
    _lon = sensor_data.get("Long", sensor_data.get("Lon", sensor_data.get("lon", 0.0)))

    # Extract timestamp
    ts_raw = sensor_data.get("Timestamp", sensor_data.get("timestamp", sensor_data.get("Time", "")))
    if ts_raw:
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.now(UTC)
    else:
        ts = datetime.now(UTC)

    # Build normalizer from vendor fields — unknown keys silently dropped
    normalizer = OhnicsNormalizer.model_validate(sensor_data)
    return normalizer.to_readings(
        sensor_id=f"ohnics-{name}",
        sensor_name=f"Ohnics {name} sensor",
        thing_name=f"Ohnics {name}",
        timestamp=ts,
        location="delft",
        quality="good",
    )
