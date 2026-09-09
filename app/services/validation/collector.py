"""Background snapshot loop for the validation harness.

Every ``validation_snapshot_interval_seconds`` seconds, capture a consolidated
state snapshot (health_monitor, circuit_breaker, recorder, DLQ, frost_worker)
and append one JSONL line to ``data/validation/snapshots.jsonl``.

The report generator (``scripts/generate_validation_report.py``) reads that
file to compute the 14-day rollup (uptime %, latency percentiles, error-rate
breakouts).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.health_monitor import health_monitor
from app.services.validation.recorder import recorder

logger = logging.getLogger("connector.validation")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _dlq_stats(dlq_path: Path) -> dict[str, Any]:
    """DLQ file size + line count + age of oldest entry."""
    try:
        size = dlq_path.stat().st_size
    except FileNotFoundError:
        return {"bytes": 0, "lines": 0, "oldest_epoch": None}
    lines = 0
    oldest_epoch: float | None = None
    try:
        with dlq_path.open("r", encoding="utf-8") as f:
            for i, raw in enumerate(f):
                lines = i + 1
                if oldest_epoch is None and raw.strip():
                    try:
                        record = json.loads(raw)
                        ts = record.get("timestamp")
                        if ts:
                            oldest_epoch = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
                    except (ValueError, TypeError):
                        pass
    except OSError:
        pass
    return {"bytes": size, "lines": lines, "oldest_epoch": oldest_epoch}


def _breaker_snapshot() -> dict[str, Any]:
    try:
        from app.services.sensorthings_client import observation_breaker  # local: avoid cycles
        return observation_breaker.snapshot()
    except Exception:
        logger.debug("breaker snapshot failed", exc_info=True)
        return {}


def _frost_worker_snapshot() -> dict[str, Any]:
    if not settings.frost_async_push_enabled:
        return {"enabled": False}
    try:
        from app.services.frost_worker import frost_worker
        return {"enabled": True, **frost_worker.stats()}
    except Exception:
        logger.debug("frost_worker snapshot failed", exc_info=True)
        return {"enabled": True, "error": "snapshot_failed"}


def _default_freshness_thresholds() -> dict[str, float]:
    """Per-source stale thresholds. A source that hasn't delivered within its
    poll interval + grace is considered stale for uptime accounting."""
    grace = max(60, int(settings.freshness_grace_seconds))
    return {
        "kafka": max(300.0, float(settings.kafka_tgv_poll_seconds) + grace),
        "ohnics": max(300.0, float(settings.ohnics_poll_seconds) + grace),
        "levellog": max(600.0, float(settings.levellog_poll_seconds) + grace),
    }


def build_snapshot(
    *,
    uptime_seconds: float,
    window_seconds: int,
    dlq_path: Path,
    freshness_thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Compose one snapshot dict. Extracted so unit tests can drive it directly."""
    thresholds = freshness_thresholds or _default_freshness_thresholds()
    return {
        "t": _now_iso(),
        "uptime_seconds": round(uptime_seconds, 1),
        "window_seconds": window_seconds,
        "sources": health_monitor.source_freshness(thresholds)["sources"],
        "push_stats": recorder.snapshot_targets(reset_samples=True),
        "circuit_breakers": _breaker_snapshot(),
        "dlq": _dlq_stats(dlq_path),
        "frost_worker": _frost_worker_snapshot(),
    }


async def snapshot_loop() -> None:
    """Async task: emit one snapshot line per interval while the harness is enabled."""
    if not recorder.enabled:
        logger.info("Validation harness disabled — snapshot loop is a no-op")
        return

    interval = max(30, int(settings.validation_snapshot_interval_seconds))
    snapshots_path = Path(settings.validation_data_dir) / "snapshots.jsonl"
    dlq_path = Path(settings.failed_observations_path)
    snapshots_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("Validation snapshot loop started (every %ds → %s)", interval, snapshots_path)
    started_monotonic = time.monotonic()
    freshness_thresholds = _default_freshness_thresholds()

    # Start marker so the report can pin the window boundary.
    recorder.record_incident(
        kind="harness_start",
        level="info",
        context={"interval_seconds": interval, "data_dir": str(snapshots_path.parent)},
    )

    while True:
        try:
            await asyncio.sleep(interval)
            snapshot = build_snapshot(
                uptime_seconds=time.monotonic() - started_monotonic,
                window_seconds=interval,
                dlq_path=dlq_path,
                freshness_thresholds=freshness_thresholds,
            )
            with snapshots_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot, separators=(",", ":"), default=str) + "\n")
        except asyncio.CancelledError:
            recorder.record_incident(kind="harness_stop", level="info", context={})
            raise
        except Exception:
            logger.exception("Validation snapshot cycle failed")
