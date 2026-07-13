from __future__ import annotations

import json
from datetime import UTC
from typing import Any

from app.config import settings
from app.models import MonitoringMqttPayload, MonitoringMqttPreview, SensorReading
from app.services.sensorthings_client import client


def _resolve_monitoring_payloads(readings: list[SensorReading]) -> list[MonitoringMqttPayload]:
    base_url = client._primary_base_url()
    if not base_url:
        return []

    payloads: list[MonitoringMqttPayload] = []
    datastream_ids = client._datastream_ids or dict(settings.datastream_ids)
    for reading in readings:
        datastream_id = datastream_ids.get(client._datastream_key(reading.sensor_id, reading.observed_property))
        datastream_id = datastream_id or datastream_ids.get(reading.sensor_id)
        if not datastream_id:
            datastream_id = client._resolve_datastream_id_live(base_url, reading)
        if not datastream_id:
            continue
        payloads.append(
            MonitoringMqttPayload(
                server_url=base_url,
                datastream_id=str(datastream_id),
                value=reading.value,
                timestamp=reading.timestamp.astimezone(UTC),
                quality=reading.quality,
                metadata={
                    "sensor_name": reading.sensor_name,
                    "thing_name": reading.thing_name,
                    "observed_property": reading.observed_property,
                    "observed_property_name": reading.observed_property_name,
                    "unit": reading.unit,
                    "device_eui": reading.device_eui,
                    "stream_key": reading.stream_key,
                },
            )
        )
    return payloads


def build_monitoring_mqtt_preview(readings: list[SensorReading]) -> MonitoringMqttPreview:
    topic = settings.monitoring_mqtt_topic if settings.monitoring_mqtt_enabled else None
    return MonitoringMqttPreview(
        connector=settings.connector_name,
        mode="live" if settings.monitoring_mqtt_enabled else "preview",
        topic=topic,
        payloads=_resolve_monitoring_payloads(readings),
    )


def publish_monitoring_mqtt(readings: list[SensorReading]) -> dict[str, Any]:
    payloads = _resolve_monitoring_payloads(readings)
    if not settings.monitoring_mqtt_enabled or not settings.monitoring_mqtt_host:
        return {
            "mode": "preview",
            "message": "Monitoring MQTT is not configured. Returning resolved payload preview only.",
            "topic": settings.monitoring_mqtt_topic,
            "payloads": [payload.model_dump(mode="json") for payload in payloads],
        }

    import paho.mqtt.client as mqtt

    client_mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if settings.monitoring_mqtt_username:
        client_mqtt.username_pw_set(
            settings.monitoring_mqtt_username,
            settings.monitoring_mqtt_password or None,
        )
    if settings.monitoring_mqtt_tls:
        client_mqtt.tls_set()

    client_mqtt.connect(settings.monitoring_mqtt_host, settings.monitoring_mqtt_port, keepalive=60)
    published: list[dict[str, Any]] = []
    try:
        for payload in payloads:
            message = json.dumps(payload.model_dump(mode="json"))
            info = client_mqtt.publish(settings.monitoring_mqtt_topic, message, qos=1)
            info.wait_for_publish()
            published.append(
                {
                    "topic": settings.monitoring_mqtt_topic,
                    "datastream_id": payload.datastream_id,
                    "published": info.is_published(),
                    "rc": info.rc,
                }
            )
    finally:
        client_mqtt.disconnect()

    return {
        "mode": "live",
        "topic": settings.monitoring_mqtt_topic,
        "published": len(published),
        "payloads": published,
        "skipped": max(len(readings) - len(payloads), 0),
    }