"""Abstract base classes for the two-tier data processing pipeline.

Provider tier (generic framing):
  - Decoder:       raw bytes → structured dict
  - Deserializer:  structured dict → normalized dict
  - Decapsulator:  provider envelope → list of payload dicts

Model tier (sensor-specific):
  - Parser:       raw payload → SensorReading
  - Normalizer:   SensorReading → SensorReading (unit conversion, field mapping)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.models import SensorReading


# ---------------------------------------------------------------------------
# Provider tier
# ---------------------------------------------------------------------------

class Decoder(ABC):
    """Decode raw bytes/wire data into a structured dict."""

    @abstractmethod
    def decode(self, raw: bytes) -> dict[str, Any]:
        ...


class Deserializer(ABC):
    """Deserialize a structured dict (e.g., unwrap Avro unions)."""

    @abstractmethod
    def deserialize(self, record: dict[str, Any]) -> dict[str, Any]:
        ...


class Decapsulator(ABC):
    """Strip provider framing and return individual measurement payloads."""

    @abstractmethod
    def decapsulate(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        ...


# ---------------------------------------------------------------------------
# Model tier
# ---------------------------------------------------------------------------

class Parser(ABC):
    """Parse a raw payload dict into a SensorReading."""

    @abstractmethod
    def parse(self, raw: dict[str, Any]) -> SensorReading:
        ...


class Normalizer(ABC):
    """Normalize a SensorReading (e.g., unit conversion)."""

    @abstractmethod
    def normalize(self, reading: SensorReading) -> SensorReading:
        ...
