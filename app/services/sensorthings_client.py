from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from app.config import settings
from app.exceptions import (
    ObservationUploadError,
)
from app.frost.cache import EntityCache
from app.frost.circuit_breaker import CircuitBreaker
from app.frost.entity_manager import EntityManager
from app.frost.http_client import FrostHTTPClient, is_retryable_status
from app.frost.target_stack import TargetStack
from app.frost.v2_adapter import adapt_datastream_payload, adapt_observation_payload, adapt_payload
from app.models import (
    ConnectorPreview,
    ObservationPayload,
    RegistrationPreview,
    SensorReading,
    TaskingTaskCreateRequest,
    TaskingTaskQuery,
)
from app.services.health_monitor import health_monitor
from app.sources.climate_adaptation import CLIMATE_ADAPTATION_ENTITY_SETS


logger = logging.getLogger(__name__)


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


# Per-target circuit breaker shared by push + replay paths.
observation_breaker = CircuitBreaker(
    failure_threshold=settings.frost_cb_failure_threshold,
    cooldown_seconds=settings.frost_cb_cooldown_seconds,
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
        self._datastream_ids: dict[str, str] = dict(settings.datastream_ids)
        self._datastream_ids.update(self._cache.collection("datastreams"))
        self._live_datastream_lookup_cache: dict[str, str] = {}
        self._observed_property_names: dict[str, str] = self._build_observed_property_name_map()
        self._tasking_capability_ids: dict[str, str] = dict(self._cache.collection("tasking_capabilities"))
        # Targets that answered CreateObservations with 4xx/501 (no dataArray support).
        self._no_batch_targets: set[str] = set()

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
        return {
            "actuators": [item for item in site_config.get("actuators", []) if isinstance(item, dict)],
            "capabilities": [item for item in site_config.get("capabilities", []) if isinstance(item, dict)],
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
                    property_payload = site_config["observed_properties"][observed_property]
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

        project_id, project_status, project_name = self._get_or_create_project(site_config["site_key"])
        project_links = [{"@iot.id": project_id}] if project_id else None

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
            return {
                "site_key": site_config["site_key"],
                "thing_name": thing_name,
                "ok": False,
                "message": f"Unable to register Thing {thing_name}.",
                "thing_status": thing_status,
                "project_id": project_id,
                "project_status": project_status,
                "project_name": project_name,
            }

        location_name = self._prefixed_name(site_config["location"]["name"])
        location_payload: dict[str, Any] = {
            "name": location_name,
            "description": site_config["location"]["description"],
            "encodingType": site_config["location"]["encodingType"],
            "location": site_config["location"]["location"],
            "Things": [{"@iot.id": thing_id}],
        }
        if project_links:
            location_payload["Projects"] = project_links
        location_id, location_status = self._get_or_create_entity(settings.locations_path, location_name, location_payload, "locations")

        sensor_ids: dict[str, str | None] = {}
        sensor_results: list[dict[str, Any]] = []
        observed_property_ids: dict[str, str | None] = {}
        datastream_ids: dict[str, str | None] = {}
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

            for observed_property in sensor["observed_properties"]:
                property_payload = site_config["observed_properties"][observed_property]
                observed_property_record = {
                    "name": property_payload["name"],
                    "definition": property_payload["definition"],
                    "description": property_payload["description"],
                }
                observed_property_id, observed_property_status = self._get_or_create_entity(
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
                    "Thing": {"@iot.id": thing_id},
                    "Sensor": {"@iot.id": sensor_id},
                    "ObservedProperty": {"@iot.id": observed_property_id},
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

        # Register on additional target stacks (v1.1 and v2.0)
        extra_target_results = self._register_entity_set_on_extra_targets(site_config)

        result: dict[str, Any] = {
            "mode": "live",
            "site_key": site_config["site_key"],
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
        if extra_target_results:
            result["extra_targets"] = extra_target_results
        return result

    def _register_entity_set_on_stack(
        self, stack: TargetStack, site_config: dict[str, Any]
    ) -> dict[str, Any]:
        """Register a full entity set on a single target stack.

        Builds v1.1 payloads and adapts them for the target's API version.
        Skips Projects/Tasking extensions for v2.0 targets.
        """
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
            "Things": [{"@iot.id": thing_id}],
        }
        if is_v2:
            location_payload = adapt_payload(location_payload)
        location_id, location_status = em.get_or_create(
            settings.locations_path, location_name, location_payload, "locations"
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

            for observed_property in sensor["observed_properties"]:
                property_payload = site_config["observed_properties"][observed_property]
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
                    "Thing": {"@iot.id": thing_id},
                    "Sensor": {"@iot.id": sensor_id},
                    "ObservedProperty": {"@iot.id": op_id},
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

    def _register_entity_set_on_extra_targets(
        self, site_config: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Register entity set on all non-primary target stacks."""
        results: list[dict[str, Any]] = []
        primary_url = self._primary_base_url()
        for stack in self._target_stacks:
            if stack.base_url == primary_url:
                continue
            try:
                result = self._register_entity_set_on_stack(stack, site_config)
                results.append(result)
                logger.info(
                    "Registered entity set on %s (%s): %s",
                    stack.label, stack.version, "ok" if result.get("ok") else "failed",
                )
            except Exception:
                logger.exception("Failed to register entities on %s", stack.label)
                results.append({
                    "target": stack.label,
                    "version": stack.version,
                    "ok": False,
                    "message": "Exception during registration (see logs).",
                })
        return results

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
            "datastream_ids": self._datastream_ids,
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
            "datastream_ids": self._datastream_ids,
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
                "Actuator": {"@iot.id": actuator_id},
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

            capability_payload["Thing"] = {"@iot.id": thing_id}

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
        capability_tasks_endpoint = self._endpoint_for_base_url(base_url, f"{settings.tasking_capabilities_path}({capability_id})/Tasks")
        response = requests.post(capability_tasks_endpoint, json=payload, headers=self._headers(), timeout=self._request_timeout())
        if not response.ok:
            fallback_payload = dict(payload)
            fallback_payload["TaskingCapability"] = {"@iot.id": capability_id}
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

        params: dict[str, str] = {
            "$top": str(query.top),
            "$orderby": "@iot.id desc",
        }
        filters: list[str] = []
        if query.capability_key:
            cache_key = self._tasking_capability_cache_key(query.site_key, query.capability_key)
            capability_id = self._tasking_capability_ids.get(cache_key) or self._registered_entities.get("tasking_capabilities", {}).get(cache_key)
            if capability_id:
                filters.append(f"TaskingCapability/@iot.id eq {capability_id}")
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

    def _post_observation(self, endpoint: str, payload: dict[str, Any], datastream_id: str) -> requests.Response:
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

        with open(self._failed_observations_path, "r", encoding="utf-8") as infile, \
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

                headers = self._headers_for_url(base_url) if base_url else self._headers()
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
                        }
                    )
                    continue

                if response.ok:
                    results.append({"datastream_id": datastream_id, "ok": True, "status_code": response.status_code})
                    continue

                # 409 Conflict = duplicate observation (already delivered) — drop it.
                if response.status_code == 409:
                    results.append({"datastream_id": datastream_id, "ok": True, "status_code": response.status_code, "duplicate": True})
                    continue

                # Permanent client error (bad payload / unknown datastream) — drop it
                # from the queue instead of retrying it on every replay pass forever.
                if not is_retryable_status(response.status_code):
                    results.append({"datastream_id": datastream_id, "ok": False, "status_code": response.status_code, "dropped": True})
                    continue

                outfile.write(stripped + "\n")
                kept += 1
                results.append({"datastream_id": datastream_id, "ok": False, "status_code": response.status_code})

        os.replace(str(tmp_path), str(self._failed_observations_path))
        if expired:
            logger.info("DLQ replay: expired %d entries older than %dh", expired, settings.failed_dlq_max_age_hours)
        return {
            "mode": "live" if settings.sensorthings_base_url else "preview",
            "replayed": sum(1 for result in results if result["ok"]),
            "dropped": sum(1 for result in results if result.get("dropped")),
            "expired": expired,
            "remaining": kept,
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
                    things_count = things_body.get("@iot.count") or things_body.get("@count") or things_body.get("count")

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
        headers = self._headers_for_url(root_url)
        stack = self._target_stack_for_url(root_url)

        try:
            response = requests.get(root_url, headers=headers, timeout=self._request_timeout())
        except requests.RequestException as exc:
            return {
                "mode": "live",
                "reachable": False,
                "status_code": getattr(getattr(exc, "response", None), "status_code", None),
                "root_url": root_url,
                "error": str(exc),
            }

        try:
            body = response.json()
        except ValueError:
            body = {}

        collections: list[str] = []
        conformance: list[str] = []
        if isinstance(body, dict):
            if isinstance(body.get("value"), list):
                collections = [item.get("name") for item in body["value"] if isinstance(item, dict) and item.get("name")]
            server_settings = body.get("serverSettings")
            if isinstance(server_settings, dict) and isinstance(server_settings.get("conformance"), list):
                conformance = [str(item) for item in server_settings["conformance"]]

        def _advertises(token: str) -> bool:
            token_lower = token.lower()
            return any(token_lower in name.lower() for name in collections) or any(
                token_lower in item.lower() for item in conformance
            )

        extensions = {
            "projects": _advertises("Projects"),
            "tasking": _advertises("Tasking") or _advertises("Tasks"),
            "opencitysense": _advertises("OpenCitySense"),
        }

        return {
            "mode": "live",
            "reachable": response.ok,
            "status_code": response.status_code,
            "root_url": root_url,
            "configured_version": stack.version if stack else "v1.1",
            "label": stack.label if stack else None,
            "collections": collections,
            "conformance": conformance,
            "extensions": extensions,
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
    ) -> dict[str, Any]:
        """POST one observation with retry. Thread-safe (uses session internally)."""
        try:
            response = self._post_observation(endpoint, payload, datastream_id)
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
            total_sent = 0
            all_results: list[dict[str, Any]] = []
            for i in range(0, len(tasks), max_obs):
                outcome = self._push_batch_data_array(base_url, tasks[i:i + max_obs])
                if outcome is None:
                    # Target doesn't support the dataArray extension. If nothing
                    # was sent yet, let the caller fall back to per-observation
                    # pushes for the whole batch; otherwise dead-letter the rest.
                    if not all_results:
                        return None
                    per_obs_endpoint = self._endpoint_for_base_url(base_url, settings.observations_path)
                    for _ep, payload, ds_id, sensor_id in tasks[i:]:
                        self._write_failed_observation(
                            {"timestamp": datetime.now(UTC).isoformat(), "datastream_id": ds_id, "endpoint": per_obs_endpoint, "payload": payload, "status_code": None, "error": "batch_unsupported_midway"},
                        )
                        all_results.append({"sensor_id": sensor_id, "datastream_id": ds_id, "status_code": None, "ok": False, "error": "batch_unsupported_midway"})
                    break
                sent_c, res_c = outcome
                total_sent += sent_c
                all_results.extend(res_c)
            return total_sent, all_results

        components = ["phenomenonTime", "result", "resultQuality", "parameters"]
        # Group per datastream, preserving order so the response array maps back.
        grouped: dict[str, list[tuple[dict[str, Any], str]]] = {}
        for _ep, payload, ds_id, sensor_id in tasks:
            grouped.setdefault(ds_id, []).append((payload, sensor_id))

        body = [
            {
                "Datastream": {"@iot.id": ds_id},
                "components": components,
                "dataArray": [
                    [
                        payload.get("phenomenonTime"),
                        payload.get("result"),
                        payload.get("resultQuality"),
                        payload.get("parameters") or {},
                    ]
                    for payload, _sid in items
                ],
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
            except ValueError:
                pass

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

    def push_observations(self, readings: list[SensorReading]) -> dict[str, Any]:
        preview = self.build_preview(readings)
        base_urls = self._base_urls()
        if not base_urls:
            return {
                "mode": "preview",
                "message": "No SensorThings server configured. Returning payload preview only.",
                "preview": preview.model_dump(mode="json"),
            }

        datastream_ids = self._datastream_ids or dict(settings.datastream_ids)

        endpoint_results: list[dict[str, Any]] = []
        total_sent = 0
        for base_url in base_urls:
            endpoint = self._endpoint_for_base_url(base_url, settings.observations_path)
            is_v2 = self._is_v2_url(base_url)
            stack = self._target_stack_for_url(base_url)
            breaker_open = not observation_breaker.allow(base_url)

            # Phase 1: resolve datastream IDs and build payloads (sequential,
            # touches caches). Skip live HTTP lookups while the breaker is open.
            tasks: list[tuple[str, dict[str, Any], str, str]] = []  # (endpoint, payload, ds_id, sensor_id)
            unresolved: list[dict[str, Any]] = []
            is_primary = not stack or stack.base_url == (self._primary_base_url() or "")
            for reading in readings:
                datastream_id = None
                if stack and not is_primary:
                    ds_key = self._datastream_key(reading.sensor_id, reading.observed_property)
                    datastream_id = stack.cache.get("datastreams", ds_key)
                if not datastream_id and is_primary:
                    datastream_id = (
                        datastream_ids.get(self._datastream_key(reading.sensor_id, reading.observed_property))
                        or datastream_ids.get(getattr(reading, "stream_key", "") or "")
                        or datastream_ids.get(reading.sensor_id)
                    )
                if not datastream_id and not breaker_open:
                    datastream_id = self._resolve_datastream_id_live(base_url, reading)
                if not datastream_id:
                    unresolved.append(
                        {"sensor_id": reading.sensor_id, "ok": False, "message": "No datastream mapping available for this sensor; dynamic lookup also failed."}
                    )
                    continue

                payload = {
                    "phenomenonTime": reading.timestamp.isoformat().replace("+00:00", "Z"),
                    "result": reading.value,
                    "resultQuality": _as_quality_list(reading.quality),
                    "Datastream": {"@iot.id": datastream_id},
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
                    for ep, pl, ds_id, sid in tasks:
                        self._write_failed_observation(
                            {"timestamp": now_iso, "datastream_id": ds_id, "endpoint": ep, "payload": pl, "status_code": None, "error": "circuit_open"},
                        )
                    logger.warning(
                        "Circuit open for %s — %d observations dead-lettered for replay", base_url, len(tasks)
                    )
                endpoint_results.append(
                    {"base_url": base_url, "endpoint": endpoint, "sent": 0, "circuit_open": True, "dead_lettered": len(tasks), "results": list(unresolved)}
                )
                continue

            # Phase 2: push. Prefer one batched CreateObservations request;
            # fall back to parallel per-observation POSTs.
            results: list[dict[str, Any]] = list(unresolved)
            sent = 0
            if tasks:
                batch_result = None
                if settings.frost_batch_push_enabled and not is_v2:
                    batch_result = self._push_batch_data_array(base_url, tasks)
                if batch_result is not None:
                    sent, batch_results = batch_result
                    results.extend(batch_results)
                else:
                    with ThreadPoolExecutor(max_workers=min(10, len(tasks))) as pool:
                        futures = {
                            pool.submit(self._push_single_observation, ep, pl, ds_id, sid): ds_id
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
                else:
                    observation_breaker.record_success(base_url)

            endpoint_results.append(
                {"base_url": base_url, "endpoint": endpoint, "sent": sent, "results": results}
            )
            total_sent += sent

        if len(endpoint_results) == 1:
            only = endpoint_results[0]
            return {"mode": "live", "endpoint": only["endpoint"], "sent": only["sent"], "total_sent": only["sent"], "results": only["results"]}

        return {"mode": "live", "multi_target": True, "sent": total_sent, "total_sent": total_sent, "targets": endpoint_results}


client = SensorThingsClient()
