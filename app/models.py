from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class SensorReading(BaseModel):
    sensor_id: str
    sensor_name: str
    observed_property: str
    unit: str
    value: float
    timestamp: datetime
    quality: str = "good"
    location: Literal["tgv", "blijdorp"] = "tgv"
    thing_name: str = "Climate adaptation setup"
    device_eui: str | None = None
    stream_key: str | None = None
    observed_property_name: str | None = None


class ObservationPayload(BaseModel):
    phenomenonTime: datetime
    result: float
    resultQuality: str = "good"
    parameters: dict[str, Any] = Field(default_factory=dict)


class ConnectorPreview(BaseModel):
    connector: str
    mode: str
    observations_endpoint: str | None = None
    readings: list[SensorReading]
    payloads: list[ObservationPayload]


class MonitoringMqttPayload(BaseModel):
    server_url: str
    datastream_id: str
    value: float
    timestamp: datetime
    quality: str = "good"
    metadata: dict[str, Any] = Field(default_factory=dict)


class MonitoringMqttPreview(BaseModel):
    connector: str
    mode: str
    topic: str | None = None
    payloads: list[MonitoringMqttPayload]


class IngestRequest(BaseModel):
    readings: list[SensorReading]


class RegistrationPreview(BaseModel):
    connector: str
    mode: str
    thing: dict[str, Any]
    sensors: list[dict[str, Any]]
    observed_properties: list[dict[str, Any]]
    datastreams: list[dict[str, Any]]
    project: dict[str, Any] | None = None
    endpoints: dict[str, str | None] = Field(default_factory=dict)


class TaskingTaskCreateRequest(BaseModel):
    site_key: Literal["tgv", "blijdorp"]
    capability_key: str
    input: dict[str, Any] = Field(default_factory=dict)
    execution_time: datetime | None = None


class TaskingTaskQuery(BaseModel):
    site_key: Literal["tgv", "blijdorp"]
    capability_key: str | None = None
    top: int = Field(default=20, ge=1, le=200)
