"""Tests for the FrostPushWorker background push worker."""

from __future__ import annotations

import queue
import threading
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from app.models import SensorReading
from app.services.frost_worker import FrostPushWorker, _SHUTDOWN


def _make_reading(**overrides) -> SensorReading:
    defaults = dict(
        sensor_id="s1",
        sensor_name="Sensor 1",
        observed_property="temperature",
        unit="degC",
        value=21.5,
        timestamp=datetime.now(UTC),
    )
    defaults.update(overrides)
    return SensorReading(**defaults)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_not_started_initially(self):
        w = FrostPushWorker()
        assert w.is_started() is False

    def test_start_creates_thread(self):
        barrier = threading.Event()

        def _blocking_run(self_inner):
            barrier.wait(timeout=5)

        with patch.object(FrostPushWorker, "_run", _blocking_run):
            w = FrostPushWorker()
            w.start(maxsize=10, coalesce_max=100)
            assert w.is_started() is True
            assert w._thread is not None
            assert w._thread.is_alive()
            barrier.set()
            w._thread.join(timeout=2)

    @patch("app.services.frost_worker.FrostPushWorker._run")
    def test_double_start_is_idempotent(self, mock_run):
        w = FrostPushWorker()
        w.start(maxsize=10, coalesce_max=100)
        thread1 = w._thread
        w.start(maxsize=20, coalesce_max=200)
        thread2 = w._thread
        assert thread1 is thread2
        w._thread.join(timeout=2)

    def test_stop_without_start_is_noop(self):
        w = FrostPushWorker()
        w.stop(drain_timeout=1)  # Should not raise

    def test_start_and_stop(self):
        w = FrostPushWorker()
        with patch("app.services.sensorthings_client.client", new=MagicMock()):
            w.start(maxsize=10, coalesce_max=100)
            assert w.is_started() is True
            w.stop(drain_timeout=5)
            # Thread should have exited
            assert not w._thread.is_alive()


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


class TestEnqueue:
    def test_enqueue_returns_false_when_not_started(self):
        w = FrostPushWorker()
        readings = [_make_reading()]
        assert w.enqueue(readings, timeout=0.1) is False

    def test_enqueue_empty_list_returns_true(self):
        w = FrostPushWorker()
        assert w.enqueue([], timeout=0.1) is True

    @patch("app.services.frost_worker.FrostPushWorker._run")
    def test_enqueue_returns_true_when_started(self, mock_run):
        w = FrostPushWorker()
        w.start(maxsize=10, coalesce_max=100)
        readings = [_make_reading()]
        assert w.enqueue(readings, timeout=1) is True
        w._thread.join(timeout=2)

    @patch("app.services.frost_worker.FrostPushWorker._run")
    def test_enqueue_returns_false_on_full_queue(self, mock_run):
        w = FrostPushWorker()
        w.start(maxsize=1, coalesce_max=100)
        # Fill the queue
        w.enqueue([_make_reading()], timeout=1)
        # Queue is full (maxsize=1), second enqueue should fail
        assert w.enqueue([_make_reading()], timeout=0.01) is False
        w._thread.join(timeout=2)

    @patch("app.services.frost_worker.FrostPushWorker._run")
    def test_enqueue_updates_queued_total(self, mock_run):
        w = FrostPushWorker()
        w.start(maxsize=10, coalesce_max=100)
        readings = [_make_reading(), _make_reading()]
        w.enqueue(readings, timeout=1)
        assert w._queued_total == 2
        w._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# record_inline_fallback
# ---------------------------------------------------------------------------


class TestInlineFallback:
    def test_record_inline_fallback_increments(self):
        w = FrostPushWorker()
        assert w._inline_fallback_total == 0
        w.record_inline_fallback()
        assert w._inline_fallback_total == 1
        w.record_inline_fallback()
        w.record_inline_fallback()
        assert w._inline_fallback_total == 3


# ---------------------------------------------------------------------------
# _push
# ---------------------------------------------------------------------------


