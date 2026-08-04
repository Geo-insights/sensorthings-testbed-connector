from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from app.frost.target import FrostTarget


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


def _load_levellog_installations() -> tuple[dict[str, object], ...]:
    """Parse LEVELLOG_INSTALLATIONS_JSON (per-installation name + coordinates).

    Format: [{"id": "<uuid>", "name": "Waterstraat", "lat": 51.99, "lon": 4.37}, ...]
    Falls back to LEVELLOG_INSTALLATION_IDS (comma-separated ids, no name/coords).
    """
    raw = os.getenv("LEVELLOG_INSTALLATIONS_JSON", "").strip()
    installs: list[dict[str, object]] = []
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                inst_id = str(item.get("id", "")).strip()
                if not inst_id:
                    continue
                installs.append(
                    {
                        "id": inst_id,
                        "name": str(item.get("name", "")).strip() or inst_id[:8],
                        "lat": _coerce_float(item.get("lat")),
                        "lon": _coerce_float(item.get("lon")),
                    }
                )
    if not installs:
        legacy = os.getenv("LEVELLOG_INSTALLATION_IDS", "").strip()
        for inst_id in (item.strip() for item in legacy.split(",")):
            if inst_id:
                installs.append({"id": inst_id, "name": inst_id[:8], "lat": None, "lon": None})
    return tuple(installs)


def _coerce_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


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
            for item in parsed:
                if isinstance(item, dict):
                    url = str(item.get("url", "")).strip().rstrip("/")
                    if url:
                        extras.append(url)
                elif isinstance(item, str) and item.strip():
                    extras.append(item.strip().rstrip("/"))
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


