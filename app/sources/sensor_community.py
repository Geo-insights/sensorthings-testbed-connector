"""Sensor.Community citizen air quality + noise sensor entity definitions and response mapping.

Sensor.Community (formerly Luftdaten.info) operates an open network of low-cost
environmental sensors maintained by citizens across Europe.  Each sensor station
reports one or more ``sensordatavalues`` entries.  Multiple sensor types can
co-exist at the same physical location (identified by ``location.id``).

Supported sensor types:
- SDS011 — particulate matter (P1→PM10, P2→PM2.5)
- BME280 / BMP280 — temperature, humidity, pressure
- DNMS — noise levels (noise_LAeq, noise_LAmin, noise_LAmax)

API endpoint: https://data.sensor.community/airrohr/v1/filter/country=NL
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading
from app.sta.canonical import resolve

logger = logging.getLogger(__name__)

# Source-specific descriptions per canonical observed property. Unit + display
# name + CF definition all come from canonical.py at read time.
_DESCRIPTIONS: dict[str, str] = {
    "pm10": "Particulate matter concentration (PM10) in ambient air.",
    "pm2_5": "Particulate matter concentration (PM2.5) in ambient air.",
    "temperature": "Outdoor air temperature at sensor location.",
    "humidity": "Relative humidity at sensor location.",
    "pressure": "Atmospheric air pressure at sensor location.",
    "noise_laeq": "Equivalent continuous A-weighted sound pressure level (LAeq).",
    "noise_lamin": "Minimum A-weighted sound pressure level (LAmin).",
    "noise_lamax": "Maximum A-weighted sound pressure level (LAmax).",
}


def build_entity_set(
    location_id: int | str,
    lat: float,
    lon: float,
    observed_properties: list[str] | None = None,
) -> dict[str, Any]:
    """Build a CLIMATE_ADAPTATION_ENTITY_SETS-format dict for one Sensor.Community location.

    One Thing per location_id; sensors at the same location are co-located.
    """
    props = observed_properties or ["pm10", "pm2_5"]
    loc_str = str(location_id)
    return {
        "site_key": f"sensor_community_{loc_str}",
        "site_name": f"SensorCommunity {loc_str}",
        "thing": {
            "name": f"SensorCommunity {loc_str}",
            "description": f"Sensor.Community citizen sensor station at location {loc_str}.",
            "properties": {
                "site": f"sensor_community_{loc_str}",
                "source": "sensor_community",
                "location_id": loc_str,
                "network": "Sensor.Community",
            },
        },
        "location": {
            "name": f"SensorCommunity {loc_str} location",
            "description": f"Sensor.Community measurement point at location {loc_str}.",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {},
        },
        "sensors": [
            {
                "sensor_id": f"sensor_community-{loc_str}",
                "name": f"SensorCommunity {loc_str} sensor",
                "description": f"Sensor.Community citizen sensor station ({loc_str}).",
                "encodingType": "application/json",
                "metadata": "https://sensor.community",
                "observed_properties": props,
                "properties": {},
            }
        ],
        "observed_properties": {k: v for k, v in _DESCRIPTIONS.items() if k in props},
    }


def parse_sensor_data(sensor_entries: list[dict[str, Any]]) -> list[SensorReading]:
    """Parse the full Sensor.Community NL-filter API response into SensorReadings.

    Each entry in the array represents one sensor at a location.  Multiple
    sensor types (SDS011, BME280, DNMS) can share a location and are processed
    as separate entries in the response array.

    Unknown ``value_type`` values are silently skipped.
    """
    readings: list[SensorReading] = []

    for entry in sensor_entries:
        if not isinstance(entry, dict):
            continue

        location = entry.get("location", {})
        if not isinstance(location, dict):
            continue

        location_id = location.get("id")
        if location_id is None:
            continue

        sensor_info = entry.get("sensor", {}) or {}
        sensor_type_info = sensor_info.get("sensor_type", {}) or {}
        sensor_type_name = str(sensor_type_info.get("name", "unknown"))

        ts_raw = str(entry.get("timestamp", "")).strip()
        if ts_raw:
            try:
                # API returns "2026-10-07 12:00:00" (space-separated, UTC)
                ts = datetime.fromisoformat(ts_raw.replace(" ", "T").replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
            except ValueError:
                ts = datetime.now(UTC)
        else:
            ts = datetime.now(UTC)

        sensor_id = f"sensor_community-{location_id}"
        sensor_name = f"SensorCommunity {location_id} {sensor_type_name} sensor"
        thing_name = f"SensorCommunity {location_id}"

        for sdv in entry.get("sensordatavalues", []):
            if not isinstance(sdv, dict):
                continue

            value_type = str(sdv.get("value_type", "")).strip()
            raw_value = sdv.get("value")
            if not value_type or raw_value is None:
                continue

            canonical = resolve(value_type)
            if canonical is None:
                logger.debug(
                    "sensor_community: unknown value_type %r at location %s — skipped",
                    value_type,
                    location_id,
                )
                continue

            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                logger.debug(
                    "sensor_community: non-numeric value %r for %s at location %s — skipped",
                    raw_value,
                    value_type,
                    location_id,
                )
                continue

            meta = canonical.meta
            readings.append(
                SensorReading(
                    sensor_id=sensor_id,
                    sensor_name=sensor_name,
                    observed_property=canonical.value,
                    observed_property_name=meta.display_name,
                    unit=meta.unit,
                    value=value,
                    timestamp=ts,
                    location=str(location_id),
                    thing_name=thing_name,
                    quality="good",
                )
            )

    return readings
