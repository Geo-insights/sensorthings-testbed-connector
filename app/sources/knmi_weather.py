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

# Wind conversion: m/s → km/h (canonical WIND_SPEED is km/h)
_MS_TO_KMH = 3.6

# KNMI EDR API parameter code → (normalizer field, scale factor)
# EDR CoverageJSON returns values in real SI units (m/s, hPa, °C, %).
_KNMI_EDR_PARAM_MAP: dict[str, tuple[str, float]] = {
    "ta": ("temperature", 1.0),            # °C
    "tg": ("groundtemperature", 1.0),      # °C
    "ff": ("windspeedkmh", _MS_TO_KMH),   # m/s → km/h
    "dd": ("winddirectiondegrees", 1.0),   # degrees
    "fxx": ("windgustskmh", _MS_TO_KMH),  # m/s → km/h
    "pp": ("airpressure", 1.0),            # hPa
    "rh": ("humidity", 1.0),               # %
    "vv": ("visibility", 1.0),             # m
    "rg": ("sunpower", 1.0),              # J/cm2
    "r1h": ("rainfalllasthour", 1.0),      # mm
}

# Legacy Buienradar integer-encoded scale factors (kept for backward compat)
_WIND_DECI_MS_TO_KMH = 0.36
_KNMI_PARAM_MAP: dict[str, tuple[str, float]] = {
    "ta": ("temperature", 0.1),
    "tg": ("groundtemperature", 0.1),
    "ff": ("windspeedkmh", _WIND_DECI_MS_TO_KMH),
    "dd": ("winddirectiondegrees", 1.0),
    "fxx": ("windgustskmh", _WIND_DECI_MS_TO_KMH),
    "pp": ("airpressure", 0.1),
    "rh": ("humidity", 1.0),
    "vv": ("visibility", 1.0),
    "rg": ("sunpower", 1.0),
    "r1h": ("rainfalllasthour", 0.1),
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
    station_names: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[SensorReading]]:
    """Parse a KNMI EDR CoverageJSON area/position response.

    The EDR ``/area`` endpoint returns a ``CoverageCollection`` with one
    ``Coverage`` per station.  Each coverage has:

    - ``domain.axes.x/y`` — lon/lat
    - ``domain.axes.t`` — list of ISO timestamps
    - ``ranges`` — dict of parameter code → NdArray with ``values``

    Returns:
        (stations, readings) — station metadata dicts for entity registration
        and SensorReadings from the latest observations per station.
    """
    coverages = data.get("coverages", [])
    if not isinstance(coverages, list):
        coverages = []

    stations: list[dict[str, Any]] = []
    all_readings: list[SensorReading] = []
    names = station_names or {}

    for i, cov in enumerate(coverages):
        if not isinstance(cov, dict):
            continue

        domain = cov.get("domain", {})
        axes = domain.get("axes", {})
        ranges = cov.get("ranges", {})

        x_vals = axes.get("x", {}).get("values", [])
        y_vals = axes.get("y", {}).get("values", [])
        t_vals = axes.get("t", {}).get("values", [])

        if not x_vals or not y_vals:
            continue

        lon = float(x_vals[0])
        lat = float(y_vals[0])

        # Station ID: derive from coordinates (EDR area response has no station ID)
        station_id = str(i)
        station_name = names.get(station_id, f"KNMI-{lat:.2f}-{lon:.2f}")

        stations.append({
            "id": station_id,
            "name": station_name,
            "lat": lat,
            "lon": lon,
        })

        # Use the latest timestamp's values (last index in the t axis)
        if not t_vals:
            continue

        latest_idx = len(t_vals) - 1
        try:
            ts = datetime.fromisoformat(str(t_vals[latest_idx]).replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.now(UTC)

        payload: dict[str, Any] = {}
        for param_code, (norm_field, scale) in _KNMI_EDR_PARAM_MAP.items():
            param_range = ranges.get(param_code)
            if not isinstance(param_range, dict):
                continue
            values = param_range.get("values", [])
            if not values or latest_idx >= len(values):
                continue
            raw = values[latest_idx]
            if raw is None:
                continue
            try:
                payload[norm_field] = float(raw) * scale
            except (ValueError, TypeError):
                continue

        if not payload:
            continue

        normalizer = KNMIWeatherNormalizer.model_validate(payload)
        readings = normalizer.to_readings(
            sensor_id=f"knmi-{station_id}",
            sensor_name=f"KNMI {station_name} sensor",
            thing_name=f"KNMI {station_name}",
            timestamp=ts,
            location="nl",
            quality="good",
        )
        all_readings.extend(readings)

    return stations, all_readings


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
