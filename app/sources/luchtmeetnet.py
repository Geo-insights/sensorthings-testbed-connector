"""Luchtmeetnet RIVM air quality sensor entity definitions and response mapping.

Luchtmeetnet is the Dutch national air quality monitoring network operated by
RIVM. Stations measure NO2, O3, PM10, PM2.5, SO2, CO, NH3, and benzene (C6H6).
API: https://api.luchtmeetnet.nl/open_api

Values are pushed in the unit of their canonical datastream: ``FORMULA_UNITS``
lists the unit the API serves each formula in next to that datastream, with
the scale between the two (CO: µg/m³ from the API, mg/m³ canonical).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, NamedTuple

from app.models import SensorReading
from app.sta.canonical import CanonicalDatastream

logger = logging.getLogger(__name__)


class FormulaUnit(NamedTuple):
    """How the values of one Luchtmeetnet formula map onto its canonical datastream."""

    canonical: CanonicalDatastream
    api_unit: str  # the unit GET /measurements serves the formula's values in
    scale: Decimal = Decimal(1)  # canonical value = API value * scale


UG_M3 = "ug/m3"  # spelled as canonical.py spells it
UG_M3_TO_MG_M3 = Decimal("0.001")

# The Luchtmeetnet formulas that are pushed: the canonical datastream of each,
# next to the unit the API serves it in. canonical.py owns units and canonical
# always wins (see CLAUDE.md), so a formula whose API unit differs from its
# datastream's unit carries the scale that converts it, applied at the source
# boundary in parse_measurements(). Formulas outside this table (NO, NOx, FN, PS,
# BCWB, H2S, C7H8, C8H10, ...) are dropped, including any that
# canonical.resolve() would map but whose API unit is not declared here.
#
# API units from GET /components/{formula} (checked Nov 2026): the descriptions
# give NO2, O3, SO2, C6H6 and CO in µg/m³ (CO: "normally around 300 (μg/m3)",
# live values 206-843). PM10 and PM2.5 are mass concentrations in µg/m³ (EU
# directive 2008/50/EC). The NH3 description names no unit; the network reports
# it in µg/m³ (live values 0.4-20). Canonical CO is mg/m³, which Samen Meten
# maps onto as well, so Luchtmeetnet CO is scaled by 0.001.
#
# GET /measurements honours only the FIRST formula of a comma-separated
# ``formula`` filter, so measurements are requested per station without a
# formula filter and narrowed to the station's canonical components.
FORMULA_UNITS: dict[str, FormulaUnit] = {
    "NO2": FormulaUnit(CanonicalDatastream.NO2, UG_M3),
    "O3": FormulaUnit(CanonicalDatastream.O3, UG_M3),
    "PM10": FormulaUnit(CanonicalDatastream.PM10, UG_M3),
    "PM25": FormulaUnit(CanonicalDatastream.PM2_5, UG_M3),
    "PM2.5": FormulaUnit(CanonicalDatastream.PM2_5, UG_M3),  # some station records list both spellings
    "SO2": FormulaUnit(CanonicalDatastream.SO2, UG_M3),
    "CO": FormulaUnit(CanonicalDatastream.CO, UG_M3, UG_M3_TO_MG_M3),
    "NH3": FormulaUnit(CanonicalDatastream.NH3, UG_M3),
    "C6H6": FormulaUnit(CanonicalDatastream.BENZENE, UG_M3),
}

SUPPORTED_FORMULAS = list(FORMULA_UNITS)


def formula_unit(formula: str) -> FormulaUnit | None:
    """The ``FORMULA_UNITS`` entry of a formula (case and surrounding space ignored), or ``None``."""
    return FORMULA_UNITS.get(formula.strip().upper())


def to_canonical_unit(entry: FormulaUnit, value: float) -> float:
    """Convert an API value of *entry*'s formula into the unit of its canonical datastream."""
    if entry.scale == 1:
        return value
    # Exact decimal arithmetic: in binary floating point 226.2 * 0.001 is
    # 0.22619999999999998, which FROST would store and serve verbatim.
    return float(Decimal(repr(value)) * entry.scale)


