"""Pydantic v2 domain models for OGC SensorThings API entities.

These models represent the STA information model independent of any
HTTP transport layer.  Each model provides:

- Field validation (non-empty names, max lengths)
- ``partial_eq()`` for semantic comparison ignoring server-assigned fields
- ``as_frost_payload()`` for serializing to FROST POST bodies
- ``from_frost_response()`` for parsing FROST JSON responses
- ``entity_type`` computed from the class name

V1 and V2 subclasses encode the payload differences between STA v1.1 and
v2.0 directly in the model hierarchy, replacing the old ``v2_adapter.py``
approach.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, ClassVar, Self

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

    _sta_version: ClassVar[str] = ""
    _content_fields: ClassVar[frozenset[str]] = frozenset()

    iot_id: str | None = Field(default=None, description="Server-assigned @iot.id")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def entity_type(self) -> str:
        """Derive entity type from the class name (e.g. STAThing -> Thing)."""
        name = type(self).__name__
        # Strip STA prefix
        if name.startswith("STA"):
            name = name[3:]
        # Strip V1/V2 suffix
        for suffix in ("V1", "V2"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                break
        return name

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
# Helper: v2 entity reference conversion
# ---------------------------------------------------------------------------

def _ref_v1(iot_id: str | int) -> dict[str, Any]:
    """Build a v1.1 inline entity reference."""
    return {"@iot.id": iot_id}


def _ref_v2(iot_id: str | int) -> dict[str, Any]:
    """Build a v2.0 inline entity reference."""
    return {"id": iot_id}


def _uom_to_result_type(uom: dict[str, Any]) -> dict[str, Any]:
    """Convert v1.1 ``unitOfMeasurement`` to v2.0 ``resultType`` (SWE-Common Quantity).

    Note: the ``definition`` field is intentionally omitted -- FROST v2.0
    misparses URLs in that position, returning *"SelfLink must contain a
    primary key in brackets"*.  The ``label`` field works fine.
    """
    result_type: dict[str, Any] = {
        "type": "Quantity",
        "uom": {"code": uom.get("symbol", "")},
    }
    label = uom.get("name", "")
    if label:
        result_type["label"] = label
    return result_type


# ---------------------------------------------------------------------------
# Thing
# ---------------------------------------------------------------------------

class STAThing(STAEntityBase):
    """Thing base -- usable directly (defaults to v1.1 payload behaviour)."""

    name: _Name
    description: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)


class ThingV1(STAThing):
    _sta_version: ClassVar[str] = "1.1"
    _content_fields: ClassVar[frozenset[str]] = frozenset({"name", "description", "properties"})


class ThingV2(STAThing):
    _sta_version: ClassVar[str] = "2.0"
    _content_fields: ClassVar[frozenset[str]] = frozenset({"name", "description", "properties", "definition"})

    definition: str | None = None


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

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
            data["Things"] = [_ref_v1(self.thing_id)]
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


class LocationV1(STALocation):
    _sta_version: ClassVar[str] = "1.1"
    _content_fields: ClassVar[frozenset[str]] = frozenset(
        {"name", "description", "encodingType", "location", "properties"}
    )


class LocationV2(STALocation):
    _sta_version: ClassVar[str] = "2.0"
    _content_fields: ClassVar[frozenset[str]] = frozenset(
        {"name", "description", "encodingType", "location", "properties", "definition"}
    )

    definition: str | None = None

    def as_frost_payload(self) -> dict[str, Any]:
        data = super().as_frost_payload()
        # Rewrite entity refs to v2 format
        if "Things" in data:
            data["Things"] = [_ref_v2(t.get("@iot.id", t.get("id", ""))) for t in data["Things"]]
        return data


# ---------------------------------------------------------------------------
# Sensor
# ---------------------------------------------------------------------------

class STASensor(STAEntityBase):
    name: _Name
    description: str = ""
    encodingType: str = "application/json"
    metadata: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)


class SensorV1(STASensor):
    _sta_version: ClassVar[str] = "1.1"
    _content_fields: ClassVar[frozenset[str]] = frozenset(
        {"name", "description", "encodingType", "metadata", "properties"}
    )


class SensorV2(STASensor):
    _sta_version: ClassVar[str] = "2.0"
    _content_fields: ClassVar[frozenset[str]] = frozenset(
        {"name", "description", "encodingType", "metadata", "properties", "definition"}
    )

    definition: str | None = None


# ---------------------------------------------------------------------------
# ObservedProperty
# ---------------------------------------------------------------------------

class STAObservedProperty(STAEntityBase):
    name: _Name
    definition: str = ""
    description: str = ""


class ObservedPropertyV1(STAObservedProperty):
    _sta_version: ClassVar[str] = "1.1"
    _content_fields: ClassVar[frozenset[str]] = frozenset({"name", "definition", "description"})


class ObservedPropertyV2(STAObservedProperty):
    _sta_version: ClassVar[str] = "2.0"
    _content_fields: ClassVar[frozenset[str]] = frozenset({"name", "definition", "description"})


# ---------------------------------------------------------------------------
# Datastream
# ---------------------------------------------------------------------------

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
            data["Thing"] = _ref_v1(self.thing_id)
        if self.sensor_id:
            data["Sensor"] = _ref_v1(self.sensor_id)
        if self.observed_property_id:
            data["ObservedProperty"] = _ref_v1(self.observed_property_id)
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


class DatastreamV1(STADatastream):
    _sta_version: ClassVar[str] = "1.1"
    _content_fields: ClassVar[frozenset[str]] = frozenset(
        {"name", "description", "observationType", "unitOfMeasurement", "properties"}
    )


class DatastreamV2(STADatastream):
    """v2.0 Datastream: ``resultType`` instead of ``observationType`` + ``unitOfMeasurement``.

    ``ObservedProperties`` (plural, many-to-many) replaces ``ObservedProperty`` (singular).
    """

    _sta_version: ClassVar[str] = "2.0"
    _content_fields: ClassVar[frozenset[str]] = frozenset(
        {"name", "description", "resultType", "properties", "definition", "resultEncoding"}
    )

    resultType: dict[str, Any] | None = None
    resultEncoding: str | None = None
    definition: str | None = None

    def as_frost_payload(self) -> dict[str, Any]:
        # Start from STAEntityBase (skip STADatastream's v1.1 ref logic)
        data = STAEntityBase.as_frost_payload(self)

        # Build resultType from unitOfMeasurement if not explicitly set
        if self.resultType:
            data["resultType"] = self.resultType
        elif self.unitOfMeasurement:
            data["resultType"] = _uom_to_result_type(self.unitOfMeasurement.model_dump())
        elif self.observationType:
            data["resultType"] = {"type": "Quantity"}

        # Remove v1.1-only fields
        data.pop("unitOfMeasurement", None)
        data.pop("observationType", None)

        # Entity refs in v2 format
        if self.thing_id:
            data["Thing"] = _ref_v2(self.thing_id)
        if self.sensor_id:
            data["Sensor"] = _ref_v2(self.sensor_id)
        if self.observed_property_id:
            # v2: ObservedProperties (plural list, many-to-many)
            data["ObservedProperties"] = [_ref_v2(self.observed_property_id)]
        for key in ("thing_id", "sensor_id", "observed_property_id"):
            data.pop(key, None)
        return data

    @classmethod
    def from_frost_response(cls, data: dict[str, Any]) -> Self:
        """Parse a v2.0 FROST response.  Handles both v2 and v1 response shapes."""
        iot_id = data.get("@iot.id") or data.get("id")
        kwargs: dict[str, Any] = {
            "iot_id": str(iot_id) if iot_id is not None else None,
            "name": data.get("name", ""),
            "description": data.get("description", ""),
            "properties": data.get("properties", {}),
        }

        # v2 fields
        if "resultType" in data:
            kwargs["resultType"] = data["resultType"]
        if "resultEncoding" in data:
            kwargs["resultEncoding"] = data["resultEncoding"]
        if "definition" in data:
            kwargs["definition"] = data["definition"]

        # Fall back to v1 fields if present (for cross-version compatibility)
        uom = data.get("unitOfMeasurement")
        if isinstance(uom, dict):
            kwargs["unitOfMeasurement"] = UnitOfMeasurement(**uom)
        if "observationType" in data:
            kwargs["observationType"] = data["observationType"]

        # Linked IDs -- support both v1 and v2 key names
        thing = data.get("Thing", {})
        if isinstance(thing, dict) and (thing.get("@iot.id") or thing.get("id")):
            kwargs["thing_id"] = str(thing.get("@iot.id") or thing.get("id"))
        sensor = data.get("Sensor", {})
        if isinstance(sensor, dict) and (sensor.get("@iot.id") or sensor.get("id")):
            kwargs["sensor_id"] = str(sensor.get("@iot.id") or sensor.get("id"))
        # v2 uses ObservedProperties (plural)
        ops = data.get("ObservedProperties", [])
        if isinstance(ops, list) and ops and isinstance(ops[0], dict):
            kwargs["observed_property_id"] = str(ops[0].get("@iot.id") or ops[0].get("id"))
        else:
            op = data.get("ObservedProperty", {})
            if isinstance(op, dict) and (op.get("@iot.id") or op.get("id")):
                kwargs["observed_property_id"] = str(op.get("@iot.id") or op.get("id"))

        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

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
            data["Datastream"] = _ref_v1(self.datastream_id)
        data.pop("datastream_id", None)
        return data

    def _business_fields(self) -> dict[str, Any]:
        fields = super()._business_fields()
        fields.pop("datastream_id", None)
        return fields


class ObservationV1(STAObservation):
    _sta_version: ClassVar[str] = "1.1"
    _content_fields: ClassVar[frozenset[str]] = frozenset(
        {"phenomenonTime", "result", "resultQuality", "parameters"}
    )


class ObservationV2(STAObservation):
    """v2.0 Observation: ``properties`` replaces ``parameters``; no ``resultQuality``."""

    _sta_version: ClassVar[str] = "2.0"
    _content_fields: ClassVar[frozenset[str]] = frozenset(
        {"phenomenonTime", "result", "properties"}
    )

    def as_frost_payload(self) -> dict[str, Any]:
        data = STAEntityBase.as_frost_payload(self)

        # v2: parameters -> properties
        params = data.pop("parameters", None)
        if params:
            data["properties"] = params

        # v2: no resultQuality
        data.pop("resultQuality", None)

        if self.datastream_id:
            data["Datastream"] = _ref_v2(self.datastream_id)
        data.pop("datastream_id", None)
        return data

    @classmethod
    def from_frost_response(cls, data: dict[str, Any]) -> Self:
        """Parse a v2.0 FROST Observation response."""
        iot_id = data.get("@iot.id") or data.get("id")
        kwargs: dict[str, Any] = {
            "iot_id": str(iot_id) if iot_id is not None else None,
            "phenomenonTime": data["phenomenonTime"],
            "result": data["result"],
        }
        # v2 uses "properties"; map to "parameters" for internal storage
        if "properties" in data:
            kwargs["parameters"] = data["properties"]
        elif "parameters" in data:
            kwargs["parameters"] = data["parameters"]

        ds = data.get("Datastream", {})
        if isinstance(ds, dict) and (ds.get("@iot.id") or ds.get("id")):
            kwargs["datastream_id"] = str(ds.get("@iot.id") or ds.get("id"))

        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Extension entities (not version-specific)
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


# ---------------------------------------------------------------------------
# Version dispatch
# ---------------------------------------------------------------------------

# Navigation property keys that may contain inline entity references.
_NAV_PROPERTIES = (
    "Thing", "Sensor", "ObservedProperty", "Datastream",
    "FeatureOfInterest", "Feature", "ProximateFeatureOfInterest",
    "Actuator", "TaskingCapability",
    "Things", "Locations", "Projects",
    "ObservedProperties", "Sensors", "Datastreams",
)


def _convert_ref(ref: Any) -> Any:
    """Convert a single ``{"@iot.id": X}`` reference to ``{"id": X}``."""
    if isinstance(ref, dict) and "@iot.id" in ref:
        new = dict(ref)
        new["id"] = new.pop("@iot.id")
        return new
    return ref


def adapt_entity_refs(payload: dict[str, Any]) -> dict[str, Any]:
    """Rewrite all ``@iot.id`` inline references to ``id`` (v2.0 format)."""
    result = dict(payload)
    for key in _NAV_PROPERTIES:
        if key not in result:
            continue
        value = result[key]
        if isinstance(value, dict):
            result[key] = _convert_ref(value)
        elif isinstance(value, list):
            result[key] = [_convert_ref(item) for item in value]
    return result


def adapt_datastream_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Transform a v1.1 Datastream dict payload to v2.0 format.

    Operates on raw dicts for backward compatibility with code that
    builds payloads as plain dicts rather than model instances.
    """
    result = adapt_entity_refs(payload)

    # v2.0: ObservedProperty (singular) -> ObservedProperties (plural list)
    if "ObservedProperty" in result and "ObservedProperties" not in result:
        op_ref = result.pop("ObservedProperty")
        result["ObservedProperties"] = [op_ref] if isinstance(op_ref, dict) else []

    # v2.0: observationType + unitOfMeasurement -> resultType (SWE-Common)
    uom = result.pop("unitOfMeasurement", None)
    obs_type = result.pop("observationType", None)

    if isinstance(uom, dict):
        result["resultType"] = _uom_to_result_type(uom)
    elif obs_type:
        result["resultType"] = {"type": "Quantity"}

    return result


