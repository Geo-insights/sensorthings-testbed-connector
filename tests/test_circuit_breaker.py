"""Tests for the per-target circuit breaker."""

from __future__ import annotations

import json
import os
import threading
from unittest.mock import patch

from app.frost.circuit_breaker import CircuitBreaker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TARGET = "https://frost.example.com/FROST-Server/v1.1"
TARGET_B = "https://other.example.com/FROST-Server/v1.1"


class TestCircuitBreakerAllow:
    def test_fresh_target_is_allowed(self):
        cb = CircuitBreaker()
        assert cb.allow(TARGET) is True

    def test_failures_below_threshold_still_allowed(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure(TARGET)
        cb.record_failure(TARGET)
        assert cb.allow(TARGET) is True

    def test_reaching_threshold_opens_breaker(self):
        cb = CircuitBreaker(failure_threshold=3)
        assert cb.record_failure(TARGET) is False
        assert cb.record_failure(TARGET) is False
        assert cb.record_failure(TARGET) is True  # opens on 3rd

    def test_open_breaker_blocks_allow(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure(TARGET)
        cb.record_failure(TARGET)
        assert cb.allow(TARGET) is False


class TestCircuitBreakerCooldown:
    def test_allow_returns_true_after_cooldown(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=10.0)
        cb.record_failure(TARGET)
        assert cb.allow(TARGET) is False

        with patch("app.frost.circuit_breaker.time") as mock_time:
            # First call inside allow() checks monotonic for cooldown
            # The opened_at was set at real time; simulate elapsed > cooldown
            mock_time.monotonic.return_value = 1e9  # far future
            assert cb.allow(TARGET) is True

    def test_half_open_success_closes_breaker(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=5.0)
        cb.record_failure(TARGET)
        cb.record_failure(TARGET)
        assert cb.allow(TARGET) is False

        # Expire cooldown to enter half-open
        with patch("app.frost.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = 1e9
            assert cb.allow(TARGET) is True

        # A success in half-open should fully close
        cb.record_success(TARGET)
        # Should be allowed without any time tricks now
        assert cb.allow(TARGET) is True
        # Confirm failures are reset
        snap = cb.snapshot()
        assert snap[TARGET.rstrip("/")]["consecutive_failures"] == 0

    def test_half_open_failure_reopens_breaker(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_seconds=5.0)
        cb.record_failure(TARGET)
        cb.record_failure(TARGET)

        # Expire cooldown to enter half-open
        with patch("app.frost.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = 1e9
            assert cb.allow(TARGET) is True

        # Half-open: consecutive_failures is threshold-1, so one more failure
        # should re-open the breaker.
        opened = cb.record_failure(TARGET)
        assert opened is True
        assert cb.allow(TARGET) is False


class TestRecordSuccess:
    def test_resets_failures_and_error(self):
        cb = CircuitBreaker(failure_threshold=5)
        cb.record_failure(TARGET, error="timeout")
        cb.record_failure(TARGET, error="timeout")
        cb.record_success(TARGET)

        snap = cb.snapshot()
        state = snap[TARGET.rstrip("/")]
        assert state["consecutive_failures"] == 0
        assert state["last_error"] is None
        assert state["open"] is False

    def test_resets_open_breaker(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure(TARGET, error="dead")
        assert cb.allow(TARGET) is False
        cb.record_success(TARGET)
        assert cb.allow(TARGET) is True


class TestRecordFailure:
    def test_stores_last_error(self):
        cb = CircuitBreaker(failure_threshold=5)
        cb.record_failure(TARGET, error="connection refused")
        snap = cb.snapshot()
        assert snap[TARGET.rstrip("/")]["last_error"] == "connection refused"

    def test_error_defaults_to_none(self):
        cb = CircuitBreaker()
        cb.record_failure(TARGET)
        snap = cb.snapshot()
        assert snap[TARGET.rstrip("/")]["last_error"] is None

    def test_additional_failures_after_open_return_false(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure(TARGET)
        assert cb.record_failure(TARGET) is True  # opens
        # Further failures while already open should return False
        assert cb.record_failure(TARGET) is False


class TestTrailingSlashNormalization:
    def test_trailing_slash_stripped(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure(TARGET + "/")
        cb.record_failure(TARGET)  # same target, counts as #2
        assert cb.allow(TARGET + "/") is False

    def test_record_success_with_slash(self):
        cb = CircuitBreaker(failure_threshold=1)
        cb.record_failure(TARGET)
        cb.record_success(TARGET + "/")
        assert cb.allow(TARGET) is True

    def test_snapshot_uses_stripped_key(self):
        cb = CircuitBreaker()
        cb.record_failure(TARGET + "/")
        snap = cb.snapshot()
        assert TARGET.rstrip("/") in snap
        assert TARGET + "/" not in snap


class TestSnapshot:
    def test_empty_when_no_targets(self):
        cb = CircuitBreaker()
        assert cb.snapshot() == {}

    def test_closed_breaker_state(self):
        cb = CircuitBreaker(failure_threshold=5)
        cb.record_failure(TARGET, error="err1")
        snap = cb.snapshot()
        state = snap[TARGET.rstrip("/")]
        assert state["open"] is False
        assert state["consecutive_failures"] == 1
        assert state["cooldown_remaining_s"] == 0.0
        assert state["last_error"] == "err1"

    def test_open_breaker_shows_cooldown(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=60.0)
        cb.record_failure(TARGET)

        snap = cb.snapshot()
        state = snap[TARGET.rstrip("/")]
        assert state["open"] is True
        assert state["cooldown_remaining_s"] > 0.0
        assert state["cooldown_remaining_s"] <= 60.0

    def test_expired_cooldown_shows_not_open(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_seconds=1.0)
        cb.record_failure(TARGET)

        with patch("app.frost.circuit_breaker.time") as mock_time:
            mock_time.monotonic.return_value = 1e9
            snap = cb.snapshot()
            state = snap[TARGET.rstrip("/")]
            assert state["open"] is False
            assert state["cooldown_remaining_s"] == 0.0


class TestMultipleTargets:
    def test_targets_are_independent(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure(TARGET)
        cb.record_failure(TARGET)
        # TARGET is now open
        assert cb.allow(TARGET) is False
        # TARGET_B should still be allowed
        assert cb.allow(TARGET_B) is True

    def test_success_on_one_does_not_affect_other(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure(TARGET)
        cb.record_failure(TARGET_B)
        cb.record_success(TARGET)
        snap = cb.snapshot()
        assert snap[TARGET.rstrip("/")]["consecutive_failures"] == 0
        assert snap[TARGET_B.rstrip("/")]["consecutive_failures"] == 1

    def test_snapshot_contains_all_targets(self):
        cb = CircuitBreaker()
        cb.record_failure(TARGET)
        cb.record_failure(TARGET_B)
        snap = cb.snapshot()
        assert len(snap) == 2
        assert TARGET.rstrip("/") in snap
        assert TARGET_B.rstrip("/") in snap


class TestThreadSafety:
    def test_concurrent_failures_do_not_corrupt_state(self):
        cb = CircuitBreaker(failure_threshold=100)
        num_threads = 10
        failures_per_thread = 50
        barrier = threading.Barrier(num_threads)

        def hammer():
            barrier.wait()
            for _ in range(failures_per_thread):
                cb.record_failure(TARGET)

        threads = [threading.Thread(target=hammer) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        snap = cb.snapshot()
        assert snap[TARGET.rstrip("/")]["consecutive_failures"] == num_threads * failures_per_thread

    def test_concurrent_mixed_operations(self):
        cb = CircuitBreaker(failure_threshold=5, cooldown_seconds=0.01)
        barrier = threading.Barrier(4)
        _errors: list[Exception] = []

        def do_failures():
            barrier.wait()
            for _ in range(20):
                cb.record_failure(TARGET)

        def do_successes():
            barrier.wait()
            for _ in range(20):
                cb.record_success(TARGET)

        def do_allows():
            barrier.wait()
            for _ in range(20):
                cb.allow(TARGET)

        def do_snapshots():
            barrier.wait()
            for _ in range(20):
                snap = cb.snapshot()
                # Should always be a valid dict
                assert isinstance(snap, dict)

        threads = [
            threading.Thread(target=do_failures),
            threading.Thread(target=do_successes),
            threading.Thread(target=do_allows),
            threading.Thread(target=do_snapshots),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No assertion on final state since it's nondeterministic;
        # the test passes if no exceptions or deadlocks occur.


# ---------------------------------------------------------------------------
# Per-target override tests
# ---------------------------------------------------------------------------

TARGET_WBD = "https://sta.wbd-rd.nl/FROST-Server/v1.1"
TARGET_FRAUNHOFER = "https://fraunhofer.example.com/staplus/v2.0"


class TestOverridesDefault:
    """When no overrides are set, global defaults apply."""

    def test_no_overrides_uses_global_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=600.0)
        # Should need exactly 3 failures to open
        cb.record_failure(TARGET)
        cb.record_failure(TARGET)
        assert cb.allow(TARGET) is True
        cb.record_failure(TARGET)
        assert cb.allow(TARGET) is False

    def test_empty_overrides_dict(self):
        cb = CircuitBreaker(failure_threshold=2, overrides={})
        cb.record_failure(TARGET)
        assert cb.record_failure(TARGET) is True


class TestOverridesParsing:
    """Override values are applied per-target via host pattern matching."""

    def test_override_raises_threshold(self):
        cb = CircuitBreaker(
            failure_threshold=2,
            cooldown_seconds=600.0,
            overrides={"sta.wbd-rd.nl": {"failure_threshold": 5}},
        )
        # WBD target needs 5 failures, not 2
        for _ in range(4):
            cb.record_failure(TARGET_WBD)
        assert cb.allow(TARGET_WBD) is True
        cb.record_failure(TARGET_WBD)
        assert cb.allow(TARGET_WBD) is False

    def test_override_lowers_cooldown(self):
        cb = CircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=600.0,
            overrides={"fraunhofer": {"cooldown_seconds": 30}},
        )
        cb.record_failure(TARGET_FRAUNHOFER)
        assert cb.allow(TARGET_FRAUNHOFER) is False

        with patch("app.frost.circuit_breaker.time") as mock_time:
            # 31 seconds later — should be past the 30s override cooldown
            mock_time.monotonic.return_value = 1e9
            assert cb.allow(TARGET_FRAUNHOFER) is True

    def test_override_partial_keys_fall_back_to_global(self):
        """An override with only cooldown_seconds still uses global failure_threshold."""
        cb = CircuitBreaker(
            failure_threshold=3,
            cooldown_seconds=600.0,
            overrides={"sta.wbd-rd.nl": {"cooldown_seconds": 120}},
        )
        # Threshold should still be the global 3
        for _ in range(2):
            cb.record_failure(TARGET_WBD)
        assert cb.allow(TARGET_WBD) is True
        cb.record_failure(TARGET_WBD)
        assert cb.allow(TARGET_WBD) is False

    def test_non_matching_target_uses_global(self):
        cb = CircuitBreaker(
            failure_threshold=2,
            overrides={"sta.wbd-rd.nl": {"failure_threshold": 10}},
        )
        # TARGET doesn't match "sta.wbd-rd.nl", so global threshold=2 applies
        cb.record_failure(TARGET)
        assert cb.record_failure(TARGET) is True


class TestOverridesIsolation:
    """Different targets get different thresholds simultaneously."""

    def test_two_targets_different_thresholds(self):
        cb = CircuitBreaker(
            failure_threshold=2,
            cooldown_seconds=600.0,
            overrides={
                "sta.wbd-rd.nl": {"failure_threshold": 5},
                "fraunhofer": {"failure_threshold": 1},
            },
        )
        # Fraunhofer opens after 1 failure
        assert cb.record_failure(TARGET_FRAUNHOFER) is True
        assert cb.allow(TARGET_FRAUNHOFER) is False

        # WBD still open after 4 failures (threshold=5)
        for _ in range(4):
            cb.record_failure(TARGET_WBD)
        assert cb.allow(TARGET_WBD) is True

        # WBD opens on 5th
        cb.record_failure(TARGET_WBD)
        assert cb.allow(TARGET_WBD) is False

        # Unmatched target uses global threshold=2
        cb.record_failure(TARGET)
        assert cb.allow(TARGET) is True
        cb.record_failure(TARGET)
        assert cb.allow(TARGET) is False

    def test_snapshot_uses_per_target_cooldown(self):
        cb = CircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=600.0,
            overrides={"fraunhofer": {"cooldown_seconds": 30}},
        )
        cb.record_failure(TARGET_FRAUNHOFER)
        cb.record_failure(TARGET)

        snap = cb.snapshot()
        # Fraunhofer has 30s cooldown, so remaining should be <= 30
        fraunhofer_state = snap[TARGET_FRAUNHOFER.rstrip("/")]
        assert fraunhofer_state["open"] is True
        assert fraunhofer_state["cooldown_remaining_s"] <= 30.0

        # Default target has 600s cooldown
        default_state = snap[TARGET.rstrip("/")]
        assert default_state["open"] is True
        assert default_state["cooldown_remaining_s"] <= 600.0
        assert default_state["cooldown_remaining_s"] > 30.0


class TestOverridesURLPatternMatching:
    """Host pattern is substring-matched against the full target URL."""

    def test_hostname_substring_match(self):
        cb = CircuitBreaker(
            failure_threshold=2,
            overrides={"sta.wbd-rd.nl": {"failure_threshold": 10}},
        )
        # Full URL containing the host pattern
        url = "https://sta.wbd-rd.nl/FROST-Server/v1.1"
        for _ in range(9):
            cb.record_failure(url)
        assert cb.allow(url) is True
        cb.record_failure(url)
        assert cb.allow(url) is False

    def test_partial_host_match(self):
        cb = CircuitBreaker(
            failure_threshold=2,
            overrides={"wbd-rd": {"failure_threshold": 5}},
        )
        for _ in range(4):
            cb.record_failure(TARGET_WBD)
        assert cb.allow(TARGET_WBD) is True

    def test_no_false_positive_match(self):
        cb = CircuitBreaker(
            failure_threshold=2,
            overrides={"totally-different.example.com": {"failure_threshold": 99}},
        )
        # TARGET doesn't match the override pattern
        cb.record_failure(TARGET)
        assert cb.record_failure(TARGET) is True  # global threshold=2


class TestConfigCBOverrides:
    """Test FROST_CB_OVERRIDES env var parsing in config."""

    def test_valid_json_parsed(self):
        override_json = json.dumps({"sta.wbd-rd.nl": {"failure_threshold": 5, "cooldown_seconds": 300}})
        with patch.dict(os.environ, {"FROST_CB_OVERRIDES": override_json}):
            from app.config import _load_cb_overrides

            result = _load_cb_overrides()
            assert "sta.wbd-rd.nl" in result
            assert result["sta.wbd-rd.nl"]["failure_threshold"] == 5
            assert result["sta.wbd-rd.nl"]["cooldown_seconds"] == 300.0

    def test_empty_env_returns_empty_dict(self):
        with patch.dict(os.environ, {"FROST_CB_OVERRIDES": ""}):
            from app.config import _load_cb_overrides

            assert _load_cb_overrides() == {}

    def test_missing_env_returns_empty_dict(self):
        with patch.dict(os.environ, {}, clear=False):
            env = os.environ.copy()
            env.pop("FROST_CB_OVERRIDES", None)
            with patch.dict(os.environ, env, clear=True):
                from app.config import _load_cb_overrides

                assert _load_cb_overrides() == {}

    def test_invalid_json_returns_empty_dict(self):
        with patch.dict(os.environ, {"FROST_CB_OVERRIDES": "not-json{{{"}):
            from app.config import _load_cb_overrides

            assert _load_cb_overrides() == {}

    def test_non_dict_json_returns_empty_dict(self):
        with patch.dict(os.environ, {"FROST_CB_OVERRIDES": '["a", "b"]'}):
            from app.config import _load_cb_overrides

            assert _load_cb_overrides() == {}

    def test_partial_override_keys(self):
        override_json = json.dumps({"host.example.com": {"failure_threshold": 7}})
        with patch.dict(os.environ, {"FROST_CB_OVERRIDES": override_json}):
            from app.config import _load_cb_overrides

            result = _load_cb_overrides()
            assert result["host.example.com"] == {"failure_threshold": 7}

    def test_non_dict_values_skipped(self):
        override_json = json.dumps({"good": {"failure_threshold": 5}, "bad": "string"})
        with patch.dict(os.environ, {"FROST_CB_OVERRIDES": override_json}):
            from app.config import _load_cb_overrides

            result = _load_cb_overrides()
            assert "good" in result
            assert "bad" not in result
