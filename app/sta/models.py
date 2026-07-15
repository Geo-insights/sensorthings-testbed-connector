"""Pydantic v2 domain models for OGC SensorThings API entities.

These models represent the STA information model independent of any
HTTP transport layer.  Each model provides:

- Field validation (non-empty names, max lengths)
- ``partial_eq()`` for semantic comparison ignoring server-assigned fields
- ``as_frost_payload()`` for serializing to FROST POST bodies
- ``from_frost_response()`` for parsing FROST JSON responses
- ``entity_type`` computed from the class name
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import BaseModel, Field, StringConstraints, computed_field, model_validator


# ---------------------------------------------------------------------------
# Shared types
# ---------------------------------------------------------------------------

_Name = Annotated[str, StringConstraints(min_length=1, max_length=256, strip_whitespace=True)]


class UnitOfMeasurement(BaseModel):
    name: str
    symbol: str
    definition: str


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class STAEntityBase(BaseModel):
    """Base for all STA entity models."""

    iot_id: str | None = Field(default=None, description="Server-assigned @iot.id")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def entity_type(self) -> str:
        """Derive entity type from the class name (e.g. STAThing -> Thing)."""
        name = type(self).__name__
        return name[3:] if name.startswith("STA") else name

    # -- Comparison --------------------------------------------------------

    def _business_fields(self) -> dict[str, Any]:
        """Return fields that define *identity* (excludes iot_id and entity_type)."""
        exclude = {"iot_id", "entity_type"}
        return {k: v for k, v in self.model_dump().items() if k not in exclude}

    def partial_eq(self, other: STAEntityBase) -> bool:
        """Compare only business/identity fields, ignoring server-assigned IDs."""
        if type(self) is not type(other):
            return False
        return self._business_fields() == other._business_fields()

    # -- Serialization -----------------------------------------------------

    def as_frost_payload(self) -> dict[str, Any]:
        """Serialize to the dict format expected by a FROST POST/PATCH."""
        data = self.model_dump(exclude={"iot_id", "entity_type"}, exclude_none=True)
        return data

    @classmethod
    def from_frost_response(cls, data: dict[str, Any]) -> Self:
        """Parse a FROST JSON response dict into this model."""
        iot_id = data.get("@iot.id") or data.get("id")
        kwargs: dict[str, Any] = {"iot_id": str(iot_id) if iot_id is not None else None}
        for field_name in cls.model_fields:
            if field_name in ("iot_id",):
                continue
            if field_name in data:
                kwargs[field_name] = data[field_name]
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Core STA entities
# ---------------------------------------------------------------------------

class STAThing(STAEntityBase):
    name: _Name
    description: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)


class STALocation(STAEntityBase):
    name: _Name
    description: str = ""
    encodingType: str = "application/geo+json"
    location: dict[str, Any] = Field(default_factory=dict)
    properties: dict[str, Any] = Field(default_factory=dict)

    # Linked entities (IDs, not full objects)
    thing_id: str | None = Field(default=None, exclude=True)

    def as_frost_payload(self) -> dict[str, Any]:
        data = super().as_frost_payload()
        if self.thing_id:
            data["Things"] = [{"@iot.id": self.thing_id}]
        data.pop("thing_id", None)
        return data

    @classmethod
    def from_frost_response(cls, data: dict[str, Any]) -> Self:
        instance = super().from_frost_response(data)
        things = data.get("Things", [])
        if things and isinstance(things, list) and isinstance(things[0], dict):
            thing_id = things[0].get("@iot.id") or things[0].get("id")
            if thing_id is not None:
                instance.thing_id = str(thing_id)
        return instance

    def _business_fields(self) -> dict[str, Any]:
        fields = super()._business_fields()
        fields.pop("thing_id", None)
        return fields


class STASensor(STAEntityBase):
    name: _Name
    description: str = ""
    encodingType: str = "application/json"
    metadata: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)


class STAObservedProperty(STAEntityBase):
    name: _Name
    definition: str = ""
    description: str = ""


class STADatastream(STAEntityBase):
    name: _Name
    description: str = ""
    observationType: str = "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement"
    unitOfMeasurement: UnitOfMeasurement | None = None
    properties: dict[str, Any] = Field(default_factory=dict)

    # Linked entity IDs
    thing_id: str | None = Field(default=None, exclude=True)
    sensor_id: str | None = Field(default=None, exclude=True)
    observed_property_id: str | None = Field(default=None, exclude=True)

    def as_frost_payload(self) -> dict[str, Any]:
        data = super().as_frost_payload()
        if self.thing_id:
            data["Thing"] = {"@iot.id": self.thing_id}
        if self.sensor_id:
            data["Sensor"] = {"@iot.id": self.sensor_id}
        if self.observed_property_id:
            data["ObservedProperty"] = {"@iot.id": self.observed_property_id}
        for key in ("thing_id", "sensor_id", "observed_property_id"):
            data.pop(key, None)
        return data

    @classmethod
    def from_frost_response(cls, data: dict[str, Any]) -> Self:
        uom = data.get("unitOfMeasurement")
        kwargs: dict[str, Any] = {
            "iot_id": str(data["@iot.id"]) if data.get("@iot.id") is not None else (str(data["id"]) if data.get("id") is not None else None),
            "name": data.get("name", ""),
            "description": data.get("description", ""),
            "observationType": data.get("observationType", ""),
            "properties": data.get("properties", {}),
        }
        if isinstance(uom, dict):
            kwargs["unitOfMeasurement"] = UnitOfMeasurement(**uom)
        # Extract linked IDs if expanded
        thing = data.get("Thing", {})
        if isinstance(thing, dict) and (thing.get("@iot.id") or thing.get("id")):
            kwargs["thing_id"] = str(thing.get("@iot.id") or thing.get("id"))
        sensor = data.get("Sensor", {})
        if isinstance(sensor, dict) and (sensor.get("@iot.id") or sensor.get("id")):
            kwargs["sensor_id"] = str(sensor.get("@iot.id") or sensor.get("id"))
        op = data.get("ObservedProperty", {})
        if isinstance(op, dict) and (op.get("@iot.id") or op.get("id")):
            kwargs["observed_property_id"] = str(op.get("@iot.id") or op.get("id"))
        return cls(**kwargs)

    def _business_fields(self) -> dict[str, Any]:
        fields = super()._business_fields()
        for key in ("thing_id", "sensor_id", "observed_property_id"):
            fields.pop(key, None)
        return fields


class STAObservation(STAEntityBase):
    phenomenonTime: datetime
    result: float
    resultQuality: list[str] = Field(default_factory=lambda: ["good"])
    parameters: dict[str, Any] = Field(default_factory=dict)

    # Linked entity
    datastream_id: str | None = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _coerce_result_quality(cls, values: Any) -> Any:
        """Accept a string resultQuality and wrap it in a list for FROST compatibility."""
        if isinstance(values, dict):
            rq = values.get("resultQuality")
            if isinstance(rq, str):
                values["resultQuality"] = [rq]
        return values

    def as_frost_payload(self) -> dict[str, Any]:
        data = super().as_frost_payload()
        if self.datastream_id:
            data["Datastream"] = {"@iot.id": self.datastream_id}
        data.pop("datastream_id", None)
        return data

    def _business_fields(self) -> dict[str, Any]:
        fields = super()._business_fields()
        fields.pop("datastream_id", None)
        return fields


# ---------------------------------------------------------------------------
# Extension entities
# ---------------------------------------------------------------------------

class STAProject(STAEntityBase):
    name: _Name
    description: str = ""
    public: bool = True


class STAActuator(STAEntityBase):
    name: _Name
    description: str = ""
    encodingType: str = "application/json"
    metadata: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)


class STATaskingCapability(STAEntityBase):
    name: _Name
    description: str = ""
    taskingParameters: dict[str, Any] = Field(default_factory=dict)
    properties: dict[str, Any] = Field(default_factory=dict)

    # Linked entity IDs
    actuator_id: str | None = Field(default=None, exclude=True)
    thing_id: str | None = Field(default=None, exclude=True)

    def as_frost_payload(self) -> dict[str, Any]:
        data = super().as_frost_payload()
        if self.actuator_id:
            data["Actuator"] = {"@iot.id": self.actuator_id}
        if self.thing_id:
            data["Thing"] = {"@iot.id": self.thing_id}
        for key in ("actuator_id", "thing_id"):
            data.pop(key, None)
        return data

    def _business_fields(self) -> dict[str, Any]:
        fields = super()._business_fields()
        for key in ("actuator_id", "thing_id"):
            fields.pop(key, None)
        return fields
