"""Sensor.Community citizen air quality + noise sensor entity definitions and response mapping.

Sensor.Community (formerly Luftdaten.info) operates an open network of low-cost
environmental sensors maintained by citizens across Europe.  Each sensor station
reports one or more ``sensordatavalues`` entries.  Multiple sensor types can
co-exist at the same physical location (identified by ``location.id``).

Supported sensor types:
- SDS011 — particulate matter (P1→PM10, P2→PM2.5)
- BME280 / BMP280 — temperature, humidity, pressure (reported in Pa, pushed as hPa)
- DNMS — noise levels (noise_LAeq, noise_LAmin, noise_LAmax)

API endpoint: https://data.sensor.community/airrohr/v1/filter/country=NL
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, NamedTuple

from app.models import SensorReading
from app.sta.canonical import CanonicalDatastream, resolve

logger = logging.getLogger(__name__)


class UnitScale(NamedTuple):
    """Conversion of one Sensor.Community value_type into its canonical unit."""

    factor: Decimal  # canonical value = raw value * factor
    source_unit: str  # the unit Sensor.Community reports
    band: tuple[float, float]  # open interval of raw values that are plainly in source_unit


# Raw pressures strictly inside this band are plainly pascal. At or below the
# lower bound the value is already hPa (no surface air pressure is 20 hPa); at or
# above the upper bound it is implausible in either unit (2000 hPa) and is left
# as is. Every scaled value lands below the lower bound, so the conversion is
# idempotent: no value is ever divided twice. scripts/fix_sc_pressure_units.py
# applies the same band to the history already in FROST.
PA_BAND: tuple[float, float] = (2_000.0, 200_000.0)
PA_TO_HPA = Decimal("0.01")

# Canonical datastreams whose unit differs from the one Sensor.Community reports
# for the value_types that resolve to them. canonical.py owns units and canonical
# always wins (see CLAUDE.md), so the value is converted here, at the source
# boundary. Keyed by the canonical member resolve() returns, not by the raw
# value_type, so every alias of such a datastream ("pressure", "Air Pressure",
# "air_pressure", ...) is converted alike.
#
# Pressure: the airrohr firmware (github.com/opendata-stuttgart/sensors-software,
# airrohr-firmware/airrohr-firmware.ino) sends the BMP180/BMP280/BME280 reading
# as returned by readPressure(), in pascal (``BME280_pressure``), and divides by
# 100 only for its own hPa display. The data API serves it as value_type
# "pressure", e.g. "100391.86"; canonical Air pressure is hPa. Every hPa
# datastream is therefore scaled from Pa. A few stations on other firmware
# already report hPa (e.g. "1001.1"): the band leaves those as is.
# "pressure_at_sealevel" is in Pa as well but resolves to no canonical
# datastream, so it is skipped before it gets here.
UNIT_SCALES: dict[CanonicalDatastream, UnitScale] = {
    member: UnitScale(PA_TO_HPA, "Pa", PA_BAND) for member in CanonicalDatastream if member.meta.unit == "hPa"
}


def to_canonical_unit(canonical: CanonicalDatastream, value: float, location_id: object = None) -> float:
    """Convert a raw Sensor.Community value to the unit of its canonical datastream.

    Values for datastreams without an entry in ``UNIT_SCALES`` are returned
    unchanged, as are values outside the entry's band (logged at debug).
    """
    scale = UNIT_SCALES.get(canonical)
    if scale is None:
        return value
    low, high = scale.band
    if not low < value < high:
        logger.debug(
            "sensor_community: %s=%r at location %s is outside the %s band (%s, %s) — not scaled",
            canonical.value,
            value,
            location_id,
            scale.source_unit,
            low,
            high,
        )
        return value
    # Exact decimal arithmetic: in binary floating point 99954.35 / 100 is
    # 999.5435000000001, which FROST would store and serve verbatim.
    return float(Decimal(repr(value)) * scale.factor)


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

            if not math.isfinite(value):
                continue

            value = to_canonical_unit(canonical, value, location_id)

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
