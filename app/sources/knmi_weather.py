"""KNMI weather station entity definitions and response mapping.

Data is sourced from KNMI (Royal Netherlands Meteorological Institute) via the
Buienradar JSON feed, which republishes the same KNMI measurement data under
CC-BY-4.0. The 41 official KNMI stations are covered.

Alternative: the KNMI Data Platform EDR API
  Base URL: https://api.dataplatform.knmi.nl/edr/v1
  Collection: 10-minute-in-situ-meteorological-observations
  Auth: free API key in Authorization header
  CoverageJSON response format (more complex to parse)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading
from app.sources.normalizers import KNMIWeatherNormalizer

logger = logging.getLogger(__name__)

# Source-specific descriptions per canonical observed property.
_DESCRIPTIONS: dict[str, str] = {
    "temperature": "Air temperature at KNMI weather station.",
    "ground_temperature": "Ground surface temperature at KNMI weather station.",
    "feel_temperature": "Feels-like (apparent) temperature at KNMI weather station.",
    "humidity": "Relative humidity at KNMI weather station.",
    "wind_speed": "Wind speed at KNMI weather station.",
    "wind_direction": "Wind direction at KNMI weather station.",
    "wind_gust": "Wind gust speed at KNMI weather station.",
    "air_pressure": "Atmospheric air pressure at KNMI weather station.",
    "visibility": "Horizontal visibility at KNMI weather station.",
    "solar_radiation": "Global solar radiation at KNMI weather station.",
    "precipitation": "Precipitation amount (last hour) at KNMI weather station.",
}

# Buienradar field -> conversion factor (None = no conversion needed)
# Wind speed: Buienradar reports m/s, canonical WIND_SPEED is km/h (inherited
# from TGV sensors).  Multiply by 3.6 to convert.
_WIND_MS_TO_KMH = 3.6


def build_entity_set(
    station_id: int | str,
    station_name: str,
    lat: float,
    lon: float,
) -> dict[str, Any]:
    """Build a CLIMATE_ADAPTATION_ENTITY_SETS-format dict for one KNMI station."""
    sid = str(station_id)
    return {
        "site_key": "nl",
        "site_name": "Netherlands",
        "thing": {
            "name": f"KNMI {station_name}",
            "description": f"KNMI official weather station: {station_name}.",
            "properties": {
                "site": "nl",
                "source": "knmi_weather",
                "knmi_station_id": sid,
                "station_name": station_name,
                "network": "KNMI",
            },
        },
        "location": {
            "name": f"KNMI {station_name} location",
            "description": f"KNMI measurement station at {station_name}.",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {},
        },
        "sensors": [
            {
                "sensor_id": f"knmi-{sid}",
                "name": f"KNMI {station_name} sensor",
                "description": f"KNMI official weather sensor at {station_name}.",
                "encodingType": "application/json",
                "metadata": "https://www.knmi.nl/kennis-en-datacentrum/achtergrond/data-ophalen-vanuit-automatische-weersstations",
                "observed_properties": list(_DESCRIPTIONS.keys()),
                "properties": {},
            }
        ],
        "observed_properties": _DESCRIPTIONS,
    }


def parse_station_measurements(
    measurements: list[dict[str, Any]],
) -> list[SensorReading]:
    """Parse the ``stationmeasurements`` array from the Buienradar feed.

    Each station entry produces one :class:`SensorReading` per mapped field.
    Wind speed and gust are converted from m/s to km/h before normalisation,
    so the normalizer receives already-converted km/h values.

    Fields absent from the payload or set to ``None`` are silently skipped.
    """
    all_readings: list[SensorReading] = []

    for station in measurements:
        if not isinstance(station, dict):
            continue

        station_id = station.get("stationid", "")
        station_name = str(station.get("stationname", station_id))
        if not station_id:
            continue

        # Parse timestamp
        ts_raw = station.get("timestamp", "")
        if ts_raw:
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(UTC)
        else:
            ts = datetime.now(UTC)

        # Build normalizer payload — apply unit conversions before passing in
        windspeed_raw = station.get("windspeed")
        windgusts_raw = station.get("windgusts")

        payload: dict[str, Any] = {
            "temperature": station.get("temperature"),
            "groundtemperature": station.get("groundtemperature"),
            "feeltemperature": station.get("feeltemperature"),
            "humidity": station.get("humidity"),
            "winddirectiondegrees": station.get("winddirectiondegrees"),
            "airpressure": station.get("airpressure"),
            "visibility": station.get("visibility"),
            "sunpower": station.get("sunpower"),
            "rainfalllasthour": station.get("rainFallLastHour"),
            # Convert m/s -> km/h before normalizer sees the values
            "windspeedkmh": windspeed_raw * _WIND_MS_TO_KMH if windspeed_raw is not None else None,
            "windgustskmh": windgusts_raw * _WIND_MS_TO_KMH if windgusts_raw is not None else None,
        }

        normalizer = KNMIWeatherNormalizer.model_validate(payload)
        sid = str(station_id)
        readings = normalizer.to_readings(
            sensor_id=f"knmi-{sid}",
            sensor_name=f"KNMI {station_name} sensor",
            thing_name=f"KNMI {station_name}",
            timestamp=ts,
            location="nl",
            quality="good",
        )
        all_readings.extend(readings)

    return all_readings
