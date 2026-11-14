"""Agent-ready error shapes for MCP tool responses.

Every error follows the contract: what, fix, recovery (retry timing + side effects),
code, and redaction (no internal URLs or credentials). Returned as MCP isError=true.
"""

from __future__ import annotations

from typing import Any


def rate_limit_error(limit: int, retry_after_seconds: int) -> dict[str, Any]:
    return {
        "error": "rate_limit_exceeded",
        "message": f"Rate limit of {limit} req/min exceeded",
        "fix": f"Wait {retry_after_seconds} seconds before retrying",
        "retry_after_seconds": retry_after_seconds,
        "side_effects": "none",
    }


def sensor_data_unavailable(
    retry_after_seconds: int = 60,
    cause: str = "unknown",
) -> dict[str, Any]:
    return {
        "error": "sensor_data_unavailable",
        "message": "Environmental sensor data source is temporarily unreachable",
        "cause": cause,
        "fix": f"Retry after {retry_after_seconds} seconds",
        "retry_after_seconds": retry_after_seconds,
        "side_effects": "none",
    }


def classify_frost_error(exc: Exception) -> dict[str, Any]:
    """Classify a FROST error into an agent-ready error shape with cause detail."""
    import httpx

    from mcp_server.frost_client import CircuitOpen

    if isinstance(exc, CircuitOpen):
        return sensor_data_unavailable(
            retry_after_seconds=60,
            cause="circuit_breaker_open (data source had repeated failures, backing off)",
        )
    if isinstance(exc, httpx.ReadTimeout):
        return sensor_data_unavailable(
            retry_after_seconds=30,
            cause="timeout (data source query took too long)",
        )
    if isinstance(exc, httpx.ConnectTimeout):
        return sensor_data_unavailable(
            retry_after_seconds=60,
            cause="connection_timeout (could not reach data source)",
        )
    if isinstance(exc, httpx.ConnectError):
        return sensor_data_unavailable(
            retry_after_seconds=60,
            cause="connection_failed (DNS or network error reaching data source)",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return sensor_data_unavailable(
            retry_after_seconds=30,
            cause=f"http_{status} (data source returned error status {status})",
        )
    return sensor_data_unavailable(
        retry_after_seconds=60,
        cause=f"{type(exc).__name__}",
    )


def invalid_parameter(value: str, valid_values: list[str]) -> dict[str, Any]:
    return {
        "error": "invalid_parameter",
        "message": f"Parameter '{value}' is not recognized",
        "fix": "Use one of the valid parameter names listed in valid_values",
        "valid_values": valid_values,
    }


def classify_supabase_error(exc: Exception) -> dict[str, Any]:
    """Classify a Supabase PostgREST error into an agent-ready error shape."""
    import httpx

    from mcp_server.supabase_client import SupabaseCircuitOpen

    base = {
        "error": "catalog_unavailable",
        "message": "BGI catalog data source is temporarily unreachable",
        "side_effects": "none",
    }
    if isinstance(exc, SupabaseCircuitOpen):
        return {**base, "cause": "circuit_breaker_open", "fix": "Retry after 60 seconds", "retry_after_seconds": 60}
    if isinstance(exc, httpx.ReadTimeout):
        return {**base, "cause": "timeout", "fix": "Retry after 30 seconds", "retry_after_seconds": 30}
    if isinstance(exc, httpx.ConnectTimeout):
        return {**base, "cause": "connection_timeout", "fix": "Retry after 60 seconds", "retry_after_seconds": 60}
    if isinstance(exc, httpx.ConnectError):
        return {**base, "cause": "connection_failed", "fix": "Retry after 60 seconds", "retry_after_seconds": 60}
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return {**base, "cause": f"http_{status}", "fix": "Retry after 30 seconds", "retry_after_seconds": 30}
    return {**base, "cause": type(exc).__name__, "fix": "Retry after 60 seconds", "retry_after_seconds": 60}


def invalid_coordinates(lat: float, lon: float) -> dict[str, Any]:
    return {
        "error": "invalid_coordinates",
        "message": f"Coordinates lat={lat}, lon={lon} are outside valid range",
        "fix": "Latitude must be between -90 and 90, longitude between -180 and 180",
    }
