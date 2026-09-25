"""OData annotation field names for SensorThings API versions.

STA v1.x uses ``@iot.``-prefixed annotations (``@iot.id``, ``@iot.selfLink``,
etc.) while STA v2.0 uses shorter OData 4.01-style names (``id``, ``@id``,
etc.).  This module provides a single dispatch point so callers never need
to scatter ``if is_v2`` checks across the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FrostODataFields:
    """OData annotation field names for a single STA version."""

    id: str
    self_link: str
    next_link: str
    count: str
    nav_link_suffix: str


# Pre-built singletons — avoid re-creating on every call.
_V1_FIELDS = FrostODataFields(
    id="@iot.id",
    self_link="@iot.selfLink",
    next_link="@iot.nextLink",
    count="@iot.count",
    nav_link_suffix="@iot.navigationLink",
)

_V2_FIELDS = FrostODataFields(
    id="id",
    self_link="@id",
    next_link="@nextLink",
    count="@count",
    nav_link_suffix="@navigationLink",
)


def odata_fields_for(version: str) -> FrostODataFields:
    """Return OData field names for the given STA version."""
    v = str(version).lstrip("v")
    if v.startswith("2"):
        return _V2_FIELDS
    return _V1_FIELDS