class TestPush:
    def test_push_empty_batch_is_noop(self):
        w = FrostPushWorker()
        mock_client = MagicMock()
        w._push([], mock_client)
        mock_client.push_observations.assert_not_called()

    def test_push_calls_client_and_records_metrics(self):
        w = FrostPushWorker()
        mock_client = MagicMock()
        mock_client.push_observations.return_value = {"total_sent": 3}
        readings = [_make_reading(), _make_reading(), _make_reading()]

        w._push(readings, mock_client)

        mock_client.push_observations.assert_called_once_with(readings)
        assert w._pushed_total == 3
        assert w._last_push_seconds is not None
        assert w._last_push_at is not None
        assert w._last_error is None

    def test_push_handles_exception_gracefully(self):
        w = FrostPushWorker()
        mock_client = MagicMock()
        mock_client.push_observations.side_effect = RuntimeError("connection refused")
        readings = [_make_reading()]

        w._push(readings, mock_client)

        assert w._pushed_total == 0
        assert w._last_error is not None
        assert "RuntimeError" in w._last_error
        assert "connection refused" in w._last_error

    def test_push_computes_frost_lag(self):
        w = FrostPushWorker()
        mock_client = MagicMock()
        mock_client.push_observations.return_value = {"total_sent": 1}
        ts = datetime.now(UTC)
        readings = [_make_reading(timestamp=ts)]

        w._push(readings, mock_client)

        # Lag should be very small (just computed)
        assert w._last_frost_lag_seconds is not None
        assert w._last_frost_lag_seconds >= 0.0

    def test_push_handles_none_total_sent(self):
        w = FrostPushWorker()
        mock_client = MagicMock()
        mock_client.push_observations.return_value = {"total_sent": None}
        readings = [_make_reading()]

        w._push(readings, mock_client)

        assert w._pushed_total == 0
        assert w._last_error is None


# ---------------------------------------------------------------------------
# Worker loop (_run) — coalescing and shutdown
# ---------------------------------------------------------------------------


class TestWorkerLoop:
    def test_worker_processes_single_batch(self):
        w = FrostPushWorker()
        mock_client = MagicMock()
        mock_client.push_observations.return_value = {"total_sent": 2}
        readings = [_make_reading(), _make_reading()]

        with patch("app.services.sensorthings_client.client", mock_client), \
             patch.object(w, "_maybe_alert_backlog"):
            w.start(maxsize=10, coalesce_max=100)
            w.enqueue(readings, timeout=1)
            w.stop(drain_timeout=5)

        mock_client.push_observations.assert_called()
        assert w._pushed_total == 2

    def test_worker_coalesces_batches(self):
        """When multiple batches are queued before the worker picks them up,
        it should coalesce them into one push call."""
        w = FrostPushWorker()
        mock_client = MagicMock()
        mock_client.push_observations.return_value = {"total_sent": 0}

        # Pre-fill the queue before starting the worker so coalescing happens
        w._queue = queue.Queue(maxsize=10)
        w._queue.put([_make_reading(value=1)])
        w._queue.put([_make_reading(value=2)])
        w._queue.put([_make_reading(value=3)])

        with patch("app.services.sensorthings_client.client", mock_client), \
             patch.object(w, "_maybe_alert_backlog"):
            w._coalesce_max = 100
            w._started = True
            w._thread = threading.Thread(target=w._run, daemon=True)
            w._thread.start()
            # Give the worker time to process then stop
            time.sleep(0.2)
            w._queue.put(_SHUTDOWN)
            w._thread.join(timeout=5)

        # All three single-reading batches should be coalesced into one call
        assert mock_client.push_observations.call_count == 1
        batch_arg = mock_client.push_observations.call_args[0][0]
        assert len(batch_arg) == 3

    def test_coalesce_respects_max(self):
        """Coalescing should stop at coalesce_max readings."""
        w = FrostPushWorker()
        mock_client = MagicMock()
        mock_client.push_observations.return_value = {"total_sent": 0}

        # Pre-fill with 5 single-reading batches but coalesce_max=3
        w._queue = queue.Queue(maxsize=10)
        for i in range(5):
            w._queue.put([_make_reading(value=float(i))])

        with patch("app.services.sensorthings_client.client", mock_client), \
             patch.object(w, "_maybe_alert_backlog"):
            w._coalesce_max = 3
            w._started = True
            w._thread = threading.Thread(target=w._run, daemon=True)
            w._thread.start()
            time.sleep(0.3)
            w._queue.put(_SHUTDOWN)
            w._thread.join(timeout=5)

        # Should have been split: first push coalesces 3 (hits cap), second gets remaining 2
        assert mock_client.push_observations.call_count == 2
        first_batch = mock_client.push_observations.call_args_list[0][0][0]
        second_batch = mock_client.push_observations.call_args_list[1][0][0]
        assert len(first_batch) == 3
        assert len(second_batch) == 2

    def test_shutdown_sentinel_drains_remaining(self):
        """When _SHUTDOWN is encountered during coalescing, the worker should
        push the current batch and then exit."""
        w = FrostPushWorker()
        mock_client = MagicMock()
        mock_client.push_observations.return_value = {"total_sent": 0}

        # Put a batch, then shutdown sentinel
        w._queue = queue.Queue(maxsize=10)
        w._queue.put([_make_reading(value=1), _make_reading(value=2)])
        w._queue.put(_SHUTDOWN)

        with patch("app.services.sensorthings_client.client", mock_client), \
             patch.object(w, "_maybe_alert_backlog"):
            w._coalesce_max = 100
            w._started = True
            w._thread = threading.Thread(target=w._run, daemon=True)
            w._thread.start()
            w._thread.join(timeout=5)

        # The batch before shutdown should have been pushed
        mock_client.push_observations.assert_called_once()
        batch_arg = mock_client.push_observations.call_args[0][0]
        assert len(batch_arg) == 2


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    def test_stats_initial(self):
        w = FrostPushWorker()
        s = w.stats()
        assert s["started"] is False
        assert s["depth"] == 0
        assert s["queued_total"] == 0
        assert s["pushed_total"] == 0
        assert s["inline_fallback_total"] == 0
        assert s["last_push_seconds"] is None
        assert s["last_frost_lag_seconds"] is None
        assert s["last_error"] is None
        assert s["last_push_age_seconds"] is None

    def test_stats_after_push(self):
        w = FrostPushWorker()
        mock_client = MagicMock()
        mock_client.push_observations.return_value = {"total_sent": 5}
        readings = [_make_reading() for _ in range(5)]

        w._push(readings, mock_client)
        s = w.stats()

        assert s["pushed_total"] == 5
        assert s["last_push_seconds"] is not None
        assert s["last_push_age_seconds"] is not None
        assert s["last_error"] is None

    def test_stats_after_error(self):
        w = FrostPushWorker()
        mock_client = MagicMock()
        mock_client.push_observations.side_effect = ValueError("bad data")

        w._push([_make_reading()], mock_client)
        s = w.stats()

        assert s["last_error"] is not None
        assert "ValueError" in s["last_error"]

    def test_stats_after_inline_fallback(self):
        w = FrostPushWorker()
        w.record_inline_fallback()
        w.record_inline_fallback()
        s = w.stats()
        assert s["inline_fallback_total"] == 2

    @patch("app.services.frost_worker.FrostPushWorker._run")
    def test_stats_shows_queue_max_when_started(self, mock_run):
        w = FrostPushWorker()
        w.start(maxsize=50, coalesce_max=200)
        s = w.stats()
        assert s["started"] is True
        assert s["queue_max"] == 50
        assert s["coalesce_max"] == 200
        w._thread.join(timeout=2)

    @patch("app.services.frost_worker.FrostPushWorker._run")
    def test_stats_tracks_depth_high_water(self, mock_run):
        w = FrostPushWorker()
        w.start(maxsize=10, coalesce_max=100)
        w.enqueue([_make_reading()], timeout=1)
        w.enqueue([_make_reading(), _make_reading()], timeout=1)
        s = w.stats()
        assert s["depth_high_water"] >= 1
        w._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# Backlog alerting
