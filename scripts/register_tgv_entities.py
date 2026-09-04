"""Register GEO_ TGV entities on the primary FROST server and new Davis datastreams on v2.

The primary FROST server has TGV observations flowing to orphan datastreams
(Milesight Things). This script creates proper GEO_ Things/Sensors/Datastreams
so the monitoring module can discover them.

Usage:
    python scripts/register_tgv_entities.py
"""
from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import requests

# Add project root to path for canonical imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.sta.canonical import resolve

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
PRIMARY_CACHE = Path(__file__).resolve().parent.parent / "data" / "registered_entities.json"
V2_CACHE = Path(__file__).resolve().parent.parent / "data" / "entities_frost-v2-demo.json"


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _headers_basic(username: str, password: str) -> dict[str, str]:
    cred = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Content-Type": "application/json", "Authorization": f"Basic {cred}"}


def _get_or_create(
    base_url: str, path: str, name: str, payload: dict[str, Any],
    headers: dict[str, str], is_v2: bool = False,
) -> str | None:
    """Find by name or create. Returns @iot.id / id."""
    id_key = "id" if is_v2 else "@iot.id"

    # Search
    resp = requests.get(
        f"{base_url}{path}",
        params={"$filter": f"name eq '{name}'", "$top": "1", "$select": f"{id_key},name"},
        headers=headers, timeout=15,
    )
    if resp.ok:
        values = resp.json().get("value", [])
        if values:
            eid = str(values[0].get(id_key) or values[0].get("@iot.id") or values[0].get("id"))
            print(f"    EXISTS {path} '{name}' -> {eid}")
            return eid

    # Create
    resp = requests.post(f"{base_url}{path}", json=payload, headers=headers, timeout=15)
    if resp.ok:
        body = resp.json() if resp.text else {}
        eid = str(body.get(id_key) or body.get("@iot.id") or body.get("id") or "")
        if not eid:
            loc = resp.headers.get("Location", "")
            import re
            m = re.search(r"\(([^)]+)\)$", loc)
            eid = m.group(1) if m else ""
        print(f"    CREATED {path} '{name}' -> {eid}")
        return eid
    else:
        print(f"    FAILED {path} '{name}': {resp.status_code} {resp.text[:200]}")
        return None


def _link_location_to_thing(
    base_url: str, thing_id: str, location_id: str,
    headers: dict[str, str], is_v2: bool = False,
) -> None:
    """PATCH the Thing to link the Location."""
    id_key = "id" if is_v2 else "@iot.id"
    resp = requests.patch(
        f"{base_url}/Things({thing_id})",
        json={"Locations": [{id_key: int(location_id) if location_id.isdigit() else location_id}]},
        headers=headers, timeout=15,
    )
    if resp.ok:
        print(f"    LINKED Thing({thing_id}) <- Location({location_id})")
    else:
        print(f"    LINK FAILED: {resp.status_code} {resp.text[:200]}")


# Entity definitions from climate_adaptation.py
ENTITY_SETS = [
    {
        "thing": {"name": "TGV Office Lab", "description": "Indoor climate monitoring at The Green Village office laboratory."},
        "location": {"name": "TGV Office Lab location", "description": "TGV office lab, TU Delft campus.", "coordinates": [4.377634, 51.996581]},
        "sensor": {"sensor_id": "tgv-officelab-climate", "name": "TGV Office Lab climate sensor", "description": "Multi-parameter indoor climate sensor."},
        "observed_properties": ["temperature", "humidity", "co2", "pressure"],
    },
    {
        "thing": {"name": "Climate Davis", "description": "Davis Vantage Pro2 outdoor weather station at The Green Village."},
        "location": {"name": "Climate Davis location", "description": "Davis weather station at TGV, TU Delft.", "coordinates": [4.377634, 51.996581]},
        "sensor": {"sensor_id": "tgv-climate-davis-weather-station", "name": "Davis weather station", "description": "Davis Vantage Pro2 measuring temperature, wind, precipitation, and derived parameters."},
        "observed_properties": [
            "air_temperature", "wind_speed", "wind_direction", "precipitation",
            "dew_point", "heat_index", "wind_chill", "wind_gust",
            "rain_rate", "uv_index", "wet_bulb_temperature", "evapotranspiration",
        ],
    },
    {
        "thing": {"name": "Weather Climatics", "description": "Climatics rooftop weather station at The Green Village."},
        "location": {"name": "Weather Climatics location", "description": "Climatics rooftop station at TGV.", "coordinates": [4.377634, 51.996581]},
        "sensor": {"sensor_id": "tgv-weather-climatics-rooftop-station", "name": "Climatics rooftop station", "description": "Climatics rooftop weather station measuring air pressure."},
        "observed_properties": ["air_pressure"],
    },
    {
        "thing": {"name": "Hitteplein", "description": "Hitteplein climate station at The Green Village."},
        "location": {"name": "Hitteplein location", "description": "Hitteplein climate station at TGV.", "coordinates": [4.377634, 51.996581]},
        "sensor": {"sensor_id": "tgv-hitteplein-climate-station", "name": "Hitteplein climate station", "description": "Hitteplein climate station measuring solar radiation and humidity."},
        "observed_properties": ["solar_radiation", "relative_humidity"],
    },
]

