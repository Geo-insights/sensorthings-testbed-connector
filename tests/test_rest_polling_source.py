"""Tests for RestPollingSource base class (T2) and zero-sensor alert (T6)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from app.models import SensorReading
from app.services.rest_polling_source import RestPollingSource

# ---------------------------------------------------------------------------
# Concrete test double
# ---------------------------------------------------------------------------


class _StubSource(RestPollingSource):
    """Minimal concrete subclass for testing the base class."""

    source_name = "stub"
    uses_dynamic_discovery = False

    def __init__(
        self,
        *,
        readings: list[SensorReading] | None = None,
        raise_on_fetch: Exception | None = None,
        entity_set_list: list[dict[str, Any]] | None = None,
        enabled: bool = True,
    ) -> None:
        super().__init__()
        self._readings = readings or []
        self._raise_on_fetch = raise_on_fetch
        self._entity_set_list = entity_set_list or []
        self._enabled = enabled

    def is_enabled(self) -> bool:
        return self._enabled

    def poll_interval(self) -> int:
        return 60

    def entity_sets(self) -> list[dict[str, Any]]:
        return self._entity_set_list

    async def _do_fetch_readings(self) -> list[SensorReading]:
        if self._raise_on_fetch:
            raise self._raise_on_fetch
        return self._readings


def _make_reading(**overrides: Any) -> SensorReading:
    defaults: dict[str, Any] = {
        "sensor_id": "s1",
        "sensor_name": "Sensor 1",
        "observed_property": "temperature",
        "unit": "°C",
        "value": 20.0,
        "timestamp": datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return SensorReading(**defaults)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# T2: RestPollingSource wraps fetch with health-monitor bookkeeping
# ---------------------------------------------------------------------------


def test_successful_fetch_returns_readings() -> None:
    readings = [_make_reading()]
    source = _StubSource(readings=readings)
    result = _run(source.fetch_readings())
    assert result == readings


def test_fetch_exception_records_source_error() -> None:
    source = _StubSource(raise_on_fetch=RuntimeError("upstream down"))

    with patch("app.services.health_monitor.health_monitor") as mock_hm:
        result = _run(source.fetch_readings())

    assert result == []
    mock_hm.record_source_error.assert_called_once()
    call_args = mock_hm.record_source_error.call_args
    assert call_args[0][0] == "stub"
    assert "RuntimeError" in call_args[0][1]


def test_fetch_exception_does_not_propagate() -> None:
    source = _StubSource(raise_on_fetch=ValueError("bad data"))
    with patch("app.services.health_monitor.health_monitor"):
        result = _run(source.fetch_readings())
    assert result == []


# ---------------------------------------------------------------------------
# T6: Zero-sensor discovery alert
# ---------------------------------------------------------------------------


def test_discovery_empty_alert_fires_when_no_sensors() -> None:
    """When uses_dynamic_discovery=True and entity_sets() is empty after a
    successful fetch, an alert should fire."""
    source = _StubSource(
        readings=[_make_reading()],
        entity_set_list=[],  # empty — no sensors discovered
    )
    source.uses_dynamic_discovery = True

    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert") as mock_alert:
        result = _run(source.fetch_readings())

    assert result  # readings were returned
    mock_alert.assert_called_once()
    assert "discovery_empty" in mock_alert.call_args[0][0]


def test_discovery_alert_does_not_fire_when_sensors_exist() -> None:
    """No alert when entity_sets() returns data."""
    source = _StubSource(
        readings=[_make_reading()],
        entity_set_list=[{"thing": "t1"}],
    )
    source.uses_dynamic_discovery = True

    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert") as mock_alert:
        _run(source.fetch_readings())

    mock_alert.assert_not_called()


def test_discovery_alert_does_not_fire_when_discovery_disabled() -> None:
    """No alert when uses_dynamic_discovery=False (static config sources)."""
    source = _StubSource(
        readings=[_make_reading()],
        entity_set_list=[],
    )
    assert not source.uses_dynamic_discovery

    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert") as mock_alert:
        _run(source.fetch_readings())

    mock_alert.assert_not_called()


def test_discovery_alert_fires_even_on_empty_readings() -> None:
    """Discovery alert still fires when entity_sets() is empty, regardless
    of whether readings came back — the source may have never discovered
    any sensors at all."""
    source = _StubSource(readings=[], entity_set_list=[])
    source.uses_dynamic_discovery = True

    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert") as mock_alert:
        _run(source.fetch_readings())

    mock_alert.assert_called_once()
    assert "discovery_empty" in mock_alert.call_args[0][0]


# ---------------------------------------------------------------------------
# T6: Empty-cycle alert (zero readings after previous success)
# ---------------------------------------------------------------------------


def test_empty_cycle_alert_fires_after_previous_readings() -> None:
    """When a source previously delivered readings but now returns 0,
    an empty-cycle alert should fire."""
    source = _StubSource(readings=[_make_reading()])

    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert"):
        _run(source.fetch_readings())  # first cycle: has readings

    # Now simulate empty response
    source._readings = []

    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert") as mock_alert:
        result = _run(source.fetch_readings())

    assert result == []
    mock_alert.assert_called_once()
    assert "empty_discovery" in mock_alert.call_args[0][0]
    assert "0 readings" in mock_alert.call_args[0][1]


def test_empty_cycle_alert_does_not_fire_on_first_empty() -> None:
    """No alert on startup when source has never delivered readings."""
    source = _StubSource(readings=[])

    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert") as mock_alert:
        _run(source.fetch_readings())

    mock_alert.assert_not_called()


def test_consecutive_empty_counter_increments() -> None:
    """Consecutive empty cycles should increment the counter."""
    source = _StubSource(readings=[_make_reading()])

    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert"):
        _run(source.fetch_readings())  # seed _has_seen_readings

    source._readings = []
    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert"):
        _run(source.fetch_readings())  # empty cycle 1
    assert source._consecutive_empty == 1

    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert"):
        _run(source.fetch_readings())  # empty cycle 2
    assert source._consecutive_empty == 2


def test_consecutive_empty_resets_on_readings() -> None:
    """Counter resets when readings arrive again."""
    source = _StubSource(readings=[_make_reading()])

    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert"):
        _run(source.fetch_readings())

    source._readings = []
    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert"):
        _run(source.fetch_readings())
    assert source._consecutive_empty == 1

    source._readings = [_make_reading()]
    with patch("app.services.health_monitor.health_monitor"), \
         patch("app.services.alerting.send_alert"):
        _run(source.fetch_readings())
    assert source._consecutive_empty == 0


# ---------------------------------------------------------------------------
# Regression: Ohnics and Levellog still work through RestPollingSource
# ---------------------------------------------------------------------------


def test_ohnics_subclass_has_dynamic_discovery() -> None:
    from app.services.ohnics_source import OhnicsPollingSource

    assert issubclass(OhnicsPollingSource, RestPollingSource)
    assert OhnicsPollingSource.uses_dynamic_discovery is True


def test_levellog_subclass_uses_static_config() -> None:
    from app.services.levellog_source import LevellogPollingSource

    assert issubclass(LevellogPollingSource, RestPollingSource)
    assert LevellogPollingSource.uses_dynamic_discovery is False
