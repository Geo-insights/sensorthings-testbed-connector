"""Discover the CARS Online (Levellog) API structure.

Steps:
1. Try to find the OAuth2 token endpoint
2. Authenticate with client credentials
3. Explore the OData endpoints to find measurement/time-series entities
"""

import json
import urllib.parse
import urllib.request

BASE_URL = "https://cars-api.carsonline.eu"
CLIENT_ID = "14563867-74aa-4772-88d2-a288027d6c7d"
CLIENT_SECRET = "FFfK6/v/owXh6OcFtTrmQ+BWyxMhTLx1jk1vfpY4Jt8hz/SyaFFPfHoZlsO1Jl4p"

# Common OAuth2 token endpoint paths to try
TOKEN_PATHS = [
    "/connect/token",
    "/oauth/token",
    "/oauth2/token",
    "/auth/token",
    "/token",
    "/api/token",
    "/identity/connect/token",
]


def try_get_token(token_url: str) -> dict | None:
    """Attempt to get an OAuth2 token from the given URL."""
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(
        token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print(f"[OK] Token obtained from {token_url}")
            print(f"  token_type: {result.get('token_type')}")
            print(f"  expires_in: {result.get('expires_in')}")
            print(f"  access_token: {result.get('access_token', '')[:40]}...")
            return result
    except urllib.error.HTTPError as e:
        print(f"  [{e.code}] {token_url}")
        return None
    except Exception as e:
        print(f"  [ERR] {token_url}: {e}")
        return None


def odata_get(path: str, token: str, top: int = 5) -> dict | list | None:
    """Fetch an OData endpoint with the bearer token."""
    sep = "&" if "?" in path else "?"
    url = f"{BASE_URL}{path}{sep}$top={top}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:200]
        except Exception:
            pass
        print(f"  [{e.code}] {url}  {body}")
        return None
    except Exception as e:
        print(f"  [ERR] {url}: {e}")
        return None


def main() -> None:
    # Step 1: Find the token endpoint
    print("=== Step 1: Discovering OAuth2 token endpoint ===\n")
    token_data = None
    for path in TOKEN_PATHS:
        token_data = try_get_token(f"{BASE_URL}{path}")
        if token_data:
            print(f"\nToken endpoint found: {BASE_URL}{path}\n")
            break

    if not token_data:
        # Also try the well-known openid-configuration
        print("\nTrying .well-known/openid-configuration ...")
        try:
            req = urllib.request.Request(f"{BASE_URL}/.well-known/openid-configuration")
            with urllib.request.urlopen(req, timeout=15) as resp:
                config = json.loads(resp.read())
                print(f"  Found OpenID config: {json.dumps(config, indent=2)[:500]}")
                if "token_endpoint" in config:
                    token_data = try_get_token(config["token_endpoint"])
        except Exception as e:
            print(f"  [ERR] {e}")

    if not token_data:
        print("\n[FAIL] Could not find token endpoint. Manual investigation needed.")
        return

    token = token_data["access_token"]

    # Step 2: Explore OData endpoints
    print("\n=== Step 2: Exploring OData endpoints ===\n")

    # Try to get the OData service document
    print("--- Service document ($metadata or root) ---")
    for path in ["/odata", "/odata/$metadata"]:
        result = odata_get(path, token, top=1)
        if result:
            print(f"  {path}: {json.dumps(result, indent=2)[:500]}")
            print()

    # Try common entity names for measurement data
    entity_candidates = [
        "/odata/measurements",
        "/odata/readings",
        "/odata/timeSeries",
        "/odata/observations",
        "/odata/dataPoints",
        "/odata/values",
        "/odata/devices",
        "/odata/sensors",
        "/odata/channels",
        "/odata/locations",
        "/odata/projects",
        "/odata/sites",
        "/odata/loggers",
        "/odata/levelLoggers",
        "/odata/groundwaterLevels",
        "/odata/monitoringPoints",
    ]

    print("--- Probing entity sets ---")
    found_entities = []
    for path in entity_candidates:
        result = odata_get(path, token, top=2)
        if result is not None:
            found_entities.append(path)
            items = result.get("value", result) if isinstance(result, dict) else result
            if isinstance(items, list) and items:
                print(f"\n  [FOUND] {path} — {len(items)} item(s) returned")
                print(f"  Fields: {list(items[0].keys()) if items else 'empty'}")
                print(f"  Sample: {json.dumps(items[0], indent=2, default=str)[:400]}")
            elif isinstance(result, dict):
                print(f"\n  [FOUND] {path} — dict response")
                print(f"  Keys: {list(result.keys())[:20]}")

    # Also try getting the full swagger spec to find more endpoints
    print("\n--- Checking Swagger spec for all paths ---")
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/swagger/v1/swagger.json",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            spec = json.loads(resp.read())
            paths = list(spec.get("paths", {}).keys())
            print(f"  Total endpoints in spec: {len(paths)}")

            # Filter for interesting endpoints
            keywords = ["measure", "read", "data", "time", "series", "value", "level",
                        "sensor", "device", "channel", "logger", "observation", "point"]
            interesting = [p for p in paths if any(kw in p.lower() for kw in keywords)]
            print(f"  Measurement-related endpoints ({len(interesting)}):")
            for p in sorted(interesting):
                methods = list(spec["paths"][p].keys())
                summary = ""
                for m in methods:
                    s = spec["paths"][p][m].get("summary", spec["paths"][p][m].get("operationId", ""))
                    if s:
                        summary = s
                        break
                print(f"    {', '.join(m.upper() for m in methods):12s} {p}  — {summary}")

            # Also show all endpoints for completeness
            print(f"\n  All endpoints ({len(paths)}):")
            for p in sorted(paths):
                methods = list(spec["paths"][p].keys())
                print(f"    {', '.join(m.upper() for m in methods):12s} {p}")
    except Exception as e:
        print(f"  [ERR] Could not fetch swagger: {e}")

    if found_entities:
        print(f"\n=== Summary: Found {len(found_entities)} accessible entity sets ===")
        for e in found_entities:
            print(f"  {e}")


if __name__ == "__main__":
    main()
