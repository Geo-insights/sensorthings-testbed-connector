"""Health monitoring singleton.

Tracks per-datastream push success/failure counts and detects stale sensors.
Thread-safe via threading.Lock.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any


class HealthMonitor:
    """Singleton health monitor for observation push tracking."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._success_counts: dict[str, int] = {}
        self._failure_counts: dict[str, int] = {}
        self._last_push_time: dict[str, float] = {}
        self._last_failure_time: dict[str, float] = {}
        self._start_time = time.monotonic()
        # Per-source wall-clock freshness (survives meaning across the process
        # lifetime; used by the Kafka watchdog and /connector/freshness).
        self._source_last_success: dict[str, float] = {}
        self._source_success_counts: dict[str, int] = {}
        self._source_last_error: dict[str, str] = {}

    def record_success(self, datastream_id: str) -> None:
        with self._lock:
            self._success_counts[datastream_id] = self._success_counts.get(datastream_id, 0) + 1
            self._last_push_time[datastream_id] = time.monotonic()

    def record_failure(self, datastream_id: str, error: str = "") -> None:
        with self._lock:
            self._failure_counts[datastream_id] = self._failure_counts.get(datastream_id, 0) + 1
            self._last_failure_time[datastream_id] = time.monotonic()

    # ------------------------------------------------------------------
    # Per-source freshness (wall clock)
    # ------------------------------------------------------------------

    def record_source_success(self, source: str) -> None:
        """Record that ``source`` (e.g. ``"kafka"``, ``"ohnics"``) just delivered data."""
        with self._lock:
            self._source_last_success[source] = time.time()
            self._source_success_counts[source] = self._source_success_counts.get(source, 0) + 1
            self._source_last_error.pop(source, None)

    def record_source_error(self, source: str, error: str = "") -> None:
        """Record that ``source`` failed a cycle (does not update last-success)."""
        with self._lock:
            self._source_last_error[source] = error

    def source_age_seconds(self, source: str) -> float | None:
        """Seconds since ``source`` last delivered data, or None if never seen."""
        with self._lock:
            last = self._source_last_success.get(source)
        return None if last is None else max(0.0, time.time() - last)

    def source_freshness(self, thresholds: dict[str, float]) -> dict[str, Any]:
        """Return per-source freshness against the given stale thresholds.

        Args:
            thresholds: Mapping of source name -> max allowed age in seconds.

        Returns a dict with ``sources`` (per-source detail) and ``any_stale``.
        A source that has a threshold but has never delivered is reported stale
        only if the process has been up longer than its threshold (avoids a
        false alarm during the first cycle after a deploy).
        """
        now_wall = time.time()
        uptime = time.monotonic() - self._start_time
        with self._lock:
            last_success = dict(self._source_last_success)
            counts = dict(self._source_success_counts)
            last_error = dict(self._source_last_error)

        sources: dict[str, Any] = {}
        any_stale = False
        for source, threshold in thresholds.items():
            last = last_success.get(source)
            age = None if last is None else max(0.0, now_wall - last)
            if age is None:
                stale = uptime > threshold
            else:
                stale = age > threshold
            any_stale = any_stale or stale
            sources[source] = {
                "last_success_utc": (
                    datetime.fromtimestamp(last, UTC).isoformat() if last is not None else None
                ),
                "age_seconds": round(age, 1) if age is not None else None,
                "threshold_seconds": round(threshold, 1),
                "success_count": counts.get(source, 0),
                "last_error": last_error.get(source),
                "stale": stale,
            }
        return {"any_stale": any_stale, "sources": sources}

    def check_stale_sensors(self, threshold_seconds: float = 3600.0) -> list[str]:
        """Return datastream IDs that haven't had a successful push within the threshold."""
        now = time.monotonic()
        stale: list[str] = []
        with self._lock:
            for ds_id, last_time in self._last_push_time.items():
                if now - last_time > threshold_seconds:
                    stale.append(ds_id)
        return stale

    def get_summary(self) -> dict[str, Any]:
        with self._lock:
            uptime_seconds = round(time.monotonic() - self._start_time, 1)
            all_ids = sorted(set(self._success_counts.keys()) | set(self._failure_counts.keys()))
            now = time.monotonic()

            datastreams: list[dict[str, Any]] = []
            for ds_id in all_ids:
                last_push = self._last_push_time.get(ds_id)
                seconds_since = round(now - last_push, 1) if last_push is not None else None
                datastreams.append({
                    "datastream_id": ds_id,
                    "success_count": self._success_counts.get(ds_id, 0),
                    "failure_count": self._failure_counts.get(ds_id, 0),
                    "seconds_since_last_push": seconds_since,
                })

            return {
                "timestamp": datetime.now(UTC).isoformat(),
                "uptime_seconds": uptime_seconds,
                "total_successes": sum(self._success_counts.values()),
                "total_failures": sum(self._failure_counts.values()),
                "tracked_datastreams": len(all_ids),
                "datastreams": datastreams,
            }

    def render_html(self) -> str:
        summary = self.get_summary()
        rows = ""
        for ds in summary["datastreams"]:
            since = ds["seconds_since_last_push"]
            since_str = f"{since}s" if since is not None else "never"
            rows += (
                f"<tr><td>{ds['datastream_id']}</td>"
                f"<td>{ds['success_count']}</td>"
                f"<td>{ds['failure_count']}</td>"
                f"<td>{since_str}</td></tr>\n"
            )
        return f"""<!DOCTYPE html>
<html><head><title>Health Report</title>
<style>table {{border-collapse: collapse;}} th, td {{border: 1px solid #ccc; padding: 6px 12px; text-align: left;}}</style>
</head><body>
<h1>Connector Health Report</h1>
<p>Uptime: {summary['uptime_seconds']}s | Successes: {summary['total_successes']} | Failures: {summary['total_failures']}</p>
<table><tr><th>Datastream</th><th>Successes</th><th>Failures</th><th>Since Last Push</th></tr>
{rows}</table>
<p><em>Generated: {summary['timestamp']}</em></p>
</body></html>"""


# Module-level singleton
health_monitor = HealthMonitor()
