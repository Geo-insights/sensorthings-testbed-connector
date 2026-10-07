"""Meet je Stad urban climate and soil moisture sensor definitions and response mapping.

Meet je Stad is a citizen-science network of LoRaWAN sensors across the Netherlands.
Sensors report temperature, humidity, particulate matter (PM2.5/PM10), and soil
conditions (moisture, temperature).

API:
  GET ?type=sensors&format=json        → array of sensor metadata (id, city, lat, lon)
  GET ?type=data&format=json&ids=X,Y   → array of observations with named fields
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading
from app.sources.normalizers import MeetJeStadNormalizer

logger = logging.getLogger(__name__)

# Source-specific descriptions per canonical observed property.
_DESCRIPTIONS: dict[str, str] = {
    "temperature": "Outdoor air temperature at Meet je Stad sensor location.",
    "humidity": "Relative humidity at Meet je Stad sensor location.",
    "pm2_5": "Particulate matter concentration (PM2.5) in ambient air.",
    "pm10": "Particulate matter concentration (PM10) in ambient air.",
    "soil_moisture": "Volumetric soil moisture content at sensor location.",
    "soil_temperature": "Soil temperature at sensor location.",
}


def build_entity_set(
    sensor_id: str,
    city: str,
    lat: float,
    lon: float,
    properties: list[str] | None = None,
) -> dict[str, Any]:
    """Build a CLIMATE_ADAPTATION_ENTITY_SETS-format dict for one Meet je Stad sensor.

    Thing name: "MeetJeStad {city} {sensor_id}"
    Site key: city name lowercased.
    """
    props = properties or list(_DESCRIPTIONS.keys())
    site_key = city.lower().replace(" ", "_")
    thing_name = f"MeetJeStad {city} {sensor_id}"
    return {
        "site_key": site_key,
        "site_name": city,
        "thing": {
            "name": thing_name,
            "description": f"Meet je Stad LoRaWAN sensor {sensor_id} in {city}.",
            "properties": {
                "site": site_key,
                "source": "meet_je_stad",
                "mjs_sensor_id": str(sensor_id),
                "city": city,
                "network": "MeetJeStad",
            },
        },
        "location": {
            "name": f"MeetJeStad {city} {sensor_id} location",
            "description": f"Meet je Stad measurement point {sensor_id} in {city}.",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {},
        },
        "sensors": [
            {
                "sensor_id": f"mjs-{sensor_id}",
                "name": f"MeetJeStad {sensor_id} sensor",
                "description": f"Meet je Stad LoRaWAN sensor ({sensor_id}).",
                "encodingType": "application/json",
                "metadata": "https://meetjestad.net",
                "observed_properties": props,
                "properties": {},
            }
        ],
        "observed_properties": {k: v for k, v in _DESCRIPTIONS.items() if k in props},
    }


def parse_data_readings(
    data_entries: list[dict[str, Any]],
    sensor_id: str,
    sensor_name: str,
) -> list[SensorReading]:
    """Parse Meet je Stad data API entries into SensorReadings.

    The API returns one entry per timestamp. The field "pm2.5" (dot notation)
    is mapped to the normalizer field "pm2_5" (underscore).

    Uses :class:`MeetJeStadNormalizer` to map fields to canonical observed
    properties with canonical units.
    """
    readings: list[SensorReading] = []
    thing_name = f"MeetJeStad {sensor_name}"

    for entry in data_entries:
        if not isinstance(entry, dict):
            continue

        # Parse timestamp
        ts_raw = entry.get("timestamp", entry.get("time", ""))
        if ts_raw:
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(UTC)
        else:
            ts = datetime.now(UTC)

        # Build the normalizer payload — translate "pm2.5" dot-key to "pm2_5"
        payload: dict[str, Any] = {}
        for key, value in entry.items():
            normalizer_key = key.replace(".", "_").replace("-", "_")
            payload[normalizer_key] = value

        normalizer = MeetJeStadNormalizer.model_validate(payload)
        entry_readings = normalizer.to_readings(
            sensor_id=f"mjs-{sensor_id}",
            sensor_name=f"MeetJeStad {sensor_id} sensor",
            thing_name=thing_name,
            timestamp=ts,
            location=sensor_name,
            quality="good",
        )
        readings.extend(entry_readings)

    return readings
