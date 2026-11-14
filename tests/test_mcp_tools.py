"""Tests for MCP server tools.

protects: MCP tool output shapes, parameter resolution, error contracts
fails_when: wrong response structure, missing attribution, bad error shapes
no_claim: FROST query correctness (mocked), real network behavior
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server import tools as tool_impls
from mcp_server.frost_client import CircuitOpen
from mcp_server.schemas import (
    FreshnessResult,
    HistoryResult,
    ParametersResult,
    ReadingsResult,
    SourcesResult,
)

# -- list_sources ----------------------------------------------------------

@pytest.mark.asyncio
async def test_list_sources_groups_by_source():
    """protects=source grouping; fails_when=sources not grouped; no_claim=FROST query (mocked)"""
    mock_client = AsyncMock()
    mock_client.get_things.return_value = {"value": [
            {"name": "Station A", "properties": {"source": "knmi"},
             "Locations": [{"location": {"coordinates": [4.5, 52.0]}}],
             "Datastreams@iot.count": 3},
            {"name": "Station B", "properties": {"source": "knmi"},
             "Locations": [{"location": {"coordinates": [4.6, 52.1]}}],
             "Datastreams@iot.count": 2},
            {"name": "Station C", "properties": {"source": "luchtmeetnet"},
             "Locations": [{"location": {"coordinates": [4.7, 52.2]}}],
             "Datastreams@iot.count": 5},
    ]}

    result = await tool_impls.list_sources(mock_client)
    assert isinstance(result, SourcesResult)
    assert len(result.sources) == 2
    knmi = next(s for s in result.sources if s.name == "knmi")
    assert knmi.station_count == 2
    assert knmi.datastream_count == 5
    assert "KNMI" in knmi.source_attribution


@pytest.mark.asyncio
async def test_list_sources_circuit_open():
    """protects=circuit breaker propagation; fails_when=no exception raised; no_claim=none"""
    mock_client = AsyncMock()
    mock_client.get_things.side_effect = CircuitOpen("test")
    with pytest.raises(CircuitOpen):
        await tool_impls.list_sources(mock_client)


# -- get_parameters --------------------------------------------------------

@pytest.mark.asyncio
async def test_get_parameters_returns_all_canonical():
    """protects=all 35 params returned; fails_when=missing params; no_claim=none (in-memory)"""
    from app.sta.canonical import CanonicalDatastream

    result = await tool_impls.get_parameters()
    assert isinstance(result, ParametersResult)
    assert len(result.parameters) == len(CanonicalDatastream)  # all 35, no dedup
    for param in result.parameters:
        assert param.name
        assert param.display_name
        assert param.unit_display
        assert param.unit_ucum  # never empty
        assert isinstance(param.aliases, list)


@pytest.mark.asyncio
async def test_get_parameters_includes_aliases():
    """protects=alias inclusion; fails_when=pm25 alias missing from PM2.5; no_claim=none"""
    result = await tool_impls.get_parameters()
    pm25 = next((p for p in result.parameters if p.name == "pm2_5"), None)
    assert pm25 is not None
    assert "pm25" in pm25.aliases or "pm2.5" in pm25.aliases


@pytest.mark.asyncio
async def test_get_parameters_ucum_codes():
    """protects=UCUM code correctness; fails_when=°C returned as ucum code; no_claim=none"""
    result = await tool_impls.get_parameters()
    temp = next(p for p in result.parameters if p.name == "temperature")
    assert temp.unit_display == "°C"
    assert temp.unit_ucum == "Cel"
    co2 = next(p for p in result.parameters if p.name == "co2")
    assert co2.unit_ucum == "[ppm]"
    noise = next(p for p in result.parameters if p.name == "noise_laeq")
    assert noise.unit_ucum == "dB"


# -- get_latest_readings ---------------------------------------------------

@pytest.mark.asyncio
async def test_get_latest_readings_invalid_coordinates():
    """protects=coordinate validation; fails_when=no error for lat=999; no_claim=FROST (not called)"""
    mock_client = AsyncMock()
    with pytest.raises(ToolError, match="invalid_coordinates"):
        await tool_impls.get_latest_readings(mock_client, lat=999, lon=0)


@pytest.mark.asyncio
async def test_get_latest_readings_invalid_parameter():
    """protects=parameter validation; fails_when=no error for nonexistent param; no_claim=FROST (not called)"""
    mock_client = AsyncMock()
    with pytest.raises(ToolError, match="invalid_parameter"):
        await tool_impls.get_latest_readings(
            mock_client, lat=51.92, lon=4.47, parameter="nonexistent_metric"
        )


@pytest.mark.asyncio
async def test_get_latest_readings_valid_alias():
    """protects=alias resolution in readings; fails_when=pm25 alias rejected; no_claim=FROST query (mocked)"""
    mock_client = AsyncMock()
    mock_client.get_datastreams.return_value = {"value": []}
    result = await tool_impls.get_latest_readings(
        mock_client, lat=51.92, lon=4.47, parameter="pm25"
    )
    # Should not be an error — "pm25" resolves to PM2_5
    assert isinstance(result, ReadingsResult)
    assert result.readings is not None


@pytest.mark.asyncio
async def test_get_latest_readings_response_shape():
    """protects=response structure; fails_when=missing fields; no_claim=FROST data accuracy (mocked)"""
    mock_client = AsyncMock()
    mock_client.get_datastreams.return_value = {"value": [
        {
            "name": "temperature",
            "ObservedProperty": {"name": "temperature"},
            "unitOfMeasurement": {"symbol": "°C"},
            "Thing": {
                "name": "Test Station",
                "properties": {"source": "knmi"},
                "Locations": [{"location": {"coordinates": [4.47, 51.92]}}],
            },
            "Observations": [
                {"result": 18.5, "phenomenonTime": "2026-11-12T10:00:00Z"}
            ],
        }
    ]}

    result = await tool_impls.get_latest_readings(mock_client, lat=51.92, lon=4.47)
    assert isinstance(result, ReadingsResult)
    assert result.count == 1
    reading = result.readings[0]
    assert reading.parameter == "temperature"
    assert reading.value == 18.5
    assert reading.unit == "°C"
    assert reading.source == "knmi"
    assert "KNMI" in reading.source_attribution


@pytest.mark.asyncio
async def test_get_latest_readings_clamps_limit():
    """protects=limit clamping; fails_when=limit>500 passed to FROST; no_claim=none"""
    mock_client = AsyncMock()
    mock_client.get_datastreams.return_value = {"value": []}
    await tool_impls.get_latest_readings(mock_client, lat=51.92, lon=4.47, limit=9999)
    call_kwargs = mock_client.get_datastreams.call_args
    assert call_kwargs.kwargs.get("top", call_kwargs.args[0] if call_kwargs.args else 500) <= 500


# -- get_data_freshness ----------------------------------------------------

@pytest.mark.asyncio
async def test_get_data_freshness_classifies_status():
    """protects=freshness classification; fails_when=wrong status label; no_claim=time accuracy"""
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    fresh_time = (now - datetime.timedelta(minutes=30)).isoformat()
    stale_time = (now - datetime.timedelta(hours=12)).isoformat()
    offline_time = (now - datetime.timedelta(days=3)).isoformat()

    mock_client = AsyncMock()
    # Step 1: get_things returns sources
    mock_client.get_things.return_value = {"value": [
        {"@iot.id": 1, "properties": {"source": "knmi"}},
        {"@iot.id": 2, "properties": {"source": "bro"}},
        {"@iot.id": 3, "properties": {"source": "bridge"}},
    ]}
    # Step 2: get_observations called per source in sorted order: bridge, bro, knmi
    mock_client.get_observations.side_effect = [
        {"value": [{"phenomenonTime": offline_time}]},   # bridge
        {"value": [{"phenomenonTime": stale_time}]},     # bro
        {"value": [{"phenomenonTime": fresh_time}]},     # knmi
    ]

    result = await tool_impls.get_data_freshness(mock_client)
    assert isinstance(result, FreshnessResult)
    statuses = {r.source: r.status for r in result.sources}
    assert statuses["knmi"] == "fresh"
    assert statuses["bro"] == "stale"
    assert statuses["bridge"] == "offline"


@pytest.mark.asyncio
async def test_get_data_freshness_marks_unreachable():
    """protects=partial failure resilience; fails_when=entire call fails on one source timeout; no_claim=none"""
    import datetime

    now = datetime.datetime.now(datetime.UTC)
    fresh_time = (now - datetime.timedelta(minutes=5)).isoformat()

    mock_client = AsyncMock()
    mock_client.get_things.return_value = {"value": [
        {"@iot.id": 1, "properties": {"source": "knmi"}},
        {"@iot.id": 2, "properties": {"source": "bro"}},
    ]}
    import httpx
    # Sorted order: bro, knmi
    mock_client.get_observations.side_effect = [
        httpx.ReadTimeout("timeout"),                      # bro
        {"value": [{"phenomenonTime": fresh_time}]},       # knmi
    ]

    result = await tool_impls.get_data_freshness(mock_client)
    assert isinstance(result, FreshnessResult)
    assert len(result.sources) == 2
    knmi = next(r for r in result.sources if r.source == "knmi")
    bro = next(r for r in result.sources if r.source == "bro")
    assert knmi.status == "fresh"
    assert bro.status == "unreachable"


# -- get_readings_history --------------------------------------------------

@pytest.mark.asyncio
async def test_get_readings_history_invalid_coordinates():
    """protects=coordinate validation; fails_when=no error for lat=999; no_claim=FROST (not called)"""
    mock_client = AsyncMock()
    with pytest.raises(ToolError, match="invalid_coordinates"):
        await tool_impls.get_readings_history(
            mock_client, lat=999, lon=0, parameter="temperature", start="2026-11-01T00:00:00Z"
        )


@pytest.mark.asyncio
async def test_get_readings_history_invalid_parameter():
    """protects=parameter validation; fails_when=no error for nonexistent param; no_claim=FROST (not called)"""
    mock_client = AsyncMock()
    with pytest.raises(ToolError, match="invalid_parameter"):
        await tool_impls.get_readings_history(
            mock_client, lat=51.92, lon=4.47, parameter="nonexistent_metric",
            start="2026-11-01T00:00:00Z",
        )


@pytest.mark.asyncio
async def test_get_readings_history_invalid_start_time():
    """protects=start time validation; fails_when=no error for bad date; no_claim=none"""
    mock_client = AsyncMock()
    with pytest.raises(ToolError, match="invalid_start_time"):
        await tool_impls.get_readings_history(
            mock_client, lat=51.92, lon=4.47, parameter="temperature",
            start="not-a-date",
        )


@pytest.mark.asyncio
async def test_get_readings_history_time_window_too_large():
    """protects=30-day max window; fails_when=no error for 60-day range; no_claim=none"""
    mock_client = AsyncMock()
    with pytest.raises(ToolError, match="time_window_too_large"):
        await tool_impls.get_readings_history(
            mock_client, lat=51.92, lon=4.47, parameter="temperature",
            start="2026-09-01T00:00:00Z", end="2026-11-01T00:00:00Z",
        )


@pytest.mark.asyncio
async def test_get_readings_history_valid_alias():
    """protects=alias resolution; fails_when=pm25 alias rejected; no_claim=FROST query (mocked)"""
    mock_client = AsyncMock()
    mock_client.get_datastreams.return_value = {"value": []}
    result = await tool_impls.get_readings_history(
        mock_client, lat=51.92, lon=4.47, parameter="pm25",
        start="2026-11-01T00:00:00Z", end="2026-11-02T00:00:00Z",
    )
    assert isinstance(result, HistoryResult)
    assert result.parameter == "pm2_5"


@pytest.mark.asyncio
async def test_get_readings_history_response_shape():
    """protects=response structure; fails_when=missing fields; no_claim=FROST data accuracy (mocked)"""
    mock_client = AsyncMock()
    mock_client.get_datastreams.return_value = {"value": [
        {
            "name": "temperature",
            "ObservedProperty": {"name": "temperature"},
            "unitOfMeasurement": {"symbol": "°C"},
            "Thing": {
                "name": "Test Station",
                "properties": {"source": "knmi"},
                "Locations": [{"location": {"coordinates": [4.47, 51.92]}}],
            },
            "Observations": [
                {"result": 17.0, "phenomenonTime": "2026-11-01T06:00:00Z"},
                {"result": 18.5, "phenomenonTime": "2026-11-01T12:00:00Z"},
                {"result": 16.2, "phenomenonTime": "2026-11-01T18:00:00Z"},
            ],
        }
    ]}

    result = await tool_impls.get_readings_history(
        mock_client, lat=51.92, lon=4.47, parameter="temperature",
        start="2026-11-01T00:00:00Z", end="2026-11-02T00:00:00Z",
    )
    assert isinstance(result, HistoryResult)
    assert result.parameter == "temperature"
    assert result.total_readings == 3
    assert len(result.stations) == 1
    station = result.stations[0]
    assert station.station_name == "Test Station"
    assert station.source == "knmi"
    assert "KNMI" in station.source_attribution
    assert station.lat == 51.92
    assert station.lon == 4.47
    assert len(station.readings) == 3
    assert station.readings[0].value == 17.0
    assert station.readings[0].unit == "°C"


@pytest.mark.asyncio
async def test_get_readings_history_clamps_limit():
    """protects=limit clamping; fails_when=limit>1000 passed to FROST; no_claim=none"""
    mock_client = AsyncMock()
    mock_client.get_datastreams.return_value = {"value": []}
    await tool_impls.get_readings_history(
        mock_client, lat=51.92, lon=4.47, parameter="temperature",
        start="2026-11-01T00:00:00Z", end="2026-11-02T00:00:00Z",
        limit=9999,
    )
    call_kwargs = mock_client.get_datastreams.call_args
    expand_str = call_kwargs.kwargs.get("expand", "")
    # The $top in the Observations expand should be clamped to 1000
    assert "$top=1000)" in expand_str


@pytest.mark.asyncio
async def test_get_readings_history_end_defaults_to_now():
    """protects=end default; fails_when=end not approximately now; no_claim=none"""
    import datetime

    mock_client = AsyncMock()
    mock_client.get_datastreams.return_value = {"value": []}
    result = await tool_impls.get_readings_history(
        mock_client, lat=51.92, lon=4.47, parameter="temperature",
        start="2026-11-13T00:00:00Z",
    )
    assert isinstance(result, HistoryResult)
    # period_end should be close to now
    end_dt = datetime.datetime.fromisoformat(result.period_end.replace("Z", "+00:00"))
    now = datetime.datetime.now(datetime.UTC)
    assert abs((now - end_dt).total_seconds()) < 5
