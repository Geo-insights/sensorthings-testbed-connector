from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Iterator, Mapping, MutableMapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, NamedTuple

import requests

from app.config import settings
from app.exceptions import (
    ObservationUploadError,
)
from app.frost.cache import EntityCache
from app.frost.circuit_breaker import CircuitBreaker
from app.frost.entity_manager import EntityManager
from app.frost.http_client import FrostHTTPClient, is_retryable_status
from app.frost.odata_fields import FrostODataFields, odata_fields_for
from app.frost.target_stack import TargetStack
from app.models import (
    ConnectorPreview,
    ObservationPayload,
    RegistrationPreview,
    SensorReading,
    TaskingTaskCreateRequest,
    TaskingTaskQuery,
)
from app.services.health_monitor import health_monitor
from app.services.validation import classify_error
from app.services.validation import recorder as validation_recorder
from app.services.validation.error_classifier import (
    UNRESOLVED_DATASTREAM as _EC_UNRESOLVED_DATASTREAM,
)
from app.services.validation.error_classifier import (
    classify_status_string,
)
from app.sources.climate_adaptation import CLIMATE_ADAPTATION_ENTITY_SETS
from app.sta.canonical import resolve
from app.sta.models import adapt_datastream_payload, adapt_observation_payload, adapt_payload

logger = logging.getLogger(__name__)


def _validation_target_label(base_url: str) -> str:
    """Look up the operator-configured label for a FROST target base URL."""
    normalized = (base_url or "").rstrip("/")
    for t in settings.frost_targets:
        if t.url.rstrip("/") == normalized:
            return t.label or ""
    return ""


def _validation_summarize_results(
    results: list[dict[str, Any]],
) -> tuple[int, int, str | None]:
    """Count (failed, dropped_permanent, dominant_error_class) from a target's
    per-observation results list."""
    from collections import Counter

    failed = 0
    dropped = 0
    counts: Counter[str] = Counter()
    for r in results:
        if r.get("ok"):
            continue
        if r.get("dropped"):
            dropped += 1
        else:
            failed += 1
        cls = classify_error(status_code=r.get("status_code"))
        if cls == "unknown":
            msg = r.get("error") or r.get("message") or ""
            if msg:
                cls = classify_status_string(str(msg))
            if cls == "unknown" and r.get("message") and "datastream" in str(r.get("message")).lower():
                cls = _EC_UNRESOLVED_DATASTREAM
        counts[cls] += 1
    dominant = counts.most_common(1)[0][0] if counts else None
    return failed, dropped, dominant


def _validation_age_samples(readings: list[SensorReading], now_wall: float) -> list[float]:
    """Per-observation age-at-push seconds (source_time → now)."""
    samples: list[float] = []
    for r in readings:
        ts = getattr(r, "timestamp", None)
        if ts is None:
            continue
        try:
            samples.append(max(0.0, now_wall - ts.timestamp()))
        except (AttributeError, TypeError):
            continue
    return samples


def _canonical_property_payload(
    observed_property_key: str,
    observed_properties: dict[str, Any] | None,
) -> dict[str, str] | None:
    """Return the {name, definition, description, unit} payload for an observed
    property, sourced from the canonical dictionary.

    ``observed_properties`` is the entity-set's ``observed_properties`` map. Its
    value may be a plain description string (current shape) or a legacy nested
    dict; only the description is read from it — name, unit, and definition
    always come from canonical.py. Returns ``None`` if the key doesn't resolve.
    """
    canonical = resolve(observed_property_key)
    if canonical is None:
        return None
    meta = canonical.meta
    raw = (observed_properties or {}).get(observed_property_key)
    if isinstance(raw, dict):
        description = raw.get("description") or meta.display_name
    elif isinstance(raw, str) and raw:
        description = raw
    else:
        description = meta.display_name
    return {
        "name": meta.display_name,
        "definition": meta.definition,
        "description": description,
        "unit": meta.unit,
    }


def _as_quality_list(quality: Any) -> Any:
    """Coerce a scalar resultQuality into a list.

    The STA spec types resultQuality as Any, but some servers (e.g. the
    collaborall.net custom server) reject scalars with
    "The result quality field must be an array." Standard FROST accepts the
    list form too, so we always send an array. Mirrors the coercion already
    done in app/sta/models.py.
    """
    if quality is None:
        return None
    if isinstance(quality, list):
        return quality
    return [quality]


def _coerce_iot_id(value: Any) -> Any:
    """Convert string entity IDs to int when numeric.

    Some SensorThings implementations (e.g. Collaborall's PHP server) reject
    ``@iot.id`` values given as strings, expecting native integers.  Standard
    FROST accepts both, so always sending ints is safe.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    return value


class _DatastreamRoute(NamedTuple):
    """What a datastream on the primary target observes, read from its properties."""

    sensor_id: str  # properties.sensor_id; "" when absent
    properties: frozenset[str]  # _property_token of properties.observed_property and .streamKey


def _canonical_key(name: Any) -> str:
    """The canonical key an observed-property name resolves to, or ""."""
    canonical = resolve(str(name or "").strip())
    return canonical.value if canonical is not None else ""


def _property_token(name: Any) -> str:
    """Comparison token for an observed-property name.

    The canonical key when the name resolves, so "pm25" and "pm2_5" compare
    equal; otherwise the trimmed lower-case name; "" for an empty name.
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    return _canonical_key(raw) or raw.lower()


def _route_for(properties: Any) -> _DatastreamRoute:
    props = properties if isinstance(properties, dict) else {}
    tokens = {_property_token(props.get("observed_property")), _property_token(props.get("streamKey"))}
    tokens.discard("")
    return _DatastreamRoute(str(props.get("sensor_id") or "").strip(), frozenset(tokens))


def _reading_property_tokens(reading: SensorReading) -> frozenset[str]:
    tokens = {_property_token(reading.observed_property), _property_token(reading.stream_key)}
    tokens.discard("")
    return frozenset(tokens)


def _key_fits_route(key: str, route: _DatastreamRoute) -> bool:
    """Whether a "sensor_id::observed_property" key may map to this datastream.

    Bare keys (a sensor id or a stream key) name no property here; the push
    path checks them against the reading instead.
    """
    sensor_id, sep, observed_property = key.rpartition("::")
    if not sep:
        return True
    if route.sensor_id and route.sensor_id != sensor_id:
        return False
    token = _property_token(observed_property)
    return not (route.properties and token and token not in route.properties)


def _sensor_of_key(key: str) -> str | None:
    """The sensor id of a ``sensor_id::observed_property`` key; None for a bare key."""
    sensor_id, sep, _observed_property = key.rpartition("::")
    return sensor_id if sep else None


class _DatastreamIdMap(MutableMapping[str, str]):
    """Datastream id per mapping key on the primary target, indexed by sensor.

    Keys are ``sensor_id::observed_property`` plus the bare fallback keys (a
    sensor id or a stream key). Registrations (each polling loop pushes and
    registers from its own threadpool task) and the warm-up write while pushes
    on other threads read: writers hold a lock, iteration runs over a copy, and
    each sensor's datastream ids are published as a frozenset. A reader never
    iterates something another thread is changing, and counting a sensor's
    datastreams is one lookup instead of a scan of every key.
    """

    def __init__(self, *sources: Mapping[str, str]) -> None:
        self._data: dict[str, str] = {}
        self._keys_by_sensor: dict[str, set[str]] = {}
        self._ids_by_sensor: dict[str, frozenset[str]] = {}
        self._lock = threading.RLock()
        for source in sources:
            self.update(source)

    def __getitem__(self, key: str) -> str:
        return self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[str]:
        return iter(self.snapshot())

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._data!r})"

    def __setitem__(self, key: str, value: str) -> None:
        with self._lock:
            self._data[key] = value
            sensor_id = _sensor_of_key(key)
            if sensor_id is not None:
                self._keys_by_sensor.setdefault(sensor_id, set()).add(key)
                self._reindex(sensor_id)

    def __delitem__(self, key: str) -> None:
        with self._lock:
            del self._data[key]
            sensor_id = _sensor_of_key(key)
            if sensor_id is not None:
                self._keys_by_sensor.get(sensor_id, set()).discard(key)
                self._reindex(sensor_id)

    def _reindex(self, sensor_id: str) -> None:
        keys = self._keys_by_sensor.get(sensor_id)
        if keys:
            self._ids_by_sensor[sensor_id] = frozenset(str(self._data[key]) for key in keys)
        else:
            self._keys_by_sensor.pop(sensor_id, None)
            self._ids_by_sensor.pop(sensor_id, None)

    def snapshot(self) -> dict[str, str]:
        """A consistent copy of the mappings."""
        with self._lock:
            return dict(self._data)

    def ids_of_sensor(self, sensor_id: str) -> frozenset[str]:
        """Datastream ids of the sensor's ``sensor_id::observed_property`` keys."""
        return self._ids_by_sensor.get(sensor_id, frozenset())


# What push_observations reports for a reading that gets no datastream; the
# DLQ/seeding side matches on it as well as on the "unresolved" flag.
UNRESOLVED_MESSAGE = "No datastream mapping available for this sensor; dynamic lookup also failed."

# Warm-up listing of the primary target's datastreams: one page per request,
# continued by id (keyset) from the connector's own base URL.
_LISTING_PAGE_SIZE = 1000
_NEXT_LINK_KEYS = ("@iot.nextLink", "@nextLink")
# A failed listing is retried from the push path (and the DLQ replay) after this
# delay, doubling per failure up to the maximum, until one succeeds.
_ROUTE_RETRY_INITIAL_SECONDS = 60.0
_ROUTE_RETRY_MAX_SECONDS = 900.0


def _listed_id(row: dict[str, Any], fields: FrostODataFields) -> Any:
    """``@iot.id`` (v1.1) or ``id`` (v2.0) of a listed entity, or None."""
    for name in (fields.id, "@iot.id", "id"):
        value = row.get(name)
        if value is not None:
            return value
    return None


