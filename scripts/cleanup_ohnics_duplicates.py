"""Remove duplicate Ohnics observations from FROST.

Duplicates are observations on the same datastream with the same
phenomenonTime and result value. Keeps the lowest @iot.id (first created)
and deletes the rest.

Usage:
    python -m scripts.cleanup_ohnics_duplicates          # dry-run (default)
    python -m scripts.cleanup_ohnics_duplicates --delete  # actually delete
"""

from __future__ import annotations

import argparse
import sys

import requests

from app.config import settings
from app.frost.http_client import FrostHTTPClient

http = FrostHTTPClient(
    base_urls=[url.strip() for url in (settings.sensorthings_base_url or "").split(",") if url.strip()]
    or [url.strip() for url in (settings.sensorthings_base_urls or "").split(",") if url.strip()],
    auth_username=settings.auth_username,
    auth_password=settings.auth_password,
    auth_token=settings.auth_token,
)

BASE = http.primary_base_url
HEADERS = http.headers()


def get_ohnics_datastreams() -> list[dict]:
    """Fetch all Ohnics datastream IDs and names."""
    prefix = settings.entity_name_prefix
    url = f"{BASE}/Datastreams"
    params = {
        "$filter": f"startswith(name,'{prefix}Ohnics')",
        "$select": "@iot.id,name",
        "$top": "200",
    }
    resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def get_observations(datastream_id: str) -> list[dict]:
    """Fetch all observations for a datastream."""
    url = f"{BASE}/Datastreams({datastream_id})/Observations"
    params = {
        "$select": "@iot.id,phenomenonTime,result",
        "$orderby": "@iot.id asc",
        "$top": "1000",
    }
    resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json().get("value", [])


def find_duplicates(observations: list[dict]) -> list[str]:
    """Return @iot.ids of duplicate observations (keeps first occurrence)."""
    seen: set[str] = set()
    duplicate_ids: list[str] = []
    for obs in observations:
        key = f"{obs['phenomenonTime']}|{obs['result']}"
        if key in seen:
            duplicate_ids.append(str(obs["@iot.id"]))
        else:
            seen.add(key)
    return duplicate_ids


def delete_observation(iot_id: str) -> int:
    """Delete a single observation. Returns status code."""
    url = f"{BASE}/Observations({iot_id})"
    resp = requests.delete(url, headers=HEADERS, timeout=30)
    return resp.status_code


def main():
    parser = argparse.ArgumentParser(description="Clean up duplicate Ohnics observations")
    parser.add_argument("--delete", action="store_true", help="Actually delete duplicates (default is dry-run)")
    args = parser.parse_args()

    if not BASE:
        print("ERROR: No FROST base URL configured.")
        sys.exit(1)

    print(f"FROST: {BASE}")
    print(f"Mode: {'DELETE' if args.delete else 'DRY-RUN'}\n")

    datastreams = get_ohnics_datastreams()
    print(f"Found {len(datastreams)} Ohnics datastreams\n")

    total_duplicates = 0
    total_deleted = 0

    for ds in datastreams:
        ds_id = str(ds["@iot.id"])
        ds_name = ds["name"]
        observations = get_observations(ds_id)
        duplicates = find_duplicates(observations)

        if duplicates:
            print(f"  {ds_name} (ID {ds_id}): {len(observations)} obs, {len(duplicates)} duplicates")
            total_duplicates += len(duplicates)

            if args.delete:
                for dup_id in duplicates:
                    status = delete_observation(dup_id)
                    if status in (200, 204):
                        total_deleted += 1
                    else:
                        print(f"    WARNING: DELETE Observations({dup_id}) returned {status}")

    print(f"\nTotal duplicates found: {total_duplicates}")
    if args.delete:
        print(f"Total deleted: {total_deleted}")
    else:
        print("Run with --delete to remove them.")


if __name__ == "__main__":
    main()