class StationDetail(NamedTuple):
    """Coordinates and measured components of one station (``GET /stations/{number}``)."""

    lat: float
    lon: float
    components: list[str]


def is_valid_wgs84(lat: float, lon: float) -> bool:
    """True for a finite WGS-84 latitude/longitude pair."""
    return math.isfinite(lat) and math.isfinite(lon) and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def parse_station_detail(body: Any) -> StationDetail | None:
    """Parse a ``GET /stations/{number}`` response into a :class:`StationDetail`.

    The detail record carries ``geometry.coordinates`` as ``[lon, lat]`` and a
    ``components`` list of formula strings; the paged ``GET /stations`` list
    carries neither (only ``number`` and ``location``). Accepts the full
    response (``{"data": {...}}``) or the bare ``data`` object.

    Returns ``None`` when the record has no usable WGS-84 point, so callers skip
    the station instead of inventing a location for it.
    """
    record = body.get("data") if isinstance(body, dict) and "data" in body else body
    if not isinstance(record, dict):
        return None
    point = _parse_point(record.get("geometry"))
    if point is None:
        return None
    lon, lat = point
    return StationDetail(lat=lat, lon=lon, components=_parse_components(record.get("components")))


def _parse_point(geometry: Any) -> tuple[float, float] | None:
    """Return ``(lon, lat)`` from a GeoJSON-style point, or ``None`` if unusable."""
    if not isinstance(geometry, dict):
        return None
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list | tuple) or len(coordinates) < 2:
        return None
    try:
        lon = float(coordinates[0])
        lat = float(coordinates[1])
    except (TypeError, ValueError):
        return None
    if not is_valid_wgs84(lat, lon):
        return None
    return lon, lat


def _parse_components(raw: Any) -> list[str]:
    """Normalise a components field (list or comma-separated string), de-duplicated in order."""
    if isinstance(raw, str):
        items: list[Any] = raw.split(",")
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    components: list[str] = []
    for item in items:
        if item is None:
            continue
        text = str(item).strip()
        if text and text not in components:
            components.append(text)
    return components


def canonical_components(components: list[str]) -> list[str]:
    """Map Luchtmeetnet formulas to canonical datastream names.

    Formulas without an entry in ``FORMULA_UNITS`` are dropped and the result
    is de-duplicated in order (``PM25`` and ``PM2.5`` both map to ``pm2_5``). A
    station gets exactly one Datastream per entry, so this is also the set of
    observed properties whose readings can be pushed for that station.
    """
    canonical_props: list[str] = []
    for formula in components:
        entry = formula_unit(formula)
        if entry is not None and entry.canonical.value not in canonical_props:
            canonical_props.append(entry.canonical.value)
    return canonical_props

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
    # Map each formula to canonical; drop formulas without a declared unit.
    canonical_props = canonical_components(components)
    unknown = [formula for formula in components if formula_unit(formula) is None]
    if unknown:
        logger.debug(
            "Luchtmeetnet build_entity_set: unknown formulas %s for station %s — skipped",
            unknown,
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

    Only formulas listed in ``FORMULA_UNITS`` are kept; their values are
    converted from the API unit into the canonical unit (CO: µg/m³ to mg/m³).

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

        declared = formula_unit(formula)
        if declared is None:
            logger.debug(
                "Luchtmeetnet: formula %r from station %s has no declared unit — skipped",
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

        meta = declared.canonical.meta
        readings.append(
            SensorReading(
                sensor_id=sensor_id,
                sensor_name=sensor_name,
                observed_property=declared.canonical.value,
                unit=meta.unit,
                value=to_canonical_unit(declared, value),
                timestamp=ts,
                quality="good",
                location="luchtmeetnet",
                thing_name=thing_name,
                observed_property_name=meta.display_name,
            )
        )

    return readings
