"""Repair Luchtmeetnet Locations stuck at the POINT(5 52) placeholder.

Before the station-detail fix, Luchtmeetnet discovery read only the paged
``GET /stations`` list (station number + name, no coordinates) and fell back
to lat=52, lon=5 for every station, so every Luchtmeetnet Location in FROST
was created at POINT(5 52). This one-off script looks up each station's real
coordinates in ``GET /stations/{number}`` and PATCHes its Location.

FROST generates a FeatureOfInterest from a Thing's Location when the first
Observation arrives and keeps reusing it; PATCHing the Location later does not
update that FeatureOfInterest (checked on FROST 2.8). So if Observations were
pushed before this repair, their generated FeatureOfInterest is still at the
placeholder; the script finds it through the Thing's Observations and PATCHes
it as well. Run it before the connector fix is deployed and there is none.

Things are matched on ``properties.station_number`` (set by the connector on
every Luchtmeetnet Thing) and, failing that, on the station name from the
Luchtmeetnet station list. Only geometries still at the placeholder point are
touched, so re-running after a partial run is safe.

Dry run by default: prints the planned PATCHes (station, current point, new
point) and writes nothing. ``--apply`` performs the PATCHes.

    python -m scripts.repair_luchtmeetnet_locations             # dry run
    python -m scripts.repair_luchtmeetnet_locations --apply     # write

The script only imports ``app.config`` and ``app.frost.http_client``, so it
can also be piped into a connector container that predates the fix:

    docker exec -i <connector-container> python - < scripts/repair_luchtmeetnet_locations.py

The FROST target and its credentials come from the connector's environment
variables only (SENSORTHINGS_BASE_URL / SENSORTHINGS_BASE_URLS and
SENSORTHINGS_AUTH_USERNAME / SENSORTHINGS_AUTH_PASSWORD /
SENSORTHINGS_AUTH_TOKEN) and are never printed. ``--target-url`` picks one of
the configured targets; credentials are never sent to a URL that is not
configured. Only STA v1.x targets are supported.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import requests

from app.config import settings
from app.frost.http_client import FrostHTTPClient
from app.frost.target import FrostTarget

# (lon, lat) that the old discovery wrote for every station.
PLACEHOLDER_POINT = (5.0, 52.0)
LOCATIONS = "Locations"
FEATURES_OF_INTEREST = "FeaturesOfInterest"
_FEATURES_OF_INTEREST_PATH = "/FeaturesOfInterest"
_POINT_TOLERANCE = 1e-9
_PAGE_SIZE = 100
_GEOJSON = "application/geo+json"
# One Observation of one Datastream is enough: FROST generates a single
# FeatureOfInterest per Location and reuses it for every Observation.
_THING_EXPAND = "Locations,Datastreams($top=1;$expand=Observations($top=1;$expand=FeatureOfInterest))"
_API_HEADERS = {"Accept": "application/json", "User-Agent": "GeoInsights-Connector/1.0"}
_API_TIMEOUT = 30.0

Point = tuple[float, float]  # (lon, lat)


@dataclass(frozen=True)
class PlannedPatch:
    station_number: str
    thing_id: Any
    thing_name: str
    entity_set: str  # LOCATIONS or FEATURES_OF_INTEREST
    entity_id: Any
    current: Point
    new: Point


@dataclass
class RepairPlan:
    things_seen: int = 0
    already_ok: int = 0
    patches: list[PlannedPatch] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def count(self, entity_set: str) -> int:
        return sum(1 for patch in self.patches if patch.entity_set == entity_set)


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


def station_detail_point(body: Any) -> Point | None:
    """Return ``(lon, lat)`` from a ``GET /stations/{number}`` response, if usable."""
    record = body.get("data") if isinstance(body, dict) and "data" in body else body
    if not isinstance(record, dict):
        return None
    return parse_point(record.get("geometry"))


def _format_coordinate(value: float) -> str:
    return f"{value:.7f}".rstrip("0").rstrip(".")


def format_point(point: Point | None) -> str:
    if point is None:
        return "(none)"
    return f"POINT({_format_coordinate(point[0])} {_format_coordinate(point[1])})"


def thing_prefix(entity_prefix: str) -> str:
    return f"{entity_prefix}Luchtmeetnet "


def station_number_for(thing: dict[str, Any], name_index: dict[str, str], entity_prefix: str) -> str | None:
    """Station number from Thing properties, else from its name via the station list."""
    properties = thing.get("properties")
    if isinstance(properties, dict):
        number = str(properties.get("station_number") or "").strip()
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
    station_point: Callable[[str], Point | None],
    name_index: dict[str, str],
    entity_prefix: str,
) -> RepairPlan:
    """Plan a PATCH for every Location and generated FeatureOfInterest still at the placeholder."""
    plan = RepairPlan(things_seen=len(things))
    for thing in things:
        thing_name = str(thing.get("name") or "")
        thing_id = thing.get("@iot.id")
        number = station_number_for(thing, name_index, entity_prefix)
        if not number:
            plan.skipped.append(f"Thing {thing_id} {thing_name!r}: no station number in properties and no name match")
            continue

        locations = _dicts(thing.get("Locations"))
        if not locations:
            plan.skipped.append(f"{number} Thing {thing_id} {thing_name!r}: Thing has no Location")
            continue

        stale: list[tuple[str, Any]] = [
            (LOCATIONS, loc.get("@iot.id")) for loc in locations if is_placeholder(parse_point(loc.get("location")))
        ]
        seen_features: set[Any] = set()
        for feature in _generated_features(thing):
            feature_id = feature.get("@iot.id")
            if feature_id not in seen_features and is_placeholder(parse_point(feature.get("feature"))):
                seen_features.add(feature_id)
                stale.append((FEATURES_OF_INTEREST, feature_id))
        if not stale:
            plan.already_ok += 1
            continue

        new_point = station_point(number)
        if new_point is None:
            plan.skipped.append(f"{number} {thing_name!r}: no usable coordinates from Luchtmeetnet; left as is")
            continue
        if is_placeholder(new_point):
            plan.skipped.append(f"{number} {thing_name!r}: Luchtmeetnet itself reports the placeholder point; left as is")
            continue

        for entity_set, entity_id in stale:
            plan.patches.append(
                PlannedPatch(
                    station_number=number,
                    thing_id=thing_id,
                    thing_name=thing_name,
                    entity_set=entity_set,
                    entity_id=entity_id,
                    current=PLACEHOLDER_POINT,
                    new=new_point,
                )
            )
    return plan


def patch_body(patch: PlannedPatch) -> dict[str, Any]:
    point = {"type": "Point", "coordinates": [patch.new[0], patch.new[1]]}
    if patch.entity_set == FEATURES_OF_INTEREST:
        return {"encodingType": _GEOJSON, "feature": point}
    return {"encodingType": _GEOJSON, "location": point}


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


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


def fetch_luchtmeetnet_things(http: FrostHTTPClient, things_path: str, entity_prefix: str) -> list[dict[str, Any]]:
    """All Things whose name starts with ``<prefix>Luchtmeetnet ``, with their geometry expanded."""
    endpoint = http.endpoint(things_path)
    if not endpoint:
        raise SystemExit("No FROST endpoint configured.")
    literal = thing_prefix(entity_prefix).replace("'", "''")
    things: list[dict[str, Any]] = []
    skip = 0
    while True:
        response = http.get(
            endpoint,
            params={
                "$filter": f"startswith(name,'{literal}')",
                "$expand": _THING_EXPAND,
                "$orderby": "@iot.id asc",
                "$top": str(_PAGE_SIZE),
                "$skip": str(skip),
            },
        )
        if not response.ok:
            raise SystemExit(f"GET {endpoint} failed with HTTP {response.status_code}.")
        body = response.json()
        page = body.get("value", []) if isinstance(body, dict) else []
        things.extend(item for item in page if isinstance(item, dict))
        if not page or not body.get("@iot.nextLink"):
            return things
        skip += len(page)


def api_get(api_url: str, path: str, params: dict[str, str] | None = None) -> Any:
    response = requests.get(f"{api_url}{path}", params=params, headers=_API_HEADERS, timeout=_API_TIMEOUT)
    response.raise_for_status()
    return response.json()


def fetch_station_name_index(get: Callable[[str, dict[str, str] | None], Any]) -> dict[str, str]:
    """``{station name: station number}`` from the paged ``GET /stations`` list."""
    index: dict[str, str] = {}
    page = 1
    while page <= 50:
        body = get("/stations", {"page": str(page)})
        items = body.get("data", []) if isinstance(body, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            number = str(item.get("number") or "").strip()
            name = str(item.get("location") or "").strip()
            if number and name:
                index[name] = number
        pagination = body.get("pagination") if isinstance(body, dict) else None
        last_page = pagination.get("last_page") if isinstance(pagination, dict) else None
        if not items or not isinstance(last_page, int) or page >= last_page:
            break
        page += 1
    return index


def make_station_point_lookup(get: Callable[[str, dict[str, str] | None], Any]) -> Callable[[str], Point | None]:
    cache: dict[str, Point | None] = {}

    def lookup(number: str) -> Point | None:
        if number not in cache:
            try:
                cache[number] = station_detail_point(get(f"/stations/{quote(number, safe='')}", None))
            except (requests.RequestException, ValueError) as exc:
                print(f"  ! station {number}: detail fetch failed ({type(exc).__name__}: {exc})")
                cache[number] = None
        return cache[number]

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
            print(f"  FAILED {label}: HTTP {response.status_code}")
    return applied, failed


def print_plan(plan: RepairPlan) -> None:
    print(f"{'station':<9} {'thing':>7}  {'entity':<26} current        -> new                            name")
    for patch in plan.patches:
        entity = f"{patch.entity_set}({patch.entity_id})"
        print(
            f"{patch.station_number:<9} {patch.thing_id!s:>7}  {entity:<26} "
            f"{format_point(patch.current):<14} -> {format_point(patch.new):<30} {patch.thing_name}"
        )
    for reason in plan.skipped:
        print(f"  skipped: {reason}")


def run(
    http: FrostHTTPClient,
    get: Callable[[str, dict[str, str] | None], Any],
    *,
    things_path: str,
    locations_path: str,
    entity_prefix: str,
    apply: bool,
) -> int:
    things = fetch_luchtmeetnet_things(http, things_path, entity_prefix)
    needs_name_index = any(
        not (isinstance(t.get("properties"), dict) and str(t["properties"].get("station_number") or "").strip())
        for t in things
    )
    name_index = fetch_station_name_index(get) if needs_name_index else {}
    plan = build_plan(things, make_station_point_lookup(get), name_index, entity_prefix)

    print_plan(plan)
    print(
        f"\n{plan.things_seen} Luchtmeetnet Things: {plan.count(LOCATIONS)} Locations and "
        f"{plan.count(FEATURES_OF_INTEREST)} FeaturesOfInterest to move off {format_point(PLACEHOLDER_POINT)}, "
        f"{plan.already_ok} Things already elsewhere, {len(plan.skipped)} skipped."
    )
    if not apply:
        print("Dry run: nothing was written. Re-run with --apply to PATCH them.")
        return 0

    applied, failed = apply_plan(http, locations_path, plan)
    print(f"Applied {applied} PATCHes, {failed} failed.")
    return 1 if failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--apply", action="store_true", help="PATCH the Locations (default: dry run)")
    parser.add_argument("--target-url", help="one of the configured FROST targets (default: the primary target)")
    parser.add_argument("--api-url", default=settings.luchtmeetnet_api_url, help="Luchtmeetnet open API base URL")
    parser.add_argument("--prefix", default=settings.entity_name_prefix, help="entity name prefix (default from env)")
    args = parser.parse_args(argv)

    target = select_target(settings.frost_targets, args.target_url)
    if target.is_v2:
        raise SystemExit(f"{target.url} is an STA v2.0 target; this script only handles v1.x.")
    http = FrostHTTPClient(
        base_urls=[target.url],
        auth_username=target.auth_username,
        auth_password=target.auth_password,
        auth_token=target.auth_token,
        timeout=settings.request_timeout_seconds,
    )
    has_auth = bool(target.auth_username or target.auth_token)
    print(f"FROST target: {target.url} (credentials from environment: {'yes' if has_auth else 'none'})")
    print(f"Mode: {'APPLY' if args.apply else 'dry run'}\n")

    api_url = str(args.api_url).rstrip("/")

    def get(path: str, params: dict[str, str] | None) -> Any:
        return api_get(api_url, path, params)

    return run(
        http,
        get,
        things_path=settings.things_path,
        locations_path=settings.locations_path,
        entity_prefix=args.prefix,
        apply=args.apply,
    )


if __name__ == "__main__":
    sys.exit(main())
