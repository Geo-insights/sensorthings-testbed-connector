"""Rate limiting and request logging middleware for the MCP server."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from mcp_server.config import settings

logger = logging.getLogger(__name__)

# Known crawler/bot user-agent substrings
_BOT_SIGNATURES = frozenset({
    "googlebot", "gptbot", "bytespider", "bingbot", "yandexbot",
    "baiduspider", "duckduckbot", "facebookexternalhit", "twitterbot",
    "slurp", "linkedinbot", "semrushbot", "ahrefsbot", "mj12bot",
    "dotbot", "petalbot", "sogou", "exabot",
})


def is_crawler(user_agent: str) -> bool:
    ua_lower = user_agent.lower()
    return any(sig in ua_lower for sig in _BOT_SIGNATURES)


class TokenBucketRateLimiter:
    """IP-based token bucket rate limiter with tiered limits.

    RPM=0 disables rate limiting (for local dev).
    Authenticated callers get a separate (higher) bucket capacity.
    """

    def __init__(
        self,
        rpm: int = settings.rate_limit_rpm,
        authenticated_rpm: int = settings.rate_limit_authenticated_rpm,
    ) -> None:
        self._rpm = rpm
        self._authenticated_rpm = authenticated_rpm
        self._buckets: dict[str, dict[str, Any]] = {}

    def _get_bucket(self, key: str, rpm: int) -> dict[str, Any]:
        if key not in self._buckets:
            self._buckets[key] = {
                "tokens": float(rpm),
                "last_refill": time.monotonic(),
                "rpm": rpm,
            }
        bucket = self._buckets[key]
        # If the tier changed (e.g. user authenticated mid-session), update
        if bucket["rpm"] != rpm:
            bucket["rpm"] = rpm
            bucket["tokens"] = min(float(rpm), bucket["tokens"])
        return bucket

    @property
    def enabled(self) -> bool:
        return self._rpm > 0

    @property
    def limit(self) -> int:
        return self._rpm

    def check(self, ip: str, *, authenticated: bool = False) -> tuple[bool, int]:
        """Check if the request is allowed.

        Returns (allowed, retry_after_seconds).
        """
        if not self.enabled:
            return True, 0

        rpm = self._authenticated_rpm if authenticated else self._rpm
        # Authenticated callers get a separate bucket keyed by "auth:{ip}"
        key = f"auth:{ip}" if authenticated else ip

        now = time.monotonic()
        bucket = self._get_bucket(key, rpm)

        # Refill tokens based on elapsed time
        elapsed = now - bucket["last_refill"]
        tokens_to_add = elapsed * (rpm / 60.0)
        bucket["tokens"] = min(float(rpm), bucket["tokens"] + tokens_to_add)
        bucket["last_refill"] = now

        if bucket["tokens"] >= 0.999:
            bucket["tokens"] -= 1.0
            return True, 0

        # Calculate how long until a token is available
        deficit = 1.0 - bucket["tokens"]
        retry_after = int(deficit / (rpm / 60.0)) + 1
        return False, retry_after


class RequestLogger:
    """Structured JSON request logger to stdout."""

    def __init__(self) -> None:
        self._stdout = sys.stdout

    def log(
        self,
        tool_name: str,
        latency_ms: int,
        ip: str,
        user_agent: str,
        status: str,
        *,
        authenticated: bool = False,
        user_id: str | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tool": tool_name,
            "latency_ms": latency_ms,
            "ip": ip,
            "user_agent": user_agent,
            "status": status,
            "crawler": is_crawler(user_agent),
            "authenticated": authenticated,
        }
        if user_id:
            record["user_id"] = user_id
        try:
            self._stdout.write(json.dumps(record) + "\n")
            self._stdout.flush()
        except Exception:
            pass  # Never let logging break the request


rate_limiter = TokenBucketRateLimiter()
request_logger = RequestLogger()
