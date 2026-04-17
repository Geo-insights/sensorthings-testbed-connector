from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SensorReading(BaseModel):
    sensor_id: str
    sensor_name: str
    observed_property: str
    unit: str
    value: float
    timestamp: datetime
    quality: str = "good"
    thing_name: str = "Wastewater Treatment Demo"


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


class IngestRequest(BaseModel):
    readings: list[SensorReading]


class RegistrationPreview(BaseModel):
    connector: str
    mode: str
    thing: dict[str, Any]
    sensors: list[dict[str, Any]]
    observed_properties: list[dict[str, Any]]
    datastreams: list[dict[str, Any]]
    endpoints: dict[str, str | None] = Field(default_factory=dict)
