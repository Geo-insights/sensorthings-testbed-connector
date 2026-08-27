"""Levellog groundwater level sensor entity definitions and response mapping.

Data is fetched from the CARS Online API (carsonline.eu) using OAuth2 client credentials.
The API uses OData conventions with /api/{Type}/{installationId} for time-series data.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading
from app.sta.canonical import CanonicalDatastream, resolve

logger = logging.getLogger(__name__)

# Source-specific descriptions per canonical observed property. Unit + display
# name + CF definition all come from canonical.py at read time.
_DESCRIPTIONS: dict[str, str] = {
    "water_level": "Groundwater level relative to reference datum.",
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
        "observed_properties": dict(_DESCRIPTIONS),
    }


def parse_logdata_readings(
    raw_data: Any,
    installation_id: str,
    installation_name: str,
) -> list[SensorReading]:
    """Parse a CARS normalizedlogdata response into SensorReadings.

    Per the CARS REST API (POST /api/{Type}/{installationId}), the normalized
    logdata response is a **table**: an array of rows, each row an array of
    string cells. The first row is the header (column names); the first column
    is the timestamp and the remaining columns are variable values. We pick the
    column whose header looks like a water level.

    A legacy list-of-dicts shape is still handled as a fallback so the parser
    keeps working if a given installation type returns objects instead.
    """
    canonical = resolve("water_level")
    if canonical is None:  # unreachable given canonical.py, but keeps mypy honest
        return []
    meta = canonical.meta

    def _emit(ts: datetime, value: float) -> SensorReading:
        return SensorReading(
            sensor_id=f"tgv-levellog-{installation_id[:8]}",
            sensor_name=f"Levellog {installation_name} sensor",
            observed_property=canonical.value,
            unit=meta.unit,
            value=value,
            timestamp=ts,
            quality="good",
            location="tgv",
            thing_name=f"Levellog {installation_name}",
            observed_property_name=meta.display_name,
        )

    def _parse_ts(raw: Any) -> datetime | None:
        if raw in (None, ""):
            return None
        try:
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None

    readings: list[SensorReading] = []

    # Preferred shape: array-of-arrays (table with a header row).
    if isinstance(raw_data, list) and raw_data and isinstance(raw_data[0], list):
        header = [str(c).strip() for c in raw_data[0]]
        # Find the value column: header containing "level"/"water"/"stand",
        # otherwise the first non-timestamp column.
        value_col = None
        for idx, col in enumerate(header[1:], start=1):
            low = col.lower()
            if any(tok in low for tok in ("level", "water", "grondwater", "stand", "gws")):
                value_col = idx
                break
        if value_col is None and len(header) > 1:
            value_col = 1

        for row in raw_data[1:]:
            if not isinstance(row, list) or value_col is None or len(row) <= value_col:
                continue
            ts = _parse_ts(row[0])
            if ts is None:
                continue
            try:
                value = float(str(row[value_col]).replace(",", "."))
            except (ValueError, TypeError):
                continue
            readings.append(_emit(ts, value))
        return readings

    # Fallback shape: list of dicts (or {"data"/"value": [...]}).
    data_points = raw_data if isinstance(raw_data, list) else (
        raw_data.get("data", raw_data.get("value", [])) if isinstance(raw_data, dict) else []
    )
    if not isinstance(data_points, list):
        return readings

    for point in data_points:
        if not isinstance(point, dict):
            continue
        ts = _parse_ts(point.get("Timestamp", point.get("timestamp", point.get("DateTime", ""))))
        if ts is None:
            continue
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
        readings.append(_emit(ts, value))

    return readings
