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


def _load_site_project_configs() -> dict[str, dict[str, object]]:
    raw = os.getenv("SENSORTHINGS_SITE_PROJECTS_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}

    parsed: dict[str, dict[str, object]] = {}
    for site_key, value in data.items():
        if not isinstance(value, dict):
            continue
        normalized_site_key = str(site_key).strip().lower()
        parsed[normalized_site_key] = {
            "name": str(value.get("name", "")).strip(),
            "id": str(value.get("id", "")).strip(),
            "description": str(value.get("description", "")).strip(),
            "public": bool(value.get("public", True)),
        }
    return parsed


def _load_site_tasking_configs() -> dict[str, dict[str, object]]:
    raw = os.getenv("SENSORTHINGS_SITE_TASKING_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}

    parsed: dict[str, dict[str, object]] = {}
    for site_key, value in data.items():
        if not isinstance(value, dict):
            continue
        normalized_site_key = str(site_key).strip().lower()
        parsed[normalized_site_key] = {
            "actuators": value.get("actuators", []) if isinstance(value.get("actuators", []), list) else [],
            "capabilities": value.get("capabilities", []) if isinstance(value.get("capabilities", []), list) else [],
        }
    return parsed


def _load_tasking_allowed_commands() -> set[str]:
    raw = os.getenv("SENSORTHINGS_TASKING_ALLOWED_COMMANDS", "").strip()
    if not raw:
        return set()

    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = None

    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}

    return {item.strip() for item in raw.split(",") if item.strip()}


def _load_tgv_device_mapping() -> dict[str, dict[str, str]]:
    raw = os.getenv("KAFKA_TGV_DEVICE_MAPPING_JSON", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {str(k): dict(v) for k, v in data.items() if isinstance(v, dict)}


def _load_float(name: str, default: str) -> float:
    raw = os.getenv(name, default).strip()
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _load_base_urls() -> tuple[str, ...]:
    raw_primary = os.getenv("SENSORTHINGS_BASE_URL", "").strip().rstrip("/")
    raw_multi = os.getenv("SENSORTHINGS_BASE_URLS", "").strip()

    extras: list[str] = []
    if raw_multi:
        try:
            parsed = json.loads(raw_multi)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, list):
            extras = [str(item).strip().rstrip("/") for item in parsed if str(item).strip()]
        else:
            extras = [item.strip().rstrip("/") for item in raw_multi.split(",") if item.strip()]

    ordered = [raw_primary] if raw_primary else []
    ordered.extend(extras)

    deduped: list[str] = []
    seen: set[str] = set()
    for url in ordered:
        if not url:
            continue
        normalized = url.rstrip("/")
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)

    return tuple(deduped)


