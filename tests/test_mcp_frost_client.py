"""Tests for the MCP FROST read client and circuit breaker.

protects: circuit breaker state transitions, auth header construction
fails_when: circuit doesn't trip, auth missing, wrong state transitions
no_claim: real HTTP behavior (httpx mocked)
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from mcp_server.frost_client import CircuitOpen, FrostReadClient


@pytest.fixture
def client() -> FrostReadClient:
    return FrostReadClient(
        base_url="http://frost-test:8080/FROST-Server/v1.1",
        username="testuser",
        password="testpass",
        timeout=5.0,
        cb_threshold=3,
        cb_cooldown=10.0,
    )


# -- Auth ------------------------------------------------------------------

def test_auth_header_basic(client: FrostReadClient):
    """protects=Basic auth header; fails_when=missing or malformed; no_claim=none"""
    headers = client._client.headers
    assert "Authorization" in headers
    assert headers["Authorization"].startswith("Basic ")


def test_no_auth_when_no_credentials():
    """protects=no auth when not configured; fails_when=auth header present; no_claim=none"""
    client = FrostReadClient(
        base_url="http://frost-test:8080/FROST-Server/v1.1",
        username="",
        password="",
    )
    assert "Authorization" not in client._client.headers


# -- Circuit breaker -------------------------------------------------------

def test_circuit_starts_closed(client: FrostReadClient):
    """protects=initial state; fails_when=circuit open on start; no_claim=none"""
    assert client._cb_failures == 0
    assert client._cb_open_since is None


def test_circuit_opens_after_threshold(client: FrostReadClient):
    """protects=threshold triggers open; fails_when=still closed after 3 failures; no_claim=none"""
    for _ in range(3):
        client._record_failure()
    assert client._cb_open_since is not None


def test_circuit_open_raises(client: FrostReadClient):
    """protects=open circuit blocks requests; fails_when=no exception; no_claim=none"""
    for _ in range(3):
        client._record_failure()
    with pytest.raises(CircuitOpen):
        client._check_circuit()


def test_circuit_half_open_after_cooldown(client: FrostReadClient):
    """protects=half-open after cooldown; fails_when=still raises after cooldown; no_claim=timing"""
    for _ in range(3):
        client._record_failure()
    # Simulate cooldown elapsed
    client._cb_open_since = time.monotonic() - 11.0  # > 10s cooldown
    # Should NOT raise — half-open allows a probe
    client._check_circuit()  # No exception = pass


def test_circuit_closes_on_success(client: FrostReadClient):
    """protects=success resets circuit; fails_when=still open after success; no_claim=none"""
    for _ in range(3):
        client._record_failure()
    assert client._cb_open_since is not None
    client._record_success()
    assert client._cb_failures == 0
    assert client._cb_open_since is None


def test_circuit_does_not_open_below_threshold(client: FrostReadClient):
    """protects=below-threshold tolerance; fails_when=opens at 2 failures (threshold=3); no_claim=none"""
    client._record_failure()
    client._record_failure()
    assert client._cb_open_since is None


# -- Query building --------------------------------------------------------

@pytest.mark.asyncio
async def test_get_things_builds_params(client: FrostReadClient):
    """protects=OData param construction; fails_when=wrong params; no_claim=HTTP (mocked)"""
    mock_response = AsyncMock()
    mock_response.json.return_value = {"value": []}
    mock_response.raise_for_status = lambda: None

    with patch.object(client._client, "get", return_value=mock_response) as mock_get:
        await client.get_things(top=10, count=True, filter_="name eq 'test'")
        call_kwargs = mock_get.call_args
        params = call_kwargs.kwargs.get("params", {})
        assert params["$top"] == "10"
        assert params["$count"] == "true"
        assert params["$filter"] == "name eq 'test'"
