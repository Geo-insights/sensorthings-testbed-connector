"""KNMI weather station entity definitions and response mapping.

Data sourced from the KNMI Data Platform EDR API (CC-BY-4.0).  Requires a
free API key from https://developer.dataplatform.knmi.nl — set ``KNMI_API_KEY``.

The EDR API returns GeoJSON for the ``/locations`` endpoint. Each Feature
contains a ``properties`` dict with the latest 10-minute observations keyed
by KNMI parameter codes (``ta``, ``ff``, ``dd``, etc.).

KNMI parameter code reference (10-minute observations):
  ta  = air temperature (0.1 °C)
  tg  = ground temperature at 10 cm (0.1 °C)
  ff  = mean wind speed (0.1 m/s)
  dd  = wind direction (degrees)
  fxx = max wind gust (0.1 m/s)
  pp  = air pressure (0.1 hPa)
  rh  = relative humidity (%)
  vv  = visibility (0-49: x100m, 50-79: x1km-5km, 80-89: x5km-30km)
  sq  = sunshine duration (0.1 hour per hour)
  rg  = global radiation (J/cm2)
  r1h = precipitation last hour (0.1 mm)
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
    "humidity": "Relative humidity at KNMI weather station.",
    "wind_speed": "Wind speed at KNMI weather station.",
    "wind_direction": "Wind direction at KNMI weather station.",
    "wind_gust": "Wind gust speed at KNMI weather station.",
    "air_pressure": "Atmospheric air pressure at KNMI weather station.",
    "visibility": "Horizontal visibility at KNMI weather station.",
    "solar_radiation": "Global solar radiation at KNMI weather station.",
    "precipitation": "Precipitation amount (last hour) at KNMI weather station.",
}

# KNMI reports wind in 0.1 m/s; canonical WIND_SPEED is km/h.
# Conversion: value * 0.1 (deci to base) * 3.6 (m/s to km/h) = * 0.36
_WIND_DECI_MS_TO_KMH = 0.36

# KNMI parameter code → (normalizer field, scale factor)
# Scale factors convert KNMI's integer-encoded values to real units.
_KNMI_PARAM_MAP: dict[str, tuple[str, float]] = {
    "ta": ("temperature", 0.1),            # 0.1 °C → °C
    "tg": ("groundtemperature", 0.1),      # 0.1 °C → °C
    "ff": ("windspeedkmh", _WIND_DECI_MS_TO_KMH),  # 0.1 m/s → km/h
    "dd": ("winddirectiondegrees", 1.0),   # degrees (no scaling)
    "fxx": ("windgustskmh", _WIND_DECI_MS_TO_KMH),  # 0.1 m/s → km/h
    "pp": ("airpressure", 0.1),            # 0.1 hPa → hPa
    "rh": ("humidity", 1.0),               # % (no scaling)
    "vv": ("visibility", 1.0),             # raw coded value (m approx)
    "rg": ("sunpower", 1.0),              # J/cm2 → treat as W/m2 proxy
    "r1h": ("rainfalllasthour", 0.1),      # 0.1 mm → mm
}


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
                "metadata": "https://developer.dataplatform.knmi.nl",
                "observed_properties": list(_DESCRIPTIONS.keys()),
                "properties": {},
            }
        ],
        "observed_properties": _DESCRIPTIONS,
    }


def parse_edr_locations(
    data: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[SensorReading]]:
    """Parse the KNMI EDR ``/locations`` GeoJSON response.

    Returns:
        (stations, readings) — station metadata dicts for entity registration
        and SensorReadings from the latest observations embedded in each Feature.
    """
    features = data.get("features", [])
    if not isinstance(features, list):
        features = []

    stations: list[dict[str, Any]] = []
    all_readings: list[SensorReading] = []

    for feature in features:
        if not isinstance(feature, dict):
            continue

        props = feature.get("properties", {})
        geom = feature.get("geometry", {})
        station_id = feature.get("id", props.get("station_id", props.get("id", "")))
        station_name = props.get("name", props.get("station_name", str(station_id)))

        if not station_id:
            continue

        # Extract coordinates from GeoJSON geometry
        coords = geom.get("coordinates", []) if isinstance(geom, dict) else []
        if len(coords) >= 2:
            lon, lat = float(coords[0]), float(coords[1])
        else:
            continue

        stations.append({
            "id": str(station_id),
            "name": str(station_name),
            "lat": lat,
            "lon": lon,
        })

        # Extract observations from properties using KNMI parameter codes
        readings = _parse_knmi_properties(props, str(station_id), str(station_name))
        all_readings.extend(readings)

    return stations, all_readings


def _parse_knmi_properties(
    props: dict[str, Any],
    station_id: str,
    station_name: str,
) -> list[SensorReading]:
    """Extract observations from a Feature's properties dict."""
    # Try to find a timestamp
    ts_raw = props.get("datetime", props.get("timestamp", props.get("time", "")))
    if ts_raw:
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.now(UTC)
    else:
        ts = datetime.now(UTC)

    payload: dict[str, Any] = {}
    for knmi_code, (norm_field, scale) in _KNMI_PARAM_MAP.items():
        raw = props.get(knmi_code)
        if raw is None:
            continue
        try:
            payload[norm_field] = float(raw) * scale
        except (ValueError, TypeError):
            continue

    if not payload:
        return []

    normalizer = KNMIWeatherNormalizer.model_validate(payload)
    return normalizer.to_readings(
        sensor_id=f"knmi-{station_id}",
        sensor_name=f"KNMI {station_name} sensor",
        thing_name=f"KNMI {station_name}",
        timestamp=ts,
        location="nl",
        quality="good",
    )


# ---------------------------------------------------------------------------
# Legacy Buienradar feed parser (kept for backward compatibility)
# ---------------------------------------------------------------------------

_WIND_MS_TO_KMH = 3.6


def parse_station_measurements(
    measurements: list[dict[str, Any]],
) -> list[SensorReading]:
    """Parse the ``stationmeasurements`` array from the Buienradar feed.

    Legacy parser — used when ``KNMI_WEATHER_API_URL`` points at
    ``data.buienradar.nl/2.0/feed/json``.
    """
    all_readings: list[SensorReading] = []

    for station in measurements:
        if not isinstance(station, dict):
            continue

        station_id = station.get("stationid", "")
        station_name = str(station.get("stationname", station_id))
        if not station_id:
            continue

        ts_raw = station.get("timestamp", "")
        if ts_raw:
            try:
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(UTC)
        else:
            ts = datetime.now(UTC)

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
