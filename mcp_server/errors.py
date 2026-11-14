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


def sensor_data_unavailable(retry_after_seconds: int = 60) -> dict[str, Any]:
    return {
        "error": "sensor_data_unavailable",
        "message": "Environmental sensor data source is temporarily unreachable",
        "fix": f"Retry after {retry_after_seconds} seconds",
        "retry_after_seconds": retry_after_seconds,
        "side_effects": "none",
    }


def invalid_parameter(value: str, valid_values: list[str]) -> dict[str, Any]:
    return {
        "error": "invalid_parameter",
        "message": f"Parameter '{value}' is not recognized",
        "fix": "Use one of the valid parameter names listed in valid_values",
        "valid_values": valid_values,
    }


def invalid_coordinates(lat: float, lon: float) -> dict[str, Any]:
    return {
        "error": "invalid_coordinates",
        "message": f"Coordinates lat={lat}, lon={lon} are outside valid range",
        "fix": "Latitude must be between -90 and 90, longitude between -180 and 180",
    }
