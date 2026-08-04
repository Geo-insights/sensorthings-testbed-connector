"""Background FROST push worker (decouples the slow push from Kafka consume).

The Kafka consume loop forwards readings to the monitoring module, hands the
same readings to this worker's bounded queue, commits the Kafka offset, and
immediately consumes the next batch — so Kafka stays live (heartbeats safe, no
max-poll eviction) and the SensorThings/FROST mirror catches up asynchronously.

Durability: FROST push failures are dead-lettered by ``push_observations``
itself (the DLQ + replay loop), and the monitoring forward is idempotent, so
committing the Kafka offset before the FROST push completes is at-least-once,
not at-most-once. The only loss window is a hard crash with readings still in
the in-memory queue; that is bounded by ``queue_max`` and drained on shutdown.

Backpressure: the queue is bounded. When it is full the caller's ``enqueue``
returns ``False`` (after an optional wait) so the consume loop can push inline
instead of dropping data — the classic "block, don't drop" backpressure.

A single worker thread is used on purpose: ``push_observations`` mutates shared
caches and circuit-breaker state that are not safe to drive from several
threads at once. Push concurrency comes from parallel per-target/per-chunk
requests inside ``push_observations``, not from multiple workers here.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("connector.events")

# Sentinel enqueued by ``stop`` to unblock and terminate the worker thread.
_SHUTDOWN = object()


class FrostPushWorker:
    """Single-threaded, bounded-queue background pusher to the FROST targets."""

    def __init__(self) -> None:
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._started = False
        self._coalesce_max = 2000
        self._backlog_alert_depth = 0
        self._backlog_alerting = False
        # Metrics (guarded by _lock).
        self._queued_total = 0
        self._pushed_total = 0
        self._inline_fallback_total = 0
        self._last_push_seconds: float | None = None
        self._last_push_at: float | None = None
        self._last_error: str | None = None
        self._last_frost_lag_seconds: float | None = None
        self._depth_high_water = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, maxsize: int, coalesce_max: int, backlog_alert_ratio: float = 0.8) -> None:
        with self._lock:
            if self._started:
                return
            self._queue = queue.Queue(maxsize=max(1, maxsize))
            self._coalesce_max = max(1, coalesce_max)
            self._backlog_alert_depth = max(1, int(max(1, maxsize) * max(0.0, min(1.0, backlog_alert_ratio))))
            self._thread = threading.Thread(target=self._run, name="frost-push-worker", daemon=True)
            self._started = True
            self._thread.start()
        logger.info(
            "FROST push worker started (queue_max=%d, coalesce_max=%d, backlog_alert_depth=%d)",
            maxsize, coalesce_max, self._backlog_alert_depth,
        )

    def stop(self, drain_timeout: float = 30.0) -> None:
        """Signal the worker to drain the queue and stop; join up to timeout."""
        with self._lock:
            if not self._started:
                return
            thread = self._thread
        try:
            self._queue.put(_SHUTDOWN, timeout=5)
        except queue.Full:
            logger.warning("FROST worker queue full while stopping; forcing shutdown")
        if thread is not None:
            thread.join(timeout=drain_timeout)

    def is_started(self) -> bool:
        with self._lock:
            return self._started

    # ------------------------------------------------------------------
    # Producer API
    # ------------------------------------------------------------------
    def enqueue(self, readings: list, timeout: float) -> bool:
        """Buffer a batch for the worker.

        Returns ``True`` when queued. Returns ``False`` when the worker is not
        running or the queue stayed full for ``timeout`` seconds — the caller
        should then push inline so nothing is dropped.
        """
        if not readings:
            return True
        with self._lock:
            if not self._started:
                return False
        try:
            self._queue.put(list(readings), timeout=max(0.0, timeout))
        except queue.Full:
            return False
        with self._lock:
            self._queued_total += len(readings)
            depth = self._queue.qsize()
            if depth > self._depth_high_water:
                self._depth_high_water = depth
        return True

    def record_inline_fallback(self) -> None:
        with self._lock:
            self._inline_fallback_total += 1

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------
    def _run(self) -> None:
        from app.services.sensorthings_client import client

        while True:
            item = self._queue.get()
            if item is _SHUTDOWN:
                self._queue.task_done()
                logger.info("FROST push worker stopping")
                return
            batch = list(item)
            self._queue.task_done()
            # Coalesce whatever else is already queued (up to the cap) into one
            # push so a larger dataArray amortises the per-request cost.
            stopping = False
            while len(batch) < self._coalesce_max:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is _SHUTDOWN:
                    self._queue.task_done()
                    stopping = True
                    break
                batch.extend(nxt)
                self._queue.task_done()
            self._push(batch, client)
            if stopping:
                logger.info("FROST push worker stopping (queue drained)")
                return
            self._maybe_alert_backlog()

    def _push(self, batch: list, client) -> None:
        if not batch:
            return
        t0 = time.monotonic()
        sent = 0
        error: str | None = None
        try:
            result = client.push_observations(batch)
            sent = int(result.get("total_sent", 0) or 0)
        except Exception as exc:  # push_observations DLQs internally; guard anyway
            logger.exception("FROST worker push failed for %d readings", len(batch))
            error = f"{type(exc).__name__}: {exc}"
        dur = time.monotonic() - t0
        newest = max(
            (r.timestamp for r in batch if getattr(r, "timestamp", None) is not None),
            default=None,
        )
        lag = (datetime.now(UTC) - newest).total_seconds() if newest is not None else None
        with self._lock:
            self._pushed_total += sent
            self._last_push_seconds = round(dur, 2)
            self._last_push_at = time.time()
            self._last_error = error
            self._last_frost_lag_seconds = round(lag, 1) if lag is not None else None

    def _maybe_alert_backlog(self) -> None:
        """Alert when the queue depth stays high (worker falling behind)."""
        from app.services.alerting import send_alert

        depth = self._queue.qsize()
        maxsize = self._queue.maxsize
        if depth >= self._backlog_alert_depth:
            if not self._backlog_alerting:
                self._backlog_alerting = True
            send_alert(
                "frost.worker_backlog",
                f"FROST push worker backlog: {depth}/{maxsize} batches queued "
                "— the SensorThings mirror is falling behind ingest.",
                level="warning",
                context={"depth": depth, "queue_max": maxsize},
                dedup_key="frost.worker_backlog",
            )
        elif self._backlog_alerting and depth == 0:
            self._backlog_alerting = False
            send_alert(
                "frost.worker_backlog",
                "FROST push worker backlog cleared — mirror caught up.",
                level="info",
                context={"depth": depth, "queue_max": maxsize},
                dedup_key="frost.worker_backlog",
                force=True,
            )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        with self._lock:
            started = self._started
            depth = self._queue.qsize() if started else 0
            return {
                "started": started,
                "depth": depth,
                "depth_high_water": self._depth_high_water,
                "queue_max": self._queue.maxsize if started else 0,
                "coalesce_max": self._coalesce_max,
                "queued_total": self._queued_total,
                "pushed_total": self._pushed_total,
                "inline_fallback_total": self._inline_fallback_total,
                "last_push_seconds": self._last_push_seconds,
                "last_frost_lag_seconds": self._last_frost_lag_seconds,
                "last_error": self._last_error,
                "last_push_age_seconds": (
                    round(time.time() - self._last_push_at, 1) if self._last_push_at is not None else None
                ),
            }


# Module-level singleton.
frost_worker = FrostPushWorker()