def _odata_id_literal(value: Any) -> str:
    """An entity id as an OData literal: bare when numeric, quoted otherwise.

    FROST compares a quoted literal as text: ``id gt '13000'`` returns ids 2, 3,
    4, ... from an integer id column.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _ids_ascend(previous: Any, ids: list[Any]) -> bool:
    """Whether a page's ids continue an ascending listing after ``previous``.

    Integer ids are checked in full; other ids (compared by the server's
    collation, not Python's) only for progress.
    """
    if not ids:
        return False
    sequence = ([previous] if previous is not None else []) + ids
    if all(isinstance(value, int) and not isinstance(value, bool) for value in sequence):
        return all(a < b for a, b in pairwise(sequence))
    return bool(ids[-1] != previous)


def _describe_error(exc: BaseException) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    return f"{type(exc).__name__}: {exc}"[:200]


@dataclass
class _RouteListing:
    """State of the primary target's datastream listing (the route guard's input)."""

    loaded: bool = False  # routes from at least one successful listing
    complete: bool = False  # that listing reached the last datastream
    loaded_at: str | None = None
    last_error: str | None = None
    failures: int = 0  # failed listings since the last success
    retry_at: float | None = None  # time.monotonic() of the next retry while not loaded


def _replay_label(payload: dict[str, Any]) -> dict[str, Any] | None:
    """The reading label a DLQ payload carries: v1.1 ``parameters``, v2.0 ``properties``."""
    for field_name in ("parameters", "properties"):
        label = payload.get(field_name)
        if isinstance(label, dict) and label.get("sensor_id") and label.get("observed_property"):
            return label
    return None


def _reading_from_label(label: dict[str, Any]) -> SensorReading:
    """The reading a DLQ line was built from, as far as routing needs it."""

    def text(name: str) -> str | None:
        value = label.get(name)
        return str(value) if value not in (None, "") else None

    return SensorReading.model_construct(
        sensor_id=str(label["sensor_id"]),
        sensor_name=text("sensor_name") or "",
        observed_property=str(label["observed_property"]),
        unit=text("unit") or "",
        value=0.0,
        timestamp=datetime.now(UTC),
        quality="good",
        location=text("location") or "",
        thing_name=text("thing_name") or "",
        device_eui=text("device_eui"),
        stream_key=text("stream_key"),
        observed_property_name=None,
    )


def _payload_datastream_id(payload: dict[str, Any]) -> Any:
    datastream = payload.get("Datastream")
    if isinstance(datastream, dict):
        for name in ("@iot.id", "id"):
            if datastream.get(name) is not None:
                return datastream[name]
    return None


def _set_payload_datastream_id(payload: dict[str, Any], datastream_id: str) -> None:
    datastream = payload.get("Datastream")
    name = "id" if isinstance(datastream, dict) and "id" in datastream and "@iot.id" not in datastream else "@iot.id"
    payload["Datastream"] = {name: _coerce_iot_id(datastream_id)}


# Per-target circuit breaker shared by push + replay paths.
observation_breaker = CircuitBreaker(
    failure_threshold=settings.frost_cb_failure_threshold,
    cooldown_seconds=settings.frost_cb_cooldown_seconds,
    overrides=settings.frost_cb_overrides,
)


class SensorThingsClient:
    def __init__(self) -> None:
        self._failed_observations_path = Path(settings.failed_observations_path)

        # Build per-target stacks from config
        cache_dir = Path(settings.registered_entities_path).parent
        self._target_stacks: list[TargetStack] = [
            TargetStack(target, cache_dir) for target in settings.frost_targets
        ]

        # Primary FROST layer components (backward-compat: first target or global config)
        self._http = FrostHTTPClient(
            base_urls=self._load_base_urls(),
            auth_username=settings.auth_username,
            auth_password=settings.auth_password,
            auth_token=settings.auth_token,
            timeout=settings.request_timeout_seconds,
        )
        self._cache = EntityCache(Path(settings.registered_entities_path))
        self._entity_manager = EntityManager(self._http, self._cache)

        # Backward-compat property alias
        self._registered_entities = self._cache.all()
        self._datastream_ids = _DatastreamIdMap(settings.datastream_ids, self._cache.collection("datastreams"))
        # Datastream id -> what it observes on the primary target (sensor_id and
        # observed-property tokens from its properties), filled by
        # warm_datastream_cache(). The push path uses it to refuse a cached
        # mapping that points at a datastream of another property or sensor.
        # Both dicts are replaced whole, never changed in place.
        self._datastream_routes: dict[str, _DatastreamRoute] = {}
        self._route_ids_by_sensor: dict[str, frozenset[str]] = {}
        self._route_listing = _RouteListing()
        # Held while a listing runs; the push path's retry skips when it is taken.
        self._warmup_lock = threading.Lock()
        self._rejected_routes_logged: set[tuple[str, str, str]] = set()
        # (base_url, "sensor_id::observed_property") already reported as unresolved.
        self._unresolved_reported: set[tuple[str, str]] = set()
        self._live_datastream_lookup_cache: dict[str, str] = {}
        # Sensors we recently failed to resolve live, keyed by their most
        # specific lookup key -> monotonic expiry. Prevents a per-push storm of
        # serial HTTP GETs for sensors with no datastream on a target (each
        # unresolvable sensor otherwise re-ran up to ~5 filter queries every
        # push cycle, which dominated push latency for large coalesced batches).
        self._live_datastream_lookup_negative: dict[str, float] = {}
        self._live_lookup_negative_ttl: float = 300.0
        self._observed_property_names: dict[str, str] = self._build_observed_property_name_map()
        self._tasking_capability_ids: dict[str, str] = dict(self._cache.collection("tasking_capabilities"))
        # Targets that answered CreateObservations with 4xx/501 (no dataArray support).
        # Seed from capabilities discovery so we skip the first-cycle 404.
        self._no_batch_targets: set[str] = {
            stack.base_url.rstrip("/")
            for stack in self._target_stacks
            if not stack.capabilities.data_array
        }

    @staticmethod
    def _load_base_urls() -> list[str]:
        configured = getattr(settings, "sensorthings_base_urls", ())
        if configured:
            return [str(url).rstrip("/") for url in configured if str(url).strip()]
        fallback = str(getattr(settings, "sensorthings_base_url", "")).strip().rstrip("/")
        return [fallback] if fallback else []

    def _build_observed_property_name_map(self) -> dict[str, str]:
        names: dict[str, str] = {}
        for entity_set in CLIMATE_ADAPTATION_ENTITY_SETS:
            observed_properties = entity_set.get("observed_properties", {})
            if not isinstance(observed_properties, dict):
                continue
            for key, payload in observed_properties.items():
                if not isinstance(payload, dict):
                    continue
                key_name = str(key).strip()
                property_name = str(payload.get("name", "")).strip()
                if key_name and property_name:
                    names[key_name] = property_name
        return names

    # -- Cache delegation (backward-compat) --------------------------------

    def _cache_entity_id(self, collection: str, name: str, entity_id: str) -> None:
        self._cache.put(collection, name, entity_id)

    def _drop_cached_entity_id(self, collection: str, name: str) -> None:
        self._cache.drop(collection, name)

    def _persist_registered_entities(self) -> None:
        self._cache._persist()

    def _datastream_key(self, sensor_id: str, observed_property: str) -> str:
        return f"{sensor_id}::{observed_property}"

    def _endpoint(self, path: str) -> str | None:
        base_url = self._primary_base_url()
        if not base_url:
            return None
        return f"{base_url}{path}"

    def _base_urls(self) -> list[str]:
        # Read from settings at runtime so monkeypatching works for tests/hot-reload.
        configured = getattr(settings, "sensorthings_base_urls", ())
        if configured:
            return [str(url).rstrip("/") for url in configured if str(url).strip()]
        fallback = str(getattr(settings, "sensorthings_base_url", "")).strip().rstrip("/")
        return [fallback] if fallback else []

    def _primary_base_url(self) -> str | None:
        urls = self._base_urls()
        return urls[0] if urls else None

    def _has_targets(self) -> bool:
        return bool(self._base_urls())

    def _endpoint_for_base_url(self, base_url: str, path: str) -> str:
        return f"{base_url.rstrip('/')}{path}"

    def _headers(self) -> dict[str, str]:
        return self._http.headers()

    def _prefixed_name(self, name: str) -> str:
        prefix = settings.entity_name_prefix.strip()
        if not prefix:
            return name
        if name.startswith(prefix):
            return name
        return f"{prefix}{name}"

    def _entity_sets_for_site(self, site_key: str) -> list[dict[str, Any]]:
        normalized_site_key = site_key.strip().lower()
        return [entity_set for entity_set in CLIMATE_ADAPTATION_ENTITY_SETS if entity_set["site_key"] == normalized_site_key]

    def _project_config_for_site(self, site_key: str) -> dict[str, Any]:
        site_config = settings.site_project_configs.get(site_key.strip().lower(), {})
        if site_config:
            return {
                "name": str(site_config.get("name", "")).strip(),
                "id": str(site_config.get("id", "")).strip(),
                "description": str(site_config.get("description", "")).strip(),
                "public": bool(site_config.get("public", True)),
            }

        return {
            "name": settings.default_project_name.strip(),
            "id": settings.default_project_id.strip(),
            "description": settings.default_project_description.strip(),
            "public": settings.default_project_public,
        }

    def _get_or_create_project(self, site_key: str) -> tuple[str | None, str, str | None]:
        # FROST Projects extension: Things, Locations, Sensors and Features link to one or
        # more Projects. A configured project id is trusted as-is; otherwise create by name.
        project_config = self._project_config_for_site(site_key)
        if project_config["id"]:
            return str(project_config["id"]), "configured", str(project_config["name"] or None)

        name = str(project_config["name"]).strip()
        if not name:
            return None, "skipped", None

        payload = {
            "name": name,
            "description": project_config["description"] or f"Project grouping for {settings.connector_name} ({site_key}).",
            "public": bool(project_config["public"]),
        }
        project_id, status = self._get_or_create_entity(settings.projects_path, name, payload, "projects")
        return project_id, status, name

    def _project_preview(self, site_key: str | None = None) -> dict[str, Any] | None:
        if site_key:
            project_config = self._project_config_for_site(site_key)
            if not (project_config["id"] or project_config["name"]):
                return None
            return {
                "name": project_config["name"] or None,
                "@iot.id": project_config["id"] or None,
                "public": bool(project_config["public"]),
                "endpoint": self._endpoint(settings.projects_path),
            }

        if settings.site_project_configs:
            by_site: dict[str, Any] = {}
            for key in sorted(settings.site_project_configs.keys()):
                preview = self._project_preview(key)
                if preview:
                    by_site[key] = preview
            if by_site:
                return {"by_site": by_site, "endpoint": self._endpoint(settings.projects_path)}

        if not (settings.default_project_id or settings.default_project_name):
            return None
        return {
            "name": settings.default_project_name or None,
            "@iot.id": settings.default_project_id or None,
            "public": settings.default_project_public,
            "endpoint": self._endpoint(settings.projects_path),
        }

    def _site_tasking_config(self, site_key: str) -> dict[str, Any]:
        site_config = settings.site_tasking_configs.get(site_key.strip().lower(), {})
        if not site_config:
            return {"actuators": [], "capabilities": []}
        actuators_raw = site_config.get("actuators", [])
        capabilities_raw = site_config.get("capabilities", [])
        return {
            "actuators": [item for item in (actuators_raw if isinstance(actuators_raw, list) else []) if isinstance(item, dict)],
            "capabilities": [item for item in (capabilities_raw if isinstance(capabilities_raw, list) else []) if isinstance(item, dict)],
        }

    def _tasking_capability_cache_key(self, site_key: str, capability_key: str) -> str:
        return f"{site_key.strip().lower()}::{capability_key.strip()}"

    def _tasking_preview(self, site_key: str | None = None) -> dict[str, Any]:
        preview: dict[str, Any] = {
            "actuators_endpoint": self._endpoint(settings.actuators_path),
            "tasking_capabilities_endpoint": self._endpoint(settings.tasking_capabilities_path),
            "tasks_endpoint": self._endpoint(settings.tasks_path),
        }
        if site_key:
            site_config = self._site_tasking_config(site_key)
            preview["site_key"] = site_key
            preview["actuator_count"] = len(site_config["actuators"])
            preview["capability_count"] = len(site_config["capabilities"])
            preview["capability_keys"] = [str(item.get("key", "")).strip() for item in site_config["capabilities"] if str(item.get("key", "")).strip()]
            return preview

        by_site: dict[str, Any] = {}
        for key in sorted(settings.site_tasking_configs.keys()):
            by_site[key] = self._tasking_preview(key)
        preview["by_site"] = by_site
        return preview

    def _resolve_thing_id(self, thing_name: str) -> str | None:
        resolved_name = self._prefixed_name(thing_name)
        cached_id = self._cache.get("things", resolved_name)
        if cached_id and self._entity_manager.cached_id_matches(settings.things_path, cached_id, resolved_name):
            return cached_id
        if cached_id:
            self._cache.drop("things", resolved_name)
        thing_id = self._entity_manager.find_by_name(settings.things_path, resolved_name)
        if thing_id:
            self._cache.put("things", resolved_name, thing_id)
        return thing_id

    def _extract_iot_id(self, response: requests.Response) -> str | None:
        return FrostHTTPClient.extract_iot_id(response)

    def _request_timeout(self) -> float:
        return settings.request_timeout_seconds

    def _fetch_entity_by_id(self, path: str, entity_id: str) -> dict[str, Any] | None:
        return self._entity_manager.fetch_by_id(path, entity_id)

    def _cached_entity_id_matches(self, path: str, entity_id: str, expected_name: str | None) -> bool:
        return self._entity_manager.cached_id_matches(path, entity_id, expected_name)

    def _find_entity_id_by_name(self, path: str, name: str) -> str | None:
        return self._entity_manager.find_by_name(path, name)

    def _escape_odata_literal(self, value: str) -> str:
        return value.replace("'", "''")

    def _extract_first_iot_id(self, body: Any) -> str | None:
        return FrostHTTPClient.extract_first_iot_id(body)

    def _fetch_datastream_id_by_filter(self, datastreams_endpoint: str, odata_filter: str, base_url: str | None = None) -> str | None:
        em = self._entity_manager
        if base_url:
            stack = self._target_stack_for_url(base_url)
            if stack:
                em = stack.entity_manager
        return em.find_by_filter(datastreams_endpoint, odata_filter)

    def _resolve_datastream_id_live(self, base_url: str, reading: SensorReading) -> str | None:
        datastreams_endpoint = self._endpoint_for_base_url(base_url, settings.datastreams_path)
        stream_key = (reading.stream_key or reading.observed_property).strip()
        observed_property_name = (reading.observed_property_name or self._observed_property_names.get(reading.observed_property, "")).strip()
        thing_name = self._prefixed_name(reading.thing_name)
        device_eui = (reading.device_eui or "").strip()

        cache_keys = [
            f"{base_url}::deviceEui::{device_eui}::streamKey::{stream_key}" if device_eui and stream_key else "",
            f"{base_url}::deviceEui::{device_eui}::observedPropertyName::{observed_property_name}" if device_eui and observed_property_name else "",
            f"{base_url}::thingName::{thing_name}::streamKey::{stream_key}" if thing_name and stream_key else "",
            f"{base_url}::thingName::{thing_name}::observedPropertyName::{observed_property_name}" if thing_name and observed_property_name else "",
        ]

        for cache_key in cache_keys:
            if cache_key and cache_key in self._live_datastream_lookup_cache:
                return self._live_datastream_lookup_cache[cache_key]

        # Negative cache: skip the live HTTP lookup entirely for sensors we
        # recently failed to resolve, so an unresolvable sensor costs one lookup
        # per TTL instead of one per push cycle.
        neg_key = next((key for key in cache_keys if key), "")
        if neg_key:
            neg_expiry = self._live_datastream_lookup_negative.get(neg_key)
            if neg_expiry is not None:
                if neg_expiry > time.monotonic():
                    return None
                del self._live_datastream_lookup_negative[neg_key]

        candidates: list[tuple[str, str]] = []
        if device_eui and stream_key:
            candidates.append(
                (
                    cache_keys[0],
                    "Thing/properties/deviceEui eq "
                    + f"'{self._escape_odata_literal(device_eui)}' and properties/streamKey eq '{self._escape_odata_literal(stream_key)}'",
                )
            )
        if device_eui and observed_property_name:
            candidates.append(
                (
                    cache_keys[1],
                    "Thing/properties/deviceEui eq "
                    + f"'{self._escape_odata_literal(device_eui)}' and ObservedProperty/name eq '{self._escape_odata_literal(observed_property_name)}'",
                )
            )
        if thing_name and stream_key:
            candidates.append(
                (
                    cache_keys[2],
                    f"Thing/name eq '{self._escape_odata_literal(thing_name)}' and properties/streamKey eq '{self._escape_odata_literal(stream_key)}'",
                )
            )
            candidates.append(
                (
                    cache_keys[2],
                    f"Thing/name eq '{self._escape_odata_literal(thing_name)}' and properties/observed_property eq '{self._escape_odata_literal(stream_key)}'",
                )
            )
        if thing_name and observed_property_name:
            candidates.append(
                (
                    cache_keys[3],
                    f"Thing/name eq '{self._escape_odata_literal(thing_name)}' and ObservedProperty/name eq '{self._escape_odata_literal(observed_property_name)}'",
                )
            )

        for cache_key, query_filter in candidates:
            datastream_id = self._fetch_datastream_id_by_filter(datastreams_endpoint, query_filter, base_url)
            if not datastream_id:
                continue
            if cache_key:
                self._live_datastream_lookup_cache[cache_key] = datastream_id
            return datastream_id

        # Remember the failure so the next push cycle doesn't repeat the storm.
        if neg_key:
            self._live_datastream_lookup_negative[neg_key] = time.monotonic() + self._live_lookup_negative_ttl
        return None

    def _create_entity(self, path: str, payload: dict[str, Any]) -> tuple[str | None, requests.Response]:
        endpoint = self._endpoint(path)
        response = requests.post(
            endpoint,
            json=payload,
            headers=self._headers(),
            timeout=self._request_timeout(),
        )
        return self._extract_iot_id(response), response

    def _get_or_create_entity(
        self,
        path: str,
        name: str,
        payload: dict[str, Any],
        collection: str,
    ) -> tuple[str | None, str]:
        return self._entity_manager.get_or_create(path, name, payload, collection)

    def _build_site_registration_preview(self, entity_sets: list[dict[str, Any]]) -> RegistrationPreview:
        thing_preview = {
            "name": self._prefixed_name("Climate adaptation sensor network"),
            "description": "Prepared SensorThings model for The Green Village and Diergaarde Blijdorp.",
            "properties": {"connector": settings.connector_name, "status": "stub"},
        }
        sensors: list[dict[str, Any]] = []
        observed_properties: list[dict[str, Any]] = []
        datastreams: list[dict[str, Any]] = []
        seen_properties: set[str] = set()

        for site_config in entity_sets:
            thing_name = self._prefixed_name(site_config["thing"]["name"])
            for sensor in site_config["sensors"]:
                sensor_name = self._prefixed_name(sensor["name"])
                sensors.append(
                    {
                        "sensor_id": sensor["sensor_id"],
                        "name": sensor_name,
                        "description": sensor["description"],
                        "encodingType": sensor["encodingType"],
                        "metadata": sensor["metadata"],
                        "properties": sensor.get("properties"),
                        "thing_name": thing_name,
                        "site_key": site_config["site_key"],
                    }
                )
                for observed_property in sensor["observed_properties"]:
                    property_payload = _canonical_property_payload(
                        observed_property, site_config.get("observed_properties"),
                    )
                    if property_payload is None:
                        logger.warning(
                            "Skipping datastream preview for non-canonical observed_property %r on %s",
                            observed_property, sensor_name,
                        )
                        continue
                    if observed_property not in seen_properties:
                        seen_properties.add(observed_property)
                        observed_properties.append(
                            {
                                "key": observed_property,
                                "name": property_payload["name"],
                                "definition": property_payload["definition"],
                                "description": property_payload["description"],
                                "unit": property_payload["unit"],
                            }
                        )
                    datastreams.append(
                        {
                            "sensor_id": sensor["sensor_id"],
                            "sensor_name": sensor_name,
                            "observed_property_key": observed_property,
                            "stream_key": observed_property,
                            "name": f"{sensor_name} - {property_payload['name']}",
                            "description": f"{property_payload['name']} observations for {thing_name}",
                            "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                            "unitOfMeasurement": {
                                "name": property_payload["name"],
                                "symbol": property_payload["unit"],
                                "definition": property_payload["definition"],
                            },
                        }
                    )

        return RegistrationPreview(
            connector=settings.connector_name,
            mode="live" if self._has_targets() else "preview",
            thing=thing_preview,
            sensors=sensors,
            observed_properties=observed_properties,
            datastreams=datastreams,
            project=self._project_preview(),
            endpoints={
                "things": self._endpoint(settings.things_path),
                "locations": self._endpoint(settings.locations_path),
                "sensors": self._endpoint(settings.sensors_path),
                "observed_properties": self._endpoint(settings.observed_properties_path),
                "datastreams": self._endpoint(settings.datastreams_path),
                "observations": self._endpoint(settings.observations_path),
                "projects": self._endpoint(settings.projects_path),
                "actuators": self._endpoint(settings.actuators_path),
                "tasking_capabilities": self._endpoint(settings.tasking_capabilities_path),
                "tasks": self._endpoint(settings.tasks_path),
            },
        )

    def build_preview(self, readings: list[SensorReading]) -> ConnectorPreview:
        payloads = [
            ObservationPayload(
                phenomenonTime=reading.timestamp,
                result=reading.value,
                resultQuality=reading.quality,
                parameters={
                    "sensor_id": reading.sensor_id,
                    "sensor_name": reading.sensor_name,
                    "observed_property": reading.observed_property,
                    "observed_property_name": reading.observed_property_name,
                    "unit": reading.unit,
                    "thing_name": reading.thing_name,
                    "location": reading.location,
                    "device_eui": reading.device_eui,
                    "stream_key": reading.stream_key,
                },
            )
            for reading in readings
        ]

        endpoint = self._endpoint(settings.observations_path)
        return ConnectorPreview(
            connector=settings.connector_name,
            mode="live" if endpoint else "preview",
            observations_endpoint=endpoint,
            readings=readings,
            payloads=payloads,
        )

    def build_registration_preview(self, readings: list[SensorReading] | None = None) -> RegistrationPreview:
        _ = readings
        return self._build_site_registration_preview(CLIMATE_ADAPTATION_ENTITY_SETS)

    def build_site_registration_preview(self, site_key: str) -> RegistrationPreview | None:
        entity_sets = self._entity_sets_for_site(site_key)
        if not entity_sets:
            return None
        preview = self._build_site_registration_preview(entity_sets)
        preview.project = self._project_preview(site_key)
        return preview

    def register_entity_set(self, site_config: dict[str, Any]) -> dict[str, Any]:
        if not self._has_targets():
            return {
                "mode": "preview",
                "message": "No SensorThings server configured. Returning registration payload preview only.",
                "preview": self._build_site_registration_preview([site_config]).model_dump(mode="json"),
            }

        # --- Primary target registration ---
        # Wrapped in try/except so secondary targets still get registered
        # when the primary is unreachable (e.g. WBD timeout).
        primary_ok = True
        primary_error: str | None = None
        project_id = project_status = project_name = None
        thing_id = thing_status = location_id = location_status = None
        sensor_ids: dict[str, str | None] = {}
        sensor_results: list[dict[str, Any]] = []
        observed_property_ids: dict[str, str | None] = {}
        datastream_ids: dict[str, str | None] = {}
        datastream_results: list[dict[str, Any]] = []

        try:
            project_id, project_status, project_name = self._get_or_create_project(site_config["site_key"])
            project_links = [{"@iot.id": _coerce_iot_id(project_id)}] if project_id else None

            thing_name = self._prefixed_name(site_config["thing"]["name"])
            thing_payload: dict[str, Any] = {
                "name": thing_name,
                "description": site_config["thing"]["description"],
                "properties": site_config["thing"]["properties"],
            }
            if project_links:
                thing_payload["Projects"] = project_links
            thing_id, thing_status = self._get_or_create_entity(settings.things_path, thing_name, thing_payload, "things")
            if not thing_id:
                primary_ok = False
                primary_error = f"Unable to register Thing {thing_name}."
            else:
                location_name = self._prefixed_name(site_config["location"]["name"])
                location_payload: dict[str, Any] = {
                    "name": location_name,
                    "description": site_config["location"]["description"],
                    "encodingType": site_config["location"]["encodingType"],
                    "location": site_config["location"]["location"],
                    "Things": [{"@iot.id": _coerce_iot_id(thing_id)}],
                }
                if project_links:
                    location_payload["Projects"] = project_links
                location_id, location_status = self._get_or_create_entity(settings.locations_path, location_name, location_payload, "locations")

                for sensor in site_config["sensors"]:
                    sensor_name = self._prefixed_name(sensor["name"])
                    sensor_payload: dict[str, Any] = {
                        "name": sensor_name,
                        "description": sensor["description"],
                        "encodingType": sensor["encodingType"],
                        "metadata": sensor["metadata"],
                    }
                    sensor_properties = sensor.get("properties")
                    if isinstance(sensor_properties, dict) and sensor_properties:
                        sensor_payload["properties"] = sensor_properties
                    if project_links:
                        sensor_payload["Projects"] = project_links
                    sensor_id, sensor_status = self._get_or_create_entity(settings.sensors_path, sensor_name, sensor_payload, "sensors")
                    sensor_ids[sensor["sensor_id"]] = sensor_id
                    sensor_results.append(
                        {
                            "sensor_id": sensor["sensor_id"],
                            "name": sensor_name,
                            "status": sensor_status,
                            "@iot.id": sensor_id,
                        }
                    )
                    if not sensor_id:
                        logger.warning("Sensor %s registration failed; skipping its datastreams", sensor_name)
                        continue

                    for observed_property in sensor["observed_properties"]:
                        property_payload = _canonical_property_payload(
                            observed_property, site_config.get("observed_properties"),
                        )
                        if property_payload is None:
                            logger.warning(
                                "Skipping datastream registration for non-canonical observed_property %r on %s",
                                observed_property, sensor_name,
                            )
                            continue
                        observed_property_record = {
                            "name": property_payload["name"],
                            "definition": property_payload["definition"],
                            "description": property_payload["description"],
                        }
                        observed_property_id, _observed_property_status = self._get_or_create_entity(
                            settings.observed_properties_path,
                            property_payload["name"],
                            observed_property_record,
                            "observed_properties",
                        )
                        observed_property_ids[observed_property] = observed_property_id

                        datastream_key = self._datastream_key(sensor["sensor_id"], observed_property)
                        datastream_payload = {
                            "name": f"{sensor_name} - {property_payload['name']}",
                            "description": f"{property_payload['name']} observations for {thing_name}",
                            "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                            "unitOfMeasurement": {
                                "name": property_payload["name"],
                                "symbol": property_payload["unit"],
                                "definition": property_payload["definition"],
                            },
                            "Thing": {"@iot.id": _coerce_iot_id(thing_id)},
                            "Sensor": {"@iot.id": _coerce_iot_id(sensor_id)},
                            "ObservedProperty": {"@iot.id": _coerce_iot_id(observed_property_id)},
                            "properties": {
                                "site_key": site_config["site_key"],
                                "thing_name": thing_name,
                                "sensor_id": sensor["sensor_id"],
                                "observed_property": observed_property,
                                "streamKey": observed_property,
                            },
                        }
                        datastream_id, datastream_status = self._get_or_create_entity(
                            settings.datastreams_path,
                            datastream_key,
                            datastream_payload,
                            "datastreams",
                        )
                        datastream_ids[datastream_key] = datastream_id
                        if datastream_id:
                            self._datastream_ids[datastream_key] = datastream_id
                        datastream_results.append(
                            {
                                "datastream_key": datastream_key,
                                "name": datastream_payload["name"],
                                "status": datastream_status,
                                "@iot.id": datastream_id,
                            }
                        )

                self._registered_entities["datastreams"].update({key: value for key, value in datastream_ids.items() if value})
                self._persist_registered_entities()
        except Exception:
            primary_ok = False
            primary_error = "Primary target unreachable (see logs)."
            logger.exception(
                "Primary target registration failed for %s; proceeding with secondary targets",
                site_config["site_key"],
            )

        # Register on additional target stacks (v1.1 and v2.0)
        extra_target_results = self._register_entity_set_on_extra_targets(site_config)

        result: dict[str, Any] = {
            "mode": "live",
            "site_key": site_config["site_key"],
            "primary_ok": primary_ok,
            "project_id": project_id,
            "project_status": project_status,
            "project_name": project_name,
            "thing_id": thing_id,
            "location_id": location_id,
            "thing_status": thing_status,
            "location_status": location_status,
            "sensor_ids": sensor_ids,
            "observed_property_ids": observed_property_ids,
            "datastream_ids": datastream_ids,
            "sensor_results": sensor_results,
            "datastream_results": datastream_results,
        }
        if primary_error:
            result["primary_error"] = primary_error
        if extra_target_results:
            result["extra_targets"] = extra_target_results
        return result

    def _register_entity_set_on_stack(
        self, stack: TargetStack, site_config: dict[str, Any]
    ) -> dict[str, Any]:
        """Register a full entity set on a single target stack.

        Builds v1.1 payloads and adapts them for the target's API version.
        Uses FROST JSON Batch when the target advertises support (2 HTTP
        round-trips instead of ~N sequential calls, race-free).
        Skips Projects/Tasking extensions for v2.0 targets.
        """
        if stack.capabilities.json_batch:
            try:
                return self._register_entity_set_on_stack_batch(stack, site_config)
            except Exception as exc:
                logger.warning(
                    "Batch registration failed on %s, falling back to sequential: %s",
                    stack.label, exc,
                )

        em = stack.entity_manager
        is_v2 = stack.is_v2

        thing_name = self._prefixed_name(site_config["thing"]["name"])
        thing_payload: dict[str, Any] = {
            "name": thing_name,
            "description": site_config["thing"]["description"],
            "properties": site_config["thing"]["properties"],
        }
        if is_v2:
            thing_payload = adapt_payload(thing_payload)
        thing_id, thing_status = em.get_or_create(
            settings.things_path, thing_name, thing_payload, "things"
        )
        if not thing_id:
            return {
                "target": stack.label,
                "version": stack.version,
                "ok": False,
                "message": f"Unable to register Thing {thing_name}.",
            }

        location_name = self._prefixed_name(site_config["location"]["name"])
        location_payload: dict[str, Any] = {
            "name": location_name,
            "description": site_config["location"]["description"],
            "encodingType": site_config["location"]["encodingType"],
            "location": site_config["location"]["location"],
            "Things": [{"@iot.id": _coerce_iot_id(thing_id)}],
        }
        if is_v2:
            location_payload = adapt_payload(location_payload)
        location_id, location_status = em.get_or_create(
            settings.locations_path, location_name, location_payload, "locations"
        )

        # Explicitly link Thing -> Location via the Thing's Locations navigation.
        # Some FROST implementations (notably the CollaborAll PHP v1.1 server)
        # ignore the nested "Things" deep-link on a Location POST, leaving the
        # Thing with no Location; the server then can't auto-resolve a
        # FeatureOfInterest and rejects every observation with HTTP 409. A PATCH
        # on the Thing is honoured by both Java and PHP FROST and is idempotent
        # where the deep-insert already linked them.
        if location_id and thing_id:
            try:
                thing_endpoint = em._http.endpoint(f"{settings.things_path}({thing_id})")
                if thing_endpoint:
                    em._http.patch(
                        thing_endpoint,
                        {"Locations": [{"@iot.id": _coerce_iot_id(location_id)}]},
                    )
            except Exception as exc:
                logger.warning(
                    "Thing->Location link PATCH failed on %s (%s): %s",
                    stack.label, stack.version, exc,
                )

        sensor_ids: dict[str, str | None] = {}
        observed_property_ids: dict[str, str | None] = {}
        datastream_results: list[dict[str, Any]] = []

        for sensor in site_config["sensors"]:
            sensor_name = self._prefixed_name(sensor["name"])
            sensor_payload: dict[str, Any] = {
                "name": sensor_name,
                "description": sensor["description"],
                "encodingType": sensor["encodingType"],
                "metadata": sensor["metadata"],
            }
            sensor_properties = sensor.get("properties")
            if isinstance(sensor_properties, dict) and sensor_properties:
                sensor_payload["properties"] = sensor_properties
            if is_v2:
                sensor_payload = adapt_payload(sensor_payload)
            sensor_id, _ = em.get_or_create(
                settings.sensors_path, sensor_name, sensor_payload, "sensors"
            )
            sensor_ids[sensor["sensor_id"]] = sensor_id
            if not sensor_id:
                logger.warning(
                    "Sensor %s failed on %s (%s); skipping its datastreams",
                    sensor_name, stack.label, stack.version,
                )
                continue

            for observed_property in sensor["observed_properties"]:
                property_payload = _canonical_property_payload(
                    observed_property, site_config.get("observed_properties"),
                )
                if property_payload is None:
                    logger.warning(
                        "Skipping datastream registration for non-canonical observed_property %r on %s (target %s)",
                        observed_property, sensor_name, stack.label,
                    )
                    continue
                op_record: dict[str, Any] = {
                    "name": property_payload["name"],
                    "definition": property_payload["definition"],
                    "description": property_payload["description"],
                }
                if is_v2:
                    op_record = adapt_payload(op_record)
                op_id, _ = em.get_or_create(
                    settings.observed_properties_path,
                    property_payload["name"],
                    op_record,
                    "observed_properties",
                )
                observed_property_ids[observed_property] = op_id

                datastream_key = self._datastream_key(sensor["sensor_id"], observed_property)
                ds_payload: dict[str, Any] = {
                    "name": f"{sensor_name} - {property_payload['name']}",
                    "description": f"{property_payload['name']} observations for {thing_name}",
                    "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                    "unitOfMeasurement": {
                        "name": property_payload["name"],
                        "symbol": property_payload["unit"],
                        "definition": property_payload["definition"],
                    },
                    "Thing": {"@iot.id": _coerce_iot_id(thing_id)},
                    "Sensor": {"@iot.id": _coerce_iot_id(sensor_id)},
                    "ObservedProperty": {"@iot.id": _coerce_iot_id(op_id)},
                    "properties": {
                        "site_key": site_config["site_key"],
                        "thing_name": thing_name,
                        "sensor_id": sensor["sensor_id"],
                        "observed_property": observed_property,
                        "streamKey": observed_property,
                    },
                }
                if is_v2:
                    ds_payload = adapt_datastream_payload(ds_payload)
                ds_id, ds_status = em.get_or_create(
                    settings.datastreams_path, datastream_key, ds_payload, "datastreams"
                )
                datastream_results.append({
                    "datastream_key": datastream_key,
                    "name": ds_payload["name"],
                    "status": ds_status,
                    "id": ds_id,
                })

        return {
            "target": stack.label,
            "version": stack.version,
            "ok": True,
            "thing_id": thing_id,
            "thing_status": thing_status,
            "location_id": location_id,
            "location_status": location_status,
            "datastream_results": datastream_results,
        }

    def _register_entity_set_on_stack_batch(
        self, stack: TargetStack, site_config: dict[str, Any]
    ) -> dict[str, Any]:
        """Register a full entity set using FROST JSON Batch (2 round-trips).

        Phase 1: batch-find all entities by name.
        Phase 2: batch-create missing entities with back-references.
        """
        em = stack.entity_manager
        is_v2 = stack.is_v2

        # -- Build entity descriptors -----------------------------------------
        thing_name = self._prefixed_name(site_config["thing"]["name"])
        thing_payload: dict[str, Any] = {
            "name": thing_name,
            "description": site_config["thing"]["description"],
            "properties": site_config["thing"]["properties"],
        }
        if is_v2:
            thing_payload = adapt_payload(thing_payload)

        location_name = self._prefixed_name(site_config["location"]["name"])
        location_payload: dict[str, Any] = {
            "name": location_name,
            "description": site_config["location"]["description"],
            "encodingType": site_config["location"]["encodingType"],
            "location": site_config["location"]["location"],
        }
        if is_v2:
            location_payload = adapt_payload(location_payload)

        # Collect all find entries: (ref_id, path, name)
        find_entries: list[tuple[str, str, str]] = [
            ("thing", settings.things_path, thing_name),
            ("location", settings.locations_path, location_name),
        ]

        # ref_id -> (path, collection, payload_builder_or_payload)
        sensor_ref_ids: dict[str, str] = {}      # sensor_id -> ref_id
        op_ref_ids: dict[str, str] = {}           # observed_property -> ref_id
        ds_descriptors: list[dict[str, Any]] = []  # for Phase 2

        for sensor in site_config["sensors"]:
            sensor_name = self._prefixed_name(sensor["name"])
            s_ref = f"sensor_{sensor['sensor_id']}"
            sensor_ref_ids[sensor["sensor_id"]] = s_ref
            find_entries.append((s_ref, settings.sensors_path, sensor_name))

            for observed_property in sensor["observed_properties"]:
                prop = _canonical_property_payload(
                    observed_property, site_config.get("observed_properties"),
                )
                if prop is None:
                    continue
                op_ref = f"op_{observed_property}"
                if observed_property not in op_ref_ids:
                    op_ref_ids[observed_property] = op_ref
                    find_entries.append((op_ref, settings.observed_properties_path, prop["name"]))

                ds_key = self._datastream_key(sensor["sensor_id"], observed_property)
                ds_name = f"{sensor_name} - {prop['name']}"
                find_entries.append((f"ds_{ds_key}", settings.datastreams_path, ds_name))
                ds_descriptors.append({
                    "ds_ref": f"ds_{ds_key}",
                    "ds_key": ds_key,
                    "ds_name": ds_name,
                    "sensor_ref": s_ref,
                    "op_ref": op_ref,
                    "prop": prop,
                    "sensor_id": sensor["sensor_id"],
                    "sensor_name": sensor_name,
                    "observed_property": observed_property,
                })

        # -- Phase 1: batch find ----------------------------------------------
        found = em.batch_find_by_name(find_entries)

        # -- Phase 2: batch create missing ------------------------------------
        to_create: list[tuple[str, str, str, dict[str, Any]]] = []

        def _id_or_ref(ref_id: str) -> Any:
            """Return a concrete ID if found, else a $ref back-reference."""
            iot_id = found.get(ref_id)
            return _coerce_iot_id(iot_id) if iot_id else f"${ref_id}"

        if not found.get("thing"):
            to_create.append(("thing", settings.things_path, "things", thing_payload))

        if not found.get("location"):
            location_payload["Things"] = [{"@iot.id": _id_or_ref("thing")}]
            to_create.append(("location", settings.locations_path, "locations", location_payload))

        for sensor in site_config["sensors"]:
            s_ref = sensor_ref_ids[sensor["sensor_id"]]
            if not found.get(s_ref):
                s_payload: dict[str, Any] = {
                    "name": self._prefixed_name(sensor["name"]),
                    "description": sensor["description"],
                    "encodingType": sensor["encodingType"],
                    "metadata": sensor["metadata"],
                }
                sensor_properties = sensor.get("properties")
                if isinstance(sensor_properties, dict) and sensor_properties:
                    s_payload["properties"] = sensor_properties
                if is_v2:
                    s_payload = adapt_payload(s_payload)
                to_create.append((s_ref, settings.sensors_path, "sensors", s_payload))

        for observed_property, op_ref in op_ref_ids.items():
            if not found.get(op_ref):
                prop = _canonical_property_payload(
                    observed_property, site_config.get("observed_properties"),
                )
                if prop is None:
                    continue
                op_record: dict[str, Any] = {
                    "name": prop["name"],
                    "definition": prop["definition"],
                    "description": prop["description"],
                }
                if is_v2:
                    op_record = adapt_payload(op_record)
                to_create.append((op_ref, settings.observed_properties_path, "observed_properties", op_record))

        datastream_results: list[dict[str, Any]] = []
        for desc in ds_descriptors:
            if found.get(desc["ds_ref"]):
                datastream_results.append({
                    "datastream_key": desc["ds_key"],
                    "name": desc["ds_name"],
                    "status": "existing",
                    "id": found[desc["ds_ref"]],
                })
                continue
            ds_payload: dict[str, Any] = {
                "name": desc["ds_name"],
                "description": f"{desc['prop']['name']} observations for {thing_name}",
                "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
                "unitOfMeasurement": {
                    "name": desc["prop"]["name"],
                    "symbol": desc["prop"]["unit"],
                    "definition": desc["prop"]["definition"],
                },
                "Thing": {"@iot.id": _id_or_ref("thing")},
                "Sensor": {"@iot.id": _id_or_ref(desc["sensor_ref"])},
                "ObservedProperty": {"@iot.id": _id_or_ref(desc["op_ref"])},
                "properties": {
                    "site_key": site_config["site_key"],
                    "thing_name": thing_name,
                    "sensor_id": desc["sensor_id"],
                    "observed_property": desc["observed_property"],
                    "streamKey": desc["observed_property"],
                },
            }
            if is_v2:
                ds_payload = adapt_datastream_payload(ds_payload)
            to_create.append((desc["ds_ref"], settings.datastreams_path, "datastreams", ds_payload))

        created: dict[str, str | None] = {}
        if to_create:
            created = em.batch_create(to_create)

        # Merge found + created
        all_ids = {k: v for k, v in {**found, **created}.items() if v}

        thing_id = all_ids.get("thing")
        location_id = all_ids.get("location")

        # Thing→Location PATCH (same workaround as sequential path)
        if location_id and thing_id:
            try:
                thing_endpoint = em._http.endpoint(f"{settings.things_path}({thing_id})")
                if thing_endpoint:
                    em._http.patch(
                        thing_endpoint,
                        {"Locations": [{"@iot.id": _coerce_iot_id(location_id)}]},
                    )
            except Exception as exc:
                logger.warning(
                    "Thing->Location link PATCH failed on %s (%s): %s",
                    stack.label, stack.version, exc,
                )

        # Build datastream results for newly created ones
        for desc in ds_descriptors:
            if desc["ds_ref"] not in created:
                continue
            ds_id = created.get(desc["ds_ref"])
            datastream_results.append({
                "datastream_key": desc["ds_key"],
                "name": desc["ds_name"],
                "status": f"created:{201 if ds_id else 'error'}",
                "id": ds_id,
            })

        logger.info(
            "Batch registration on %s: %d found, %d created (2 HTTP round-trips)",
            stack.label, len(found) - list(found.values()).count(None),
            len(created) - list(created.values()).count(None),
        )

        return {
            "target": stack.label,
            "version": stack.version,
            "ok": bool(thing_id),
            "thing_id": thing_id,
            "thing_status": "existing" if found.get("thing") else "created",
            "location_id": location_id,
            "location_status": "existing" if found.get("location") else "created",
            "datastream_results": datastream_results,
            "batch": True,
        }

    def _register_entity_set_on_extra_targets(
        self, site_config: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Register entity set on all non-primary target stacks in parallel.

        Each target has its own TargetStack (HTTP client, cache, entity manager)
        with no shared mutable state, so parallel registration is safe.
        """
        primary_url = self._primary_base_url()
        extra_stacks = [s for s in self._target_stacks if s.base_url != primary_url]
        if not extra_stacks:
            return []

        def _register_on(stack: TargetStack) -> dict[str, Any]:
            try:
                result = self._register_entity_set_on_stack(stack, site_config)
                logger.info(
                    "Registered entity set on %s (%s): %s",
                    stack.label, stack.version, "ok" if result.get("ok") else "failed",
                )
                return result
            except Exception:
                logger.exception("Failed to register entities on %s", stack.label)
                return {
                    "target": stack.label,
                    "version": stack.version,
                    "ok": False,
                    "message": "Exception during registration (see logs).",
                }

        with ThreadPoolExecutor(max_workers=len(extra_stacks)) as pool:
            return list(pool.map(_register_on, extra_stacks))

    def register_demo_entities(self, readings: list[SensorReading] | None = None) -> dict[str, Any]:
        _ = readings
        if not self._has_targets():
            return {
                "mode": "preview",
                "message": "No SensorThings server configured. Returning registration payload preview only.",
                "preview": self.build_registration_preview().model_dump(mode="json"),
            }

        site_results = [self.register_entity_set(site_config) for site_config in CLIMATE_ADAPTATION_ENTITY_SETS]
        return {
            "mode": "live",
            "site_results": site_results,
            "datastream_ids": self._datastream_ids.snapshot(),
            "registered_entities": self._registered_entities,
        }

    def register_site_entities(self, site_key: str) -> dict[str, Any] | None:
        entity_sets = self._entity_sets_for_site(site_key)
        if not entity_sets:
            return None

        if not self._has_targets():
            return {
                "mode": "preview",
                "site_key": site_key,
                "message": "No SensorThings server configured. Returning registration payload preview only.",
                "preview": self._build_site_registration_preview(entity_sets).model_dump(mode="json"),
            }

        site_results = [self.register_entity_set(site_config) for site_config in entity_sets]
        return {
            "mode": "live",
            "site_key": site_key,
            "site_results": site_results,
            "datastream_ids": self._datastream_ids.snapshot(),
            "registered_entities": self._registered_entities,
        }

    def patch_sensor_properties(self, sensor_name: str, properties: dict[str, Any]) -> dict[str, Any]:
        """PATCH the properties field of an existing Sensor entity on FROST."""
        prefixed = self._prefixed_name(sensor_name)
        iot_id = self._registered_entities.get("sensors", {}).get(prefixed)
        if not iot_id:
            return {"ok": False, "message": f"Sensor '{prefixed}' not registered; register first."}
        endpoint = self._endpoint(f"{settings.sensors_path}({iot_id})")
        if not endpoint:
            return {"ok": False, "message": "No FROST server configured."}
        try:
            response = requests.patch(
                endpoint,
                json={"properties": properties},
                headers=self._headers(),
                timeout=self._request_timeout(),
            )
            return {"ok": response.ok, "status_code": response.status_code, "@iot.id": iot_id}
        except requests.RequestException as exc:
            return {"ok": False, "message": str(exc)}

    def register_site_tasking(self, site_key: str) -> dict[str, Any] | None:
        entity_sets = self._entity_sets_for_site(site_key)
        if not entity_sets:
            return None

        tasking_config = self._site_tasking_config(site_key)
        if not self._has_targets():
            return {
                "mode": "preview",
                "site_key": site_key,
                "message": "No SensorThings server configured. Returning tasking preview only.",
                "preview": self._tasking_preview(site_key),
            }

        if not tasking_config["actuators"] and not tasking_config["capabilities"]:
            return {
                "mode": "preview",
                "site_key": site_key,
                "message": "No tasking configuration found for this site. Set SENSORTHINGS_SITE_TASKING_JSON.",
                "preview": self._tasking_preview(site_key),
            }

        actuator_ids: dict[str, str | None] = {}
        actuator_results: list[dict[str, Any]] = []
        capability_ids: dict[str, str | None] = {}
        capability_results: list[dict[str, Any]] = []

        for actuator in tasking_config["actuators"]:
            actuator_key = str(actuator.get("key", "")).strip()
            if not actuator_key:
                continue
            configured_actuator_id = str(actuator.get("id", "")).strip()
            if configured_actuator_id:
                actuator_ids[actuator_key] = configured_actuator_id
                actuator_results.append(
                    {
                        "key": actuator_key,
                        "name": str(actuator.get("name", actuator_key)).strip(),
                        "status": "configured",
                        "@iot.id": configured_actuator_id,
                    }
                )
                continue

            actuator_name = self._prefixed_name(str(actuator.get("name", actuator_key)).strip())
            actuator_payload: dict[str, Any] = {
                "name": actuator_name,
                "description": str(actuator.get("description", f"Actuator for {site_key}.")).strip(),
                "encodingType": str(actuator.get("encodingType", "application/json")).strip(),
                "metadata": str(actuator.get("metadata", "https://example.org/tasking/metadata")).strip(),
            }
            actuator_properties = actuator.get("properties", {})
            if isinstance(actuator_properties, dict) and actuator_properties:
                actuator_payload["properties"] = actuator_properties

            actuator_id, actuator_status = self._get_or_create_entity(
                settings.actuators_path,
                f"{site_key.strip().lower()}::{actuator_key}",
                actuator_payload,
                "actuators",
            )
            actuator_ids[actuator_key] = actuator_id
            actuator_results.append(
                {
                    "key": actuator_key,
                    "name": actuator_name,
                    "status": actuator_status,
                    "@iot.id": actuator_id,
                }
            )

        for capability in tasking_config["capabilities"]:
            capability_key = str(capability.get("key", "")).strip()
            if not capability_key:
                continue

            configured_capability_id = str(capability.get("id", "")).strip()
            if configured_capability_id:
                capability_ids[capability_key] = configured_capability_id
                cache_key = self._tasking_capability_cache_key(site_key, capability_key)
                self._tasking_capability_ids[cache_key] = configured_capability_id
                self._cache_entity_id("tasking_capabilities", cache_key, configured_capability_id)
                capability_results.append(
                    {
                        "key": capability_key,
                        "name": str(capability.get("name", capability_key)).strip(),
                        "status": "configured",
                        "@iot.id": configured_capability_id,
                    }
                )
                continue

            actuator_key = str(capability.get("actuator_key", "")).strip()
            actuator_id = actuator_ids.get(actuator_key)
            if not actuator_id:
                capability_results.append(
                    {
                        "key": capability_key,
                        "status": "skipped:no_actuator",
                        "@iot.id": None,
                        "message": f"Missing actuator '{actuator_key}' for capability '{capability_key}'.",
                    }
                )
                continue

            capability_name = str(capability.get("name", capability_key)).strip()
            capability_payload: dict[str, Any] = {
                "name": capability_name,
                "description": str(capability.get("description", f"Tasking capability {capability_key}.")).strip(),
                "Actuator": {"@iot.id": _coerce_iot_id(actuator_id)},
            }

            thing_name = str(capability.get("thing_name", "")).strip()
            thing_id: str | None = None
            if thing_name:
                thing_id = self._resolve_thing_id(thing_name)

            if not thing_id:
                capability_results.append(
                    {
                        "key": capability_key,
                        "status": "skipped:no_thing",
                        "@iot.id": None,
                        "message": f"Missing or unresolved Thing '{thing_name}' for capability '{capability_key}'.",
                    }
                )
                continue

            capability_payload["Thing"] = {"@iot.id": _coerce_iot_id(thing_id)}

            tasking_parameters = capability.get("taskingParameters")
            if isinstance(tasking_parameters, dict) and tasking_parameters:
                capability_payload["taskingParameters"] = tasking_parameters
            else:
                parameters = capability.get("parameters")
                fields: list[dict[str, Any]] = []
                if isinstance(parameters, list):
                    for parameter in parameters:
                        if not isinstance(parameter, dict):
                            continue
                        parameter_name = str(parameter.get("name", "")).strip()
                        if not parameter_name:
                            continue
                        parameter_type = str(parameter.get("type", parameter.get("dataType", "Text"))).strip() or "Text"
                        fields.append(
                            {
                                "name": parameter_name,
                                "label": str(parameter.get("label", parameter_name)),
                                "description": str(parameter.get("description", "")).strip(),
                                "type": parameter_type,
                            }
                        )
                capability_payload["taskingParameters"] = {
                    "type": "DataRecord",
                    "field": fields,
                }

            capability_properties = capability.get("properties", {})
            if isinstance(capability_properties, dict):
                merged_properties = dict(capability_properties)
                merged_properties.setdefault("site_key", site_key)
                merged_properties.setdefault("capability_key", capability_key)
                capability_payload["properties"] = merged_properties
            else:
                capability_payload["properties"] = {"site_key": site_key, "capability_key": capability_key}

            parameters = capability.get("parameters")
            if isinstance(parameters, list) and parameters:
                capability_payload["parameters"] = parameters

            cache_key = self._tasking_capability_cache_key(site_key, capability_key)
            capability_id, capability_status = self._get_or_create_entity(
                settings.tasking_capabilities_path,
                capability_name,
                capability_payload,
                "tasking_capabilities",
            )
            capability_ids[capability_key] = capability_id
            if capability_id:
                self._tasking_capability_ids[cache_key] = capability_id
                self._cache_entity_id("tasking_capabilities", cache_key, capability_id)
            capability_results.append(
                {
                    "key": capability_key,
                    "name": capability_name,
                    "status": capability_status,
                    "@iot.id": capability_id,
                }
            )

        return {
            "mode": "live",
            "site_key": site_key,
            "actuator_ids": actuator_ids,
            "capability_ids": capability_ids,
            "actuator_results": actuator_results,
            "capability_results": capability_results,
        }

    def create_task(self, request: TaskingTaskCreateRequest) -> dict[str, Any]:
        if not self._has_targets():
            return {
                "mode": "preview",
                "message": "No SensorThings server configured. Returning task preview only.",
                "preview": {
                    "site_key": request.site_key,
                    "capability_key": request.capability_key,
                    "input": request.input,
                    "execution_time": request.execution_time.isoformat() if request.execution_time else None,
                },
            }

        allowed = settings.tasking_allowed_commands
        if allowed and request.capability_key not in allowed:
            return {
                "mode": "live",
                "ok": False,
                "site_key": request.site_key,
                "capability_key": request.capability_key,
                "message": "Capability key is not allowed by SENSORTHINGS_TASKING_ALLOWED_COMMANDS.",
            }

        cache_key = self._tasking_capability_cache_key(request.site_key, request.capability_key)
        capability_id = self._tasking_capability_ids.get(cache_key) or self._registered_entities.get("tasking_capabilities", {}).get(cache_key)

        if not capability_id:
            tasking_config = self._site_tasking_config(request.site_key)
            for capability in tasking_config.get("capabilities", []):
                if str(capability.get("key", "")).strip() == request.capability_key:
                    configured_capability_id = str(capability.get("id", "")).strip()
                    if configured_capability_id:
                        capability_id = configured_capability_id
                        self._tasking_capability_ids[cache_key] = configured_capability_id
                        self._cache_entity_id("tasking_capabilities", cache_key, configured_capability_id)
                    break

        if not capability_id:
            registration = self.register_site_tasking(request.site_key)
            if registration is None:
                return {
                    "mode": "live",
                    "ok": False,
                    "site_key": request.site_key,
                    "capability_key": request.capability_key,
                    "message": "Unknown site key.",
                }
            capability_id = registration.get("capability_ids", {}).get(request.capability_key)

        if not capability_id:
            return {
                "mode": "live",
                "ok": False,
                "site_key": request.site_key,
                "capability_key": request.capability_key,
                "message": "No TaskingCapability registered for this capability key.",
            }

        payload: dict[str, Any] = {
            "taskingParameters": request.input,
        }
        if request.execution_time is not None:
            payload["executionTime"] = request.execution_time.isoformat().replace("+00:00", "Z")

        base_url = self._primary_base_url()
        if not base_url:
            return {"ok": False, "error": "No FROST base URL configured"}
        capability_tasks_endpoint = self._endpoint_for_base_url(base_url, f"{settings.tasking_capabilities_path}({capability_id})/Tasks")
        response = requests.post(capability_tasks_endpoint, json=payload, headers=self._headers(), timeout=self._request_timeout())
        if not response.ok:
            fallback_payload = dict(payload)
            fallback_payload["TaskingCapability"] = {"@iot.id": _coerce_iot_id(capability_id)}
            endpoint = self._endpoint(settings.tasks_path)
            response = requests.post(endpoint, json=fallback_payload, headers=self._headers(), timeout=self._request_timeout())

        task_id = self._extract_iot_id(response)
        return {
            "mode": "live",
            "ok": response.ok,
            "status_code": response.status_code,
            "site_key": request.site_key,
            "capability_key": request.capability_key,
            "task_id": task_id,
            "body": response.text[:500],
        }

    def list_tasks(self, query: TaskingTaskQuery) -> dict[str, Any] | None:
        if not self._entity_sets_for_site(query.site_key):
            return None

        if not self._has_targets():
            return {
                "mode": "preview",
                "site_key": query.site_key,
                "tasks": [],
                "message": "No SensorThings server configured.",
            }

        # Use v1.1 OData fields for the primary target (tasking queries go there)
        _fields = odata_fields_for(
            self._target_stacks[0].version if self._target_stacks else "1.1"
        )
        params: dict[str, str] = {
            "$top": str(query.top),
            "$orderby": f"{_fields.id} desc",
        }
        filters: list[str] = []
        if query.capability_key:
            cache_key = self._tasking_capability_cache_key(query.site_key, query.capability_key)
            capability_id = self._tasking_capability_ids.get(cache_key) or self._registered_entities.get("tasking_capabilities", {}).get(cache_key)
            if capability_id:
                filters.append(f"TaskingCapability/{_fields.id} eq {capability_id}")
            else:
                return {
                    "mode": "live",
                    "site_key": query.site_key,
                    "tasks": [],
                    "message": "No TaskingCapability id known for this capability key. Run tasking registration first.",
                }

        if filters:
            params["$filter"] = " and ".join(filters)

        endpoint = self._endpoint(settings.tasks_path)
        response = requests.get(endpoint, params=params, headers=self._headers(), timeout=self._request_timeout())

        try:
            body = response.json()
        except ValueError:
            body = {}

        tasks = body.get("value", []) if isinstance(body, dict) else []
        if not isinstance(tasks, list):
            tasks = []

        return {
            "mode": "live",
            "ok": response.ok,
            "status_code": response.status_code,
            "site_key": query.site_key,
            "capability_key": query.capability_key,
            "tasks": tasks,
        }

    def _post_observation(
        self, endpoint: str, payload: dict[str, Any], datastream_id: str,
        base_url: str | None = None,
    ) -> requests.Response:
        if base_url:
            stack = self._target_stack_for_url(base_url)
            if stack:
                return stack.http.post_with_retry(endpoint, payload, datastream_id)
        return self._http.post_with_retry(endpoint, payload, datastream_id)

    def _write_failed_observation(self, record: dict[str, Any]) -> None:
        self._failed_observations_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            current_size = self._failed_observations_path.stat().st_size
        except FileNotFoundError:
            current_size = 0
        if current_size >= settings.failed_dlq_max_bytes:
            logger.warning(
                "DLQ at %d bytes (limit %d) — dropping observation for datastream %s",
                current_size,
                settings.failed_dlq_max_bytes,
                record.get("datastream_id", "?"),
            )
            return
        with self._failed_observations_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _base_url_for_endpoint(self, endpoint: str) -> str | None:
        """Match an absolute endpoint URL back to a configured target base URL."""
        for base_url in self._base_urls():
            if endpoint.startswith(base_url.rstrip("/")):
                return base_url
        return None

    def _observation_exists_at(
        self, obs_endpoint: str, headers: dict[str, str], datastream_id: Any, phenomenon_time: str,
        *, version: str = "1.1",
    ) -> bool | None:
        """Return True if an Observation already exists for (datastream, phenomenonTime).

        Used by the DLQ replay path to avoid re-creating an observation whose
        original POST timed out *after* the server committed it — a duplicate a
        stock FROST (which doesn't enforce uniqueness) would silently accept.
        Returns ``None`` when the check is indeterminate (network/parse error or
        unknown datastream) so the caller falls back to writing rather than risk
        data loss.
        """
        ds_ref = _coerce_iot_id(datastream_id)
        if ds_ref in (None, "", "unknown"):
            return None
        _fields = odata_fields_for(version)
        params = {
            "$filter": f"Datastream/{_fields.id} eq {ds_ref} and phenomenonTime eq {phenomenon_time}",
            "$top": "1",
            "$select": _fields.id,
        }
        try:
            response = requests.get(obs_endpoint, params=params, headers=headers, timeout=self._request_timeout())
        except requests.RequestException:
            return None
        if not response.ok:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        value = body.get("value") if isinstance(body, dict) else None
        if not isinstance(value, list):
            return None
        return len(value) > 0

    def _check_replay_route(
        self, base_url: str, payload: dict[str, Any], datastream_id: Any,
    ) -> tuple[str, str | None, str | None]:
        """Route check of a dead-lettered observation for the primary target.

        Lines written before the routing fix can name the datastream that the
        old routing chose: another property's or another sensor's. The line's
        datastream is checked against the warm-up listing as push_observations
        checks a cached mapping, using the reading's label (``parameters``, or
        ``properties`` in a v2.0 payload). A refused line is resolved again
        the way push_observations resolves a reading: a cached mapping that
        passes the check, then the live lookup on the reading's own Thing.

        Returns ``(outcome, datastream id, refusal)``. Outcomes: "unchecked"
        (no sensor_id/observed_property label), "unknown" (the listing does not
        know the datastream), "ok", "rewritten" (the payload now names the
        reading's own datastream, whose id is returned) and "dropped" (the
        reading has no datastream of its own).
        """
        label = _replay_label(payload)
        if label is None:
            return "unchecked", None, None
        reading = _reading_from_label(label)
        current = _payload_datastream_id(payload)
        ds_id = str(current if current is not None else datastream_id)
        if ds_id not in self._datastream_routes:
            return "unknown", ds_id, None
        datastream_ids = self._datastream_ids or dict(settings.datastream_ids)
        refusal = self._route_refusal(ds_id, reading, "key", datastream_ids)
        if refusal is None:
            return "ok", ds_id, None
        own_id, _refused = self._resolve_cached_datastream_id(datastream_ids, reading)
        if not own_id:
            # Unchecked, as in push_observations: it only finds datastreams of
            # the reading's own Thing (Samen Meten readings carry another
            # sensor_id than their datastreams, and resolve only this way).
            own_id = self._resolve_datastream_id_live(base_url, reading)
        if not own_id:
            return "dropped", None, refusal
        if str(own_id) == ds_id:
            return "ok", ds_id, None
        _set_payload_datastream_id(payload, str(own_id))
        return "rewritten", str(own_id), refusal

    def replay_failed_observations(self, max_lines: int | None = None) -> dict[str, Any]:
        if not self._failed_observations_path.exists():
            return {
                "mode": "live" if settings.sensorthings_base_url else "preview",
                "replayed": 0,
                "remaining": 0,
                "message": "No failed observations file found.",
            }

        tmp_path = self._failed_observations_path.with_suffix(".tmp")
        # Clean up stale temp file from a prior crash during replay.
        if tmp_path.exists():
            tmp_path.unlink()

        max_age_seconds = settings.failed_dlq_max_age_hours * 3600
        now = datetime.now(UTC)
        results: list[dict[str, Any]] = []
        attempted = 0
        kept = 0
        expired = 0
        primary_url = (self._primary_base_url() or "").rstrip("/")
        route_counts: Counter[str] = Counter()
        listing_retried = False

        with open(self._failed_observations_path, encoding="utf-8") as infile, \
             open(tmp_path, "w", encoding="utf-8") as outfile:
            for line in infile:
                stripped = line.strip()
                if not stripped:
                    continue

                # Past the attempt limit: copy remaining lines without parsing.
                if max_lines is not None and attempted >= max_lines:
                    outfile.write(stripped + "\n")
                    kept += 1
                    continue

                try:
                    record = json.loads(stripped)
                except json.JSONDecodeError:
                    outfile.write(stripped + "\n")
                    kept += 1
                    continue

                # Age-based expiry: drop entries older than the configured max.
                ts_str = record.get("timestamp")
                if ts_str and max_age_seconds > 0:
                    try:
                        entry_time = datetime.fromisoformat(ts_str)
                        if (now - entry_time).total_seconds() > max_age_seconds:
                            expired += 1
                            continue
                    except (ValueError, TypeError):
                        pass

                endpoint = record.get("endpoint") or self._endpoint(settings.observations_path)
                payload = record.get("payload")
                datastream_id = record.get("datastream_id", "unknown")
                if not endpoint or not isinstance(payload, dict):
                    outfile.write(stripped + "\n")
                    kept += 1
                    continue

                # Skip targets whose circuit is currently open — retrying now would
                # just burn timeouts; the next replay pass picks them up.
                base_url = self._base_url_for_endpoint(endpoint)
                if base_url and not observation_breaker.allow(base_url):
                    outfile.write(stripped + "\n")
                    kept += 1
                    continue

                # Route check (primary target): never replay a reading into a
                # datastream of another sensor or observed property.
                note: dict[str, Any] = {}
                counted = False
                if base_url and base_url.rstrip("/") == primary_url:
                    if not listing_retried:
                        listing_retried = True
                        self._retry_route_listing_if_due()
                    outcome, routed_id, refusal = self._check_replay_route(base_url, payload, datastream_id)
                    route_counts[outcome] += 1
                    if outcome in ("rewritten", "dropped"):
                        # Resolving again can cost lookups: count it against the pass limit.
                        attempted += 1
                        counted = True
                    if outcome == "dropped":
                        results.append(
                            {"datastream_id": datastream_id, "ok": False, "status_code": None, "dropped": True, "route_refused": refusal}
                        )
                        continue
                    if outcome == "rewritten" and routed_id:
                        note = {"rerouted_from": str(datastream_id)}
                        datastream_id = routed_id
                        record["datastream_id"] = routed_id
                        stripped = json.dumps(record, ensure_ascii=False)

                headers = self._headers_for_url(base_url) if base_url else self._headers()

                # Dedup probe: a write that timed out after the server committed it
                # would be duplicated on replay against a server that doesn't enforce
                # (Datastream, phenomenonTime) uniqueness. Check once here — the replay
                # path is rare and low-volume, so the round-trip cost that would be
                # prohibitive on the hot path is acceptable. An indeterminate result
                # falls through to a normal POST (never drop, so no data loss).
                if settings.frost_replay_dedup_probe:
                    phenomenon_time = payload.get("phenomenonTime")
                    ds_ref = _payload_datastream_id(payload)
                    if ds_ref is None:
                        ds_ref = datastream_id
                    replay_stack = self._target_stack_for_url(base_url) if base_url else None
                    replay_version = replay_stack.version if replay_stack else "1.1"
                    if phenomenon_time and self._observation_exists_at(endpoint, headers, ds_ref, str(phenomenon_time), version=replay_version) is True:
                        results.append({"datastream_id": datastream_id, "ok": True, "status_code": 200, "duplicate": True, **note})
                        continue

                if not counted:
                    attempted += 1
                try:
                    response = requests.post(endpoint, json=payload, headers=headers, timeout=self._request_timeout())
                except requests.RequestException as exc:
                    outfile.write(stripped + "\n")
                    kept += 1
                    results.append(
                        {
                            "datastream_id": datastream_id,
                            "ok": False,
                            "status_code": getattr(getattr(exc, "response", None), "status_code", None),
                            "error": str(exc),
                            **note,
                        }
                    )
                    continue

                if response.ok:
                    results.append({"datastream_id": datastream_id, "ok": True, "status_code": response.status_code, **note})
                    continue

                # 409 Conflict = duplicate observation (already delivered) — drop it.
                if response.status_code == 409:
                    results.append({"datastream_id": datastream_id, "ok": True, "status_code": response.status_code, "duplicate": True, **note})
                    continue

                # Permanent client error (bad payload / unknown datastream) — drop it
                # from the queue instead of retrying it on every replay pass forever.
                if not is_retryable_status(response.status_code):
                    results.append({"datastream_id": datastream_id, "ok": False, "status_code": response.status_code, "dropped": True, **note})
                    continue

                outfile.write(stripped + "\n")
                kept += 1
                results.append({"datastream_id": datastream_id, "ok": False, "status_code": response.status_code, **note})

        os.replace(str(tmp_path), str(self._failed_observations_path))
        if expired:
            logger.info("DLQ replay: expired %d entries older than %dh", expired, settings.failed_dlq_max_age_hours)
        if route_counts["rewritten"] or route_counts["dropped"] or route_counts["unchecked"] or (
            route_counts and not self._route_listing.loaded
        ):
            logger.warning(
                "DLQ replay route check on %s: %d line(s) named a datastream of another sensor or observed "
                "property: %d rewritten to the reading's own datastream, %d dropped (none found); %d line(s) "
                "without a sensor_id/observed_property label and %d naming a datastream the listing does not "
                "know replayed unchecked; %d checked%s",
                primary_url, route_counts["rewritten"] + route_counts["dropped"], route_counts["rewritten"],
                route_counts["dropped"], route_counts["unchecked"], route_counts["unknown"], route_counts["ok"],
                "" if self._route_listing.loaded else " (no datastream listing loaded: route guard off)",
            )
        return {
            "mode": "live" if settings.sensorthings_base_url else "preview",
            "replayed": sum(1 for result in results if result["ok"]),
            "dropped": sum(1 for result in results if result.get("dropped")),
            "expired": expired,
            "remaining": kept,
            "route_checked": route_counts["ok"],
            "route_rewritten": route_counts["rewritten"],
            "route_dropped": route_counts["dropped"],
            "route_unchecked": route_counts["unchecked"],
            "route_unknown": route_counts["unknown"],
            "results": results,
        }

    def _headers_for_url(self, base_url: str) -> dict[str, str]:
        """Return auth headers appropriate for the given target URL."""
        stack = self._target_stack_for_url(base_url)
        if stack:
            return stack.http.headers()
        return self._headers()

    def _check_frost_status_for_base_url(self, root_url: str) -> dict[str, Any]:
        headers = self._headers_for_url(root_url)
        stack = self._target_stack_for_url(root_url)
        target_version = stack.version if stack else "v1.1"

        started = time.perf_counter()
        try:
            root_response = requests.get(root_url, headers=headers, timeout=self._request_timeout())
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            version = root_response.headers.get("SensorThings-Version") or root_response.headers.get("X-SensorThings-Version")
            if not version and root_response.headers.get("Content-Type", "").startswith("application/json"):
                try:
                    body = root_response.json()
                except ValueError:
                    body = {}
                if isinstance(body, dict):
                    version = body.get("version") or body.get("apiVersion")

            things_count = None
            things_status_code = None
            things_endpoint = self._endpoint_for_base_url(root_url, settings.things_path)
            if things_endpoint:
                things_response = requests.get(
                    things_endpoint,
                    params={"$count": "true", "$top": "0"},
                    headers=headers,
                    timeout=self._request_timeout(),
                )
                if things_response.status_code == 400:
                    things_response = requests.get(
                        things_endpoint,
                        params={"$count": "true", "$top": "1"},
                        headers=headers,
                        timeout=self._request_timeout(),
                    )
                things_status_code = things_response.status_code
                try:
                    things_body = things_response.json()
                except ValueError:
                    things_body = {}
                if isinstance(things_body, dict):
                    fields = odata_fields_for(target_version)
                    things_count = things_body.get(fields.count) or things_body.get("@iot.count") or things_body.get("@count") or things_body.get("count")

            return {
                "mode": "live",
                "reachable": root_response.ok,
                "status_code": root_response.status_code,
                "response_time_ms": elapsed_ms,
                "version": version,
                "configured_version": target_version,
                "things_count": things_count,
                "things_status_code": things_status_code,
                "root_url": root_url,
                "label": stack.label if stack else None,
            }
        except requests.RequestException as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            return {
                "mode": "live",
                "reachable": False,
                "status_code": getattr(getattr(exc, "response", None), "status_code", None),
                "response_time_ms": elapsed_ms,
                "version": None,
                "configured_version": target_version,
                "things_count": None,
                "root_url": root_url,
                "label": stack.label if stack else None,
                "error": str(exc),
            }

    def check_frost_status(self) -> dict[str, Any]:
        base_urls = self._base_urls()
        if not base_urls:
            return {
                "mode": "preview",
                "reachable": False,
                "message": "SensorThings base URL is not configured, so the connector is in preview mode.",
            }
        if len(base_urls) == 1:
            return self._check_frost_status_for_base_url(base_urls[0])

        targets = [self._check_frost_status_for_base_url(url) for url in base_urls]
        return {
            "mode": "live",
            "multi_target": True,
            "reachable": all(target.get("reachable") for target in targets),
            "targets": targets,
        }

    def _check_capabilities_for_base_url(self, root_url: str) -> dict[str, Any]:
        stack = self._target_stack_for_url(root_url)

        # Reuse the capabilities discovered at startup by TargetStack rather
        # than re-probing the landing page on every call.
        if stack is None:
            return {
                "mode": "live",
                "reachable": False,
                "root_url": root_url,
                "error": "No TargetStack configured for this URL",
            }

        caps = stack.capabilities
        is_v2 = stack.is_v2
        normalized = root_url.rstrip("/")
        batch_observed_unsupported = normalized in self._no_batch_targets

        extensions = {
            "projects": caps.projects,
            "tasking": caps.tasking,
            "opencitysense": any("opencitysense" in c.lower() for c in caps.conformance),
            "data_array": caps.data_array,
            "mqtt": caps.mqtt,
            "batch_request": caps.json_batch,
        }

        write_capabilities = {
            "create_observations_dataArray": {
                "advertised": caps.data_array,
                "observed_unsupported": batch_observed_unsupported,
                "in_use": settings.frost_batch_push_enabled and not is_v2 and not batch_observed_unsupported,
            },
            "json_batch_registration": {
                "advertised": caps.json_batch,
                "in_use": caps.json_batch,
            },
            "client_specified_id": is_v2,
            "observation_duplicate_handling": "optimistic-write; 409-as-delivered (server-dependent)",
        }

        # Quick reachability probe — HEAD is cheaper than GET.
        reachable = False
        status_code = None
        try:
            response = requests.head(root_url, headers=self._headers_for_url(root_url), timeout=self._request_timeout())
            reachable = response.ok
            status_code = response.status_code
        except requests.RequestException:
            pass

        return {
            "mode": "live",
            "reachable": reachable,
            "status_code": status_code,
            "root_url": root_url,
            "configured_version": stack.version,
            "label": stack.label,
            "collections": caps.collections,
            "conformance": caps.conformance,
            "extensions": extensions,
            "write_capabilities": write_capabilities,
        }

    def check_capabilities(self) -> dict[str, Any]:
        base_urls = self._base_urls()
        if not base_urls:
            return {
                "mode": "preview",
                "reachable": False,
                "message": "SensorThings base URL is not configured, so the connector is in preview mode.",
            }
        if len(base_urls) == 1:
            return self._check_capabilities_for_base_url(base_urls[0])

        targets = [self._check_capabilities_for_base_url(url) for url in base_urls]
        return {
            "mode": "live",
            "multi_target": True,
            "reachable": all(target.get("reachable") for target in targets),
            "targets": targets,
        }

    def _target_stack_for_url(self, base_url: str) -> TargetStack | None:
        """Find the TargetStack matching a base URL."""
        normalized = base_url.rstrip("/")
        for stack in self._target_stacks:
            if stack.base_url.rstrip("/") == normalized:
                return stack
        return None

    def _is_v2_url(self, base_url: str) -> bool:
        """Check if a base URL corresponds to a v2.0 target."""
        stack = self._target_stack_for_url(base_url)
        return stack.is_v2 if stack else False

    def _push_single_observation(
        self, endpoint: str, payload: dict[str, Any], datastream_id: str, sensor_id: str,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        """POST one observation with retry. Thread-safe (uses session internally)."""
        try:
            response = self._post_observation(endpoint, payload, datastream_id, base_url=base_url)
        except (ObservationUploadError, requests.RequestException) as exc:
            last_status = getattr(exc, "last_status", None) or getattr(getattr(exc, "response", None), "status_code", None)
            health_monitor.record_failure(datastream_id, str(exc))
            logger.error(
                "Final observation push failure (datastream=%s status=%s endpoint=%s): %s",
                datastream_id,
                last_status,
                endpoint,
                exc,
            )
            self._write_failed_observation(
                {"timestamp": datetime.now(UTC).isoformat(), "datastream_id": datastream_id, "endpoint": endpoint, "payload": payload, "status_code": last_status, "error": str(exc)},
            )
            return {"sensor_id": sensor_id, "datastream_id": datastream_id, "status_code": last_status, "ok": False, "error": str(exc)}

        if response.ok:
            health_monitor.record_success(datastream_id)
        elif response.status_code == 409:
            # Duplicate — the observation is already on the server. Treat as a
            # success and don't dead-letter it.
            health_monitor.record_success(datastream_id)
            return {"sensor_id": sensor_id, "datastream_id": datastream_id, "status_code": 409, "ok": True, "duplicate": True}
        elif not is_retryable_status(response.status_code):
            # A 4xx with an HTML body (instead of JSON) means the STA server
            # isn't actually responding — a reverse proxy or load balancer is
            # returning an error page. Treat it as retryable (dead-letter for
            # replay) rather than permanently dropping. The Fraunhofer v2.0
            # outage on Sep 24 returned HTML 404s that were incorrectly dropped.
            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type or response.text.lstrip().startswith("<html"):
                logger.warning(
                    "Observation got HTML error page (datastream=%s status=%s endpoint=%s) — treating as retryable, dead-lettering: %s",
                    datastream_id,
                    response.status_code,
                    endpoint,
                    response.text[:200],
                )
                self._write_failed_observation(
                    {"timestamp": datetime.now(UTC).isoformat(), "datastream_id": datastream_id, "endpoint": endpoint, "payload": payload, "status_code": response.status_code, "error": f"html_error_page status={response.status_code}", "body": response.text[:500]},
                )
                return {"sensor_id": sensor_id, "datastream_id": datastream_id, "status_code": response.status_code, "ok": False, "error": "html_error_page"}
            # Permanent client error (bad payload / unknown datastream). Drop it
            # instead of dead-lettering so the replay loop can't retry it forever.
            health_monitor.record_failure(datastream_id, f"dropped status {response.status_code}")
            logger.warning(
                "Observation dropped (datastream=%s status=%s endpoint=%s) — non-retryable, not dead-lettered: %s",
                datastream_id,
                response.status_code,
                endpoint,
                response.text[:200],
            )
            return {"sensor_id": sensor_id, "datastream_id": datastream_id, "status_code": response.status_code, "ok": False, "dropped": True, "body": response.text[:500]}
        else:
            logger.error(
                "Final observation push failure (datastream=%s status=%s endpoint=%s)",
                datastream_id,
                response.status_code,
                endpoint,
            )
            self._write_failed_observation(
                {"timestamp": datetime.now(UTC).isoformat(), "datastream_id": datastream_id, "endpoint": endpoint, "payload": payload, "status_code": response.status_code, "body": response.text[:500]},
            )
        return {"sensor_id": sensor_id, "datastream_id": datastream_id, "status_code": response.status_code, "ok": response.ok, "body": response.text[:500]}

    def _push_batch_data_array(
        self,
        base_url: str,
        tasks: list[tuple[str, dict[str, Any], str, str]],
    ) -> tuple[int, list[dict[str, Any]]] | None:
        """Push observations via the CreateObservations (dataArray) extension.

        One POST per push cycle instead of one per observation. Returns
        ``(sent, results)`` or ``None`` when the target does not support the
        extension (caller falls back to per-observation pushes).
        """
        normalized = base_url.rstrip("/")
        if normalized in self._no_batch_targets:
            return None

        # Chunk large batches so one CreateObservations request doesn't exceed
        # the server timeout (a 4500-obs batch read-times-out at 15s).
        max_obs = settings.frost_batch_max_observations
        if max_obs > 0 and len(tasks) > max_obs:
            # Push the chunks concurrently (bounded) so a large cycle isn't gated
            # by each chunk's serial position; each chunk is an independent
            # CreateObservations POST to the same target. This is the dominant
            # per-cycle cost for big backlogs, so parallelising it (rather than
            # the old sequential loop) is the main throughput win.
            chunks = [tasks[i:i + max_obs] for i in range(0, len(tasks), max_obs)]
            max_conc = max(1, settings.frost_batch_max_concurrency)
            outcomes: list[tuple[int, list[dict[str, Any]]] | None] = [None] * len(chunks)
            with ThreadPoolExecutor(max_workers=min(max_conc, len(chunks))) as pool:
                futures = {
                    pool.submit(self._push_batch_data_array, base_url, chunk): idx
                    for idx, chunk in enumerate(chunks)
                }
                for future in as_completed(futures):
                    outcomes[futures[future]] = future.result()
            # Every chunk reported "unsupported": nothing was sent, so let the
            # caller fall back to per-observation pushes for the whole batch.
            if all(outcome is None for outcome in outcomes):
                return None
            total_sent = 0
            all_results: list[dict[str, Any]] = []
            per_obs_endpoint = self._endpoint_for_base_url(base_url, settings.observations_path)
            for idx, outcome in enumerate(outcomes):
                if outcome is None:
                    # This chunk's target rejected the dataArray extension while
                    # other chunks succeeded — dead-letter its observations for
                    # the replay loop instead of losing them.
                    for _ep, payload, ds_id, sensor_id in chunks[idx]:
                        self._write_failed_observation(
                            {"timestamp": datetime.now(UTC).isoformat(), "datastream_id": ds_id, "endpoint": per_obs_endpoint, "payload": payload, "status_code": None, "error": "batch_unsupported_midway"},
                        )
                        all_results.append({"sensor_id": sensor_id, "datastream_id": ds_id, "status_code": None, "ok": False, "error": "batch_unsupported_midway"})
                    continue
                sent_c, res_c = outcome
                total_sent += sent_c
                all_results.extend(res_c)
            return total_sent, all_results

        components = ["phenomenonTime", "result", "resultQuality", "parameters"]
        is_v2 = self._is_v2_url(base_url)
        # v2.0 observation payloads have already been adapted upstream
        # (parameters -> properties, resultQuality dropped, @iot.id -> id), so
        # the dataArray columns and the Datastream reference differ from v1.1.
        if is_v2:
            components = ["phenomenonTime", "result", "properties"]

        def _data_row(payload: dict[str, Any]) -> list[Any]:
            if is_v2:
                return [payload.get("phenomenonTime"), payload.get("result"), payload.get("properties") or {}]
            return [
                payload.get("phenomenonTime"),
                payload.get("result"),
                payload.get("resultQuality"),
                payload.get("parameters") or {},
            ]

        def _ds_ref(ds_id: str) -> dict[str, Any]:
            f = odata_fields_for("2.0" if is_v2 else "1.1")
            return {f.id: _coerce_iot_id(ds_id)}

        # Group per datastream, preserving order so the response array maps back.
        grouped: dict[str, list[tuple[dict[str, Any], str]]] = {}
        for _ep, payload, ds_id, sensor_id in tasks:
            grouped.setdefault(ds_id, []).append((payload, sensor_id))

        body = [
            {
                "Datastream": _ds_ref(ds_id),
                "components": components,
                "dataArray": [_data_row(payload) for payload, _sid in items],
            }
            for ds_id, items in grouped.items()
        ]
        # Flat view in the same order as the response array.
        flat: list[tuple[str, dict[str, Any], str]] = [
            (ds_id, payload, sensor_id)
            for ds_id, items in grouped.items()
            for payload, sensor_id in items
        ]

        url = f"{normalized}/CreateObservations"
        try:
            response = requests.post(
                url, json=body, headers=self._headers_for_url(base_url), timeout=settings.frost_batch_timeout_seconds
            )
        except requests.RequestException as exc:
            # Network failure: dead-letter everything for the replay loop.
            logger.error(
                "Batch observation push failed (%d observations, url=%s): %s", len(flat), url, exc
            )
            results = []
            per_obs_endpoint = self._endpoint_for_base_url(base_url, settings.observations_path)
            for ds_id, payload, sensor_id in flat:
                health_monitor.record_failure(ds_id, str(exc))
                self._write_failed_observation(
                    {"timestamp": datetime.now(UTC).isoformat(), "datastream_id": ds_id, "endpoint": per_obs_endpoint, "payload": payload, "status_code": None, "error": str(exc)},
                )
                results.append({"sensor_id": sensor_id, "datastream_id": ds_id, "status_code": None, "ok": False, "error": str(exc)})
            return 0, results

        if response.status_code in {400, 404, 405, 501}:
            # Target does not implement the dataArray extension.
            logger.info(
                "CreateObservations not supported at %s (status %s); falling back to per-observation pushes",
                url,
                response.status_code,
            )
            self._no_batch_targets.add(normalized)
            return None

        results = []
        sent = 0
        entries: list[Any] = []
        if response.ok:
            try:
                parsed = response.json()
                if isinstance(parsed, list):
                    entries = parsed
                    # Log first few entries for debug if any are errors
                    error_entries = [e for e in entries[:5] if isinstance(e, str) and not e.startswith("http")]
                    if error_entries:
                        logger.warning("CreateObservations response errors (first %d): %s", len(error_entries), error_entries)
            except ValueError:
                pass
        else:
            logger.warning("CreateObservations HTTP %d: %s", response.status_code, response.text[:300])

        per_obs_endpoint = self._endpoint_for_base_url(base_url, settings.observations_path)
        for i, (ds_id, payload, sensor_id) in enumerate(flat):
            entry = entries[i] if i < len(entries) else None
            entry_error = isinstance(entry, str) and entry.lower().startswith("error")
            failed = (not response.ok) or entry_error
            if not failed:
                health_monitor.record_success(ds_id)
                sent += 1
                results.append({"sensor_id": sensor_id, "datastream_id": ds_id, "status_code": response.status_code, "ok": True})
                continue

            error = str(entry) if entry else f"batch status {response.status_code}"
            health_monitor.record_failure(ds_id, error)
            # Per-entry errors are validation failures, and a non-retryable HTTP
            # status is a permanent client error — drop both instead of
            # dead-lettering them for the replay loop to retry forever.
            permanent = entry_error or (not response.ok and not is_retryable_status(response.status_code))
            if permanent:
                results.append({"sensor_id": sensor_id, "datastream_id": ds_id, "status_code": response.status_code, "ok": False, "dropped": True, "error": error})
                continue
            self._write_failed_observation(
                {"timestamp": datetime.now(UTC).isoformat(), "datastream_id": ds_id, "endpoint": per_obs_endpoint, "payload": payload, "status_code": response.status_code, "error": error},
            )
            results.append({"sensor_id": sensor_id, "datastream_id": ds_id, "status_code": response.status_code, "ok": False, "error": error})

        if sent < len(flat):
            logger.warning(
                "Batch push to %s: %d/%d observations accepted", url, sent, len(flat)
            )
        return sent, results

    def warm_datastream_cache(self, max_datastreams: int = 100_000) -> int:
        """Pre-populate the datastream-id cache from the primary FROST target.

        The push and monitoring-forward paths resolve datastream ids from
        ``self._datastream_ids`` first, falling back to a per-reading serial
        live OData lookup. Fetching the datastreams once at startup avoids that
        cold-cache walk on the first cycles after a (re)deploy. Returns the
        number of mapping keys added.

        Also purges stale cached entries whose IDs don't exist on the current
        server (e.g. carried over from a previously-configured FROST target),
        and ``sensor_id::observed_property`` entries whose ID does exist but
        names a datastream of another sensor or property: FROST numbers each
        server's datastreams from 1, so ids carried over from a previous target
        collide with unrelated datastreams on the new one. Records what every
        listed datastream observes, so push_observations can refuse a mapping
        that would cross properties or sensors.

        The listing is ordered by id and paged by id (_list_datastreams).
        Unordered, FROST returns datastreams in storage order, which its
        observation triggers keep changing (every insert updates the datastream
        row), so ``$skip`` pages can overlap or miss datastreams.

        A failed listing (FROST down or timing out) changes nothing, is logged
        as a warning and returns 0; until a listing succeeds, push_observations
        and the DLQ replay retry it with backoff (_retry_route_listing_if_due),
        and /health reports the state (route_status).
        """
        base_url = self._primary_base_url()
        if not base_url:
            return 0
        with self._warmup_lock:
            return self._warm_datastream_cache(base_url, max_datastreams)

    def _warm_datastream_cache(self, base_url: str, max_datastreams: int) -> int:
        """warm_datastream_cache's work; the caller holds ``self._warmup_lock``."""
        # What the listing is checked against. A key that a registration on
        # another thread adds or changes while the listing runs can name a
        # datastream created after its page was read: it is left alone.
        before = self._datastream_ids.snapshot()
        started = time.monotonic()
        try:
            listed, complete, pages = self._list_datastreams(base_url, max_datastreams)
        except Exception as exc:
            self._record_listing_failure(base_url, exc)
            return 0
        server_ids = {ds_id for ds_id, _props in listed}

        # The listing also stops at max_datastreams. It is then partial: an id
        # missing from it proves nothing, and uniqueness can't be judged.
        if not complete:
            logger.warning(
                "Datastream cache warm-up stopped at %d datastreams on %s: not purging "
                "missing ids, no sensor-id or stream-key fallback entries",
                len(server_ids), base_url,
            )

        routes = {ds_id: _route_for(props) for ds_id, props in listed}
        route_ids_by_sensor: dict[str, set[str]] = {}
        for ds_id, listed_route in routes.items():
            if listed_route.sensor_id:
                route_ids_by_sensor.setdefault(listed_route.sensor_id, set()).add(ds_id)
        self._datastream_routes = routes
        self._route_ids_by_sensor = {sensor_id: frozenset(ids) for sensor_id, ids in route_ids_by_sensor.items()}
        state = self._route_listing
        failures_before = state.failures
        state.loaded, state.complete = True, complete
        state.loaded_at = datetime.now(UTC).isoformat()
        state.last_error, state.failures, state.retry_at = None, 0, None

        # Purge cached entries whose id is not on the server, or names a
        # datastream of another sensor or observed property. A complete but
        # empty listing purges everything: a server without datastreams (e.g. a
        # fresh primary) can't hold any cached id, and keeping them would route
        # readings into whatever datastream later gets that id.
        stale: list[str] = []
        mismatched: list[str] = []
        for key, cached_id in before.items():
            if self._datastream_ids.get(key) != cached_id:
                continue
            ds_key = str(cached_id)
            if complete and ds_key not in server_ids:
                stale.append(key)
                continue
            route = routes.get(ds_key)
            if route is not None and not _key_fits_route(key, route):
                mismatched.append(key)
        if stale or mismatched:
            examples = [f"{key} -> {before[key]}" for key in mismatched[:10]]
            cached = self._cache.collection("datastreams")
            for key in stale + mismatched:
                self._datastream_ids.pop(key, None)
                cached.pop(key, None)
            self._persist_registered_entities()  # once, not per dropped key
            if stale:
                logger.warning(
                    "Datastream cache purged %d stale id(s) not found on %s",
                    len(stale), base_url,
                )
            if mismatched:
                logger.warning(
                    "Datastream cache purged %d mapping(s) to a datastream of another sensor or "
                    "observed property on %s (e.g. %s)",
                    len(mismatched), base_url, ", ".join(examples),
                )

        # Add the server's datastreams. Full keys under the raw property names
        # and their canonical keys, so a reading of "pm2_5" also finds a
        # datastream registered as "pm25". Bare keys (a sensor id, a stream key)
        # are fallbacks for readings without a full-key match: add one only when
        # it names exactly one datastream on the server, since the first of
        # several would hand a reading another sensor's or property's datastream.
        sensor_counts = Counter(route.sensor_id for route in routes.values() if route.sensor_id)
        stream_counts: Counter[str] = Counter()
        for _ds_id, props in listed:
            stream_counts[str(props.get("streamKey") or props.get("observed_property") or "").strip()] += 1
        warmed = 0
        for ds_id, props in listed:
            sensor_id = str(props.get("sensor_id") or "").strip()
            observed_property = str(props.get("observed_property") or props.get("streamKey") or "").strip()
            stream_key = str(props.get("streamKey") or props.get("observed_property") or "").strip()
            keys: list[str] = []
            if sensor_id:
                for name in (observed_property, stream_key, _canonical_key(observed_property), _canonical_key(stream_key)):
                    if name:
                        keys.append(self._datastream_key(sensor_id, name))
            if complete and stream_key and stream_counts[stream_key] == 1:
                keys.append(stream_key)
            if complete and sensor_id and sensor_counts[sensor_id] == 1:
                keys.append(sensor_id)
            for key in keys:
                if key not in self._datastream_ids:
                    self._datastream_ids[key] = ds_id
                    warmed += 1
        logger.info(
            "Datastream cache warm-up listed %d datastreams on %s in %d request(s), %.1fs (%s%s): "
            "%d added, %d stale and %d mismatched purged",
            len(server_ids), base_url, pages, time.monotonic() - started,
            "complete" if complete else "partial",
            f", after {failures_before} failed attempt(s)" if failures_before else "",
            warmed, len(stale), len(mismatched),
        )
        return warmed

    def _list_datastreams(
        self, base_url: str, max_datastreams: int,
    ) -> tuple[list[tuple[str, dict[str, Any]]], bool, int]:
        """Every datastream on a target as ``(id, properties)``, in id order.

        Pages by id: ``$orderby=id asc``, then ``$filter=id gt <last id>``,
        each request built from the configured base URL. A server-supplied
        next link is never requested: it names the server's own idea of its
        root, and would be sent the credentials. A next link in either
        convention (``@iot.nextLink``; ``@nextLink``, which FROST's /v2.0 path
        answers even to a target declared as v1.1) or a full page means there
        is more; the listing is complete only when a page is short and has no
        next link.

        Returns ``(datastreams, complete, requests)``. Stops early, as partial,
        at ``max_datastreams`` or when the ids stop ascending (a server that
        ignores ``$orderby`` would make id paging skip datastreams). Raises on
        an HTTP or parse error.
        """
        stack = self._target_stack_for_url(base_url)
        fields = stack.fields if stack else odata_fields_for("1.1")
        endpoint = self._endpoint_for_base_url(base_url, settings.datastreams_path)
        headers = self._headers_for_url(base_url)
        page_size = max(1, min(max_datastreams, _LISTING_PAGE_SIZE))
        next_link_keys = {fields.next_link, *_NEXT_LINK_KEYS}
        listed: list[tuple[str, dict[str, Any]]] = []
        last_id: Any = None
        pages = 0
        while True:
            params = {"$select": f"{fields.id},name,properties", "$orderby": "id asc", "$top": str(page_size)}
            if last_id is not None:
                params["$filter"] = f"id gt {_odata_id_literal(last_id)}"
            response = requests.get(endpoint, params=params, headers=headers, timeout=self._request_timeout())
            response.raise_for_status()
            body = response.json()
            page = body.get("value") if isinstance(body, dict) else None
            if not isinstance(page, list):
                raise ValueError(f"the Datastreams listing of {base_url} has no 'value' list")
            pages += 1
            ids: list[Any] = []
            for row in page:
                ds_id = _listed_id(row, fields) if isinstance(row, dict) else None
                if ds_id is None:
                    continue
                ids.append(ds_id)
                props = row.get("properties")
                listed.append((str(ds_id), props if isinstance(props, dict) else {}))
            if not any(body.get(key) for key in next_link_keys) and len(page) < page_size:
                return listed, True, pages
            if len(listed) >= max_datastreams:
                return listed, False, pages
            if not _ids_ascend(last_id, ids):
                logger.warning(
                    "Datastream listing on %s is not in id order at page %d (%d datastreams): "
                    "stopping, the listing counts as partial",
                    base_url, pages, len(listed),
                )
                return listed, False, pages
            last_id = ids[-1]

    def _record_listing_failure(self, base_url: str, exc: BaseException) -> None:
        """Log a failed listing and, while no routes are loaded, schedule its retry."""
        state = self._route_listing
        state.failures += 1
        state.last_error = _describe_error(exc)
        if state.loaded:
            logger.warning(
                "Datastream listing on %s failed (%s); the routes listed at %s stay in use",
                base_url, state.last_error, state.loaded_at,
            )
            return
        delay = min(_ROUTE_RETRY_INITIAL_SECONDS * 2 ** min(state.failures - 1, 10), _ROUTE_RETRY_MAX_SECONDS)
        state.retry_at = time.monotonic() + delay
        logger.warning(
            "Datastream cache warm-up failed on %s (%s; attempt %d): cached datastream mappings are not "
            "checked against the datastreams' sensor and observed property until a listing succeeds; "
            "retrying from the push path in %.0fs",
            base_url, state.last_error, state.failures, delay,
        )

    def _retry_route_listing_if_due(self) -> None:
        """Retry a failed warm-up listing once its backoff has passed.

        Called by push_observations and the DLQ replay on their own thread
        (both run in the threadpool), only while no listing has succeeded.
        Does not wait when another thread is listing, and leaves a target with
        an open circuit to the breaker's cooldown.
        """
        state = self._route_listing
        if state.loaded or state.retry_at is None or time.monotonic() < state.retry_at:
            return
        base_url = self._primary_base_url()
        if not base_url or observation_breaker.snapshot().get(base_url.rstrip("/"), {}).get("open"):
            return
        if not self._warmup_lock.acquire(blocking=False):
            return
        try:
            if state.loaded or state.retry_at is None or time.monotonic() < state.retry_at:
                return
            logger.info("Retrying the datastream listing on %s (%d failed attempt(s))", base_url, state.failures)
            self._warm_datastream_cache(base_url, 100_000)
        except Exception as exc:
            self._record_listing_failure(base_url, exc)
        finally:
            self._warmup_lock.release()

    def route_status(self) -> dict[str, Any]:
        """State of the route guard's datastream listing, for /health."""
        state = self._route_listing
        retry_in = None
        if not state.loaded and state.retry_at is not None:
            retry_in = round(max(0.0, state.retry_at - time.monotonic()), 1)
        return {
            "routes_loaded": state.loaded,
            "route_count": len(self._datastream_routes),
            "complete": state.complete,
            "last_error": state.last_error,
            "failed_attempts": state.failures,
            "loaded_at": state.loaded_at,
            "next_retry_in_seconds": retry_in,
        }

    def _resolve_cached_datastream_id(
        self, datastream_ids: Mapping[str, str], reading: SensorReading,
    ) -> tuple[str | None, list[str]]:
        """Resolve a reading's datastream on the primary target from the id map.

        Tries the ``sensor_id::observed_property`` key, then the reading's
        stream key, then its bare sensor id, and returns the first datastream
        the reading may go to, plus why the others were refused. A datastream
        whose properties (as listed at warm-up) name another observed property
        or sensor is always refused, so a fallback can never put a reading into
        another property's datastream. Without listed properties the full key
        and the stream key are trusted (both name the property), and the bare
        sensor id only when the sensor has exactly one known datastream.
        """
        lookups = [(self._datastream_key(reading.sensor_id, reading.observed_property), "key")]
        stream_key = (reading.stream_key or "").strip()
        if stream_key:
            lookups.append((stream_key, "stream_key"))
        if reading.sensor_id:
            lookups.append((reading.sensor_id, "sensor_id"))

        refused: list[str] = []
        for key, kind in lookups:
            candidate = datastream_ids.get(key)
            if not candidate:
                continue
            ds_id = str(candidate)
            reason = self._route_refusal(ds_id, reading, kind, datastream_ids)
            if reason is None:
                return ds_id, refused
            refused.append(reason)
            log_key = (key, ds_id, reading.observed_property)
            if log_key not in self._rejected_routes_logged:
                self._rejected_routes_logged.add(log_key)
                logger.warning(
                    "Not routing %s %s via %r: %s",
                    reading.sensor_id, reading.observed_property, key, reason,
                )
        return None, refused

    def _route_refusal(
        self, ds_id: str, reading: SensorReading, kind: str, datastream_ids: Mapping[str, str],
    ) -> str | None:
        """Why ``reading`` may not go to datastream ``ds_id``; None when it may."""
        route = self._datastream_routes.get(ds_id)
        if route is not None and route.sensor_id and route.sensor_id != reading.sensor_id:
            return f"datastream {ds_id} belongs to sensor {route.sensor_id}"
        if route is not None and route.properties:
            if _reading_property_tokens(reading) & route.properties:
                return None
            return f"datastream {ds_id} observes {', '.join(sorted(route.properties))}"
        if kind != "sensor_id":
            return None
        # Bare sensor id, datastream property unknown: only a sensor's sole datastream.
        known = self._known_datastreams_of(reading.sensor_id, datastream_ids)
        known.add(ds_id)
        if len(known) == 1:
            return None
        return f"sensor {reading.sensor_id} has {len(known)} datastreams and datastream {ds_id}'s observed property is unknown"

    def _known_datastreams_of(self, sensor_id: str, datastream_ids: Mapping[str, str]) -> set[str]:
        """Ids of a sensor's datastreams: its mapped full keys and its listed datastreams.

        Uses the per-sensor indexes, so it neither scans every key per reading
        nor iterates a map that a registration on another thread is changing.
        """
        if isinstance(datastream_ids, _DatastreamIdMap):
            known = set(datastream_ids.ids_of_sensor(sensor_id))
        else:  # a plain map (the settings fallback): iterate a copy
            known = {str(value) for key, value in list(datastream_ids.items()) if _sensor_of_key(key) == sensor_id}
        known.update(self._route_ids_by_sensor.get(sensor_id, ()))
        return known

    def push_observations(
        self,
        readings: list[SensorReading],
        *,
        source: str = "unknown",
    ) -> dict[str, Any]:
        preview = self.build_preview(readings)
        base_urls = self._base_urls()
        if not base_urls:
            return {
                "mode": "preview",
                "message": "No SensorThings server configured. Returning payload preview only.",
                "preview": preview.model_dump(mode="json"),
            }

        # A failed startup listing leaves the route guard off: retry it here,
        # once its backoff has passed, before this push resolves its readings.
        self._retry_route_listing_if_due()
        datastream_ids = self._datastream_ids or dict(settings.datastream_ids)

        endpoint_results: list[dict[str, Any]] = []
        total_sent = 0

        def _push_target(base_url: str) -> tuple[dict[str, Any], int]:
            # Validation instrumentation — captured once per target per push cycle
            # so the harness can compute per-(source,target) latency, error rate,
            # and DLQ/circuit activity. All calls are cheap no-ops when the harness
            # kill switch is off.
            _val_started = time.perf_counter()
            _val_target_label = _validation_target_label(base_url)
            endpoint = self._endpoint_for_base_url(base_url, settings.observations_path)
            is_v2 = self._is_v2_url(base_url)
            stack = self._target_stack_for_url(base_url)
            breaker_open = not observation_breaker.allow(base_url)

            # Phase 1: resolve datastream IDs and build payloads (sequential,
            # touches caches). Skip live HTTP lookups while the breaker is open.
            tasks: list[tuple[str, dict[str, Any], str, str]] = []  # (endpoint, payload, ds_id, sensor_id)
            unresolved: list[dict[str, Any]] = []
            refused_mappings = 0
            is_primary = not stack or stack.base_url == (self._primary_base_url() or "")
            for reading in readings:
                datastream_id = None
                refused: list[str] = []
                if stack and not is_primary:
                    ds_key = self._datastream_key(reading.sensor_id, reading.observed_property)
                    datastream_id = stack.cache.get("datastreams", ds_key)
                if not datastream_id and is_primary:
                    # Only a datastream of the reading's own observed property:
                    # a mapping to another property's datastream is refused, and
                    # the reading falls through to the (property-specific) live
                    # lookup or stays unresolved.
                    datastream_id, refused = self._resolve_cached_datastream_id(datastream_ids, reading)
                    refused_mappings += len(refused)
                if not datastream_id and not breaker_open:
                    datastream_id = self._resolve_datastream_id_live(base_url, reading)
                if not datastream_id:
                    entry: dict[str, Any] = {
                        "sensor_id": reading.sensor_id,
                        "observed_property": reading.observed_property,
                        "ok": False,
                        "message": UNRESOLVED_MESSAGE,
                        # Not pushed and not dead-lettered: nothing of it reached FROST.
                        "unresolved": True,
                    }
                    if refused:
                        entry["refused"] = refused
                    unresolved.append(entry)
                    continue

                payload = {
                    "phenomenonTime": reading.timestamp.isoformat().replace("+00:00", "Z"),
                    "result": reading.value,
                    "resultQuality": _as_quality_list(reading.quality),
                    "Datastream": {"@iot.id": _coerce_iot_id(datastream_id)},
                    "parameters": {
                        "sensor_id": reading.sensor_id,
                        "sensor_name": reading.sensor_name,
                        "observed_property": reading.observed_property,
                        "unit": reading.unit,
                        "location": reading.location,
                        "thing_name": reading.thing_name,
                        "device_eui": reading.device_eui,
                        "stream_key": reading.stream_key,
                    },
                }
                if is_v2:
                    payload = adapt_observation_payload(payload)
                tasks.append((endpoint, payload, datastream_id, reading.sensor_id))

            if unresolved:
                # Warn the first time a sensor::property goes unresolved on a
                # target (silent drops have hidden stale datastreams before);
                # repeats of known keys only at debug level.
                keys = sorted({f"{u['sensor_id']}::{u['observed_property']}" for u in unresolved})
                new_keys = [key for key in keys if (base_url, key) not in self._unresolved_reported]
                self._unresolved_reported.update((base_url, key) for key in new_keys)
                logger.log(
                    logging.WARNING if new_keys else logging.DEBUG,
                    "%s push to %s: %d of %d readings have no datastream of their own observed property "
                    "and are not pushed (%d mapping(s) to another property's or sensor's datastream "
                    "refused); %d new: %s",
                    source, base_url, len(unresolved), len(readings), refused_mappings, len(new_keys),
                    ", ".join(new_keys[:10]) + (" ..." if len(new_keys) > 10 else ""),
                )

            # Circuit open: dead-letter everything in one pass (no HTTP, one log line).
            if breaker_open:
                try:
                    dlq_size = self._failed_observations_path.stat().st_size
                except FileNotFoundError:
                    dlq_size = 0
                if dlq_size >= settings.failed_dlq_max_bytes:
                    logger.warning(
                        "Circuit open for %s — %d observations DROPPED (DLQ full at %d bytes)",
                        base_url, len(tasks), dlq_size,
                    )
                else:
                    now_iso = datetime.now(UTC).isoformat()
                    for ep, pl, ds_id, _sid in tasks:
                        self._write_failed_observation(
                            {"timestamp": now_iso, "datastream_id": ds_id, "endpoint": ep, "payload": pl, "status_code": None, "error": "circuit_open"},
                        )
                    logger.warning(
                        "Circuit open for %s — %d observations dead-lettered for replay", base_url, len(tasks)
                    )
                _val_dur_ms = (time.perf_counter() - _val_started) * 1000.0
                validation_recorder.record_push_attempt(
                    source=source,
                    target=base_url,
                    target_label=_val_target_label,
                    duration_ms=_val_dur_ms,
                    readings_count=len(readings),
                    sent_count=0,
                    failed_count=len(unresolved),
                    dropped_permanent_count=0,
                    dropped_circuit_count=len(tasks),
                    error_class="circuit_open",
                    error_msg="circuit_open",
                )
                return (
                    {"base_url": base_url, "endpoint": endpoint, "sent": 0, "circuit_open": True, "dead_lettered": len(tasks), "results": list(unresolved)},
                    0,
                )

            # Phase 2: push. Prefer one batched CreateObservations request;
            # fall back to parallel per-observation POSTs.
            results: list[dict[str, Any]] = list(unresolved)
            sent = 0
            if tasks:
                batch_result = None
                if settings.frost_batch_push_enabled and (not is_v2 or settings.frost_v2_batch_push_enabled):
                    batch_result = self._push_batch_data_array(base_url, tasks)
                if batch_result is not None:
                    sent, batch_results = batch_result
                    results.extend(batch_results)
                else:
                    with ThreadPoolExecutor(max_workers=min(10, len(tasks))) as pool:
                        futures = {
                            pool.submit(self._push_single_observation, ep, pl, ds_id, sid, base_url): ds_id
                            for ep, pl, ds_id, sid in tasks
                        }
                        for future in as_completed(futures):
                            result = future.result()
                            results.append(result)
                            if result.get("ok"):
                                sent += 1

                # Cycle-level breaker bookkeeping: a cycle where nothing got
                # through counts as one failure; anything else closes it.
                if sent == 0:
                    first_error = next((r.get("error") for r in results if r.get("error")), None)
                    if observation_breaker.record_failure(base_url, first_error):
                        logger.error(
                            "Circuit opened for %s after %d consecutive failed push cycles (cooldown %.0fs)",
                            base_url,
                            settings.frost_cb_failure_threshold,
                            settings.frost_cb_cooldown_seconds,
                        )
                        validation_recorder.record_incident(
                            kind="circuit_open",
                            target=base_url,
                            source=source,
                            level="warning",
                            context={
                                "failure_threshold": settings.frost_cb_failure_threshold,
                                "cooldown_seconds": settings.frost_cb_cooldown_seconds,
                                "first_error": (first_error or "")[:200] or None,
                            },
                        )
                else:
                    # If the breaker was previously observed open and we now got
                    # some sent, that's a half-open → closed transition worth
                    # recording. Reading snapshot before record_success gives us
                    # the pre-close state.
                    _pre = observation_breaker.snapshot().get(base_url.rstrip("/"), {})
                    _was_open = bool(_pre.get("open"))
                    observation_breaker.record_success(base_url)
                    if _was_open:
                        validation_recorder.record_incident(
                            kind="circuit_close",
                            target=base_url,
                            source=source,
                            level="info",
                            context={"sent": sent},
                        )

            _val_dur_ms = (time.perf_counter() - _val_started) * 1000.0
            _val_failed, _val_dropped_permanent, _val_error_class = _validation_summarize_results(results)
            # If everything succeeded we still record for latency samples, but
            # without an error_class so events.jsonl stays quiet on a clean run.
            validation_recorder.record_push_attempt(
                source=source,
                target=base_url,
                target_label=_val_target_label,
                duration_ms=_val_dur_ms,
                readings_count=len(readings),
                sent_count=sent,
                failed_count=_val_failed,
                dropped_permanent_count=_val_dropped_permanent,
                dropped_circuit_count=0,
                error_class=_val_error_class,
                error_msg=None,
                age_at_push_samples=_validation_age_samples(readings, time.time()),
            )
            return (
                {"base_url": base_url, "endpoint": endpoint, "sent": sent, "results": results},
                sent,
            )

        # Push to all FROST targets concurrently. The per-target work (resolve +
        # batch or parallel per-observation POST) is independent, so a slow
        # target (e.g. a v2.0 server doing per-observation inserts, or a
        # timing-out secondary) no longer serializes the whole ingest cycle.
        # Results are re-ordered to match ``base_urls`` for deterministic output.
        if len(base_urls) == 1:
            result, sent = _push_target(base_urls[0])
            endpoint_results.append(result)
            total_sent += sent
        else:
            results_by_url: dict[str, tuple[dict[str, Any], int]] = {}
            with ThreadPoolExecutor(max_workers=len(base_urls)) as pool:
                futures = {pool.submit(_push_target, base_url): base_url for base_url in base_urls}
                for future in as_completed(futures):
                    results_by_url[futures[future]] = future.result()
            for base_url in base_urls:
                result, sent = results_by_url[base_url]
                endpoint_results.append(result)
                total_sent += sent

        if len(endpoint_results) == 1:
            only = endpoint_results[0]
            return {"mode": "live", "endpoint": only["endpoint"], "sent": only["sent"], "total_sent": only["sent"], "results": only["results"]}

        return {"mode": "live", "multi_target": True, "sent": total_sent, "total_sent": total_sent, "targets": endpoint_results}


client = SensorThingsClient()
