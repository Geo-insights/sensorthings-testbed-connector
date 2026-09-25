"""Tests for app.frost.odata_fields — OData field dispatch dataclass."""

from __future__ import annotations

import pytest

from app.frost.odata_fields import odata_fields_for


class TestOdataFieldsFor:
    """odata_fields_for() returns correct field names per STA version."""

    def test_v1_1_fields(self) -> None:
        fields = odata_fields_for("1.1")
        assert fields.id == "@iot.id"
        assert fields.self_link == "@iot.selfLink"
        assert fields.next_link == "@iot.nextLink"
        assert fields.count == "@iot.count"
        assert fields.nav_link_suffix == "@iot.navigationLink"

    def test_v2_0_fields(self) -> None:
        fields = odata_fields_for("2.0")
        assert fields.id == "id"
        assert fields.self_link == "@id"
        assert fields.next_link == "@nextLink"
        assert fields.count == "@count"
        assert fields.nav_link_suffix == "@navigationLink"

    def test_v1_1_with_leading_v(self) -> None:
        fields = odata_fields_for("v1.1")
        assert fields.id == "@iot.id"

    def test_v2_0_with_leading_v(self) -> None:
        fields = odata_fields_for("v2.0")
        assert fields.id == "id"

    def test_v1_0_returns_v1_fields(self) -> None:
        """STA 1.0 uses the same annotations as 1.1."""
        fields = odata_fields_for("1.0")
        assert fields.id == "@iot.id"
        assert fields.self_link == "@iot.selfLink"
        assert fields.next_link == "@iot.nextLink"
        assert fields.count == "@iot.count"

    def test_v1_0_with_leading_v(self) -> None:
        fields = odata_fields_for("v1.0")
        assert fields.id == "@iot.id"


class TestFrostODataFieldsFrozen:
    """FrostODataFields is immutable (frozen dataclass)."""

    def test_cannot_set_id(self) -> None:
        fields = odata_fields_for("1.1")
        with pytest.raises(AttributeError):
            fields.id = "changed"  # type: ignore[misc]

    def test_cannot_set_count(self) -> None:
        fields = odata_fields_for("2.0")
        with pytest.raises(AttributeError):
            fields.count = "changed"  # type: ignore[misc]


class TestSingletonReuse:
    """Repeated calls return the same singleton objects."""

    def test_v1_same_object(self) -> None:
        a = odata_fields_for("1.1")
        b = odata_fields_for("v1.1")
        assert a is b

    def test_v2_same_object(self) -> None:
        a = odata_fields_for("2.0")
        b = odata_fields_for("v2.0")
        assert a is b
