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


@dataclass
class _TargetState:
    consecutive_failures: int = 0
    opened_at: float | None = None  # time.monotonic() when the breaker opened
    last_error: str | None = None


@dataclass
class CircuitBreaker:
    """Thread-safe consecutive-failure circuit breaker keyed by target URL."""

    failure_threshold: int = 3
    cooldown_seconds: float = 600.0
    _states: dict[str, _TargetState] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _state(self, target: str) -> _TargetState:
        return self._states.setdefault(target.rstrip("/"), _TargetState())

    def allow(self, target: str) -> bool:
        """True when pushes to the target should be attempted."""
        with self._lock:
            state = self._state(target)
            if state.opened_at is None:
                return True
            if time.monotonic() - state.opened_at >= self.cooldown_seconds:
                # Half-open: allow one attempt; success closes, failure re-opens.
                state.opened_at = None
                state.consecutive_failures = max(0, self.failure_threshold - 1)
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
            state = self._state(target)
            state.consecutive_failures += 1
            state.last_error = error
            if state.consecutive_failures >= self.failure_threshold and state.opened_at is None:
                state.opened_at = time.monotonic()
                return True
            return False

    def snapshot(self) -> dict[str, dict]:
        """Current breaker state per target (for health/status endpoints)."""
        with self._lock:
            now = time.monotonic()
            return {
                target: {
                    "open": state.opened_at is not None
                    and (now - state.opened_at) < self.cooldown_seconds,
                    "consecutive_failures": state.consecutive_failures,
                    "cooldown_remaining_s": (
                        max(0.0, round(self.cooldown_seconds - (now - state.opened_at), 1))
                        if state.opened_at is not None
                        else 0.0
                    ),
                    "last_error": state.last_error,
                }
                for target, state in self._states.items()
            }
