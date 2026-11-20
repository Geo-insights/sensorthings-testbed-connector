"""Repair Luchtmeetnet Locations stuck at the POINT(5 52) placeholder.

Before the station-detail fix, Luchtmeetnet discovery read only the paged
``GET /stations`` list (station number + name, no coordinates) and fell back
to lat=52, lon=5 for every station, so every Luchtmeetnet Location in FROST
was created at POINT(5 52). Registration finds an existing Location by name and
never updates it, so this one-off script looks up each station's coordinates
in ``GET /stations/{number}`` and PATCHes its Location.

Run it before the fixed connector pushes any Luchtmeetnet Observation. FROST
generates a FeatureOfInterest from a Thing's Location when the first
Observation arrives and keeps reusing it after the Location is PATCHed (checked
on FROST 2.8), so Observations pushed before the repair stay at the
placeholder. The script checks this precondition: when a Luchtmeetnet
Datastream already has an Observation it prints a warning and refuses
``--apply`` unless ``--allow-observations`` is given. With that flag it also
PATCHes the placeholder FeaturesOfInterest it finds through one Observation of
every Luchtmeetnet Datastream (FROST reuses one generated FeatureOfInterest per
Location).

Things are matched on ``properties.station_number`` (set by the connector on
every Luchtmeetnet Thing) and, failing that, on the station name from the
Luchtmeetnet station list. Only geometries still at the placeholder are
touched, so re-running is safe; after ``--apply``, run the dry run again: it
should find nothing left to move.

SensorThings v1.1 and v2.0 endpoints both work (FROST serves both from one
database). Query options follow the target's declared ``version``; ids and
next links in responses are read in either convention, so a v2.0 URL declared
as v1.1 (the connector's default version) works as well.

Luchtmeetnet requests are retried: HTTP 429 after its Retry-After (else 15,
30, 60 s), 5xx, timeouts and unreadable bodies after 2, 5, 10 s. After a
persistent 429, or three stations in a row failing, no further station is
requested in that run. Failed stations are reported apart from stations
without usable coordinates.

Dry run by default: prints the planned PATCHes (station, current point, new
point) and writes nothing. ``--apply`` performs them.

    python -m scripts.repair_luchtmeetnet_locations             # dry run
    python -m scripts.repair_luchtmeetnet_locations --apply     # write

The script only imports ``app.config``, ``app.exceptions`` and ``app.frost``,
which predate it, so it can also be piped into a running connector container
whose image does not contain it; arguments go after ``-``, and ``-u`` keeps the
output unbuffered:

    docker compose exec -T connector python -u - < scripts/repair_luchtmeetnet_locations.py
    docker compose exec -T connector python -u - --apply < scripts/repair_luchtmeetnet_locations.py

Exit status (the first that applies, in this order):
    4  Luchtmeetnet Observations already exist and --allow-observations was not
       given; nothing was written
    1  a FROST request failed (reading the Things, or a PATCH)
    2  stations were not checked because the Luchtmeetnet API failed (HTTP 429
       after retries, 5xx, timeout, unreadable body): re-run in a few minutes
    3  placeholder geometries were left because Luchtmeetnet has no usable
       coordinates for the station or does not know it: re-running won't help
    0  otherwise

The FROST target and its credentials come from the connector's environment
variables only (SENSORTHINGS_BASE_URL / SENSORTHINGS_BASE_URLS and
SENSORTHINGS_AUTH_USERNAME / SENSORTHINGS_AUTH_PASSWORD /
SENSORTHINGS_AUTH_TOKEN) and are never printed. ``--target-url`` picks one of
the configured targets; credentials are never sent to a URL that is not
configured.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import requests

from app.config import settings
from app.exceptions import FrostConnectionError
from app.frost.http_client import FrostHTTPClient
from app.frost.odata_fields import FrostODataFields, odata_fields_for
from app.frost.target import FrostTarget

# (lon, lat) that the old discovery wrote for every station.
PLACEHOLDER_POINT = (5.0, 52.0)
LOCATIONS = "Locations"
FEATURES_OF_INTEREST = "FeaturesOfInterest"
# FROST's core data model keeps this entity set name on its v2.0 endpoint too.
_FEATURES_OF_INTEREST_PATH = "/FeaturesOfInterest"
_POINT_TOLERANCE = 1e-9
_PAGE_SIZE = 100
_GEOJSON = "application/geo+json"
_API_HEADERS = {"Accept": "application/json", "User-Agent": "GeoInsights-Connector/1.0"}
_API_TIMEOUT = 30.0
# Luchtmeetnet API retries: waits before the 2nd, 3rd and 4th attempt.
_RATE_LIMIT_WAITS = (15.0, 30.0, 60.0)
_ERROR_WAITS = (2.0, 5.0, 10.0)
_API_ATTEMPTS = len(_ERROR_WAITS) + 1
_MAX_RETRY_AFTER = 120.0
# Stop requesting station details after this many stations in a row failed.
_MAX_CONSECUTIVE_FAILURES = 3

EXIT_OK = 0
EXIT_FROST_FAILED = 1
EXIT_API_FAILED = 2
EXIT_LEFT_AT_PLACEHOLDER = 3
EXIT_OBSERVATIONS_EXIST = 4

# Why a placeholder geometry was left in place. The Luchtmeetnet API failed
# (re-running later can help):
RATE_LIMITED = "rate_limited"
SERVER_ERROR = "server_error"
CLIENT_ERROR = "client_error"
UNREACHABLE = "unreachable"
BAD_RESPONSE = "bad_response"
NOT_CHECKED = "not_checked"
# ...or the answer itself rules a repair out (re-running does not help):
UNKNOWN_STATION = "unknown_station"
NO_COORDINATES = "no_coordinates"
PLACEHOLDER_UPSTREAM = "placeholder_upstream"
NO_STATION_NUMBER = "no_station_number"
# api_get's category for HTTP 404; a station lookup reports it as UNKNOWN_STATION.
NOT_FOUND = "not_found"

API_FAILURES = frozenset({RATE_LIMITED, SERVER_ERROR, CLIENT_ERROR, UNREACHABLE, BAD_RESPONSE, NOT_CHECKED})
REASONS = {
    RATE_LIMITED: "Luchtmeetnet rate limit (HTTP 429) after retries",
    SERVER_ERROR: "Luchtmeetnet server error after retries",
    CLIENT_ERROR: "Luchtmeetnet refused the request",
    UNREACHABLE: "Luchtmeetnet API timeout or connection error after retries",
    BAD_RESPONSE: "unreadable Luchtmeetnet response",
    NOT_CHECKED: "not requested: the Luchtmeetnet API kept failing earlier in this run",
    UNKNOWN_STATION: "station unknown to the Luchtmeetnet detail endpoint (HTTP 404)",
    NO_COORDINATES: "no usable coordinates in the Luchtmeetnet station record",
    PLACEHOLDER_UPSTREAM: "Luchtmeetnet itself reports POINT(5 52)",
    NO_STATION_NUMBER: "no station number in the Thing and no station-name match",
    NOT_FOUND: "HTTP 404",
}

Point = tuple[float, float]  # (lon, lat)


class LuchtmeetnetError(Exception):
    """A Luchtmeetnet API request failed; ``category`` says how."""

    def __init__(self, category: str, detail: str) -> None:
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail


@dataclass(frozen=True)
class Unresolved:
    """Why a station's coordinates are not known."""

    category: str
    detail: str = ""

    @property
    def is_api_failure(self) -> bool:
        return self.category in API_FAILURES

    def describe(self) -> str:
        reason = REASONS.get(self.category, self.category)
        return f"{reason} ({self.detail})" if self.detail else reason


