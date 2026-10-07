"""Luchtmeetnet RIVM air quality sensor entity definitions and response mapping.

Luchtmeetnet is the Dutch national air quality monitoring network operated by
RIVM. Stations measure NO2, O3, PM10, PM2.5, SO2, CO, NH3, and benzene (C6H6).
API: https://api.luchtmeetnet.nl/open_api
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading
from app.sta.canonical import resolve

logger = logging.getLogger(__name__)

# Luchtmeetnet formula strings to try discovering per station.
# The API also accepts comma-separated formulas in a single request.
SUPPORTED_FORMULAS = ["NO2", "O3", "PM10", "PM25", "SO2", "CO", "NH3", "C6H6"]

# Source-specific descriptions per canonical observed property.
_DESCRIPTIONS: dict[str, str] = {
    "no2": "Nitrogen dioxide concentration in ambient air.",
    "o3": "Ozone concentration in ambient air.",
    "pm10": "Particulate matter concentration (PM10) in ambient air.",
    "pm2_5": "Particulate matter concentration (PM2.5) in ambient air.",
    "so2": "Sulfur dioxide concentration in ambient air.",
    "co": "Carbon monoxide concentration in ambient air.",
    "nh3": "Ammonia concentration in ambient air.",
    "benzene": "Benzene concentration in ambient air.",
}


def build_entity_set(
    station_number: str,
    station_name: str,
    lat: float,
    lon: float,
    components: list[str],
) -> dict[str, Any]:
    """Build a CLIMATE_ADAPTATION_ENTITY_SETS-format dict for one Luchtmeetnet station.

    :param station_number: RIVM station identifier (e.g. "NL49007").
    :param station_name: Human-readable station name.
    :param lat: WGS-84 latitude.
    :param lon: WGS-84 longitude.
    :param components: List of formula strings reported by this station.
    """
    # Resolve each formula to canonical; drop unknowns.
    canonical_props: list[str] = []
    for formula in components:
        canonical = resolve(formula)
        if canonical is not None:
            if canonical.value not in canonical_props:
                canonical_props.append(canonical.value)
        else:
            logger.debug(
                "Luchtmeetnet build_entity_set: unknown formula %r for station %s — skipped",
                formula,
                station_number,
            )

    thing_name = f"Luchtmeetnet {station_name}"
    return {
        "site_key": "luchtmeetnet",
        "site_name": "Luchtmeetnet RIVM",
        "thing": {
            "name": thing_name,
            "description": (
                f"Luchtmeetnet RIVM air quality monitoring station {station_name} "
                f"(station number {station_number})."
            ),
            "properties": {
                "site": "luchtmeetnet",
                "source": "luchtmeetnet",
                "station_number": station_number,
                "network": "Luchtmeetnet",
                "operator": "RIVM",
            },
        },
        "location": {
            "name": f"Luchtmeetnet {station_name} location",
            "description": (
                f"Air quality measurement point {station_name} ({station_number})."
            ),
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {},
        },
        "sensors": [
            {
                "sensor_id": f"luchtmeetnet-{station_number}",
                "name": f"Luchtmeetnet {station_name} sensor",
                "description": (
                    f"Luchtmeetnet RIVM ambient air quality sensor at {station_name}."
                ),
                "encodingType": "application/json",
                "metadata": "https://www.luchtmeetnet.nl",
                "observed_properties": canonical_props,
                "properties": {},
            }
        ],
        "observed_properties": {
            k: v for k, v in _DESCRIPTIONS.items() if k in canonical_props
        },
    }


def parse_measurements(
    data: list[dict[str, Any]],
    station_number: str,
    station_name: str,
) -> list[SensorReading]:
    """Parse Luchtmeetnet measurements API response into SensorReadings.

    :param data: The ``data`` array from the API response, i.e. a list of
        ``{"formula": "NO2", "value": 23.5, "timestamp_measured": "..."}`` dicts.
    :param station_number: RIVM station identifier used as the sensor_id base.
    :param station_name: Human-readable station name used for thing/sensor names.
    """
    readings: list[SensorReading] = []
    sensor_id = f"luchtmeetnet-{station_number}"
    sensor_name = f"Luchtmeetnet {station_name} sensor"
    thing_name = f"Luchtmeetnet {station_name}"

    for entry in data:
        if not isinstance(entry, dict):
            continue

        formula = str(entry.get("formula", "")).strip()
        if not formula:
            continue

        canonical = resolve(formula)
        if canonical is None:
            logger.debug(
                "Luchtmeetnet: unknown formula %r from station %s — skipped",
                formula,
                station_number,
            )
            continue

        raw_value = entry.get("value")
        if raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (ValueError, TypeError):
            logger.warning(
                "Luchtmeetnet: non-numeric value %r for %s at station %s — skipped",
                raw_value,
                formula,
                station_number,
            )
            continue

        ts_raw = entry.get("timestamp_measured", "")
        if ts_raw:
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(UTC)
        else:
            ts = datetime.now(UTC)

        meta = canonical.meta
        readings.append(
            SensorReading(
                sensor_id=sensor_id,
                sensor_name=sensor_name,
                observed_property=canonical.value,
                unit=meta.unit,
                value=value,
                timestamp=ts,
                quality="good",
                location="luchtmeetnet",
                thing_name=thing_name,
                observed_property_name=meta.display_name,
            )
        )

    return readings
