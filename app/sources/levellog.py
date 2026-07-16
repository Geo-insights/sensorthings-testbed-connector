"""Levellog groundwater level sensor entity definitions and response mapping.

Data is fetched from the CARS Online API (carsonline.eu) using OAuth2 client credentials.
The API uses OData conventions with /api/{Type}/{installationId} for time-series data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading

OBSERVED_PROPERTIES: dict[str, dict[str, str]] = {
    "water_level": {
        "name": "Groundwater level",
        "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#water_surface_height_above_reference_datum",
        "description": "Groundwater level relative to reference datum.",
        "unit": "m",
    },
}


def build_entity_set(
    installation_name: str,
    installation_id: str,
    lat: float = 51.9966,
    lon: float = 4.3776,
) -> dict[str, Any]:
    """Build a CLIMATE_ADAPTATION_ENTITY_SETS-format dict for one Levellog installation."""
    return {
        "site_key": "tgv",
        "site_name": "The Green Village",
        "thing": {
            "name": f"Levellog {installation_name}",
            "description": f"Groundwater level monitoring at The Green Village ({installation_name}).",
            "properties": {
                "site": "tgv",
                "campus": "TU Delft",
                "source": "levellog",
                "api": "cars-online",
                "installation_id": installation_id,
            },
        },
        "location": {
            "name": f"Levellog {installation_name} location",
            "description": f"Groundwater sensor location at The Green Village ({installation_name}).",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {},
        },
        "sensors": [
            {
                "sensor_id": f"tgv-levellog-{installation_id[:8]}",
                "name": f"Levellog {installation_name} sensor",
                "description": f"Groundwater level sensor connected via CARS Online API ({installation_name}).",
                "encodingType": "application/json",
                "metadata": "https://cars-api.carsonline.eu",
                "observed_properties": ["water_level"],
                "properties": {},
            }
        ],
        "observed_properties": OBSERVED_PROPERTIES,
    }


def parse_logdata_readings(
    raw_data: dict[str, Any],
    installation_id: str,
    installation_name: str,
) -> list[SensorReading]:
    """Parse CARS normalizedlogdata response into SensorReadings.

    The CARS API returns time-series data in various formats. This function
    handles the common structure and extracts water level values.
    """
    readings: list[SensorReading] = []

    # The normalized logdata response may contain a list of data points
    # with timestamps and variable values. Exact structure depends on the
    # installation type and variables configured.
    data_points = raw_data if isinstance(raw_data, list) else raw_data.get("data", raw_data.get("value", []))
    if not isinstance(data_points, list):
        return readings

    prop_def = OBSERVED_PROPERTIES["water_level"]
    for point in data_points:
        if not isinstance(point, dict):
            continue

        # Extract timestamp
        ts_raw = point.get("Timestamp", point.get("timestamp", point.get("DateTime", "")))
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except ValueError:
            continue

        # Extract value — try common field names for water level
        value = None
        for key in ("Value", "value", "Level", "level", "WaterLevel", "NormalizedValue"):
            if key in point:
                try:
                    value = float(point[key])
                    break
                except (ValueError, TypeError):
                    continue

        if value is None:
            continue

        readings.append(
            SensorReading(
                sensor_id=f"tgv-levellog-{installation_id[:8]}",
                sensor_name=f"Levellog {installation_name} sensor",
                observed_property="water_level",
                unit=prop_def["unit"],
                value=value,
                timestamp=ts,
                quality="good",
                location="tgv",
                thing_name=f"Levellog {installation_name}",
                observed_property_name=prop_def["name"],
            )
        )

    return readings
