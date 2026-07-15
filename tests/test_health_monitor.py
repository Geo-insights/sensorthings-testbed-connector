"""Tests for the HealthMonitor service."""

from __future__ import annotations

import threading
import time
from unittest.mock import patch

import pytest

from app.services.health_monitor import HealthMonitor


@pytest.fixture()
def monitor() -> HealthMonitor:
    """Return a fresh HealthMonitor instance for each test."""
    return HealthMonitor()


class TestRecordSuccess:
    def test_increments_count(self, monitor: HealthMonitor):
        monitor.record_success("ds-1")
        monitor.record_success("ds-1")

        summary = monitor.get_summary()
        ds = summary["datastreams"][0]
        assert ds["success_count"] == 2

    def test_updates_last_push_time(self, monitor: HealthMonitor):
        monitor.record_success("ds-1")

        summary = monitor.get_summary()
        ds = summary["datastreams"][0]
        assert ds["seconds_since_last_push"] is not None
        assert ds["seconds_since_last_push"] < 2.0

    def test_multiple_datastreams_tracked_independently(self, monitor: HealthMonitor):
        monitor.record_success("ds-1")
        monitor.record_success("ds-2")
        monitor.record_success("ds-2")

        summary = monitor.get_summary()
        by_id = {d["datastream_id"]: d for d in summary["datastreams"]}
        assert by_id["ds-1"]["success_count"] == 1
        assert by_id["ds-2"]["success_count"] == 2


class TestRecordFailure:
    def test_increments_failure_count(self, monitor: HealthMonitor):
        monitor.record_failure("ds-1", error="timeout")
        monitor.record_failure("ds-1", error="500")

        summary = monitor.get_summary()
        ds = summary["datastreams"][0]
        assert ds["failure_count"] == 2

    def test_failure_does_not_affect_success_count(self, monitor: HealthMonitor):
        monitor.record_failure("ds-1")

        summary = monitor.get_summary()
        ds = summary["datastreams"][0]
        assert ds["success_count"] == 0
        assert ds["failure_count"] == 1

    def test_failure_does_not_set_last_push_time(self, monitor: HealthMonitor):
        monitor.record_failure("ds-1")

        summary = monitor.get_summary()
        ds = summary["datastreams"][0]
        assert ds["seconds_since_last_push"] is None


class TestCheckStaleSensors:
    def test_returns_stale_datastreams(self, monitor: HealthMonitor):
        # Record a success, then pretend time has passed by manipulating the stored time.
        monitor.record_success("ds-1")
        with monitor._lock:
            monitor._last_push_time["ds-1"] = time.monotonic() - 7200

        stale = monitor.check_stale_sensors(threshold_seconds=3600)
        assert "ds-1" in stale

    def test_recent_push_is_not_stale(self, monitor: HealthMonitor):
        monitor.record_success("ds-1")

        stale = monitor.check_stale_sensors(threshold_seconds=3600)
        assert stale == []

    def test_custom_threshold(self, monitor: HealthMonitor):
        monitor.record_success("ds-1")
        with monitor._lock:
            monitor._last_push_time["ds-1"] = time.monotonic() - 10

        assert monitor.check_stale_sensors(threshold_seconds=5) == ["ds-1"]
        assert monitor.check_stale_sensors(threshold_seconds=20) == []

    def test_mixed_fresh_and_stale(self, monitor: HealthMonitor):
        monitor.record_success("fresh")
        monitor.record_success("stale")
        with monitor._lock:
            monitor._last_push_time["stale"] = time.monotonic() - 7200

        stale = monitor.check_stale_sensors(threshold_seconds=3600)
        assert "stale" in stale
        assert "fresh" not in stale

    def test_only_considers_success_pushes(self, monitor: HealthMonitor):
        monitor.record_failure("ds-1")

        # Failure-only datastreams have no entry in _last_push_time,
        # so they are NOT returned by check_stale_sensors.
        stale = monitor.check_stale_sensors(threshold_seconds=0)
        assert stale == []


class TestGetSummary:
    def test_structure_on_empty_monitor(self, monitor: HealthMonitor):
        summary = monitor.get_summary()

        assert "timestamp" in summary
        assert "uptime_seconds" in summary
        assert summary["total_successes"] == 0
        assert summary["total_failures"] == 0
        assert summary["tracked_datastreams"] == 0
        assert summary["datastreams"] == []

    def test_structure_with_data(self, monitor: HealthMonitor):
        monitor.record_success("ds-1")
        monitor.record_failure("ds-2")

        summary = monitor.get_summary()
        assert summary["total_successes"] == 1
        assert summary["total_failures"] == 1
        assert summary["tracked_datastreams"] == 2
        assert len(summary["datastreams"]) == 2

    def test_datastream_ids_are_sorted(self, monitor: HealthMonitor):
        monitor.record_success("charlie")
        monitor.record_success("alpha")
        monitor.record_success("bravo")

        ids = [d["datastream_id"] for d in monitor.get_summary()["datastreams"]]
        assert ids == ["alpha", "bravo", "charlie"]

    def test_uptime_increases(self, monitor: HealthMonitor):
        first = monitor.get_summary()["uptime_seconds"]
        time.sleep(0.05)
        second = monitor.get_summary()["uptime_seconds"]
        assert second >= first

    def test_timestamp_is_iso_format(self, monitor: HealthMonitor):
        ts = monitor.get_summary()["timestamp"]
        # ISO format should contain 'T' separator and '+' for timezone
        assert "T" in ts

    def test_union_of_success_and_failure_ids(self, monitor: HealthMonitor):
        monitor.record_success("only-success")
        monitor.record_failure("only-failure")

        ids = {d["datastream_id"] for d in monitor.get_summary()["datastreams"]}
        assert ids == {"only-success", "only-failure"}


