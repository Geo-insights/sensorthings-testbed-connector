"""Ohnics air quality sensor entity definitions and response mapping.

Ohnics sensors in Delft are identified by a "de-" prefix in the Name field.
The 5min.json endpoint returns the latest 5-minute readings for all sensors.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading

# CF Convention definitions for air quality observed properties
OBSERVED_PROPERTIES: dict[str, dict[str, str]] = {
    "pm25": {
        "name": "PM2.5 concentration",
        "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#mass_concentration_of_pm2p5_ambient_aerosol_particles_in_air",
        "description": "Particulate matter concentration (PM2.5) in ambient air.",
        "unit": "ug/m3",
    },
    "air_temperature": {
        "name": "Air temperature",
        "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature",
        "description": "Outdoor air temperature at sensor location.",
        "unit": "Cel",
    },
}

# Mapping from Ohnics JSON field names to our observed property keys.
# Confirmed from live API: P2 = PM2.5, T = temperature.
FIELD_TO_PROPERTY: dict[str, str] = {
    "P2": "pm25",
    "T": "air_temperature",
}


def build_entity_set(
    sensor_name: str,
    lat: float,
    lon: float,
    measured_properties: list[str] | None = None,
) -> dict[str, Any]:
    """Build a CLIMATE_ADAPTATION_ENTITY_SETS-format dict for one Ohnics sensor."""
    props = measured_properties or ["pm25", "air_temperature"]
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
        "observed_properties": {k: v for k, v in OBSERVED_PROPERTIES.items() if k in props},
    }


def parse_sensor_readings(sensor_data: dict[str, Any]) -> list[SensorReading]:
    """Parse a single Ohnics sensor JSON object into SensorReadings.

    The exact field names depend on the API response shape; this function tries
    several known variants for sensor name, coordinates, timestamp, and values.
    """
    # Extract sensor name
    name = str(sensor_data.get("Name", sensor_data.get("name", "")))
    if not name:
        return []

    # Extract coordinates (optional — used for entity registration, not readings)
    lat = sensor_data.get("Lat", sensor_data.get("lat", 0.0))
    lon = sensor_data.get("Long", sensor_data.get("Lon", sensor_data.get("lon", 0.0)))

    # Extract timestamp
    ts_raw = sensor_data.get("Timestamp", sensor_data.get("timestamp", sensor_data.get("Time", "")))
    if ts_raw:
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.now(UTC)
    else:
        ts = datetime.now(UTC)

    readings: list[SensorReading] = []
    for field_key, prop_key in FIELD_TO_PROPERTY.items():
        value = sensor_data.get(field_key)
        if value is None:
            continue
        try:
            float_val = float(value)
        except (ValueError, TypeError):
            continue

        prop_def = OBSERVED_PROPERTIES[prop_key]
        readings.append(
            SensorReading(
                sensor_id=f"ohnics-{name}",
                sensor_name=f"Ohnics {name} sensor",
                observed_property=prop_key,
                unit=prop_def["unit"],
                value=float_val,
                timestamp=ts,
                quality="good",
                location="delft",
                thing_name=f"Ohnics {name}",
                observed_property_name=prop_def["name"],
            )
        )
    return readings