# Map from stream_key -> canonical observed_property for the DATASTREAM_IDS_JSON output
STREAM_KEY_MAP = {
    "Inside Temperature": "temperature",
    "Inside Relative Humidity": "humidity",
    "Outside Temperature": "air_temperature",
    "Wind Speed": "wind_speed",
    "Wind Direction": "wind_direction",
    "Daily Rain": "precipitation",
    "Barometric Pressure": "air_pressure",
    "Solar Radiation": "solar_radiation",
    "Outside Relative Humidity": "relative_humidity",
    "Dew Point": "dew_point",
    "Heat Index": "heat_index",
    "Wind Chill": "wind_chill",
    "10 Minutes Average Wind Gust": "wind_gust",
    "Rain Rate": "rain_rate",
    "Ultraviolet Radiation Index": "uv_index",
    "Wet Bulb Temperature (indication)": "wet_bulb_temperature",
    "Current Evapotranspiration": "evapotranspiration",
}


def register_on_server(
    base_url: str, headers: dict[str, str], prefix: str,
    cache: dict, is_v2: bool = False,
) -> dict[str, str]:
    """Register all TGV entities on one server. Returns {ds_cache_key: ds_id}."""
    id_key = "id" if is_v2 else "@iot.id"
    ds_ids: dict[str, str] = {}

    for es in ENTITY_SETS:
        thing_name = f"{prefix}{es['thing']['name']}"
        print(f"\n  === {thing_name} ===")

        # Thing
        thing_id = _get_or_create(base_url, "/Things", thing_name, {
            "name": thing_name,
            "description": es["thing"]["description"],
            "properties": {"site": "tgv", "campus": "TU Delft", "source": "kafka"},
        }, headers, is_v2)
        if not thing_id:
            continue
        cache.setdefault("things", {})[thing_name] = thing_id

        # Location
        loc_name = f"{prefix}{es['location']['name']}"
        loc_id = _get_or_create(base_url, "/Locations", loc_name, {
            "name": loc_name,
            "description": es["location"]["description"],
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": es["location"]["coordinates"]},
        }, headers, is_v2)
        if loc_id:
            cache.setdefault("locations", {})[loc_name] = loc_id
            _link_location_to_thing(base_url, thing_id, loc_id, headers, is_v2)

        # Sensor
        sensor_name = f"{prefix}{es['sensor']['name']}"
        sensor_id = _get_or_create(base_url, "/Sensors", sensor_name, {
            "name": sensor_name,
            "description": es["sensor"]["description"],
            "encodingType": "application/json",
            "metadata": "https://thegreenvillage.org",
        }, headers, is_v2)
        if not sensor_id:
            continue
        cache.setdefault("sensors", {})[sensor_name] = sensor_id

        # ObservedProperties + Datastreams
        for op_key in es["observed_properties"]:
            canonical = resolve(op_key)
            if canonical is None:
                print(f"    SKIP unknown property {op_key}")
                continue
            meta = canonical.meta

            # ObservedProperty
            op_id = _get_or_create(base_url, "/ObservedProperties", meta.display_name, {
                "name": meta.display_name,
                "definition": meta.definition,
                "description": meta.display_name,
            }, headers, is_v2)
            if op_id:
                cache.setdefault("observed_properties", {})[meta.display_name] = op_id

            # Datastream
            ds_name = f"{sensor_name} - {meta.display_name}"
            ds_payload: dict[str, Any] = {
                "name": ds_name,
                "description": f"{meta.display_name} observations for {thing_name}",
                "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                "unitOfMeasurement": {
                    "name": meta.display_name,
                    "symbol": meta.unit,
                    "definition": meta.definition,
                },
                "Thing": {id_key: int(thing_id) if thing_id.isdigit() else thing_id},
                "Sensor": {id_key: int(sensor_id) if sensor_id.isdigit() else sensor_id},
                "ObservedProperty": {id_key: int(op_id) if op_id and op_id.isdigit() else op_id},
                "properties": {
                    "site_key": "tgv",
                    "thing_name": thing_name,
                    "sensor_id": es["sensor"]["sensor_id"],
                    "observed_property": canonical.value,
                    "streamKey": canonical.value,
                },
            }
            if is_v2:
                # Adapt for v2: remove observationType, transform unitOfMeasurement to resultType
                ds_payload.pop("observationType", None)
                uom = ds_payload.pop("unitOfMeasurement", {})
                ds_payload["resultType"] = {
                    "type": "Quantity",
                    "label": uom.get("name", ""),
                    "uom": {"code": uom.get("symbol", "")},
                }
                # v2: ObservedProperty -> ObservedProperties (list)
                op_ref = ds_payload.pop("ObservedProperty", None)
                if op_ref:
                    ds_payload["ObservedProperties"] = [op_ref]

            ds_id = _get_or_create(base_url, "/Datastreams", ds_name, ds_payload, headers, is_v2)
            if ds_id:
                ds_cache_key = f"{es['sensor']['sensor_id']}::{canonical.value}"
                cache.setdefault("datastreams", {})[ds_cache_key] = ds_id
                ds_ids[ds_cache_key] = ds_id

    return ds_ids


