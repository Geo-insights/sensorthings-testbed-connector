"""Diagnose observation delivery across all FROST target servers.

Queries each FROST server for datastream counts, observation counts,
latest timestamps, and observation intervals to assess connector performance.

Usage:
    python scripts/diagnose_observations.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

# Load .env manually (avoid dependency on python-dotenv for standalone script)
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


def _auth_header(username: str, password: str) -> dict[str, str]:
    if not username:
        return {"Content-Type": "application/json"}
    cred = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Content-Type": "application/json", "Authorization": f"Basic {cred}"}


def _get_id(entity: dict) -> str:
    return str(entity.get("@iot.id") or entity.get("id", "?"))


def _get_count(body: dict) -> int | None:
    return body.get("@iot.count") or body.get("@count")


# ---------------------------------------------------------------------------
# Server definitions
# ---------------------------------------------------------------------------

def _build_targets(env: dict) -> list[dict]:
    """Build list of target servers from env vars."""
    targets: list[dict] = []

    # Primary server
    primary = env.get("SENSORTHINGS_BASE_URL", "")
    if primary:
        targets.append({
            "url": primary.rstrip("/"),
            "version": "v1.1",
            "label": "primary",
            "username": env.get("SENSORTHINGS_AUTH_USERNAME", ""),
            "password": env.get("SENSORTHINGS_AUTH_PASSWORD", ""),
        })

    # Additional servers from JSON
    urls_raw = env.get("SENSORTHINGS_BASE_URLS", "")
    if urls_raw:
        try:
            items = json.loads(urls_raw)
            for item in items:
                if isinstance(item, str):
                    targets.append({
                        "url": item.rstrip("/"),
                        "version": "v1.1",
                        "label": item.split("//")[1].split("/")[0] if "//" in item else item,
                        "username": env.get("SENSORTHINGS_AUTH_USERNAME", ""),
                        "password": env.get("SENSORTHINGS_AUTH_PASSWORD", ""),
                    })
                elif isinstance(item, dict):
                    targets.append({
                        "url": item["url"].rstrip("/"),
                        "version": item.get("version", "v1.1"),
                        "label": item.get("label", item["url"].split("//")[1].split("/")[0]),
                        "username": item.get("auth_username") or env.get("SENSORTHINGS_AUTH_USERNAME", ""),
                        "password": item.get("auth_password") or env.get("SENSORTHINGS_AUTH_PASSWORD", ""),
                    })
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"  WARNING: Failed to parse SENSORTHINGS_BASE_URLS: {exc}")

    return targets


# ---------------------------------------------------------------------------
# FROST queries
# ---------------------------------------------------------------------------

def _query_server(target: dict, prefix: str) -> dict:
    """Query a single FROST server for datastream/observation diagnostics."""
    url = target["url"]
    headers = _auth_header(target["username"], target["password"])
    result: dict = {
        "url": url,
        "label": target["label"],
        "version": target["version"],
        "reachable": False,
        "response_time_ms": None,
        "thing_count": None,
        "datastream_count": None,
        "total_observations": 0,
        "datastreams": [],
        "errors": [],
    }

    # 1. Connectivity check
    t0 = time.monotonic()
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        result["response_time_ms"] = round((time.monotonic() - t0) * 1000)
        result["reachable"] = resp.ok
        if not resp.ok:
            result["errors"].append(f"Root returned {resp.status_code}")
            return result
    except requests.RequestException as exc:
        result["errors"].append(f"Connection failed: {exc}")
        return result

    # 2. Thing count
    try:
        resp = requests.get(f"{url}/Things", params={"$count": "true", "$top": "0"}, headers=headers, timeout=15)
        if resp.ok:
            result["thing_count"] = _get_count(resp.json())
    except Exception as exc:
        result["errors"].append(f"Thing count failed: {exc}")

    # 3. All datastreams with latest observations
    try:
        params = {
            "$count": "true",
            "$top": "200",
            "$orderby": "name asc",
            "$expand": "Thing($select=name),Observations($orderby=phenomenonTime desc;$top=10;$count=true)",
        }
        resp = requests.get(f"{url}/Datastreams", params=params, headers=headers, timeout=30)
        if not resp.ok:
            result["errors"].append(f"Datastreams query returned {resp.status_code}")
            return result
        body = resp.json()
    except Exception as exc:
        result["errors"].append(f"Datastreams query failed: {exc}")
        return result

    ds_list = body.get("value", [])
    result["datastream_count"] = _get_count(body) or len(ds_list)

    now = datetime.now(UTC)

    for ds in ds_list:
        ds_id = _get_id(ds)
        ds_name = ds.get("name", "?")
        thing_name = "?"
        thing_data = ds.get("Thing")
        if isinstance(thing_data, dict):
            thing_name = thing_data.get("name", "?")

        observations = ds.get("Observations", [])
        # Could be dict with value key or list
        if isinstance(observations, dict):
            obs_count = _get_count(observations)
            obs_list = observations.get("value", [])
        else:
            obs_count = len(observations)
            obs_list = observations

        # Latest observation
        latest_time = None
        latest_value = None
        if obs_list:
            latest = obs_list[0]
            raw_ts = latest.get("phenomenonTime", "")
            latest_value = latest.get("result")
            try:
                latest_time = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Calculate average interval from last 10 observations
        avg_interval_seconds = None
        if len(obs_list) >= 2:
            timestamps = []
            for obs in obs_list:
                raw = obs.get("phenomenonTime", "")
                try:
                    timestamps.append(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
                except (ValueError, TypeError):
                    pass
            if len(timestamps) >= 2:
                intervals = []
                for i in range(len(timestamps) - 1):
                    diff = (timestamps[i] - timestamps[i + 1]).total_seconds()
                    if diff > 0:
                        intervals.append(diff)
                if intervals:
                    avg_interval_seconds = round(sum(intervals) / len(intervals))

        # Staleness
        age_seconds = None
        if latest_time:
            age_seconds = round((now - latest_time).total_seconds())

        if obs_count is not None:
            result["total_observations"] += obs_count

        result["datastreams"].append({
            "id": ds_id,
            "name": ds_name,
            "thing": thing_name,
            "obs_count": obs_count,
            "latest_time": latest_time.isoformat() if latest_time else None,
            "latest_value": latest_value,
            "age_seconds": age_seconds,
            "avg_interval_seconds": avg_interval_seconds,
        })

    return result


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_age(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h ago"


def _format_interval(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _format_count(count: int | None) -> str:
    if count is None:
        return "?"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.1f}K"
    return str(count)


def _print_server_report(server: dict) -> None:
    label = server["label"]
    url = server["url"]
    version = server["version"]

    print(f"\n{'=' * 80}")
    print(f"  {label}  ({version})")
    print(f"  {url}")
    print(f"{'=' * 80}")

    if not server["reachable"]:
        print(f"  UNREACHABLE  {', '.join(server['errors'])}")
        return

    print(f"  Response: {server['response_time_ms']}ms | "
          f"Things: {_format_count(server['thing_count'])} | "
          f"Datastreams: {_format_count(server['datastream_count'])} | "
          f"Observations: {_format_count(server['total_observations'])}")

    if server["errors"]:
        for err in server["errors"]:
            print(f"  WARNING: {err}")

    if not server["datastreams"]:
        print("  No datastreams found.")
        return

    # Table header
    print()
    hdr = f"  {'ID':>5}  {'Thing':<25} {'Datastream':<40} {'Count':>8}  {'Last Value':>12}  {'Age':>12}  {'Avg Interval':>14}"
    print(hdr)
    print(f"  {'-' * (len(hdr) - 2)}")

    # Group by thing
    by_thing: dict[str, list[dict]] = {}
    for ds in server["datastreams"]:
        by_thing.setdefault(ds["thing"], []).append(ds)

    for thing_name in sorted(by_thing.keys()):
        for ds in sorted(by_thing[thing_name], key=lambda d: d["name"]):
            # Staleness indicator
            age_str = _format_age(ds["age_seconds"])
            stale = ""
            if ds["age_seconds"] is not None and ds["age_seconds"] > 3600:
                stale = " !"
            if ds["age_seconds"] is not None and ds["age_seconds"] > 86400:
                stale = " !!"

            val_str = ""
            if ds["latest_value"] is not None:
                val_str = str(ds["latest_value"])
                if len(val_str) > 12:
                    val_str = val_str[:11] + "~"

            name_short = ds["name"]
            if len(name_short) > 40:
                name_short = name_short[:37] + "..."

            thing_short = thing_name
            if len(thing_short) > 25:
                thing_short = thing_short[:22] + "..."

            print(
                f"  {ds['id']:>5}  {thing_short:<25} {name_short:<40} "
                f"{_format_count(ds['obs_count']):>8}  {val_str:>12}  {age_str:>12}  "
                f"{_format_interval(ds['avg_interval_seconds']):>14}{stale}"
            )

    # Summary: stale datastreams
    stale_ds = [ds for ds in server["datastreams"] if ds["age_seconds"] is not None and ds["age_seconds"] > 3600]
    if stale_ds:
        print(f"\n  STALE (>1h): {len(stale_ds)} datastreams")
        for ds in sorted(stale_ds, key=lambda d: d["age_seconds"] or 0, reverse=True):
            print(f"    - {ds['name']} ({_format_age(ds['age_seconds'])})")


def _print_cross_server_comparison(servers: list[dict]) -> None:
    reachable = [s for s in servers if s["reachable"]]
    if len(reachable) < 2:
        return

    print(f"\n{'=' * 80}")
    print("  CROSS-SERVER COMPARISON")
    print(f"{'=' * 80}")

    # Build a map of datastream name -> count per server
    all_ds_names: set[str] = set()
    for s in reachable:
        for ds in s["datastreams"]:
            all_ds_names.add(ds["name"])

    # Find discrepancies
    discrepancies = []
    for name in sorted(all_ds_names):
        counts = {}
        for s in reachable:
            matching = [ds for ds in s["datastreams"] if ds["name"] == name]
            if matching:
                counts[s["label"]] = matching[0]["obs_count"]
            else:
                counts[s["label"]] = None  # Missing entirely

        values = [c for c in counts.values() if c is not None]
        if len(values) >= 2 and (max(values) - min(values)) > max(values) * 0.05:
            discrepancies.append((name, counts))
        elif len(values) < len(reachable):
            discrepancies.append((name, counts))

    if not discrepancies:
        print("  All servers have consistent observation counts (within 5%).")
    else:
        print(f"  Found {len(discrepancies)} datastreams with count differences:\n")
        for name, counts in discrepancies[:20]:
            parts = [f"{label}: {_format_count(c)}" for label, c in counts.items()]
            print(f"    {name}")
            print(f"      {' | '.join(parts)}")

    # Overall comparison table
    print(f"\n  {'Server':<30} {'Things':>8} {'Datastreams':>12} {'Observations':>14} {'Latency':>10}")
    print(f"  {'-' * 76}")
    for s in reachable:
        print(
            f"  {s['label']:<30} {_format_count(s['thing_count']):>8} "
            f"{_format_count(s['datastream_count']):>12} "
            f"{_format_count(s['total_observations']):>14} "
            f"{s['response_time_ms']}ms" if s['response_time_ms'] else ""
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    env = _load_env(ENV_PATH)
    prefix = env.get("SENSORTHINGS_ENTITY_NAME_PREFIX", "")
    targets = _build_targets(env)

    if not targets:
        print("No FROST targets found in .env")
        sys.exit(1)

    print(f"Diagnosing {len(targets)} FROST target(s)...")
    print(f"Entity prefix: '{prefix}'")
    print(f"Timestamp: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")

    servers = []
    for target in targets:
        print(f"\n  Querying {target['label']} ({target['version']})...", end="", flush=True)
        result = _query_server(target, prefix)
        servers.append(result)
        if result["reachable"]:
            print(f" OK ({result['response_time_ms']}ms)")
        else:
            print(f" FAILED")

    for server in servers:
        _print_server_report(server)

    _print_cross_server_comparison(servers)

    # Summary
    print(f"\n{'=' * 80}")
    print("  SUMMARY")
    print(f"{'=' * 80}")
    total_obs = sum(s["total_observations"] for s in servers if s["reachable"])
    total_ds = sum(s.get("datastream_count") or 0 for s in servers if s["reachable"])
    reachable = sum(1 for s in servers if s["reachable"])
    print(f"  Servers reachable: {reachable}/{len(servers)}")
    print(f"  Total datastreams across all servers: {total_ds}")
    print(f"  Total observations across all servers: {_format_count(total_obs)}")

    # Check for any completely stale sources
    for server in servers:
        if not server["reachable"]:
            continue
        all_stale = all(
            ds["age_seconds"] is not None and ds["age_seconds"] > 7200
            for ds in server["datastreams"]
        ) if server["datastreams"] else False
        if all_stale and server["datastreams"]:
            print(f"  WARNING: ALL datastreams on {server['label']} are >2h stale - connector may be down!")

    print()


if __name__ == "__main__":
    main()
