from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from app.config import settings
from app.models import ConnectorPreview, ObservationPayload, RegistrationPreview, SensorReading
from app.sources.climate_adaptation import CLIMATE_ADAPTATION_ENTITY_SETS


logger = logging.getLogger(__name__)


class SensorThingsClient:
    def __init__(self) -> None:
        self._registered_entities_path = Path(settings.registered_entities_path)
        self._failed_observations_path = Path(settings.failed_observations_path)
        self._registered_entities = self._load_registered_entities()
        self._datastream_ids: dict[str, str] = dict(settings.datastream_ids)
        self._datastream_ids.update(self._registered_entities.get("datastreams", {}))

    def _load_registered_entities(self) -> dict[str, dict[str, str]]:
        default_state: dict[str, dict[str, str]] = {
            "things": {},
            "locations": {},
            "sensors": {},
            "observed_properties": {},
            "datastreams": {},
        }
        if not self._registered_entities_path.exists():
            return default_state

        try:
            data = json.loads(self._registered_entities_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default_state

        if not isinstance(data, dict):
            return default_state

        for key in default_state:
            entries = data.get(key)
            if isinstance(entries, dict):
                default_state[key] = {str(name): str(entity_id) for name, entity_id in entries.items()}
        return default_state

    def _persist_registered_entities(self) -> None:
        self._registered_entities_path.parent.mkdir(parents=True, exist_ok=True)
        self._registered_entities_path.write_text(
            json.dumps(self._registered_entities, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _cache_entity_id(self, collection: str, name: str, entity_id: str) -> None:
        self._registered_entities.setdefault(collection, {})[name] = entity_id
        self._persist_registered_entities()

    def _datastream_key(self, sensor_id: str, observed_property: str) -> str:
        return f"{sensor_id}::{observed_property}"

    def _endpoint(self, path: str) -> str | None:
        if not settings.sensorthings_base_url:
            return None
        return f"{settings.sensorthings_base_url}{path}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if settings.auth_token:
            headers["Authorization"] = f"Bearer {settings.auth_token}"
        return headers

    def _extract_iot_id(self, response: requests.Response) -> str | None:
        try:
            body = response.json()
            if isinstance(body, dict):
                if body.get("@iot.id") is not None:
                    return str(body["@iot.id"])
                if body.get("id") is not None:
                    return str(body["id"])
        except ValueError:
            pass

        location = response.headers.get("Location", "")
        match = re.search(r"\(([^)]+)\)$", location)
        if match:
            return match.group(1).strip("'")
        return None

    def _request_timeout(self) -> float:
        return settings.request_timeout_seconds

    def _find_entity_id_by_name(self, path: str, name: str) -> str | None:
        endpoint = self._endpoint(path)
        if not endpoint:
            return None

        # [VERIFY against live server] FROST v1.1 should support $filter=name eq '...'.
        response = requests.get(
            endpoint,
            params={"$filter": f"name eq '{name}'", "$top": 1},
            headers=self._headers(),
            timeout=self._request_timeout(),
        )
        if not response.ok:
            return None

        try:
            body = response.json()
        except ValueError:
            return None

        candidates: list[dict[str, Any]] = []
        if isinstance(body, dict):
            if isinstance(body.get("value"), list):
                candidates = [item for item in body["value"] if isinstance(item, dict)]
            elif body.get("@iot.id") is not None:
                return str(body["@iot.id"])

        if not candidates:
            return None

        candidate = candidates[0]
        if candidate.get("@iot.id") is not None:
            return str(candidate["@iot.id"])
        if candidate.get("id") is not None:
            return str(candidate["id"])
        return None

    def _create_entity(self, path: str, payload: dict[str, Any]) -> tuple[str | None, requests.Response]:
        response = requests.post(
            self._endpoint(path),
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
        cached_id = self._registered_entities.get(collection, {}).get(name)
        if cached_id:
            return cached_id, "cached"

        existing_id = self._find_entity_id_by_name(path, name)
        if existing_id:
            self._cache_entity_id(collection, name, existing_id)
            return existing_id, "existing"

        entity_id, response = self._create_entity(path, payload)
        if entity_id:
            self._cache_entity_id(collection, name, entity_id)
        return entity_id, f"created:{response.status_code}"

    def _build_site_registration_preview(self, entity_sets: list[dict[str, Any]]) -> RegistrationPreview:
        thing_preview = {
            "name": "Climate adaptation sensor network",
            "description": "Prepared SensorThings model for The Green Village and Diergaarde Blijdorp.",
            "properties": {"connector": settings.connector_name, "status": "stub"},
        }
        sensors: list[dict[str, Any]] = []
        observed_properties: list[dict[str, Any]] = []
        datastreams: list[dict[str, Any]] = []
        seen_properties: set[str] = set()

        for site_config in entity_sets:
            thing_name = site_config["thing"]["name"]
            for sensor in site_config["sensors"]:
                sensors.append(
                    {
                        "sensor_id": sensor["sensor_id"],
                        "name": sensor["name"],
                        "description": sensor["description"],
                        "encodingType": sensor["encodingType"],
                        "metadata": sensor["metadata"],
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
                            "sensor_name": sensor["name"],
                            "observed_property_key": observed_property,
                            "name": f"{sensor['name']} - {property_payload['name']}",
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
            mode="live" if settings.sensorthings_base_url else "preview",
            thing=thing_preview,
            sensors=sensors,
            observed_properties=observed_properties,
            datastreams=datastreams,
            endpoints={
                "things": self._endpoint(settings.things_path),
                "locations": self._endpoint(settings.locations_path),
                "sensors": self._endpoint(settings.sensors_path),
                "observed_properties": self._endpoint(settings.observed_properties_path),
                "datastreams": self._endpoint(settings.datastreams_path),
                "observations": self._endpoint(settings.observations_path),
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
                    "unit": reading.unit,
                    "thing_name": reading.thing_name,
                    "location": reading.location,
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

    def register_entity_set(self, site_config: dict[str, Any]) -> dict[str, Any]:
        if not settings.sensorthings_base_url:
            return {
                "mode": "preview",
                "message": "No SensorThings server configured. Returning registration payload preview only.",
                "preview": self._build_site_registration_preview([site_config]).model_dump(mode="json"),
            }

        thing_name = site_config["thing"]["name"]
        thing_payload = {
            "name": thing_name,
            "description": site_config["thing"]["description"],
            "properties": site_config["thing"]["properties"],
        }
        thing_id, thing_status = self._get_or_create_entity(settings.things_path, thing_name, thing_payload, "things")
        if not thing_id:
            return {
                "site_key": site_config["site_key"],
                "thing_name": thing_name,
                "ok": False,
                "message": f"Unable to register Thing {thing_name}.",
                "thing_status": thing_status,
            }

        location_name = site_config["location"]["name"]
        location_payload = {
            "name": location_name,
            "description": site_config["location"]["description"],
            "encodingType": site_config["location"]["encodingType"],
            "location": site_config["location"]["location"],
            "Things": [{"@iot.id": thing_id}],
        }
        location_id, location_status = self._get_or_create_entity(settings.locations_path, location_name, location_payload, "locations")

        sensor_ids: dict[str, str | None] = {}
        sensor_results: list[dict[str, Any]] = []
        observed_property_ids: dict[str, str | None] = {}
        datastream_ids: dict[str, str | None] = {}
        datastream_results: list[dict[str, Any]] = []

        for sensor in site_config["sensors"]:
            sensor_payload = {
                "name": sensor["name"],
                "description": sensor["description"],
                "encodingType": sensor["encodingType"],
                "metadata": sensor["metadata"],
            }
            sensor_id, sensor_status = self._get_or_create_entity(settings.sensors_path, sensor["name"], sensor_payload, "sensors")
            sensor_ids[sensor["sensor_id"]] = sensor_id
            sensor_results.append(
                {
                    "sensor_id": sensor["sensor_id"],
                    "name": sensor["name"],
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
                    "name": f"{sensor['name']} - {property_payload['name']}",
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

        return {
            "mode": "live",
            "site_key": site_config["site_key"],
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

    def register_demo_entities(self, readings: list[SensorReading] | None = None) -> dict[str, Any]:
        _ = readings
        if not settings.sensorthings_base_url:
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

    def _post_observation(self, endpoint: str, payload: dict[str, Any], datastream_id: str) -> requests.Response:
        attempts = 3
        current_wait = 2.0
        last_response: requests.Response | None = None

        for attempt in range(1, attempts + 1):
            try:
                response = requests.post(endpoint, json=payload, headers=self._headers(), timeout=self._request_timeout())
                last_response = response
                if response.ok:
                    return response

                logger.warning(
                    "Observation push failed",
                    extra={
                        "timestamp": datetime.now(UTC).isoformat(),
                        "datastream_id": datastream_id,
                        "status_code": response.status_code,
                        "attempt": attempt,
                    },
                )
            except requests.RequestException as exc:
                logger.warning(
                    "Observation push raised an exception",
                    extra={
                        "timestamp": datetime.now(UTC).isoformat(),
                        "datastream_id": datastream_id,
                        "status_code": getattr(getattr(exc, "response", None), "status_code", None),
                        "attempt": attempt,
                    },
                )
                last_response = None

            if attempt < attempts:
                time.sleep(current_wait)
                current_wait *= 2

        if last_response is None:
            raise requests.RequestException("Observation push failed after retries without a response.")
        return last_response

    def _write_failed_observation(self, record: dict[str, Any]) -> None:
        self._failed_observations_path.parent.mkdir(parents=True, exist_ok=True)
        with self._failed_observations_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def replay_failed_observations(self) -> dict[str, Any]:
        if not self._failed_observations_path.exists():
            return {
                "mode": "live" if settings.sensorthings_base_url else "preview",
                "replayed": 0,
                "remaining": 0,
                "message": "No failed observations file found.",
            }

        lines = self._failed_observations_path.read_text(encoding="utf-8").splitlines()
        kept_lines: list[str] = []
        results: list[dict[str, Any]] = []

        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                kept_lines.append(line)
                continue

            endpoint = record.get("endpoint") or self._endpoint(settings.observations_path)
            payload = record.get("payload")
            datastream_id = record.get("datastream_id", "unknown")
            if not endpoint or not isinstance(payload, dict):
                kept_lines.append(line)
                continue

            try:
                response = requests.post(endpoint, json=payload, headers=self._headers(), timeout=self._request_timeout())
            except requests.RequestException as exc:
                kept_lines.append(line)
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

            kept_lines.append(line)
            results.append({"datastream_id": datastream_id, "ok": False, "status_code": response.status_code})

        self._failed_observations_path.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8")
        return {
            "mode": "live" if settings.sensorthings_base_url else "preview",
            "replayed": sum(1 for result in results if result["ok"]),
            "remaining": len(kept_lines),
            "results": results,
        }

    def check_frost_status(self) -> dict[str, Any]:
        if not settings.sensorthings_base_url:
            return {
                "mode": "preview",
                "reachable": False,
                "message": "SensorThings base URL is not configured, so the connector is in preview mode.",
            }

        root_url = settings.sensorthings_base_url
        started = time.perf_counter()
        try:
            root_response = requests.get(root_url, headers=self._headers(), timeout=self._request_timeout())
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
            things_endpoint = self._endpoint(settings.things_path)
            if things_endpoint:
                # [VERIFY against live server] FROST should return an @iot.count field for $count=true requests.
                things_response = requests.get(
                    things_endpoint,
                    params={"$count": "true", "$top": "0"},
                    headers=self._headers(),
                    timeout=self._request_timeout(),
                )
                things_status_code = things_response.status_code
                try:
                    things_body = things_response.json()
                except ValueError:
                    things_body = {}
                if isinstance(things_body, dict):
                    things_count = things_body.get("@iot.count") or things_body.get("count")

            return {
                "mode": "live",
                "reachable": root_response.ok,
                "status_code": root_response.status_code,
                "response_time_ms": elapsed_ms,
                "version": version,
                "things_count": things_count,
                "things_status_code": things_status_code,
                "root_url": root_url,
            }
        except requests.RequestException as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
            return {
                "mode": "live",
                "reachable": False,
                "status_code": getattr(getattr(exc, "response", None), "status_code", None),
                "response_time_ms": elapsed_ms,
                "version": None,
                "things_count": None,
                "root_url": root_url,
                "error": str(exc),
            }

    def push_observations(self, readings: list[SensorReading]) -> dict[str, Any]:
        preview = self.build_preview(readings)
        if not preview.observations_endpoint:
            return {
                "mode": "preview",
                "message": "No SensorThings server configured. Returning payload preview only.",
                "preview": preview.model_dump(mode="json"),
            }

        datastream_ids = self._datastream_ids or dict(settings.datastream_ids)
        if not datastream_ids:
            return {
                "mode": "preview",
                "message": "No datastream IDs known yet. Run /connector/register-demo first or set SENSORTHINGS_DATASTREAM_IDS_JSON.",
                "preview": preview.model_dump(mode="json"),
            }

        results: list[dict[str, Any]] = []
        sent = 0
        for reading in readings:
            datastream_id = datastream_ids.get(self._datastream_key(reading.sensor_id, reading.observed_property)) or datastream_ids.get(reading.sensor_id)
            if not datastream_id:
                results.append(
                    {
                        "sensor_id": reading.sensor_id,
                        "ok": False,
                        "message": "No datastream mapping available for this sensor.",
                    }
                )
                continue

            payload = {
                "phenomenonTime": reading.timestamp.isoformat().replace("+00:00", "Z"),
                "result": reading.value,
                "resultQuality": reading.quality,
                "Datastream": {"@iot.id": datastream_id},
                "parameters": {
                    "sensor_id": reading.sensor_id,
                    "sensor_name": reading.sensor_name,
                    "observed_property": reading.observed_property,
                    "unit": reading.unit,
                    "location": reading.location,
                    "thing_name": reading.thing_name,
                },
            }
            try:
                response = self._post_observation(preview.observations_endpoint, payload, datastream_id)
            except requests.RequestException as exc:
                response = getattr(exc, "response", None)
                logger.error(
                    "Final observation push failure",
                    extra={
                        "timestamp": datetime.now(UTC).isoformat(),
                        "datastream_id": datastream_id,
                        "status_code": getattr(response, "status_code", None),
                    },
                )
                self._write_failed_observation(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "datastream_id": datastream_id,
                        "endpoint": preview.observations_endpoint,
                        "payload": payload,
                        "status_code": getattr(response, "status_code", None),
                        "error": str(exc),
                    }
                )
                results.append(
                    {
                        "sensor_id": reading.sensor_id,
                        "datastream_id": datastream_id,
                        "status_code": getattr(response, "status_code", None),
                        "ok": False,
                        "error": str(exc),
                    }
                )
                continue

            sent += 1
            results.append(
                {
                    "sensor_id": reading.sensor_id,
                    "datastream_id": datastream_id,
                    "status_code": response.status_code,
                    "ok": response.ok,
                    "body": response.text[:500],
                }
            )
            if not response.ok:
                logger.error(
                    "Final observation push failure",
                    extra={
                        "timestamp": datetime.now(UTC).isoformat(),
                        "datastream_id": datastream_id,
                        "status_code": response.status_code,
                    },
                )
                self._write_failed_observation(
                    {
                        "timestamp": datetime.now(UTC).isoformat(),
                        "datastream_id": datastream_id,
                        "endpoint": preview.observations_endpoint,
                        "payload": payload,
                        "status_code": response.status_code,
                        "body": response.text[:500],
                    }
                )

        return {
            "mode": "live",
            "endpoint": preview.observations_endpoint,
            "sent": sent,
            "results": results,
        }


client = SensorThingsClient()
