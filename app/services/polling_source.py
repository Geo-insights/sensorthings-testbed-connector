"""Abstract base class for REST API polling data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any

from app.models import SensorReading


class PollingSource(ABC):
    """Base class for REST API data sources that are periodically polled."""

    source_name: str

    @abstractmethod
    async def fetch_readings(self) -> list[SensorReading]:
        """Fetch latest data from the external API and return SensorReadings."""
        ...

    async def fetch_reading_batches(self) -> AsyncGenerator[list[SensorReading], None]:
        """A cycle's readings, in the batches the polling loop registers and pushes one at a time.

        Most sources deliver a cycle at once: one batch, ``fetch_readings()``. A
        source whose cycle takes long yields batches while it fetches, so its
        readings reach FROST during the cycle instead of at its end.
        """
        yield await self.fetch_readings()

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
