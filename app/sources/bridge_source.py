from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import settings
from app.models import IngestRequest, SensorReading
from app.sources.climate_adaptation import generate_demo_readings


def _parse_readings(payload: Any) -> list[SensorReading]:
    if isinstance(payload, dict):
        parsed = IngestRequest.model_validate(payload)
        return parsed.readings
    if isinstance(payload, list):
        return [SensorReading.model_validate(reading) for reading in payload]
    raise ValueError("Bridge payload must be a list of readings or an object containing a readings array.")


def load_bridge_readings() -> list[SensorReading]:
    if not settings.bridge_payload_path:
        return generate_demo_readings()

    payload_path = Path(settings.bridge_payload_path)
    if not payload_path.is_absolute():
        payload_path = Path(__file__).resolve().parents[2] / payload_path

    if not payload_path.exists():
        return generate_demo_readings()

    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        return _parse_readings(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return generate_demo_readings()