@dataclass(frozen=True)
class PlannedPatch:
    station_number: str
    thing_id: str | None
    thing_name: str
    entity_set: str  # LOCATIONS or FEATURES_OF_INTEREST
    entity_id: str
    current: Point
    new: Point


@dataclass(frozen=True)
class LeftAtPlaceholder:
    station_number: str | None
    thing_id: str | None
    thing_name: str
    reason: Unresolved


@dataclass
class RepairPlan:
    things_seen: int = 0
    already_ok: int = 0
    patches: list[PlannedPatch] = field(default_factory=list)
    left: list[LeftAtPlaceholder] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    frost_errors: list[str] = field(default_factory=list)
    observed_things: int = 0
    observed_datastreams: int = 0

    def count(self, entity_set: str) -> int:
        return sum(1 for patch in self.patches if patch.entity_set == entity_set)

    def left_by_category(self) -> list[tuple[str, int]]:
        return sorted(Counter(item.reason.category for item in self.left).items())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def parse_point(value: Any) -> Point | None:
    """Return ``(lon, lat)`` of a GeoJSON Point (bare or wrapped in a Feature)."""
    if not isinstance(value, dict):
        return None
    if value.get("type") == "Feature":
        return parse_point(value.get("geometry"))
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list | tuple) or len(coordinates) < 2:
        return None
    try:
        lon = float(coordinates[0])
        lat = float(coordinates[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        return None
    return lon, lat


def is_placeholder(point: Point | None) -> bool:
    if point is None:
        return False
    return (
        abs(point[0] - PLACEHOLDER_POINT[0]) < _POINT_TOLERANCE
        and abs(point[1] - PLACEHOLDER_POINT[1]) < _POINT_TOLERANCE
    )


def station_detail_point(body: Any) -> Point | Unresolved:
    """``(lon, lat)`` from a ``GET /stations/{number}`` response, or why there is none.

    A body without a station record is an API failure (``BAD_RESPONSE``); a
    record without a usable point is ``NO_COORDINATES``.
    """
    record = body.get("data") if isinstance(body, dict) else None
    if not isinstance(record, dict):
        return Unresolved(BAD_RESPONSE, "no station record in the response")
    point = parse_point(record.get("geometry"))
    if point is None:
        return Unresolved(NO_COORDINATES, f"geometry {str(record.get('geometry'))[:80]}")
    return point


def _format_coordinate(value: float) -> str:
    return f"{value:.7f}".rstrip("0").rstrip(".")


def format_point(point: Point | None) -> str:
    if point is None:
        return "(none)"
    return f"POINT({_format_coordinate(point[0])} {_format_coordinate(point[1])})"


def thing_prefix(entity_prefix: str) -> str:
    return f"{entity_prefix}Luchtmeetnet "


def thing_expand(fields: FrostODataFields) -> str:
    """``$expand`` for the Things query: Locations, and one Observation with its FoI per Datastream."""
    key = fields.id
    return (
        f"Locations,Datastreams($select={key};$expand=Observations($top=1;$select={key};"
        f"$expand=FeatureOfInterest($select={key},feature)))"
    )


def entity_id(entity: dict[str, Any], fields: FrostODataFields) -> str | None:
    """An entity's id in the target's convention, falling back to the other one."""
    return FrostHTTPClient.extract_iot_id_from_body(entity, fields)


def has_next_page(body: dict[str, Any], fields: FrostODataFields) -> bool:
    return bool(body.get(fields.next_link) or body.get("@iot.nextLink") or body.get("@nextLink"))


def _station_number_property(thing: dict[str, Any]) -> str:
    properties = thing.get("properties")
    if isinstance(properties, dict):
        return str(properties.get("station_number") or "").strip()
    return ""


def station_number_for(thing: dict[str, Any], name_index: dict[str, str], entity_prefix: str) -> str | None:
    """Station number from Thing properties, else from its name via the station list."""
    number = _station_number_property(thing)
    if number:
        return number
    name = str(thing.get("name") or "")
    prefix = thing_prefix(entity_prefix)
    if name.startswith(prefix):
        return name_index.get(name[len(prefix):].strip())
    return None


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _generated_features(thing: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """FeaturesOfInterest reachable through the expanded Datastreams/Observations."""
    for datastream in _dicts(thing.get("Datastreams")):
        for observation in _dicts(datastream.get("Observations")):
            feature = observation.get("FeatureOfInterest")
            if isinstance(feature, dict):
                yield feature


def build_plan(
    things: list[dict[str, Any]],
    station_point: Callable[[str], Point | Unresolved],
    name_index: dict[str, str] | Unresolved,
    entity_prefix: str,
    fields: FrostODataFields,
) -> RepairPlan:
    """Plan a PATCH for every Location and generated FeatureOfInterest still at the placeholder."""
    plan = RepairPlan(things_seen=len(things))
    for thing in things:
        thing_name = str(thing.get("name") or "")
        thing_id = entity_id(thing, fields)
        observed = [ds for ds in _dicts(thing.get("Datastreams")) if _dicts(ds.get("Observations"))]
        if observed:
            plan.observed_things += 1
            plan.observed_datastreams += len(observed)

        locations = _dicts(thing.get("Locations"))
        if not locations:
            plan.skipped.append(f"Thing {thing_id} {thing_name!r}: Thing has no Location")
            continue

        stale: list[tuple[str, str | None]] = [
            (LOCATIONS, entity_id(loc, fields)) for loc in locations if is_placeholder(parse_point(loc.get("location")))
        ]
        seen_features: set[str | None] = set()
        for feature in _generated_features(thing):
            feature_id = entity_id(feature, fields)
            if feature_id not in seen_features and is_placeholder(parse_point(feature.get("feature"))):
                seen_features.add(feature_id)
                stale.append((FEATURES_OF_INTEREST, feature_id))
        if not stale:
            plan.already_ok += 1
            continue
        if any(stale_id is None for _, stale_id in stale):
            plan.frost_errors.append(f"Thing {thing_id} {thing_name!r}: FROST returned an entity without an id")
            continue

        if isinstance(name_index, Unresolved) and not _station_number_property(thing):
            plan.left.append(LeftAtPlaceholder(None, thing_id, thing_name, name_index))
            continue
        number = station_number_for(thing, name_index if isinstance(name_index, dict) else {}, entity_prefix)
        if not number:
            plan.left.append(LeftAtPlaceholder(None, thing_id, thing_name, Unresolved(NO_STATION_NUMBER)))
            continue

        new_point = station_point(number)
        if isinstance(new_point, Unresolved):
            plan.left.append(LeftAtPlaceholder(number, thing_id, thing_name, new_point))
            continue
        if is_placeholder(new_point):
            plan.left.append(LeftAtPlaceholder(number, thing_id, thing_name, Unresolved(PLACEHOLDER_UPSTREAM)))
            continue

        for entity_set, stale_id in stale:
            assert stale_id is not None
            plan.patches.append(
                PlannedPatch(
                    station_number=number,
                    thing_id=thing_id,
                    thing_name=thing_name,
                    entity_set=entity_set,
                    entity_id=stale_id,
                    current=PLACEHOLDER_POINT,
                    new=new_point,
                )
            )
    return plan


def patch_body(patch: PlannedPatch) -> dict[str, Any]:
    """PATCH payload; the same on SensorThings v1.1 and v2.0 (as the connector writes Locations)."""
    point = {"type": "Point", "coordinates": [patch.new[0], patch.new[1]]}
    if patch.entity_set == FEATURES_OF_INTEREST:
        return {"encodingType": _GEOJSON, "feature": point}
    return {"encodingType": _GEOJSON, "location": point}


def exit_status(plan: RepairPlan, frost_failures: int) -> int:
    if frost_failures or plan.frost_errors:
        return EXIT_FROST_FAILED
    if any(item.reason.is_api_failure for item in plan.left):
        return EXIT_API_FAILED
    if plan.left:
        return EXIT_LEFT_AT_PLACEHOLDER
    return EXIT_OK


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def select_target(targets: Sequence[FrostTarget], target_url: str | None) -> FrostTarget:
    if not targets:
        raise SystemExit("No FROST target configured (SENSORTHINGS_BASE_URL / SENSORTHINGS_BASE_URLS).")
    if target_url:
        wanted = target_url.rstrip("/")
        for target in targets:
            if target.url.rstrip("/") == wanted:
                return target
        raise SystemExit(f"{wanted} is not a configured FROST target; refusing to send credentials to it.")
    return targets[0]


def url_version(url: str) -> str | None:
    """The SensorThings version in a service root URL's last path segment (``v1.1``, ``v2.0``)."""
    segment = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return segment if re.fullmatch(r"v\d+\.\d+", segment) else None


def _frost_failure(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    message = body.get("message") if isinstance(body, dict) else None
    return f"HTTP {response.status_code}" + (f": {str(message)[:200]}" if message else "")


def fetch_luchtmeetnet_things(
    http: FrostHTTPClient, things_path: str, entity_prefix: str, fields: FrostODataFields
) -> list[dict[str, Any]]:
    """All Things whose name starts with ``<prefix>Luchtmeetnet ``, with their geometry expanded.

    Pages with ``$skip``; next links from the server are only used to tell
    whether there is another page, never followed.
    """
    endpoint = http.endpoint(things_path)
    if not endpoint:
        raise SystemExit("No FROST endpoint configured.")
    literal = thing_prefix(entity_prefix).replace("'", "''")
    things: list[dict[str, Any]] = []
    skip = 0
    while True:
        try:
            response = http.get(
                endpoint,
                params={
                    "$filter": f"startswith(name,'{literal}')",
                    "$expand": thing_expand(fields),
                    "$orderby": f"{fields.id} asc",
                    "$top": str(_PAGE_SIZE),
                    "$skip": str(skip),
                },
            )
        except FrostConnectionError as exc:
            raise SystemExit(f"GET {endpoint} failed: {type(exc.cause or exc).__name__}.") from None
        if not response.ok:
            raise SystemExit(f"GET {endpoint} failed with {_frost_failure(response)}.")
        try:
            body = response.json()
        except ValueError:
            raise SystemExit(f"GET {endpoint} returned a body that is not JSON.") from None
        page = body.get("value") if isinstance(body, dict) else None
        if not isinstance(page, list):
            raise SystemExit(f"GET {endpoint} returned no 'value' list.")
        things.extend(item for item in page if isinstance(item, dict))
        if not page or not has_next_page(body, fields):
            return things
        skip += len(page)


def _retry_after(response: requests.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), _MAX_RETRY_AFTER))
    except ValueError:  # an HTTP date: use the default backoff
        return None


def api_get(
    api_url: str,
    path: str,
    params: dict[str, str] | None = None,
    *,
    http_get: Callable[..., requests.Response] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Any:
    """GET a Luchtmeetnet API path and return the parsed JSON body.

    Retries HTTP 429 (after Retry-After, else 15, 30, 60 s), 5xx, timeouts and
    connection errors, and bodies that are not JSON (after 2, 5, 10 s). Raises
    :class:`LuchtmeetnetError` once the attempts run out, or at once for other
    HTTP errors (``NOT_FOUND`` for 404, ``CLIENT_ERROR`` otherwise).
    """
    send = http_get or requests.get
    pause = sleep or _sleep
    url = f"{api_url}{path}"
    error = LuchtmeetnetError(UNREACHABLE, "no attempt made")
    for attempt in range(1, _API_ATTEMPTS + 1):
        backoff = _ERROR_WAITS[min(attempt, len(_ERROR_WAITS)) - 1]
        try:
            response = send(url, params=params, headers=_API_HEADERS, timeout=_API_TIMEOUT)
        except requests.RequestException as exc:
            error = LuchtmeetnetError(UNREACHABLE, type(exc).__name__)
        else:
            status = response.status_code
            if status == 429:
                error = LuchtmeetnetError(RATE_LIMITED, "HTTP 429")
                retry_after = _retry_after(response)
                backoff = (
                    retry_after
                    if retry_after is not None
                    else _RATE_LIMIT_WAITS[min(attempt, len(_RATE_LIMIT_WAITS)) - 1]
                )
            elif status >= 500:
                error = LuchtmeetnetError(SERVER_ERROR, f"HTTP {status}")
            elif status == 404:
                raise LuchtmeetnetError(NOT_FOUND, "HTTP 404")
            elif status >= 400:
                raise LuchtmeetnetError(CLIENT_ERROR, f"HTTP {status}")
            else:
                try:
                    return response.json()
                except ValueError:
                    error = LuchtmeetnetError(BAD_RESPONSE, "body is not JSON")
        if attempt < _API_ATTEMPTS:
            pause(backoff)
    raise error


def fetch_station_name_index(get: Callable[[str, dict[str, str] | None], Any]) -> dict[str, str]:
    """``{station name: station number}`` from the paged ``GET /stations`` list.

    Raises :class:`LuchtmeetnetError` when a page fails or is not a station list.
    """
    index: dict[str, str] = {}
    page = 1
    while page <= 50:
        body = get("/stations", {"page": str(page)})
        items = body.get("data") if isinstance(body, dict) else None
        pagination = body.get("pagination") if isinstance(body, dict) else None
        last_page = pagination.get("last_page") if isinstance(pagination, dict) else None
        if not isinstance(items, list) or not items or not isinstance(last_page, int):
            raise LuchtmeetnetError(BAD_RESPONSE, f"station list page {page} is empty or has no pagination")
        for item in items:
            if not isinstance(item, dict):
                continue
            number = str(item.get("number") or "").strip()
            name = str(item.get("location") or "").strip()
            if number and name:
                index[name] = number
        if page >= last_page:
            break
        page += 1
    return index


def make_station_point_lookup(
    get: Callable[[str, dict[str, str] | None], Any],
) -> Callable[[str], Point | Unresolved]:
    """``lookup(number)``: the station's ``(lon, lat)`` from its detail record, or why not.

    Stops requesting after a persistent HTTP 429 or after
    ``_MAX_CONSECUTIVE_FAILURES`` stations in a row failed: further stations
    are then ``NOT_CHECKED``.
    """
    cache: dict[str, Point | Unresolved] = {}
    stopped = ""
    consecutive_failures = 0

    def lookup(number: str) -> Point | Unresolved:
        nonlocal stopped, consecutive_failures
        if number in cache:
            return cache[number]
        result: Point | Unresolved
        if stopped:
            result = Unresolved(NOT_CHECKED, stopped)
        else:
            try:
                result = station_detail_point(get(f"/stations/{quote(number, safe='')}", None))
            except LuchtmeetnetError as exc:
                category = UNKNOWN_STATION if exc.category == NOT_FOUND else exc.category
                result = Unresolved(category, exc.detail)
            if isinstance(result, Unresolved) and result.is_api_failure:
                print(f"  ! station {number}: {result.describe()}")
                consecutive_failures += 1
                if result.category == RATE_LIMITED:
                    stopped = "after a persistent HTTP 429"
                elif consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    stopped = f"after {consecutive_failures} stations in a row failed"
            else:
                consecutive_failures = 0
        cache[number] = result
        return result

    return lookup


def apply_plan(http: FrostHTTPClient, locations_path: str, plan: RepairPlan) -> tuple[int, int]:
    applied = failed = 0
    for patch in plan.patches:
        path = locations_path if patch.entity_set == LOCATIONS else _FEATURES_OF_INTEREST_PATH
        label = f"{patch.station_number} {patch.entity_set}({patch.entity_id})"
        endpoint = http.endpoint(f"{path}({patch.entity_id})")
        if not endpoint:
            failed += 1
            continue
        try:
            response = http.patch(endpoint, patch_body(patch))
        except Exception as exc:
            print(f"  FAILED {label}: {type(exc).__name__}")
            failed += 1
            continue
        if response.ok:
            applied += 1
            print(f"  patched {label} -> {format_point(patch.new)}")
        else:
            failed += 1
            print(f"  FAILED {label}: {_frost_failure(response)}")
    return applied, failed


def print_plan(plan: RepairPlan) -> None:
    print(f"{'station':<9} {'thing':>7}  {'entity':<26} current        -> new                            name")
    for patch in plan.patches:
        entity = f"{patch.entity_set}({patch.entity_id})"
        print(
            f"{patch.station_number:<9} {patch.thing_id!s:>7}  {entity:<26} "
            f"{format_point(patch.current):<14} -> {format_point(patch.new):<30} {patch.thing_name}"
        )
    for item in plan.left:
        print(
            f"  left at the placeholder: {item.station_number or '-'} Thing {item.thing_id} "
            f"{item.thing_name!r}: {item.reason.describe()}"
        )
    for reason in plan.skipped:
        print(f"  skipped: {reason}")
    for error in plan.frost_errors:
        print(f"  FROST error: {error}")


def print_summary(plan: RepairPlan) -> None:
    print(
        f"\n{plan.things_seen} Luchtmeetnet Things: {plan.count(LOCATIONS)} Locations and "
        f"{plan.count(FEATURES_OF_INTEREST)} FeaturesOfInterest to move off {format_point(PLACEHOLDER_POINT)}, "
        f"{plan.already_ok} Things already elsewhere, {len(plan.left)} left at the placeholder."
    )
    if plan.left:
        print(f"Left at {format_point(PLACEHOLDER_POINT)}, by reason:")
        for category, count in plan.left_by_category():
            hint = "re-run in a few minutes" if category in API_FAILURES else "re-running will not help"
            print(f"  {count:>4}  {REASONS.get(category, category)} [{category}: {hint}]")


def print_observation_warning(plan: RepairPlan, allow_observations: bool) -> None:
    bar = "!" * 79
    print(
        f"\n{bar}\n"
        f"!! {plan.observed_datastreams} Luchtmeetnet Datastreams on {plan.observed_things} Things already have Observations.\n"
        "!! This repair must run before the connector pushes Luchtmeetnet data: FROST generated a\n"
        "!! FeatureOfInterest from each placeholder Location for those Observations and keeps\n"
        "!! reusing it after the Location moves. Only the FeatureOfInterest of one Observation per\n"
        "!! Datastream is checked."
    )
    if allow_observations:
        print("!! --allow-observations given: placeholder FeaturesOfInterest found that way are PATCHed too.")
    else:
        print("!! --apply is refused. Check those Observations, then re-run with --allow-observations.")
    print(bar)


def run(
    http: FrostHTTPClient,
    get: Callable[[str, dict[str, str] | None], Any],
    *,
    fields: FrostODataFields,
    things_path: str,
    locations_path: str,
    entity_prefix: str,
    apply: bool,
    allow_observations: bool = False,
) -> int:
    things = fetch_luchtmeetnet_things(http, things_path, entity_prefix, fields)
    name_index: dict[str, str] | Unresolved = {}
    if any(not _station_number_property(thing) for thing in things):
        try:
            name_index = fetch_station_name_index(get)
        except LuchtmeetnetError as exc:
            category = CLIENT_ERROR if exc.category == NOT_FOUND else exc.category
            name_index = Unresolved(category, f"station list: {exc.detail}")
    plan = build_plan(things, make_station_point_lookup(get), name_index, entity_prefix, fields)

    print_plan(plan)
    print_summary(plan)
    if plan.observed_datastreams:
        print_observation_warning(plan, allow_observations)
        if not allow_observations:
            print("Nothing was written.")
            return EXIT_OBSERVATIONS_EXIST

    if not apply:
        print("Dry run: nothing was written." + (" Re-run with --apply to PATCH them." if plan.patches else ""))
        return exit_status(plan, 0)

    applied, failed = apply_plan(http, locations_path, plan)
    print(f"Applied {applied} PATCHes, {failed} failed.")
    return exit_status(plan, failed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--apply", action="store_true", help="PATCH the Locations (default: dry run)")
    parser.add_argument(
        "--allow-observations",
        action="store_true",
        help="repair even when Luchtmeetnet Observations already exist (their placeholder FeaturesOfInterest too)",
    )
    parser.add_argument("--target-url", help="one of the configured FROST targets (default: the primary target)")
    parser.add_argument("--api-url", default=settings.luchtmeetnet_api_url, help="Luchtmeetnet open API base URL")
    parser.add_argument("--prefix", default=settings.entity_name_prefix, help="entity name prefix (default from env)")
    args = parser.parse_args(argv)

    target = select_target(settings.frost_targets, args.target_url)
    fields = odata_fields_for(target.version)
    http = FrostHTTPClient(
        base_urls=[target.url],
        auth_username=target.auth_username,
        auth_password=target.auth_password,
        auth_token=target.auth_token,
        timeout=settings.request_timeout_seconds,
    )
    has_auth = bool(target.auth_username or target.auth_token)
    print(
        f"FROST target: {target.url} (declared SensorThings {target.version}; "
        f"credentials from environment: {'yes' if has_auth else 'none'})"
    )
    endpoint_version = url_version(target.url)
    if endpoint_version and odata_fields_for(endpoint_version) != fields:
        print(f"  note: the URL is a {endpoint_version} endpoint; ids and next links are read in both conventions")
    print(f"Mode: {'APPLY' if args.apply else 'dry run'}\n")

    api_url = str(args.api_url).rstrip("/")

    def get(path: str, params: dict[str, str] | None) -> Any:
        return api_get(api_url, path, params)

    return run(
        http,
        get,
        fields=fields,
        things_path=settings.things_path,
        locations_path=settings.locations_path,
        entity_prefix=args.prefix,
        apply=args.apply,
        allow_observations=args.allow_observations,
    )


if __name__ == "__main__":
    sys.exit(main())
