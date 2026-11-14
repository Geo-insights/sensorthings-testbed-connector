"""Pydantic output schemas for MCP tools.

Used as return type annotations so the MCP SDK auto-generates ``outputSchema``
in tool definitions and returns ``structuredContent`` alongside text content.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# -- list_sources ----------------------------------------------------------


class SourceInfo(BaseModel):
    name: str
    station_count: int
    datastream_count: int
    source_attribution: str


class SourcesResult(BaseModel):
    sources: list[SourceInfo]


# -- get_parameters --------------------------------------------------------


class ParameterInfo(BaseModel):
    name: str
    display_name: str
    unit_display: str
    unit_ucum: str
    definition_url: str
    aliases: list[str]


class ParametersResult(BaseModel):
    parameters: list[ParameterInfo]


# -- get_latest_readings ---------------------------------------------------


class Reading(BaseModel):
    parameter: str
    value: float | int | str | None = None
    unit: str
    timestamp: str | None = None
    station_name: str
    lat: float | None = None
    lon: float | None = None
    source: str
    source_attribution: str


class ReadingsResult(BaseModel):
    readings: list[Reading]
    count: int


# -- get_data_freshness ----------------------------------------------------


class FreshnessEntry(BaseModel):
    source: str
    source_attribution: str
    latest_observation: str | None = None
    age_seconds: int | None = None
    status: str = Field(description="One of: fresh (<1h), stale (<24h), offline (>24h), no_data, unreachable")
    error: str | None = None


class FreshnessResult(BaseModel):
    sources: list[FreshnessEntry]


# -- get_readings_history --------------------------------------------------


class HistoryReading(BaseModel):
    timestamp: str
    value: float | int | str | None = None
    unit: str


class StationHistory(BaseModel):
    station_name: str
    source: str
    source_attribution: str
    lat: float | None = None
    lon: float | None = None
    readings: list[HistoryReading]


class HistoryResult(BaseModel):
    stations: list[StationHistory]
    parameter: str
    period_start: str
    period_end: str
    total_readings: int
