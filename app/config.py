from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def _load_datastream_map() -> dict[str, str]:
    raw = os.getenv("SENSORTHINGS_DATASTREAM_IDS_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {str(key): str(value) for key, value in data.items()}


@dataclass(frozen=True)
class Settings:
    connector_name: str = os.getenv("CONNECTOR_NAME", "UrbanAdapt SensorThings Connector")
    sensorthings_base_url: str = os.getenv("SENSORTHINGS_BASE_URL", "").rstrip("/")
    things_path: str = os.getenv("SENSORTHINGS_THINGS_PATH", "/Things")
    sensors_path: str = os.getenv("SENSORTHINGS_SENSORS_PATH", "/Sensors")
    observed_properties_path: str = os.getenv("SENSORTHINGS_OBSERVED_PROPERTIES_PATH", "/ObservedProperties")
    datastreams_path: str = os.getenv("SENSORTHINGS_DATASTREAMS_PATH", "/Datastreams")
    observations_path: str = os.getenv("SENSORTHINGS_OBSERVATIONS_PATH", "/Observations")
    auth_token: str = os.getenv("SENSORTHINGS_AUTH_TOKEN", "")
    datastream_ids: dict[str, str] = field(default_factory=_load_datastream_map)
    debug: bool = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "on"}


settings = Settings()
