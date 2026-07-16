"""Abstract base class for REST API polling data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.models import SensorReading


class PollingSource(ABC):
    """Base class for REST API data sources that are periodically polled."""

    source_name: str

    @abstractmethod
    async def fetch_readings(self) -> list[SensorReading]:
        """Fetch latest data from the external API and return SensorReadings."""
        ...

    @abstractmethod
    def entity_sets(self) -> list[dict[str, Any]]:
        """Return CLIMATE_ADAPTATION_ENTITY_SETS-format dicts for FROST registration."""
        ...

    @abstractmethod
    def is_enabled(self) -> bool:
        """Whether this source is enabled in the current configuration."""
        ...

    @abstractmethod
    def poll_interval(self) -> int:
        """Seconds between poll cycles (minimum 10)."""
        ...