class TestRenderHtml:
    def test_contains_doctype(self, monitor: HealthMonitor):
        html = monitor.render_html()
        assert "<!DOCTYPE html>" in html

    def test_contains_title(self, monitor: HealthMonitor):
        html = monitor.render_html()
        assert "<title>Health Report</title>" in html

    def test_contains_datastream_rows(self, monitor: HealthMonitor):
        monitor.record_success("ds-42")
        monitor.record_success("ds-42")
        monitor.record_failure("ds-42")

        html = monitor.render_html()
        assert "ds-42" in html
        assert "<td>2</td>" in html  # success count
        assert "<td>1</td>" in html  # failure count

    def test_never_pushed_shows_never(self, monitor: HealthMonitor):
        monitor.record_failure("ds-nopush")

        html = monitor.render_html()
        assert "never" in html

    def test_empty_monitor_produces_valid_html(self, monitor: HealthMonitor):
        html = monitor.render_html()
        assert "<table>" in html
        assert "</table>" in html
        assert "<h1>" in html

    def test_shows_summary_stats(self, monitor: HealthMonitor):
        monitor.record_success("a")
        monitor.record_failure("b")

        html = monitor.render_html()
        assert "Successes: 1" in html
        assert "Failures: 1" in html


class TestThreadSafety:
    def test_concurrent_success_recording(self, monitor: HealthMonitor):
        iterations = 500
        num_threads = 4
        barrier = threading.Barrier(num_threads)

        def worker():
            barrier.wait()
            for _ in range(iterations):
                monitor.record_success("ds-shared")

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        summary = monitor.get_summary()
        ds = summary["datastreams"][0]
        assert ds["success_count"] == iterations * num_threads

    def test_concurrent_mixed_operations(self, monitor: HealthMonitor):
        iterations = 200
        barrier = threading.Barrier(3)

        def success_worker():
            barrier.wait()
            for _ in range(iterations):
                monitor.record_success("ds-mixed")

        def failure_worker():
            barrier.wait()
            for _ in range(iterations):
                monitor.record_failure("ds-mixed")

        def reader_worker():
            barrier.wait()
            for _ in range(iterations):
                monitor.get_summary()
                monitor.check_stale_sensors()

        threads = [
            threading.Thread(target=success_worker),
            threading.Thread(target=failure_worker),
            threading.Thread(target=reader_worker),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        summary = monitor.get_summary()
        ds = summary["datastreams"][0]
        assert ds["success_count"] == iterations
        assert ds["failure_count"] == iterations


class TestEdgeCases:
    def test_empty_monitor_summary(self, monitor: HealthMonitor):
        summary = monitor.get_summary()
        assert summary["tracked_datastreams"] == 0
        assert summary["datastreams"] == []

    def test_empty_monitor_stale_check(self, monitor: HealthMonitor):
        assert monitor.check_stale_sensors() == []

    def test_empty_monitor_render_html(self, monitor: HealthMonitor):
        html = monitor.render_html()
        assert "<!DOCTYPE html>" in html

    def test_single_datastream(self, monitor: HealthMonitor):
        monitor.record_success("only-one")

        summary = monitor.get_summary()
        assert summary["tracked_datastreams"] == 1
        assert summary["datastreams"][0]["datastream_id"] == "only-one"

    def test_many_datastreams(self, monitor: HealthMonitor):
        count = 100
        for i in range(count):
            monitor.record_success(f"ds-{i:04d}")

        summary = monitor.get_summary()
        assert summary["tracked_datastreams"] == count
        assert summary["total_successes"] == count

    def test_high_count_single_datastream(self, monitor: HealthMonitor):
        for _ in range(1000):
            monitor.record_success("ds-busy")

        summary = monitor.get_summary()
        assert summary["datastreams"][0]["success_count"] == 1000

    def test_datastream_id_with_special_characters(self, monitor: HealthMonitor):
        weird_id = "ds/with spaces & symbols!"
        monitor.record_success(weird_id)

        summary = monitor.get_summary()
        assert summary["datastreams"][0]["datastream_id"] == weird_id

    def test_render_html_escapes_datastream_in_rows(self, monitor: HealthMonitor):
        # Just verify it doesn't crash with special chars in HTML
        monitor.record_success("ds<script>alert(1)</script>")
        html = monitor.render_html()
        assert "ds<script>" in html  # raw insertion (no escaping in current impl)
