"""Tests for the custom exception hierarchy."""

from __future__ import annotations

import pytest

from app.exceptions import (
    ConnectorError,
    DatastreamResolutionError,
    EntityValidationError,
    FrostConnectionError,
    FrostRequestError,
    ObservationUploadError,
    PayloadTransformError,
)


class TestExceptionHierarchy:
    def test_all_inherit_from_connector_error(self):
        for exc_cls in (
            FrostConnectionError,
            FrostRequestError,
            EntityValidationError,
            DatastreamResolutionError,
            ObservationUploadError,
            PayloadTransformError,
        ):
            assert issubclass(exc_cls, ConnectorError)

    def test_frost_request_error_inherits_from_connection_error(self):
        assert issubclass(FrostRequestError, FrostConnectionError)


class TestFrostConnectionError:
    def test_basic(self):
        exc = FrostConnectionError("http://frost:8080")
        assert exc.url == "http://frost:8080"
        assert "frost:8080" in str(exc)
        assert exc.cause is None

    def test_with_cause(self):
        cause = TimeoutError("timed out")
        exc = FrostConnectionError("http://frost:8080", cause=cause)
        assert exc.cause is cause

    def test_custom_message(self):
        exc = FrostConnectionError("http://frost:8080", message="DNS failed")
        assert str(exc) == "DNS failed"


class TestFrostRequestError:
    def test_attributes(self):
        exc = FrostRequestError("http://frost:8080/Things", 404, {"error": "not found"})
        assert exc.url == "http://frost:8080/Things"
        assert exc.status_code == 404
        assert exc.response_body == {"error": "not found"}
        assert "404" in str(exc)

    def test_catchable_as_connection_error(self):
        exc = FrostRequestError("http://frost:8080", 500)
        with pytest.raises(FrostConnectionError):
            raise exc


class TestEntityValidationError:
    def test_with_field(self):
        exc = EntityValidationError("Thing", "cannot be empty", field="name")
        assert exc.entity_type == "Thing"
        assert exc.field == "name"
        assert "Thing.name" in str(exc)

    def test_without_field(self):
        exc = EntityValidationError("Datastream", "missing unit")
        assert exc.field is None
        assert "Datastream" in str(exc)


class TestDatastreamResolutionError:
    def test_basic(self):
        exc = DatastreamResolutionError("sensor-1", "temperature")
        assert exc.sensor_id == "sensor-1"
        assert exc.observed_property == "temperature"
        assert "sensor-1" in str(exc)

    def test_with_optional_fields(self):
        exc = DatastreamResolutionError("s1", "temp", thing_name="Thing1", base_url="http://x")
        assert exc.thing_name == "Thing1"
        assert exc.base_url == "http://x"
        assert "Thing1" in str(exc)


class TestObservationUploadError:
    def test_basic(self):
        exc = ObservationUploadError("42", 3, last_status=500, last_error="server error")
        assert exc.datastream_id == "42"
        assert exc.attempts == 3
        assert exc.last_status == 500
        assert "42" in str(exc)
        assert "3 attempts" in str(exc)


class TestPayloadTransformError:
    def test_basic(self):
        exc = PayloadTransformError("kafka", "missing field 'value'", raw_data={"x": 1})
        assert exc.source == "kafka"
        assert exc.details == "missing field 'value'"
        assert exc.raw_data == {"x": 1}
        assert "kafka" in str(exc)
