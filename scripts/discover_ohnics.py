"""Discover the Ohnics 5min.json API response structure.

Fetches the endpoint, prints the JSON shape, and lists all Delft ("de-*") sensors
with their fields, coordinates, and measurement values.
"""

import json
import ssl
import urllib.request

URL = "https://ohnics.online/5min.json"
PREFIX = "de-"


def fetch(url: str) -> list | dict:
    # Try HTTPS first; fall back to unverified if cert fails
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.loads(resp.read())
    except (ssl.SSLCertVerificationError, urllib.error.URLError):
        print("[!] TLS verification failed, retrying with verify=False ...")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, timeout=15, context=ctx) as resp:
            return json.loads(resp.read())


def main() -> None:
    data = fetch(URL)

    # Show top-level structure
    if isinstance(data, list):
        print(f"Response is a JSON array with {len(data)} items.\n")
        if data:
            first = data[0]
            print("=== Fields in first item ===")
            for key, val in first.items():
                print(f"  {key}: {type(val).__name__} = {val!r}")
            print()
    elif isinstance(data, dict):
        print(f"Response is a JSON object with keys: {list(data.keys())}\n")
        # If it wraps an array, try common keys
        for key in ("data", "results", "sensors", "features", "items"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                print(f"Unwrapped '{key}' array with {len(data)} items.\n")
                if data:
                    first = data[0]
                    print("=== Fields in first item ===")
                    for k, v in first.items():
                        print(f"  {k}: {type(v).__name__} = {v!r}")
                    print()
                break
    else:
        print(f"Unexpected response type: {type(data)}")
        return

    # Filter for Delft sensors
    if isinstance(data, list):
        delft = [s for s in data if isinstance(s, dict) and str(s.get("Name", s.get("name", ""))).startswith(PREFIX)]
        print(f"=== Delft sensors (prefix '{PREFIX}'): {len(delft)} ===\n")
        for sensor in delft:
            name = sensor.get("Name", sensor.get("name", "???"))
            print(f"--- {name} ---")
            for key, val in sensor.items():
                print(f"  {key}: {val!r}")
            print()

        # Summarize unique field names across all Delft sensors
        if delft:
            all_keys = set()
            for s in delft:
                all_keys.update(s.keys())
            print(f"All unique fields across Delft sensors: {sorted(all_keys)}")

            # Identify numeric fields (potential measurements)
            numeric_keys = set()
            for s in delft:
                for k, v in s.items():
                    if isinstance(v, (int, float)) and k.lower() not in ("lat", "lon", "latitude", "longitude", "id"):
                        numeric_keys.add(k)
            if numeric_keys:
                print(f"Numeric fields (potential measurements): {sorted(numeric_keys)}")


if __name__ == "__main__":
    main()