@dataclass(frozen=True)
class Settings:
    connector_name: str = os.getenv("CONNECTOR_NAME", "UrbanAdapt SensorThings Connector")
    bridge_payload_path: str = os.getenv("CONNECTOR_BRIDGE_PAYLOAD_PATH", "").strip()
    monitoring_mqtt_enabled: bool = os.getenv("MONITORING_MQTT_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    monitoring_mqtt_host: str = os.getenv("MONITORING_MQTT_HOST", "").strip()
    monitoring_mqtt_port: int = int(os.getenv("MONITORING_MQTT_PORT", "1883"))
    monitoring_mqtt_topic: str = os.getenv("MONITORING_MQTT_TOPIC", "monitoring/readings/bridge")
    monitoring_mqtt_username: str = os.getenv("MONITORING_MQTT_USERNAME", "")
    monitoring_mqtt_password: str = os.getenv("MONITORING_MQTT_PASSWORD", "")
    monitoring_mqtt_tls: bool = os.getenv("MONITORING_MQTT_TLS", "false").lower() in {"1", "true", "yes", "on"}
    sensorthings_base_url: str = os.getenv("SENSORTHINGS_BASE_URL", "").rstrip("/")
    sensorthings_base_urls: tuple[str, ...] = field(default_factory=_load_base_urls)
    things_path: str = os.getenv("SENSORTHINGS_THINGS_PATH", "/Things")
    locations_path: str = os.getenv("SENSORTHINGS_LOCATIONS_PATH", "/Locations")
    sensors_path: str = os.getenv("SENSORTHINGS_SENSORS_PATH", "/Sensors")
    observed_properties_path: str = os.getenv("SENSORTHINGS_OBSERVED_PROPERTIES_PATH", "/ObservedProperties")
    datastreams_path: str = os.getenv("SENSORTHINGS_DATASTREAMS_PATH", "/Datastreams")
    observations_path: str = os.getenv("SENSORTHINGS_OBSERVATIONS_PATH", "/Observations")
    projects_path: str = os.getenv("SENSORTHINGS_PROJECTS_PATH", "/Projects")
    actuators_path: str = os.getenv("SENSORTHINGS_ACTUATORS_PATH", "/Actuators")
    tasking_capabilities_path: str = os.getenv("SENSORTHINGS_TASKING_CAPABILITIES_PATH", "/TaskingCapabilities")
    tasks_path: str = os.getenv("SENSORTHINGS_TASKS_PATH", "/Tasks")
    default_project_name: str = os.getenv("SENSORTHINGS_DEFAULT_PROJECT_NAME", "").strip()
    default_project_id: str = os.getenv("SENSORTHINGS_DEFAULT_PROJECT_ID", "").strip()
    default_project_description: str = os.getenv("SENSORTHINGS_DEFAULT_PROJECT_DESCRIPTION", "").strip()
    default_project_public: bool = os.getenv("SENSORTHINGS_DEFAULT_PROJECT_PUBLIC", "true").lower() in {"1", "true", "yes", "on"}
    site_project_configs: dict[str, dict[str, object]] = field(default_factory=_load_site_project_configs)
    site_tasking_configs: dict[str, dict[str, object]] = field(default_factory=_load_site_tasking_configs)
    tasking_allowed_commands: set[str] = field(default_factory=_load_tasking_allowed_commands)
    auth_token: str = os.getenv("SENSORTHINGS_AUTH_TOKEN", "")
    auth_username: str = os.getenv("SENSORTHINGS_AUTH_USERNAME", "")
    auth_password: str = os.getenv("SENSORTHINGS_AUTH_PASSWORD", "")
    entity_name_prefix: str = os.getenv("SENSORTHINGS_ENTITY_NAME_PREFIX", "")
    registered_entities_path: str = os.getenv("SENSORTHINGS_REGISTERED_ENTITIES_PATH", "data/registered_entities.json")
    failed_observations_path: str = os.getenv("SENSORTHINGS_FAILED_OBSERVATIONS_PATH", "data/failed_observations.jsonl")
    request_timeout_seconds: float = field(default_factory=lambda: _load_float("SENSORTHINGS_REQUEST_TIMEOUT_SECONDS", "15"))
    datastream_ids: dict[str, str] = field(default_factory=_load_datastream_map)
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    frost_public_base_url: str = os.getenv("FROST_PUBLIC_BASE_URL", "").strip()
    frost_internal_base_url: str = os.getenv("FROST_INTERNAL_BASE_URL", "").strip()
    sensor_config_dir: str = os.getenv("SENSOR_CONFIG_DIR", "config/sites")
    debug: bool = os.getenv("DEBUG", "false").lower() in {"1", "true", "yes", "on"}
    # --- Kafka / TGV source ---
    kafka_tgv_enabled: bool = os.getenv("KAFKA_TGV_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    kafka_tgv_bootstrap_servers: str = os.getenv("KAFKA_TGV_BOOTSTRAP_SERVERS", "").strip()
    kafka_tgv_api_key: str = os.getenv("KAFKA_TGV_API_KEY", "").strip()
    kafka_tgv_api_password: str = os.getenv("KAFKA_TGV_API_PASSWORD", "").strip()
    kafka_tgv_schema_registry_url: str = os.getenv("KAFKA_TGV_SCHEMA_REGISTRY_URL", "").strip()
    kafka_tgv_schema_registry_username: str = os.getenv("KAFKA_TGV_SCHEMA_REGISTRY_USERNAME", "").strip()
    kafka_tgv_schema_registry_password: str = os.getenv("KAFKA_TGV_SCHEMA_REGISTRY_PASSWORD", "").strip()
    kafka_tgv_consumer_group: str = os.getenv("KAFKA_TGV_CONSUMER_GROUP", "gv_sa-j3r03zm_mathis_van_der_voordt").strip()
    kafka_tgv_client_id: str = os.getenv("KAFKA_TGV_CLIENT_ID_CONSUMER", "geo-insights-consumer").strip()
    kafka_tgv_topic: str = os.getenv("KAFKA_TGV_TOPIC", "tud_gv_officelab-climate").strip()
    kafka_tgv_poll_seconds: int = int(os.getenv("KAFKA_TGV_POLL_SECONDS", "300"))
    kafka_tgv_device_mapping: dict[str, dict[str, str]] = field(default_factory=_load_tgv_device_mapping)
    # --- Ohnics air quality source ---
    ohnics_enabled: bool = os.getenv("OHNICS_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    ohnics_api_url: str = os.getenv("OHNICS_API_URL", "https://ohnics.online/5min.json").strip()
    ohnics_poll_seconds: int = int(os.getenv("OHNICS_POLL_SECONDS", "300"))
    ohnics_sensor_prefix: str = os.getenv("OHNICS_SENSOR_PREFIX", "de-").strip()
    # --- Levellog / CARS Online groundwater source ---
    levellog_enabled: bool = os.getenv("LEVELLOG_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    levellog_api_url: str = os.getenv("LEVELLOG_API_URL", "https://cars-api.carsonline.eu").strip().rstrip("/")
    levellog_token_url: str = os.getenv("LEVELLOG_TOKEN_URL", "https://cars-api.carsonline.eu/token").strip()
    levellog_client_id: str = os.getenv("LEVELLOG_CLIENT_ID", "").strip()
    levellog_client_secret: str = os.getenv("LEVELLOG_CLIENT_SECRET", "").strip()
    levellog_installation_ids: str = os.getenv("LEVELLOG_INSTALLATION_IDS", "").strip()
    levellog_poll_seconds: int = int(os.getenv("LEVELLOG_POLL_SECONDS", "900"))


settings = Settings()