def adapt_observation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Transform a v1.1 Observation dict payload to v2.0 format."""
    result = adapt_entity_refs(payload)

    # v2.0: parameters -> properties
    if "parameters" in result:
        result["properties"] = result.pop("parameters")

    # v2.0: resultQuality is removed
    result.pop("resultQuality", None)

    return result


def adapt_payload(payload: dict[str, Any], *, entity_hint: str = "") -> dict[str, Any]:
    """Adapt a v1.1 dict payload for v2.0, auto-detecting the entity type.

    ``entity_hint`` can be ``"datastream"``, ``"observation"``, etc.
    If omitted, the function guesses from payload keys.
    """
    hint = entity_hint.lower()

    if hint == "datastream" or "unitOfMeasurement" in payload or "observationType" in payload:
        return adapt_datastream_payload(payload)

    if hint == "observation" or ("parameters" in payload and "result" in payload):
        return adapt_observation_payload(payload)

    # Default: only convert entity references.
    return adapt_entity_refs(payload)


def class_map_for(version: str) -> dict[str, type[STAEntityBase]]:
    """Return a mapping of entity-type name to the correct versioned model class.

    >>> class_map_for("1.1")["Thing"]
    <class 'app.sta.models.ThingV1'>
    >>> class_map_for("v2.0")["Observation"]
    <class 'app.sta.models.ObservationV2'>
    """
    v = str(version).lstrip("v")
    if v.startswith("2"):
        return {
            "Thing": ThingV2,
            "Location": LocationV2,
            "Sensor": SensorV2,
            "ObservedProperty": ObservedPropertyV2,
            "Datastream": DatastreamV2,
            "Observation": ObservationV2,
        }
    return {
        "Thing": ThingV1,
        "Location": LocationV1,
        "Sensor": SensorV1,
        "ObservedProperty": ObservedPropertyV1,
        "Datastream": DatastreamV1,
        "Observation": ObservationV1,
    }
