"""Concrete pipeline components for the bridge (JSON file) data source."""

from __future__ import annotations

import json
from typing import Any

from app.models import SensorReading
from app.pipeline.base import Decoder, Parser


class JsonDecoder(Decoder):
    """Decode raw JSON bytes into a Python dict."""

    def decode(self, raw: bytes) -> dict[str, Any]:
        return json.loads(raw)


class BridgeReadingParser(Parser):
    """Parse a raw reading dict into a SensorReading via Pydantic validation."""

    def parse(self, raw: dict[str, Any]) -> SensorReading:
        return SensorReading.model_validate(raw)
