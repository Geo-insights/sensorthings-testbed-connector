"""Base class for REST API polling sources with shared error handling.

Sits between :class:`PollingSource` (pure ABC) and concrete sources like
:class:`OhnicsPollingSource` / :class:`LevellogPollingSource`.  Provides:

- Standardised ``health_monitor`` error recording on fetch failure
- Zero-discovery alerting — fires when a source that has previously
  delivered data starts returning 0 readings (guards against upstream
  API schema changes or filter regressions)
- Dynamic-discovery alerting — fires when ``entity_sets()`` is empty
  after a successful fetch (for sources with runtime sensor discovery)
- Consistent success/failure logging
"""

from __future__ import annotations

import logging
from abc import abstractmethod

from app.models import SensorReading
from app.services.polling_source import PollingSource

logger = logging.getLogger("connector.rest_polling")


class RestPollingSource(PollingSource):
    """Intermediate base for HTTP/REST polling sources.

    Subclasses must still implement all four :class:`PollingSource` abstract
    methods plus :meth:`_do_fetch_readings`.  This class wraps
    ``fetch_readings`` with health-monitor bookkeeping and zero-discovery
    alerting so individual sources don't duplicate that logic.
    """

    source_name: str

    # Set to ``True`` in subclasses that use dynamic sensor discovery
    # (e.g. Ohnics).  When ``True``, an additional alert fires if
    # ``entity_sets()`` is still empty after a successful fetch.
    uses_dynamic_discovery: bool = False

    def __init__(self) -> None:
        self._has_seen_readings = False
        self._consecutive_empty = 0

    # ------------------------------------------------------------------
    # Wrapped fetch — subclasses override ``_do_fetch_readings`` instead
    # ------------------------------------------------------------------

    async def fetch_readings(self) -> list[SensorReading]:
        """Fetch readings with automatic health-monitor bookkeeping.

        Subclasses should override :meth:`_do_fetch_readings` instead of
        this method.
        """
        from app.services.health_monitor import health_monitor

        try:
            readings = await self._do_fetch_readings()
        except Exception as exc:
            logger.exception("%s: fetch cycle failed", self.source_name)
            health_monitor.record_source_error(
                self.source_name.lower(),
                f"{type(exc).__name__}: {exc}",
            )
            return []

        if readings:
            self._has_seen_readings = True
            self._consecutive_empty = 0
            logger.info(
                "%s: fetched %d readings", self.source_name, len(readings),
            )
        elif self._has_seen_readings:
            # T6: zero-discovery alert — source previously delivered data but
            # this cycle returned nothing.  Could indicate an upstream schema
            # change, filter regression, or data gap.
            self._consecutive_empty += 1
            self._emit_empty_cycle_alert()

        # Dynamic-discovery alert — fires when the source uses runtime sensor
        # discovery and entity_sets() is empty after a successful fetch.
        if self.uses_dynamic_discovery and not self.entity_sets():
            self._emit_discovery_empty_alert()

        return readings

    @abstractmethod
    async def _do_fetch_readings(self) -> list[SensorReading]:
        """Actual fetch logic — override in subclasses.

        Exceptions propagate to :meth:`fetch_readings` which records them
        in health_monitor.
        """
        ...

    # ------------------------------------------------------------------
    # Zero-discovery alerting (T6)
    # ------------------------------------------------------------------

    def _emit_empty_cycle_alert(self) -> None:
        """Alert when a previously-productive source returns 0 readings."""
        from app.services.alerting import send_alert

        send_alert(
            f"{self.source_name.lower()}.empty_discovery",
            f"{self.source_name}: API responded successfully but yielded "
            f"0 readings ({self._consecutive_empty} consecutive empty "
            f"cycle(s)). Possible upstream schema change or filter regression.",
            level="warning",
            context={
                "source": self.source_name,
                "consecutive_empty_cycles": self._consecutive_empty,
            },
            dedup_key=f"{self.source_name.lower()}.empty_discovery",
        )

    def _emit_discovery_empty_alert(self) -> None:
        """Alert when dynamic discovery returned no sensors."""
        from app.services.alerting import send_alert

        send_alert(
            f"{self.source_name.lower()}.discovery_empty",
            f"{self.source_name}: fetch succeeded but zero sensors discovered. "
            f"Check upstream API availability or filter configuration.",
            level="warning",
            context={"source": self.source_name},
            dedup_key=f"{self.source_name.lower()}.discovery_empty",
        )