def _load_frost_targets() -> tuple[FrostTarget, ...]:
    """Parse FROST targets from env vars, supporting version + per-target auth.

    ``SENSORTHINGS_BASE_URLS`` accepts:
    - Plain strings (default to v1.1 with global auth)
    - JSON objects with ``url``, ``version``, ``label``, ``auth_username``, etc.

    ``SENSORTHINGS_BASE_URL`` is prepended as the primary target (v1.1).
    """
    global_username = os.getenv("SENSORTHINGS_AUTH_USERNAME", "")
    global_password = os.getenv("SENSORTHINGS_AUTH_PASSWORD", "")
    global_token = os.getenv("SENSORTHINGS_AUTH_TOKEN", "")

    raw_primary = os.getenv("SENSORTHINGS_BASE_URL", "").strip().rstrip("/")
    raw_multi = os.getenv("SENSORTHINGS_BASE_URLS", "").strip()

    items: list[dict | str] = []
    if raw_multi:
        try:
            parsed = json.loads(raw_multi)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, list):
            items = list(parsed)
        else:
            items = [item.strip() for item in raw_multi.split(",") if item.strip()]

    all_items: list[dict | str] = []
    if raw_primary:
        all_items.append(raw_primary)
    all_items.extend(items)

    targets: list[FrostTarget] = []
    seen: set[str] = set()
    for item in all_items:
        if isinstance(item, dict):
            url = str(item.get("url", "")).strip().rstrip("/")
            version = str(item.get("version", "v1.1")).strip()
            label = str(item.get("label", "")).strip()
            auth_user = str(item.get("auth_username", "")).strip()
            auth_pass = str(item.get("auth_password", "")).strip()
            auth_tok = str(item.get("auth_token", "")).strip()
            # Fall back to global auth only when no auth fields are present.
            # Explicitly setting auth_username to "" means "no auth" (public).
            has_explicit_auth = "auth_username" in item or "auth_token" in item
            if not has_explicit_auth:
                auth_user = global_username
                auth_pass = global_password
                auth_tok = global_token
        else:
            url = str(item).strip().rstrip("/")
            version = "v1.1"
            label = ""
            auth_user = global_username
            auth_pass = global_password
            auth_tok = global_token

        if not url:
            continue
        if url in seen:
            continue
        seen.add(url)

        targets.append(FrostTarget(
            url=url,
            version=version,
            auth_username=auth_user,
            auth_password=auth_pass,
            auth_token=auth_tok,
            label=label,
        ))

    return tuple(targets)


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
    frost_targets: tuple[FrostTarget, ...] = field(default_factory=_load_frost_targets)
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
    # Max messages pulled+pushed per consume cycle. Must be small enough that a
    # full push (batch size x FROST targets) completes well within
    # max.poll.interval.ms, or librdkafka evicts the consumer from the group
    # (MAXPOLL) and it silently stops consuming. Large backlogs + a slow target
    # are exactly what triggers this. FROST push throughput (~200 obs/min
    # against the slowest target), NOT Kafka consume, is the bottleneck, so a
    # bigger batch only makes each push longer: 100 msgs (~900 obs) pushes in
    # ~5 min; 500 msgs (~4500 obs) can exceed the 15 min max-poll window and get
    # the consumer evicted. Keep this modest and let auto-skip-on-lag handle
    # catastrophic backlogs instead of a huge batch. Also keeps each cycle's
    # observation count under the monitoring push cap (2000).
    kafka_tgv_batch_max_messages: int = int(os.getenv("KAFKA_TGV_BATCH_MAX_MESSAGES", "100"))
    # librdkafka max.poll.interval.ms (ms). If the app doesn't call consume()
    # within this window the broker evicts the consumer. Default 15 min gives a
    # slow push plenty of headroom over the 5 min librdkafka default.
    kafka_tgv_max_poll_interval_ms: int = int(os.getenv("KAFKA_TGV_MAX_POLL_INTERVAL_MS", "900000"))
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
    levellog_installations: tuple[dict[str, object], ...] = field(default_factory=_load_levellog_installations)
    levellog_poll_seconds: int = int(os.getenv("LEVELLOG_POLL_SECONDS", "900"))
    # --- Monitoring module direct push ---
    monitoring_push_url: str = os.getenv("MONITORING_PUSH_URL", "").strip().rstrip("/")
    monitoring_push_key: str = os.getenv("MONITORING_PUSH_KEY", "").strip()
    # --- Observation push reliability ---
    # Batch pushes via the SensorThings CreateObservations (dataArray) extension.
    frost_batch_push_enabled: bool = os.getenv("FROST_BATCH_PUSH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    # Also batch-push to v2.0 targets (Fraunhofer FROST-StaV2Core). Kept as a
    # separate kill-switch because the v2.0 dataArray format is less battle-
    # tested; disable to fall back to per-observation POSTs for v2.0 only. A
    # target that rejects CreateObservations is remembered and auto-falls-back
    # regardless of this flag.
    frost_v2_batch_push_enabled: bool = os.getenv("FROST_V2_BATCH_PUSH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    # Cap observations per CreateObservations request so a large backlog doesn't
    # exceed the server timeout (a 4500-obs batch read-times-out at 15s).
    frost_batch_max_observations: int = int(os.getenv("FROST_BATCH_MAX_OBSERVATIONS", "500"))
    # Batch requests carry many observations, so allow a longer read timeout.
    frost_batch_timeout_seconds: float = field(default_factory=lambda: _load_float("FROST_BATCH_TIMEOUT_SECONDS", "60"))
    # Circuit breaker: skip a target after N consecutive failed push cycles.
    frost_cb_failure_threshold: int = int(os.getenv("FROST_CB_FAILURE_THRESHOLD", "3"))
    frost_cb_cooldown_seconds: float = field(default_factory=lambda: _load_float("FROST_CB_COOLDOWN_SECONDS", "600"))
    # --- FROST push concurrency & async worker (throughput / decoupling) ---
    # Push the FROST_BATCH_MAX_OBSERVATIONS-sized chunks of one large
    # CreateObservations batch concurrently per target instead of sequentially,
    # so a multi-chunk cycle isn't gated by each chunk's serial position behind
    # the slowest target.
    frost_batch_max_concurrency: int = int(os.getenv("FROST_BATCH_MAX_CONCURRENCY", "4"))
    # Decouple the (slow, multi-target) FROST push from the Kafka consume loop.
    # When enabled, the consume loop forwards to monitoring, hands readings to a
    # background worker queue, commits, and immediately consumes the next batch —
    # so Kafka stays live and the FROST mirror catches up asynchronously. FROST
    # failures are still dead-lettered (DLQ + replay) and the monitoring forward
    # is idempotent, so committing before the push completes is at-least-once,
    # not at-most-once.
    frost_async_push_enabled: bool = os.getenv("FROST_ASYNC_PUSH_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    # Max reading-batches buffered for the worker. When full the consume loop
    # blocks up to frost_worker_enqueue_timeout_seconds (backpressure) then
    # pushes inline so nothing is dropped.
    frost_worker_queue_max: int = int(os.getenv("FROST_WORKER_QUEUE_MAX", "50"))
    # The worker coalesces queued batches up to this many readings into one push
    # to amortise the per-request/round-trip cost against slow targets.
    frost_worker_coalesce_max: int = int(os.getenv("FROST_WORKER_COALESCE_MAX", "2000"))
    frost_worker_enqueue_timeout_seconds: float = field(default_factory=lambda: _load_float("FROST_WORKER_ENQUEUE_TIMEOUT_SECONDS", "30"))
    # Alert when the worker queue depth stays at/above this fraction of its max.
    frost_worker_backlog_alert_ratio: float = field(default_factory=lambda: _load_float("FROST_WORKER_BACKLOG_ALERT_RATIO", "0.8"))
    # Scheduled replay of the failed-observations dead-letter queue.
    failed_replay_enabled: bool = os.getenv("FAILED_REPLAY_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    failed_replay_interval_seconds: int = int(os.getenv("FAILED_REPLAY_INTERVAL_SECONDS", "900"))
    failed_replay_max_lines: int = int(os.getenv("FAILED_REPLAY_MAX_LINES", "500"))
    # Alert when the DLQ isn't draining: remaining entries after a replay pass
    # stay at/above this. 0 disables the alert.
    failed_replay_backlog_alert: int = int(os.getenv("FAILED_REPLAY_BACKLOG_ALERT", "2000"))
    # Before re-posting a dead-letter observation, probe for an existing
    # (Datastream, phenomenonTime). Prevents replay from duplicating a write that
    # timed out after the server committed it, on servers that don't enforce
    # observation uniqueness (i.e. don't return 409). The hot streaming path stays
    # probe-free; this only runs on the rare replay path.
    frost_replay_dedup_probe: bool = os.getenv("FROST_REPLAY_DEDUP_PROBE", "true").lower() in {"1", "true", "yes", "on"}
    # Safety bounds for the DLQ file.
    failed_dlq_max_bytes: int = int(os.getenv("FAILED_DLQ_MAX_BYTES", str(50 * 1024 * 1024)))  # 50 MB
    failed_dlq_max_age_hours: int = int(os.getenv("FAILED_DLQ_MAX_AGE_HOURS", "48"))
    # --- Alerting (best-effort outbound webhook) ---
    # Optional. When set, stall / auth-failure / auto-reconnect events are POSTed
    # as a signed JSON envelope (Slack-compatible ``text`` field included). No-op
    # when empty. Mirrors the monitoring module's webhook delivery pattern.
    alert_webhook_url: str = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    alert_webhook_secret: str = os.getenv("ALERT_WEBHOOK_SECRET", "").strip()
    # Per-event-key cooldown so a persistent condition doesn't spam the webhook.
    alert_min_interval_seconds: int = int(os.getenv("ALERT_MIN_INTERVAL_SECONDS", "1800"))
    # --- Freshness / stall detection ---
    # A source is considered "stale" when no successful push has happened within
    # (expected interval + grace). Used by the Kafka watchdog and /connector/freshness.
    freshness_grace_seconds: int = int(os.getenv("FRESHNESS_GRACE_SECONDS", "600"))
    # Explicit override for the Kafka source stall threshold (seconds). When 0,
    # derived from kafka_tgv_poll_seconds + freshness_grace_seconds.
    kafka_stale_seconds: int = int(os.getenv("KAFKA_STALE_SECONDS", "0"))
    # Backlog-staleness guard. When the newest observation pushed in a cycle is
    # older than this many seconds, the consumer is replaying a stale backlog
    # (downstream sees an old "latest" timestamp). 0 disables the check.
    kafka_max_lag_seconds: int = int(os.getenv("KAFKA_MAX_LAG_SECONDS", "3600"))
    # When True, exceeding kafka_max_lag_seconds auto-triggers skip-to-latest
    # (drops the backlog to go live). When False (default) it only alerts — a
    # human decides, since skipping discards the un-pushed backlog.
    kafka_auto_skip_on_lag: bool = os.getenv("KAFKA_AUTO_SKIP_ON_LAG", "false").lower() in {"1", "true", "yes", "on"}


settings = Settings()
