"""BRO groundwater: parsing the BRO GM API's responses, and the entity sets and readings built from them.

BRO (Basisregistratie Ondergrond) is the Dutch subsurface register. Its public
groundwater monitoring API (https://publiek.broservices.nl/gm/) serves, as
checked against the live API on 2026-11-27:

1. POST ``gmw/v1/characteristics/searches`` with a bounding box: the monitoring
   wells (GMW) in it, as namespaced XML (``GMW_C`` per well: ``broId``,
   ``standardizedLocation`` in EPSG:4258 as "lat lon", ``removed``).
2. GET ``v1/gmw-relations/{gmwBroId}``: JSON, per monitoring tube its groundwater
   level dossiers (GLD) under ``gldReferences``.
3. GET ``gld/v1/objects/{gldBroId}/observationsSummary``: JSON, one entry per
   observation (a delivery: ``observationId``, ``startDate``/``endDate`` as
   dd-mm-yyyy, ``observationType``, ``observationStatus``).
4. GET ``gld/v1/objects/{gldBroId}/observations/{observationId}`` with both
   ``startTVPTime`` and ``endTVPTime`` (``YYYY-MM-DDTHH:MM:SS``, Dutch local
   time, no zone): that observation's points in the interval, as WaterML 2.0
   ``MeasurementTVP`` (time with offset, value in m, a StatusQualityControl).

Owners deliver levels to BRO in batches, often months after measuring, so a
dossier's newest value can be far in the past.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.models import SensorReading
from app.sta.canonical import CanonicalDatastream

logger = logging.getLogger(__name__)

WATER_LEVEL = CanonicalDatastream.WATER_LEVEL.value
_WATER_LEVEL_META = CanonicalDatastream.WATER_LEVEL.meta

# Source-specific description for the single observed property.
_DESCRIPTIONS: dict[str, str] = {
    WATER_LEVEL: "Groundwater level in the monitoring tube, m relative to NAP (BRO GLD).",
}

# Observation types and statuses of a GLD (observationsSummary).
REGULAR = "reguliereMeting"  # the regular series; a "controlemeting" is a manual check
STATUS_RANK = {"volledigBeoordeeld": 2, "voorlopig": 1, "onbekend": 0}  # a higher rank wins at one time

# StatusQualityControl of a point -> SensorReading.quality; "afgekeurd" (rejected) is left out.
REJECTED = "afgekeurd"
_QUALITY = {"goedgekeurd": "good", "onbeslist": "uncertain", "onbekend": "unknown"}


def _local(tag: str) -> str:
    """An element's tag without its namespace."""
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((e for e in element if _local(e.tag) == name), None)


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


# ---------------------------------------------------------------------------
# Wells (GMW search)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Well:
    bro_id: str
    lat: float
    lon: float


def parse_wells(xml_text: str, max_wells: int) -> list[Well]:
    """The wells of a GMW characteristics search, sorted by BRO id, at most ``max_wells``.

    Removed or deregistered wells and wells without an EPSG:4258 position are left out.
    """
    root = ET.fromstring(xml_text)
    wells: dict[str, Well] = {}
    for doc in (e for e in root.iter() if _local(e.tag) == "GMW_C"):
        bro_id = _text(_child(doc, "broId"))
        if not bro_id or _text(_child(doc, "removed")) == "ja" or _text(_child(doc, "deregistered")) == "ja":
            continue
        position = _position(_child(doc, "standardizedLocation"))
        if position is None:
            continue
        wells[bro_id] = Well(bro_id, *position)
    ordered = [wells[k] for k in sorted(wells)]
    if max_wells and len(ordered) > max_wells:
        logger.warning("BRO: %d wells in the bounding box; keeping the first %d by BRO id (BRO_MAX_WELLS)",
                       len(ordered), max_wells)
        ordered = ordered[:max_wells]
    return ordered


