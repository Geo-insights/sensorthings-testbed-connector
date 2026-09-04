"""Diagnose FROST v2.0 observation push failures.

Tests whether the v2 demo server accepts observations for registered
datastreams, and reports the exact error when it doesn't.

Usage:
    python scripts/diagnose_v2_push.py
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import requests

V2_BASE = "https://ogc-demo.k8s.ilt-dmz.iosb.fraunhofer.de/FROST-StaV2Core/v2.0"
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "entities_frost-v2-demo.json"
TIMEOUT = 30

# Load .env for auth
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def _load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.strip()
    return env


def _headers(env: dict) -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    token = env.get("SENSORTHINGS_AUTH_TOKEN", "")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def main() -> None:
    env = _load_env(ENV_PATH)
    headers = _headers(env)

    # 1. Connectivity check
    print(f"Testing v2 server: {V2_BASE}")
    try:
        resp = requests.get(V2_BASE, headers=headers, timeout=TIMEOUT)
        print(f"  Root: {resp.status_code} ({len(resp.content)} bytes)")
        if not resp.ok:
            print(f"  ERROR: {resp.text[:500]}")
            return
    except requests.RequestException as exc:
        print(f"  UNREACHABLE: {exc}")
        return

    # 2. Check registered datastreams in cache
    if not CACHE_PATH.exists():
        print(f"\n  No v2 entity cache at {CACHE_PATH}")
        return

    cache = json.loads(CACHE_PATH.read_text())
    ds_cache = cache.get("datastreams", {})
    print(f"\n  Cached datastreams: {len(ds_cache)}")
    for key, ds_id in ds_cache.items():
        print(f"    {key} -> id {ds_id}")

    if not ds_cache:
        print("  No datastreams to test.")
        return

    # 3. Verify datastreams exist on server
    print("\n  Verifying datastreams on server...")
    for key, ds_id in ds_cache.items():
        try:
            resp = requests.get(
                f"{V2_BASE}/Datastreams({ds_id})",
                headers=headers,
                params={"$select": "id,name"},
                timeout=TIMEOUT,
            )
            if resp.ok:
                name = resp.json().get("name", "?")
                print(f"    DS {ds_id}: EXISTS ({name})")
            else:
                print(f"    DS {ds_id}: {resp.status_code} — {resp.text[:200]}")
        except requests.RequestException as exc:
            print(f"    DS {ds_id}: ERROR — {exc}")

    # 4. Test single observation push (v2 format)
    print("\n  Testing single observation push (v2 format)...")
    test_ds_id = next(iter(ds_cache.values()))
    test_ds_key = next(iter(ds_cache.keys()))
    now = datetime.now(UTC)

    # v2 observation format (adapted from v2_adapter.py)
    v2_payload = {
        "phenomenonTime": now.isoformat().replace("+00:00", "Z"),
        "result": -999.99,  # sentinel value we can identify and delete
        "Datastream": {"id": int(test_ds_id) if test_ds_id.isdigit() else test_ds_id},
        "properties": {
            "sensor_id": "diagnose-v2-test",
            "observed_property": test_ds_key.split("::")[-1] if "::" in test_ds_key else "test",
            "unit": "test",
        },
    }

    print(f"    Target datastream: {test_ds_key} (id {test_ds_id})")
    print(f"    Payload: {json.dumps(v2_payload, indent=2)}")

    try:
        resp = requests.post(
            f"{V2_BASE}/Observations",
            json=v2_payload,
            headers=headers,
            timeout=TIMEOUT,
        )
        print(f"\n    Response: {resp.status_code}")
        print(f"    Headers: {dict(resp.headers)}")
        body = resp.text[:1000]
        print(f"    Body: {body}")

        if resp.ok:
            print("\n    SUCCESS — v2 observation push works!")
            # Try to clean up the test observation
            location = resp.headers.get("Location", "")
            if location:
                print(f"    Cleaning up test observation at {location}...")
                del_resp = requests.delete(location, headers=headers, timeout=TIMEOUT)
                print(f"    Delete: {del_resp.status_code}")
        else:
            print(f"\n    FAILED — v2 rejects observations with {resp.status_code}")
    except requests.RequestException as exc:
        print(f"\n    ERROR: {exc}")

    # 5. Test CreateObservations batch (v2 format)
    print("\n  Testing batch CreateObservations (v2 format)...")
    batch_body = [
        {
            "Datastream": {"id": int(test_ds_id) if test_ds_id.isdigit() else test_ds_id},
            "components": ["phenomenonTime", "result", "properties"],
            "dataArray": [
                [
                    now.isoformat().replace("+00:00", "Z"),
                    -998.88,  # another sentinel
                    {"sensor_id": "diagnose-v2-batch-test", "unit": "test"},
                ],
            ],
        }
    ]

    try:
        resp = requests.post(
            f"{V2_BASE}/CreateObservations",
            json=batch_body,
            headers=headers,
            timeout=TIMEOUT,
        )
        print(f"    Response: {resp.status_code}")
        body = resp.text[:1000]
        print(f"    Body: {body}")

        if resp.ok:
            print("\n    SUCCESS — v2 batch push works!")
        elif resp.status_code in {400, 404, 405, 501}:
            print(f"\n    CreateObservations NOT SUPPORTED (status {resp.status_code})")
            print("    Connector should fall back to per-observation pushes.")
        else:
            print(f"\n    FAILED — v2 batch rejects with {resp.status_code}")
    except requests.RequestException as exc:
        print(f"\n    ERROR: {exc}")


if __name__ == "__main__":
    main()
