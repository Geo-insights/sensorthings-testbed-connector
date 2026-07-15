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

    def record_success(self, datastream_id: str) -> None:
        with self._lock:
            self._success_counts[datastream_id] = self._success_counts.get(datastream_id, 0) + 1
            self._last_push_time[datastream_id] = time.monotonic()

    def record_failure(self, datastream_id: str, error: str = "") -> None:
        with self._lock:
            self._failure_counts[datastream_id] = self._failure_counts.get(datastream_id, 0) + 1
            self._last_failure_time[datastream_id] = time.monotonic()

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
