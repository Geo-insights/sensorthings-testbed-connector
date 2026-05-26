from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _load_datastream_map() -> dict[str, str]:
    raw = os.getenv("SENSORTHINGS_DATASTREAM_IDS_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {str(key): str(value) for key, value in data.items()}


def _load_float(name: str, default: str) -> float:
    raw = os.getenv(name, default).strip()
    try:
        return float(raw)
    except ValueError:
        return float(default)


@dataclass(frozen=True)
class Settings:
    connector_name: str = os.getenv("CONNECTOR_NAME", "UrbanAdapt SensorThings Connector")
    bridge_payload_path: str = os.getenv("CONNECTOR_BRIDGE_PAYLOAD_PATH", "").strip()
    sensorthings_base_url: str = os.getenv("SENSORTHINGS_BASE_URL", "").rstrip("/")
    things_path: str = os.getenv("SENSORTHINGS_THINGS_PATH", "/Things")
    locations_path: str = os.getenv("SENSORTHINGS_LOCATIONS_PATH", "/Locations")
    sensors_path: str = os.getenv("SENSORTHINGS_SENSORS_PATH", "/Sensors")
    observed_properties_path: str = os.getenv("SENSORTHINGS_OBSERVED_PROPERTIES_PATH", "/ObservedProperties")
    datastreams_path: str = os.getenv("SENSORTHINGS_DATASTREAMS_PATH", "/Datastreams")
    observations_path: str = os.getenv("SENSORTHINGS_OBSERVATIONS_PATH", "/Observations")
    auth_token: str = os.getenv("SENSORTHINGS_AUTH_TOKEN", "")
    auth_username: str = os.getenv("SENSORTHINGS_AUTH_USERNAME", "")
    auth_password: str = os.getenv("SENSORTHINGS_AUTH_PASSWORD", "")
    entity_name_prefix: str = os.getenv("SENSORTHINGS_ENTITY_NAME_PREFIX", "")
    registered_entities_path: str = os.getenv("SENSORTHINGS_REGISTERED_ENTITIES_PATH", "data/registered_entities.json")
    failed_observations_path: str = os.getenv("SENSORTHINGS_FAILED_OBSERVATIONS_PATH", "data/failed_observations.jsonl")
    request_timeout_seconds: float = field(default_factory=lambda: _load_float("SENSORTHINGS_REQUEST_TIMEOUT_SECONDS", "15"))
    datastream_ids: dict[str, str] = field(default_factory=_load_datastream_map)
    debug: bool = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "on"}


settings = Settings()
