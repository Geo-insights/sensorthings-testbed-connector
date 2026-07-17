"""SensorThings API v1.1 → v2.0 payload adapter.

Pure functions that transform v1.1-style JSON payloads into the v2.0
format before sending them to a FROST v2.0 server.  All internal code
continues to build v1.1 payloads; adaptation happens at the HTTP boundary.

Key differences handled:
- Entity references: ``{"@iot.id": X}`` → ``{"id": X}``
- Datastream: ``observationType`` + ``unitOfMeasurement`` → ``resultType``
- Observation: ``parameters`` → ``properties``
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

# Navigation property keys that may contain inline entity references.
_NAV_PROPERTIES = (
    "Thing", "Sensor", "ObservedProperty", "Datastream",
    "FeatureOfInterest", "Feature", "ProximateFeatureOfInterest",
    "Actuator", "TaskingCapability",
    "Things", "Locations", "Projects",
    "ObservedProperties", "Sensors", "Datastreams",
)


# ---------------------------------------------------------------------------
# Entity reference conversion
# ---------------------------------------------------------------------------

def _convert_ref(ref: Any) -> Any:
    """Convert a single ``{"@iot.id": X}`` reference to ``{"id": X}``."""
    if isinstance(ref, dict) and "@iot.id" in ref:
        new = dict(ref)
        new["id"] = new.pop("@iot.id")
        return new
    return ref


def adapt_entity_refs(payload: dict[str, Any]) -> dict[str, Any]:
    """Rewrite all ``@iot.id`` inline references to ``id``."""
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


# ---------------------------------------------------------------------------
# Datastream payload
# ---------------------------------------------------------------------------

def _uom_to_result_type(uom: dict[str, Any]) -> dict[str, Any]:
    """Convert v1.1 ``unitOfMeasurement`` to v2.0 ``resultType`` (SWE-Common Quantity).

    Note: the ``definition`` field is intentionally omitted — FROST v2.0
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


def adapt_datastream_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Transform a v1.1 Datastream creation payload to v2.0 format."""
    result = adapt_entity_refs(payload)

    # v2.0: ObservedProperty (singular) → ObservedProperties (plural list, many-to-many)
    if "ObservedProperty" in result and "ObservedProperties" not in result:
        op_ref = result.pop("ObservedProperty")
        result["ObservedProperties"] = [op_ref] if isinstance(op_ref, dict) else []

    # v2.0: observationType + unitOfMeasurement → resultType (SWE-Common)
    uom = result.pop("unitOfMeasurement", None)
    obs_type = result.pop("observationType", None)

    if isinstance(uom, dict):
        result["resultType"] = _uom_to_result_type(uom)
    elif obs_type:
        result["resultType"] = {"type": "Quantity"}

    return result


# ---------------------------------------------------------------------------
# Observation payload
# ---------------------------------------------------------------------------

def adapt_observation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Transform a v1.1 Observation creation payload to v2.0 format."""
    result = adapt_entity_refs(payload)

    # v2.0: parameters → properties
    if "parameters" in result:
        result["properties"] = result.pop("parameters")

    # v2.0: resultQuality is removed
    result.pop("resultQuality", None)

    return result


# ---------------------------------------------------------------------------
# General adapter
# ---------------------------------------------------------------------------

def adapt_payload(payload: dict[str, Any], *, entity_hint: str = "") -> dict[str, Any]:
    """Adapt a v1.1 payload for v2.0, auto-detecting the entity type.

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