# ---------------------------------------------------------------------------


class TestBacklogAlert:
    def test_alert_fires_when_depth_exceeds_threshold(self):
        w = FrostPushWorker()
        w._backlog_alert_depth = 2
        w._queue = queue.Queue(maxsize=10)
        # Fill queue to trigger alert
        for _ in range(3):
            w._queue.put("dummy")

        with patch("app.services.alerting.send_alert") as mock_alert:
            w._maybe_alert_backlog()

        mock_alert.assert_called_once()
        call_args = mock_alert.call_args
        assert call_args[0][0] == "frost.worker_backlog"
        assert call_args[1]["level"] == "warning"
        assert w._backlog_alerting is True

    def test_alert_clears_when_depth_drops_to_zero(self):
        w = FrostPushWorker()
        w._backlog_alert_depth = 2
        w._backlog_alerting = True
        w._queue = queue.Queue(maxsize=10)
        # Queue is empty

        with patch("app.services.alerting.send_alert") as mock_alert:
            w._maybe_alert_backlog()

        mock_alert.assert_called_once()
        call_args = mock_alert.call_args
        assert call_args[1]["level"] == "info"
        assert w._backlog_alerting is False

    def test_no_alert_when_below_threshold(self):
        w = FrostPushWorker()
        w._backlog_alert_depth = 5
        w._queue = queue.Queue(maxsize=10)
        w._queue.put("dummy")  # depth=1, below threshold of 5

        with patch("app.services.alerting.send_alert") as mock_alert:
            w._maybe_alert_backlog()

        mock_alert.assert_not_called()
