"""Stall thresholds of the polling sources.

A polling source counts as stalled when nothing has reached FROST for its poll
interval plus the grace, plus twice the longest wait for data in its last cycle
that delivered. A Samen Meten cycle reads 11,000+ datastreams and can take two
hours; a fixed poll + grace (1,200 s) reported it stalled for most of every
cycle. ``SAMEN_METEN_STALE_SECONDS`` sets its threshold outright. The in-loop
watchdog (tests/test_polling_batches.py) and ``/connector/freshness`` use the
same threshold.
"""

from __future__ import annotations

import dataclasses
import time
import types

import pytest
from fastapi import Response

import app.main as main
import app.routes.connector as connector_routes
from app.routes.connector import freshness
from app.services.health_monitor import health_monitor


def _use_settings(monkeypatch: pytest.MonkeyPatch, **changes: object) -> None:
    """Settings with ``changes``, for the thresholds (app.main) and the endpoint (app.routes.connector)."""
    changed = dataclasses.replace(main.settings, **changes)
    monkeypatch.setattr(main, "settings", changed)
    monkeypatch.setattr(connector_routes, "settings", changed)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """A fresh per-source freshness state, grace 600 s and no override."""
    for state in ("_source_last_success", "_source_success_counts", "_source_last_error", "_source_longest_wait"):
        monkeypatch.setattr(health_monitor, state, {})
    _use_settings(monkeypatch, freshness_grace_seconds=600, samen_meten_stale_seconds=0)


def test_without_a_recorded_wait_the_threshold_is_poll_plus_grace():
    """protects=a source with no measured cycle keeps the old threshold;
    fails_when=a missing wait raises or counts as something;
    no_claim=the first cycle after a restart taking longer."""
    assert main._polling_stale_threshold("ohnics", 300) == 900


def test_the_longest_wait_widens_the_threshold_twice_over():
    """protects=poll + grace + 2 x the longest wait (Samen Meten: an 80-minute wait gives 3 h 0 min);
    fails_when=the wait is ignored, or added once (a stretch spanning the end of one cycle and the start of the next);
    no_claim=how the wait is measured (tests/test_polling_batches.py)."""
    health_monitor.record_source_wait("samen_meten", 4_800)

    assert main._polling_stale_threshold("samen_meten", 600) == 600 + 600 + 2 * 4_800


def test_samen_meten_stale_seconds_sets_the_threshold_outright(monkeypatch):
    """protects=SAMEN_METEN_STALE_SECONDS wins over the derived threshold, for Samen Meten only;
    fails_when=the override is ignored, widened by the wait, or applied to other sources;
    no_claim=the value production should use."""
    _use_settings(monkeypatch, samen_meten_stale_seconds=5_400)
    health_monitor.record_source_wait("samen_meten", 4_800)
    health_monitor.record_source_wait("ohnics", 30)

    assert main._polling_stale_threshold("samen_meten", 600) == 5_400
    assert main._polling_stale_threshold("ohnics", 300) == 300 + 600 + 60


def test_a_negative_override_derives_the_threshold(monkeypatch):
    """protects=only a positive SAMEN_METEN_STALE_SECONDS overrides;
    fails_when=-1 becomes a threshold of -1 (always stalled);
    no_claim=config validation."""
    _use_settings(monkeypatch, samen_meten_stale_seconds=-1)

    assert main._polling_stale_threshold("samen_meten", 600) == 1_200


def test_cycle_waits_measure_start_gaps_and_tail(monkeypatch):
    """protects=the longest wait counts the stretch before the first delivery, between deliveries and after the last;
    fails_when=one of the three stretches is left out;
    no_claim=what counts as a delivery (a push with new readings, in the loop)."""
    clock = types.SimpleNamespace(now=0.0)
    monkeypatch.setattr(main, "time", types.SimpleNamespace(monotonic=lambda: clock.now))
    waits = main._CycleWaits()
    assert not waits.delivered

    clock.now = 100.0
    waits.delivery()
    clock.now = 400.0
    waits.delivery()
    clock.now = 450.0
    assert waits.delivered
    assert waits.longest() == 300.0

    clock.now = 900.0
    assert waits.longest() == 500.0


@pytest.fixture()
def only_samen_meten(monkeypatch):
    """/connector/freshness with Samen Meten (poll 600 s) as its only enabled source."""
    disabled = (
        "kafka_tgv_enabled",
        "ohnics_enabled",
        "levellog_enabled",
        "luchtmeetnet_enabled",
        "sensor_community_enabled",
        "knmi_weather_enabled",
        "meet_je_stad_enabled",
        "bro_enabled",
    )
    _use_settings(
        monkeypatch,
        **dict.fromkeys(disabled, False),
        samen_meten_enabled=True,
        samen_meten_poll_seconds=600,
    )
    health_monitor._source_last_success["samen_meten"] = time.time() - 2_000


def test_freshness_reports_a_long_cycle_source_stale_without_a_measured_wait(only_samen_meten):
    """protects=the endpoint still flags a source past poll + grace when no cycle has been measured;
    fails_when=a missing wait makes the endpoint lenient;
    no_claim=sources other than Samen Meten."""
    response = Response()

    result = freshness(response)

    assert response.status_code == 503
    assert result["sources"]["samen_meten"]["threshold_seconds"] == 1_200
    assert result["sources"]["samen_meten"]["stale"] is True


def test_freshness_uses_the_widened_threshold(only_samen_meten):
    """protects=/connector/freshness and the watchdog agree: poll + grace + 2 x the longest wait;
    fails_when=the endpoint keeps poll + grace and reports Samen Meten stale for most of every cycle;
    no_claim=the HTTP status when another source is stale."""
    health_monitor.record_source_wait("samen_meten", 1_000)
    response = Response()

    result = freshness(response)

    assert response.status_code != 503
    sm = result["sources"]["samen_meten"]
    assert sm["threshold_seconds"] == 3_200
    assert sm["longest_wait_seconds"] == 1_000
    assert sm["stale"] is False
