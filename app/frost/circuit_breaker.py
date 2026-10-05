"""Per-target circuit breaker for observation pushes.

Prevents a dead/slow FROST target from stalling every push cycle: after a
number of consecutive failed cycles, the target is "open" (skipped) for a
cooldown period. Readings for an open target are written straight to the
dead-letter queue and re-sent by the replay loop once the target recovers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class _TargetState:
    consecutive_failures: int = 0
    opened_at: float | None = None  # time.monotonic() when the breaker opened
    last_error: str | None = None


@dataclass
class CircuitBreaker:
    """Thread-safe consecutive-failure circuit breaker keyed by target URL.

    Per-target threshold overrides can be supplied via *overrides*: a dict
    keyed by host pattern (substring-matched against the target URL) whose
    values are dicts with optional ``failure_threshold`` and/or
    ``cooldown_seconds`` keys.  Missing keys fall back to the instance-level
    defaults.
    """

    failure_threshold: int = 3
    cooldown_seconds: float = 600.0
    overrides: dict[str, dict[str, int | float]] = field(default_factory=dict)
    _states: dict[str, _TargetState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Cache resolved overrides so the substring scan runs at most once per
    # normalised target URL.
    _resolved: dict[str, tuple[int, float]] = field(default_factory=dict)

    def _thresholds(self, target_key: str) -> tuple[int, float]:
        """Return *(failure_threshold, cooldown_seconds)* for *target_key*.

        Checks ``self.overrides`` for a key that is a substring of
        *target_key* (typically the host portion matches).  The result is
        cached so repeated calls are O(1).
        """
        cached = self._resolved.get(target_key)
        if cached is not None:
            return cached

        ft = self.failure_threshold
        cd = self.cooldown_seconds
        for pattern, values in self.overrides.items():
            if pattern in target_key or pattern in (urlparse(target_key).hostname or ""):
                ft = int(values.get("failure_threshold", ft))
                cd = float(values.get("cooldown_seconds", cd))
                break
        result = (ft, cd)
        self._resolved[target_key] = result
        return result

    def _state(self, target: str) -> _TargetState:
        return self._states.setdefault(target.rstrip("/"), _TargetState())

    def allow(self, target: str) -> bool:
        """True when pushes to the target should be attempted."""
        with self._lock:
            key = target.rstrip("/")
            state = self._state(key)
            ft, cd = self._thresholds(key)
            if state.opened_at is None:
                return True
            if time.monotonic() - state.opened_at >= cd:
                # Half-open: allow one attempt; success closes, failure re-opens.
                state.opened_at = None
                state.consecutive_failures = max(0, ft - 1)
                return True
            return False

    def record_success(self, target: str) -> None:
        with self._lock:
            state = self._state(target)
            state.consecutive_failures = 0
            state.opened_at = None
            state.last_error = None

    def record_failure(self, target: str, error: str | None = None) -> bool:
        """Record a failed cycle. Returns True when the breaker (re-)opens."""
        with self._lock:
            key = target.rstrip("/")
            state = self._state(key)
            ft, _cd = self._thresholds(key)
            state.consecutive_failures += 1
            state.last_error = error
            if state.consecutive_failures >= ft and state.opened_at is None:
                state.opened_at = time.monotonic()
                return True
            return False

    def snapshot(self) -> dict[str, dict]:
        """Current breaker state per target (for health/status endpoints)."""
        with self._lock:
            now = time.monotonic()
            result: dict[str, dict] = {}
            for target, state in self._states.items():
                _ft, cd = self._thresholds(target)
                result[target] = {
                    "open": state.opened_at is not None
                    and (now - state.opened_at) < cd,
                    "consecutive_failures": state.consecutive_failures,
                    "cooldown_remaining_s": (
                        max(0.0, round(cd - (now - state.opened_at), 1))
                        if state.opened_at is not None
                        else 0.0
                    ),
                    "last_error": state.last_error,
                }
            return result