def _position(location: ET.Element | None) -> tuple[float, float] | None:
    """(lat, lon) of a standardizedLocation (EPSG:4258, "lat lon")."""
    if location is None:
        return None
    pos = next((e for e in location.iter() if _local(e.tag) == "pos"), None)
    parts = _text(pos).split()
    if len(parts) < 2:
        return None
    try:
        return float(parts[0]), float(parts[1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tubes and their dossiers (gmw-relations)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tube:
    number: int
    gld_ids: tuple[str, ...]


def parse_relations(data: Any) -> list[Tube]:
    """The monitoring tubes of a gmw-relations response that have a GLD, by tube number."""
    tubes = []
    refs = data.get("monitoringTubeReferences") if isinstance(data, dict) else None
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict) or not isinstance(ref.get("tubeNumber"), int):
            continue
        glds = tuple(sorted({str(g["broId"]) for g in ref.get("gldReferences") or []
                             if isinstance(g, dict) and g.get("broId")}))
        if glds:
            tubes.append(Tube(ref["tubeNumber"], glds))
    return sorted(tubes, key=lambda t: t.number)


# ---------------------------------------------------------------------------
# Observations of a dossier (observationsSummary) and their points
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationSummary:
    observation_id: str
    start: date | None
    end: date | None
    observation_type: str
    status: str


def _ddmmyyyy(value: Any) -> date | None:
    try:
        return datetime.strptime(str(value), "%d-%m-%Y").date()
    except (TypeError, ValueError):
        return None


def parse_summary(data: Any) -> list[ObservationSummary]:
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not item.get("observationId"):
            continue
        out.append(ObservationSummary(str(item["observationId"]), _ddmmyyyy(item.get("startDate")),
                                      _ddmmyyyy(item.get("endDate")), str(item.get("observationType") or ""),
                                      str(item.get("observationStatus") or "")))
    return out


@dataclass(frozen=True)
class Point:
    time: datetime  # UTC
    value: float
    quality: str  # StatusQualityControl, e.g. "goedgekeurd"


def parse_points(xml_text: str) -> list[Point]:
    """The MeasurementTVP points of an observation response; points without a time or a number are skipped."""
    root = ET.fromstring(xml_text)
    points = []
    for tvp in (e for e in root.iter() if _local(e.tag) == "MeasurementTVP"):
        try:
            when = datetime.fromisoformat(_text(_child(tvp, "time")).replace("Z", "+00:00"))
            value = float(_text(_child(tvp, "value")))
        except ValueError:
            continue
        if when.tzinfo is None:
            continue  # BRO times carry an offset; one without cannot be placed
        # The StatusQualityControl is the swe:value inside metadata/qualifier/Category.
        metadata = _child(tvp, "metadata")
        quality = next((_text(e) for e in metadata.iter() if _local(e.tag) == "value"), "") if metadata is not None else ""
        points.append(Point(when.astimezone(UTC), value, quality))
    return points


def merge_points(series: Iterable[tuple[str, Sequence[Point]]]) -> list[Point]:
    """One point per time from several observations of a dossier, (status, points) each.

    Observations can overlap (a provisional delivery later fully assessed): the
    higher status wins (STATUS_RANK), then the later observation. Rejected points
    are left out.
    """
    best: dict[datetime, tuple[int, int, Point]] = {}
    for order, (status, points) in enumerate(series):
        rank = STATUS_RANK.get(status, 0)
        for point in points:
            if point.quality == REJECTED:
                continue
            current = best.get(point.time)
            if current is None or (rank, order) >= current[:2]:
                best[point.time] = (rank, order, point)
    return [best[t][2] for t in sorted(best)]


# ---------------------------------------------------------------------------
# Entity sets and readings
# ---------------------------------------------------------------------------


def sensor_id_for(well_id: str, tube: int) -> str:
    """Our sensor id of a monitoring tube: one datastream per tube."""
    return f"bro-{well_id}-{tube}"


def _sensor_name(well_id: str, tube: int) -> str:
    return f"BRO {well_id} tube {tube} sensor"


def build_entity_set(well: Well, tubes: Sequence[int]) -> dict[str, Any]:
    """One Thing per well, one Sensor (and groundwater level Datastream) per tube with a GLD."""
    return {
        "site_key": "bro",
        "site_name": "BRO Groundwater",
        "thing": {
            "name": f"BRO {well.bro_id}",
            "description": f"BRO groundwater monitoring well {well.bro_id} (Basisregistratie Ondergrond).",
            "properties": {
                "site": "bro",
                "source": "bro_groundwater",
                "bro_well_id": well.bro_id,
                "network": "BRO",
            },
        },
        "location": {
            "name": f"BRO {well.bro_id} location",
            "description": f"BRO monitoring well {well.bro_id}.",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [well.lon, well.lat]},
            "properties": {},
        },
        "sensors": [
            {
                "sensor_id": sensor_id_for(well.bro_id, tube),
                "name": _sensor_name(well.bro_id, tube),
                "description": f"Groundwater level in tube {tube} of BRO well {well.bro_id} (GLD).",
                "encodingType": "application/json",
                "metadata": "https://publiek.broservices.nl/gm/",
                "observed_properties": [WATER_LEVEL],
                "properties": {"tube_number": tube},
            }
            for tube in sorted(set(tubes))
        ],
        "observed_properties": dict(_DESCRIPTIONS),
    }


def readings_for(points: Iterable[Point], well_id: str, tube: int) -> list[SensorReading]:
    """SensorReadings of a tube's points (canonical groundwater level, m); rejected points are left out."""
    return [
        SensorReading(
            sensor_id=sensor_id_for(well_id, tube),
            sensor_name=_sensor_name(well_id, tube),
            observed_property=WATER_LEVEL,
            observed_property_name=_WATER_LEVEL_META.display_name,
            unit=_WATER_LEVEL_META.unit,
            value=point.value,
            timestamp=point.time,
            quality=_QUALITY.get(point.quality, "unknown"),
            location="bro",
            thing_name=f"BRO {well_id}",
        )
        for point in points
        if point.quality != REJECTED
    ]
