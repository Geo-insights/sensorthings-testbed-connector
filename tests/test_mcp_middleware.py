"""Tests for MCP server middleware (rate limiter + request logger).

protects: rate limiting behavior, logging output format
fails_when: rate limit not enforced, log fields missing
no_claim: real IP extraction, real user-agent parsing
"""

from __future__ import annotations

import json
from io import StringIO

from mcp_server.middleware import (
    RequestLogger,
    TokenBucketRateLimiter,
    is_crawler,
)

# -- is_crawler ------------------------------------------------------------

def test_is_crawler_googlebot():
    """protects=bot detection; fails_when=Googlebot not detected; no_claim=none"""
    assert is_crawler("Mozilla/5.0 (compatible; Googlebot/2.1)") is True


def test_is_crawler_claude():
    """protects=non-bot pass-through; fails_when=Claude flagged as bot; no_claim=none"""
    assert is_crawler("Claude/1.0") is False


def test_is_crawler_gptbot():
    """protects=GPTBot detection; fails_when=GPTBot not detected; no_claim=none"""
    assert is_crawler("GPTBot/1.0") is True


def test_is_crawler_empty():
    """protects=empty UA handling; fails_when=crash on empty; no_claim=none"""
    assert is_crawler("") is False


# -- TokenBucketRateLimiter ------------------------------------------------

def test_rate_limiter_allows_under_limit():
    """protects=normal requests pass; fails_when=blocked under limit; no_claim=none"""
    limiter = TokenBucketRateLimiter(rpm=10)
    for _ in range(10):
        allowed, _ = limiter.check("1.2.3.4")
        assert allowed is True


def test_rate_limiter_blocks_over_limit():
    """protects=rate limit enforcement; fails_when=request 11 passes at rpm=10; no_claim=timing"""
    limiter = TokenBucketRateLimiter(rpm=10)
    for _ in range(10):
        limiter.check("1.2.3.4")
    allowed, retry_after = limiter.check("1.2.3.4")
    assert allowed is False
    assert retry_after > 0


def test_rate_limiter_per_ip():
    """protects=per-IP isolation; fails_when=one IP blocks another; no_claim=none"""
    limiter = TokenBucketRateLimiter(rpm=1)
    limiter.check("1.1.1.1")  # Consumes IP1's token
    allowed, _ = limiter.check("2.2.2.2")  # IP2 should still have tokens
    assert allowed is True


def test_rate_limiter_disabled_at_zero():
    """protects=RPM=0 disables; fails_when=blocked when disabled; no_claim=none"""
    limiter = TokenBucketRateLimiter(rpm=0)
    for _ in range(1000):
        allowed, _ = limiter.check("1.2.3.4")
        assert allowed is True


def test_rate_limiter_returns_retry_seconds():
    """protects=retry-after value; fails_when=negative or zero; no_claim=exact timing"""
    limiter = TokenBucketRateLimiter(rpm=60)  # 1 per second
    for _ in range(60):
        limiter.check("1.2.3.4")
    _, retry_after = limiter.check("1.2.3.4")
    assert retry_after >= 1


# -- RequestLogger ---------------------------------------------------------

def test_request_logger_output_format():
    """protects=log JSON shape; fails_when=missing fields or invalid JSON; no_claim=none"""
    buf = StringIO()
    logger = RequestLogger()
    logger._stdout = buf

    logger.log("list_sources", 42, "1.2.3.4", "Claude/1.0", "ok")

    output = buf.getvalue().strip()
    record = json.loads(output)
    assert record["tool"] == "list_sources"
    assert record["latency_ms"] == 42
    assert record["ip"] == "1.2.3.4"
    assert record["user_agent"] == "Claude/1.0"
    assert record["status"] == "ok"
    assert record["crawler"] is False
    assert "ts" in record


def test_request_logger_flags_crawler():
    """protects=crawler flag in logs; fails_when=Googlebot not flagged; no_claim=none"""
    buf = StringIO()
    logger = RequestLogger()
    logger._stdout = buf

    logger.log("get_parameters", 5, "1.2.3.4", "Googlebot/2.1", "ok")

    record = json.loads(buf.getvalue().strip())
    assert record["crawler"] is True