def main():
    env = _load_env()
    prefix = env.get("SENSORTHINGS_ENTITY_NAME_PREFIX", "")

    # Load caches
    primary_cache = json.loads(PRIMARY_CACHE.read_text()) if PRIMARY_CACHE.exists() else {}
    v2_cache = json.loads(V2_CACHE.read_text()) if V2_CACHE.exists() else {}

    # Primary FROST server
    v1_url = env["SENSORTHINGS_BASE_URL"].rstrip("/")
    v1_headers = _headers_basic(
        env.get("SENSORTHINGS_AUTH_USERNAME", ""),
        env.get("SENSORTHINGS_AUTH_PASSWORD", ""),
    )

    print(f"{'='*70}")
    print(f"  Registering on PRIMARY: {v1_url}")
    print(f"{'='*70}")
    v1_ds = register_on_server(v1_url, v1_headers, prefix, primary_cache, is_v2=False)

    # V2 FROST server (only new datastreams needed — existing ones already there)
    v2_url = "https://ogc-demo.k8s.ilt-dmz.iosb.fraunhofer.de/FROST-StaV2Core/v2.0"
    v2_headers = {"Content-Type": "application/json"}

    print(f"\n{'='*70}")
    print(f"  Registering on V2: {v2_url}")
    print(f"{'='*70}")
    v2_ds = register_on_server(v2_url, v2_headers, prefix, v2_cache, is_v2=True)

    # Save caches
    PRIMARY_CACHE.write_text(json.dumps(primary_cache, indent=2))
    V2_CACHE.write_text(json.dumps(v2_cache, indent=2))
    print(f"\nSaved primary cache ({len(primary_cache.get('datastreams', {}))} datastreams)")
    print(f"Saved v2 cache ({len(v2_cache.get('datastreams', {}))} datastreams)")

    # Build new SENSORTHINGS_DATASTREAM_IDS_JSON
    new_ds_json: dict[str, str] = {}
    for stream_key, canonical_key in STREAM_KEY_MAP.items():
        # Find the sensor_id for this canonical key
        for es in ENTITY_SETS:
            if canonical_key in es["observed_properties"]:
                cache_key = f"{es['sensor']['sensor_id']}::{canonical_key}"
                if cache_key in v1_ds:
                    new_ds_json[stream_key] = v1_ds[cache_key]
                break

    print(f"\n{'='*70}")
    print(f"  NEW SENSORTHINGS_DATASTREAM_IDS_JSON for Render")
    print(f"{'='*70}")
    print(json.dumps(new_ds_json))

    # Also show what changed
    old_ds_json = json.loads(env.get("SENSORTHINGS_DATASTREAM_IDS_JSON", "{}"))
    print(f"\nOld IDs ({len(old_ds_json)} entries):")
    for k, v in old_ds_json.items():
        new_v = new_ds_json.get(k, "MISSING")
        changed = " <-- CHANGED" if new_v != v else ""
        print(f"  {k:40s} {v:>5s} -> {new_v:>5s}{changed}")

    new_keys = set(new_ds_json.keys()) - set(old_ds_json.keys())
    if new_keys:
        print(f"\nNew entries ({len(new_keys)}):")
        for k in sorted(new_keys):
            print(f"  {k:40s} -> {new_ds_json[k]:>5s}")


if __name__ == "__main__":
    main()
