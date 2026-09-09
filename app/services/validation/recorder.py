"""Thread-safe recorder that captures push attempts + incidents for the harness.

Two things happen on every call:

1. In-memory rolling aggregates per ``(source, target)`` key are updated:
   attempt counts, sent/failed/dropped counts, errors bucketed by taxonomy
   class, and bounded reservoirs of push-batch latency and per-observation
   age-at-push (source_time → push_completed).

2. An append-only line is written to disk:
   * ``events.jsonl`` — only when the attempt was non-clean (any failed /
     dropped counts or an ``error_class`` set), so this file stays small even
     on a healthy 14-day run.
   * ``incidents.jsonl`` — always, for every incident.

Files are rotated by size to survive multi-day runs on Render's persistent
disk. When the harness is disabled all methods are cheap no-ops.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("connector.validation")

MONITORING_TARGET = "monitoring"  # pseudo-target label for the HTTP monitoring push


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _percentiles(samples: list[float] | deque[float], ps: tuple[int, ...] = (50, 95, 99)) -> dict[str, Any]:
    """Compute simple nearest-rank percentiles from an unsorted sample set."""
    if not samples:
        return {}
    ordered = sorted(samples)
    n = len(ordered)
    out: dict[str, Any] = {
        "count": n,
        "min": round(ordered[0], 2),
        "max": round(ordered[-1], 2),
    }
    for p in ps:
        # Nearest-rank method: rank = ceil(p * n / 100). Using math.ceil avoids
        # Python's banker's-rounding behaviour in round() which pushes p50 of a
        # 5-sample stream to the wrong element.
        rank = max(0, min(n - 1, math.ceil(p / 100.0 * n) - 1))
        out[f"p{p}"] = round(ordered[rank], 2)
    return out


class _TargetStats:
    __slots__ = (
        "age_at_push_samples",
        "attempts",
        "dropped_circuit",
        "dropped_permanent",
        "errors_by_class",
        "failed",
        "last_failure_epoch",
        "last_success_epoch",
        "latency_samples",
        "sent",
    )

    def __init__(self, sample_cap: int) -> None:
        self.attempts: int = 0
        self.sent: int = 0
        self.failed: int = 0
        self.dropped_permanent: int = 0
        self.dropped_circuit: int = 0
        self.errors_by_class: dict[str, int] = {}
        self.latency_samples: deque[float] = deque(maxlen=sample_cap)
        self.age_at_push_samples: deque[float] = deque(maxlen=sample_cap)
        self.last_success_epoch: float | None = None
        self.last_failure_epoch: float | None = None


class ValidationRecorder:
    """Singleton harness recorder. Safe no-op when ``enabled`` is False."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = False
        self._data_dir = Path("data/validation")
        self._events_max_bytes = 20 * 1024 * 1024
        self._incidents_max_bytes = 5 * 1024 * 1024
        self._sample_reservoir_size = 1000
        self._rotate_keep = 5
        self._stats: dict[str, _TargetStats] = {}
        self._started_monotonic = time.monotonic()
        self._started_wall = time.time()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def configure(
        self,
        *,
        enabled: bool,
        data_dir: str | Path,
        events_max_bytes: int,
        incidents_max_bytes: int,
        sample_reservoir_size: int,
        rotate_keep: int = 5,
    ) -> None:
        """Wire runtime settings. Call once from lifespan() at startup."""
        with self._lock:
            self._enabled = bool(enabled)
            self._data_dir = Path(data_dir)
            self._events_max_bytes = max(1024, int(events_max_bytes))
            self._incidents_max_bytes = max(1024, int(incidents_max_bytes))
            self._sample_reservoir_size = max(100, int(sample_reservoir_size))
            self._rotate_keep = max(1, int(rotate_keep))
            if self._enabled:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                self._started_monotonic = time.monotonic()
                self._started_wall = time.time()
                logger.info("Validation harness ENABLED, writing to %s", self._data_dir)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    def reset(self) -> None:
        """Drop all in-memory aggregates. Test-only helper; production use of the
        singleton never calls this — a fresh run is a fresh process."""
        with self._lock:
            self._stats.clear()
            self._started_monotonic = time.monotonic()
            self._started_wall = time.time()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def record_push_attempt(
        self,
        *,
        source: str,
        target: str,
        target_label: str = "",
        duration_ms: float,
        readings_count: int,
        sent_count: int,
        failed_count: int = 0,
        dropped_permanent_count: int = 0,
        dropped_circuit_count: int = 0,
        error_class: str | None = None,
        error_msg: str | None = None,
        age_at_push_samples: list[float] | None = None,
    ) -> None:
        """Record one batch-level push attempt.

        Writes to ``events.jsonl`` only when the attempt was NOT fully clean
        (any non-zero failed/dropped counts or an ``error_class``).
        In-memory aggregates are always updated so healthy pushes still show
        up in the /validation/summary rollup and in snapshot latency samples.
        """
        if not self._enabled:
            return
        key = f"{source}::{target}"
        now = time.time()
        with self._lock:
            stats = self._stats.get(key)
            if stats is None:
                stats = _TargetStats(self._sample_reservoir_size)
                self._stats[key] = stats
            stats.attempts += 1
            stats.sent += max(0, int(sent_count))
            stats.failed += max(0, int(failed_count))
            stats.dropped_permanent += max(0, int(dropped_permanent_count))
            stats.dropped_circuit += max(0, int(dropped_circuit_count))
            if sent_count > 0:
                stats.last_success_epoch = now
            if failed_count > 0 or dropped_permanent_count > 0 or dropped_circuit_count > 0:
                stats.last_failure_epoch = now
            if error_class:
                stats.errors_by_class[error_class] = stats.errors_by_class.get(error_class, 0) + 1
            if duration_ms is not None:
                stats.latency_samples.append(float(duration_ms))
            if age_at_push_samples:
                for s in age_at_push_samples:
                    stats.age_at_push_samples.append(float(s))

        if failed_count > 0 or dropped_permanent_count > 0 or dropped_circuit_count > 0 or error_class:
            self._append(self._events_path(), {
                "t": _now_iso(),
                "kind": "push_attempt",
                "source": source,
                "target": target,
                "target_label": target_label,
                "duration_ms": round(float(duration_ms), 2) if duration_ms is not None else None,
                "readings_count": int(readings_count),
                "sent": int(sent_count),
                "failed": int(failed_count),
                "dropped_permanent": int(dropped_permanent_count),
                "dropped_circuit": int(dropped_circuit_count),
                "error_class": error_class,
                "error_msg": (error_msg or "")[:400] or None,
            }, self._events_max_bytes)

    def record_incident(
        self,
        *,
        kind: str,
        target: str | None = None,
        source: str | None = None,
        level: str = "info",
        context: dict[str, Any] | None = None,
    ) -> None:
        """Append one line to ``incidents.jsonl``."""
        if not self._enabled:
            return
        self._append(self._incidents_path(), {
            "t": _now_iso(),
            "kind": kind,
            "target": target,
            "source": source,
            "level": level,
            "context": context or {},
        }, self._incidents_max_bytes)

    # ------------------------------------------------------------------
    # Snapshot (called by collector; resets the rolling reservoirs)
    # ------------------------------------------------------------------
    def snapshot_targets(self, *, reset_samples: bool = True) -> dict[str, dict[str, Any]]:
        """Per-(source,target) rollup. Optionally reset latency/age reservoirs."""
        if not self._enabled:
            return {}
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            for key, stats in self._stats.items():
                out[key] = {
                    "attempts": stats.attempts,
                    "sent": stats.sent,
                    "failed": stats.failed,
                    "dropped_permanent": stats.dropped_permanent,
                    "dropped_circuit": stats.dropped_circuit,
                    "errors_by_class": dict(stats.errors_by_class),
                    "latency_ms": _percentiles(stats.latency_samples),
                    "age_at_push_s": _percentiles(stats.age_at_push_samples),
                    "last_success_epoch": stats.last_success_epoch,
                    "last_failure_epoch": stats.last_failure_epoch,
                }
                if reset_samples:
                    stats.latency_samples.clear()
                    stats.age_at_push_samples.clear()
        return out

    def summary(self) -> dict[str, Any]:
        """Non-destructive summary for /validation/summary."""
        return {
            "enabled": self._enabled,
            "uptime_seconds": round(time.monotonic() - self._started_monotonic, 1),
            "started_utc": datetime.fromtimestamp(self._started_wall, UTC).isoformat().replace("+00:00", "Z"),
            "data_dir": str(self._data_dir),
            "targets": self.snapshot_targets(reset_samples=False),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _events_path(self) -> Path:
        return self._data_dir / "events.jsonl"

    def _incidents_path(self) -> Path:
        return self._data_dir / "incidents.jsonl"

    def _append(self, path: Path, line: dict[str, Any], max_bytes: int) -> None:
        try:
            self._maybe_rotate(path, max_bytes)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, separators=(",", ":"), default=str) + "\n")
        except OSError as exc:
            logger.warning("Validation write failed (%s): %s", path.name, exc)

    def _maybe_rotate(self, path: Path, max_bytes: int) -> None:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        if size < max_bytes:
            return
        keep = self._rotate_keep
        # Drop the oldest, then shift .N-1 → .N, ..., .1 → .2, base → .1.
        oldest = path.with_name(path.name + f".{keep}")
        try:
            if oldest.exists():
                oldest.unlink()
        except OSError:
            pass
        for i in range(keep - 1, 0, -1):
            src = path.with_name(path.name + f".{i}")
            dst = path.with_name(path.name + f".{i + 1}")
            try:
                if src.exists():
                    os.replace(str(src), str(dst))
            except OSError:
                pass
        with contextlib.suppress(OSError):
            os.replace(str(path), str(path.with_name(path.name + ".1")))


# Module-level singleton.
recorder = ValidationRecorder()
