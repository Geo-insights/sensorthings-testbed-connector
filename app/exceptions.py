"""Custom exception hierarchy for the SensorThings connector.

Provides typed, context-rich exceptions replacing generic catches,
enabling structured error handling throughout the application.
"""

from __future__ import annotations

from typing import Any


class ConnectorError(Exception):
    """Base exception for all connector errors."""


class FrostConnectionError(ConnectorError):
    """Cannot reach the FROST server at all (network/DNS/timeout)."""

    def __init__(self, url: str, message: str = "", cause: Exception | None = None) -> None:
        self.url = url
        self.cause = cause
        super().__init__(message or f"Cannot connect to FROST server at {url}")


class FrostRequestError(FrostConnectionError):
    """FROST server returned an HTTP error response."""

    def __init__(
        self,
        url: str,
        status_code: int,
        response_body: Any = None,
        message: str = "",
    ) -> None:
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(
            url=url,
            message=message or f"FROST request to {url} failed with status {status_code}",
        )


class EntityValidationError(ConnectorError):
    """STA entity failed Pydantic or field-level validation."""

    def __init__(self, entity_type: str, details: str, field: str | None = None) -> None:
        self.entity_type = entity_type
        self.field = field
        self.details = details
        msg = f"Validation error on {entity_type}"
        if field:
            msg += f".{field}"
        msg += f": {details}"
        super().__init__(msg)


class DatastreamResolutionError(ConnectorError):
    """No datastream could be found for a given sensor reading."""

    def __init__(
        self,
        sensor_id: str,
        observed_property: str,
        thing_name: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.sensor_id = sensor_id
        self.observed_property = observed_property
        self.thing_name = thing_name
        self.base_url = base_url
        parts = [f"sensor_id={sensor_id!r}", f"observed_property={observed_property!r}"]
        if thing_name:
            parts.append(f"thing_name={thing_name!r}")
        if base_url:
            parts.append(f"base_url={base_url!r}")
        super().__init__(f"Cannot resolve datastream for {', '.join(parts)}")


class ObservationUploadError(ConnectorError):
    """Observation POST failed after all retry attempts."""

    def __init__(
        self,
        datastream_id: str,
        attempts: int,
        last_status: int | None = None,
        last_error: str = "",
    ) -> None:
        self.datastream_id = datastream_id
        self.attempts = attempts
        self.last_status = last_status
        self.last_error = last_error
        msg = f"Observation upload to datastream {datastream_id} failed after {attempts} attempts"
        if last_status:
            msg += f" (last status: {last_status})"
        if last_error:
            msg += f": {last_error}"
        super().__init__(msg)


class PayloadTransformError(ConnectorError):
    """Failed to deserialize, decode, or transform an incoming payload."""

    def __init__(self, source: str, details: str, raw_data: Any = None) -> None:
        self.source = source
        self.details = details
        self.raw_data = raw_data
        super().__init__(f"Payload transform error from {source}: {details}")
