from __future__ import annotations

import re
from typing import Any

import requests

from app.config import settings
from app.models import ConnectorPreview, ObservationPayload, RegistrationPreview, SensorReading


class SensorThingsClient:
    def __init__(self) -> None:
        self._datastream_ids: dict[str, str] = dict(settings.datastream_ids)

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

    def build_registration_preview(self, readings: list[SensorReading]) -> RegistrationPreview:
        thing_name = readings[0].thing_name if readings else "Wastewater Treatment Demo"
        sensors: list[dict[str, Any]] = []
        observed_properties: list[dict[str, Any]] = []
        datastreams: list[dict[str, Any]] = []
        seen_properties: set[str] = set()

        for reading in readings:
            sensors.append(
                {
                    "sensor_id": reading.sensor_id,
                    "name": reading.sensor_name,
                    "description": f"{reading.observed_property} sensor for the testbed connector",
                    "encodingType": "application/json",
                    "metadata": f"https://example.org/sensors/{reading.sensor_id}",
                }
            )

            if reading.observed_property not in seen_properties:
                seen_properties.add(reading.observed_property)
                observed_properties.append(
                    {
                        "key": reading.observed_property,
                        "name": reading.observed_property.replace("_", " ").title(),
                        "definition": f"https://example.org/observed-properties/{reading.observed_property}",
                        "description": f"Observed property for {reading.observed_property} values",
                    }
                )

            datastreams.append(
                {
                    "sensor_id": reading.sensor_id,
                    "sensor_name": reading.sensor_name,
                    "observed_property_key": reading.observed_property,
                    "name": f"{reading.sensor_name} Datastream",
                    "description": f"Datastream for {reading.sensor_name}",
                    "observationType": "OM_Measurement",
                    "unitOfMeasurement": {
                        "name": reading.unit,
                        "symbol": reading.unit,
                        "definition": reading.unit,
                    },
                    "properties": {
                        "sensor_id": reading.sensor_id,
                        "unit": reading.unit,
                        "connector": settings.connector_name,
                    },
                }
            )

        return RegistrationPreview(
            connector=settings.connector_name,
            mode="live" if settings.sensorthings_base_url else "preview",
            thing={
                "name": thing_name,
                "description": "Demo asset for a wastewater cleaning digital twin testbed",
                "properties": {
                    "project": "Geonovum SensorThings Testbed",
                    "connector": settings.connector_name,
                },
            },
            sensors=sensors,
            observed_properties=observed_properties,
            datastreams=datastreams,
            endpoints={
                "things": self._endpoint(settings.things_path),
                "sensors": self._endpoint(settings.sensors_path),
                "observed_properties": self._endpoint(settings.observed_properties_path),
                "datastreams": self._endpoint(settings.datastreams_path),
                "observations": self._endpoint(settings.observations_path),
            },
        )

    def register_demo_entities(self, readings: list[SensorReading]) -> dict[str, Any]:
        preview = self.build_registration_preview(readings)
        if not settings.sensorthings_base_url:
            return {
                "mode": "preview",
                "message": "No SensorThings server configured. Returning registration payload preview only.",
                "preview": preview.model_dump(mode="json"),
            }

        headers = self._headers()

        thing_response = requests.post(
            self._endpoint(settings.things_path),
            json=preview.thing,
            headers=headers,
            timeout=15,
        )
        thing_id = self._extract_iot_id(thing_response)

        sensor_ids: dict[str, str | None] = {}
        for sensor_payload in preview.sensors:
            response = requests.post(
                self._endpoint(settings.sensors_path),
                json={key: value for key, value in sensor_payload.items() if key != "sensor_id"},
                headers=headers,
                timeout=15,
            )
            sensor_ids[sensor_payload["sensor_id"]] = self._extract_iot_id(response)

        observed_property_ids: dict[str, str | None] = {}
        for prop_payload in preview.observed_properties:
            response = requests.post(
                self._endpoint(settings.observed_properties_path),
                json={key: value for key, value in prop_payload.items() if key != "key"},
                headers=headers,
                timeout=15,
            )
            observed_property_ids[prop_payload["key"]] = self._extract_iot_id(response)

        datastream_results: list[dict[str, Any]] = []
        for datastream_payload in preview.datastreams:
            response = requests.post(
                self._endpoint(settings.datastreams_path),
                json={
                    "name": datastream_payload["name"],
                    "description": datastream_payload["description"],
                    "observationType": datastream_payload["observationType"],
                    "unitOfMeasurement": datastream_payload["unitOfMeasurement"],
                    "Thing": {"@iot.id": thing_id},
                    "Sensor": {"@iot.id": sensor_ids.get(datastream_payload["sensor_id"])},
                    "ObservedProperty": {"@iot.id": observed_property_ids.get(datastream_payload["observed_property_key"])},
                    "properties": datastream_payload["properties"],
                },
                headers=headers,
                timeout=15,
            )
            datastream_id = self._extract_iot_id(response)
            if datastream_id:
                self._datastream_ids[datastream_payload["sensor_id"]] = datastream_id
            datastream_results.append(
                {
                    "sensor_id": datastream_payload["sensor_id"],
                    "datastream_id": datastream_id,
                    "status_code": response.status_code,
                    "ok": response.ok,
                    "body": response.text[:500],
                }
            )

        return {
            "mode": "live",
            "thing_id": thing_id,
            "datastream_ids": self._datastream_ids,
            "results": datastream_results,
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

        headers = self._headers()
        results: list[dict[str, Any]] = []
        sent = 0
        for reading in readings:
            datastream_id = datastream_ids.get(reading.sensor_id)
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
                },
            }
            response = requests.post(
                preview.observations_endpoint,
                json=payload,
                headers=headers,
                timeout=15,
            )
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

        return {
            "mode": "live",
            "endpoint": preview.observations_endpoint,
            "sent": sent,
            "results": results,
        }


client = SensorThingsClient()
