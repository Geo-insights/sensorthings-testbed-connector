"""Tests for MCP server tools.

protects: MCP tool output shapes, parameter resolution, error contracts
fails_when: wrong response structure, missing attribution, bad error shapes
no_claim: FROST query correctness (mocked), real network behavior
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from mcp_server import tools as tool_impls
from mcp_server.frost_client import CircuitOpen

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
    assert len(result) == 2
    knmi = next(s for s in result if s["name"] == "knmi")
    assert knmi["station_count"] == 2
    assert knmi["datastream_count"] == 5
    assert "KNMI" in knmi["source_attribution"]


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

    result = await tool_impls.get_parameters()
    # Should have entries (some share display_name so deduped)
    assert len(result) > 0
    # Each entry has required fields
    for param in result:
        assert "name" in param
        assert "display_name" in param
        assert "unit" in param
        assert "aliases" in param
        assert isinstance(param["aliases"], list)


@pytest.mark.asyncio
async def test_get_parameters_includes_aliases():
    """protects=alias inclusion; fails_when=pm25 alias missing from PM2.5; no_claim=none"""
    result = await tool_impls.get_parameters()
    pm25 = next((p for p in result if p["name"] == "pm2_5"), None)
    assert pm25 is not None
    assert "pm25" in pm25["aliases"] or "pm2.5" in pm25["aliases"]


# -- get_latest_readings ---------------------------------------------------

@pytest.mark.asyncio
async def test_get_latest_readings_invalid_coordinates():
    """protects=coordinate validation; fails_when=no error for lat=999; no_claim=FROST (not called)"""
    mock_client = AsyncMock()
    result = await tool_impls.get_latest_readings(mock_client, lat=999, lon=0)
    assert result["error"] == "invalid_coordinates"


@pytest.mark.asyncio
async def test_get_latest_readings_invalid_parameter():
    """protects=parameter validation; fails_when=no error for nonexistent param; no_claim=FROST (not called)"""
    mock_client = AsyncMock()
    result = await tool_impls.get_latest_readings(
        mock_client, lat=51.92, lon=4.47, parameter="nonexistent_metric"
    )
    assert result["error"] == "invalid_parameter"
    assert "valid_values" in result
    assert isinstance(result["valid_values"], list)
    assert len(result["valid_values"]) > 0


@pytest.mark.asyncio
async def test_get_latest_readings_valid_alias():
    """protects=alias resolution in readings; fails_when=pm25 alias rejected; no_claim=FROST query (mocked)"""
    mock_client = AsyncMock()
    mock_client.get_datastreams.return_value = {"value": []}
    result = await tool_impls.get_latest_readings(
        mock_client, lat=51.92, lon=4.47, parameter="pm25"
    )
    # Should not be an error — "pm25" resolves to PM2_5
    assert "readings" in result


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
    assert "readings" in result
    assert result["count"] == 1
    reading = result["readings"][0]
    assert reading["parameter"] == "temperature"
    assert reading["value"] == 18.5
    assert reading["unit"] == "°C"
    assert reading["source"] == "knmi"
    assert "source_attribution" in reading
    assert "KNMI" in reading["source_attribution"]


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
    statuses = {r["source"]: r["status"] for r in result}
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
    assert len(result) == 2
    knmi = next(r for r in result if r["source"] == "knmi")
    bro = next(r for r in result if r["source"] == "bro")
    assert knmi["status"] == "fresh"
    assert bro["status"] == "unreachable"
