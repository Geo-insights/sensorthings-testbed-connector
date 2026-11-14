"""Tests for MCP server resources.

protects: MCP resource registration and read handlers
fails_when: resources not registered, wrong data shape, missing fields
no_claim: FROST query correctness (mocked), real network behavior
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

# -- Resource registration ---------------------------------------------------

def test_sources_resource_registered():
    """protects=resource registration; fails_when=sensorthings://sources not in resource list"""
    from mcp_server.server import mcp

    # The MCPServer stores resources internally; check via list_resources handler
    assert hasattr(mcp, "_resource_manager")
    manager = mcp._resource_manager
    # Static resources are stored by URI string
    assert "sensorthings://sources" in manager._resources


def test_parameters_resource_registered():
    """protects=resource registration; fails_when=sensorthings://parameters not in resource list"""
    from mcp_server.server import mcp

    manager = mcp._resource_manager
    assert "sensorthings://parameters" in manager._resources


# -- Resource read handlers --------------------------------------------------

@pytest.mark.asyncio
async def test_sources_resource_returns_json():
    """protects=sources resource output shape; fails_when=not valid JSON array"""
    from mcp_server.server import sources_resource

    mock_client = AsyncMock()
    mock_client.get_things.return_value = {"value": [
        {"name": "Station A", "properties": {"source": "knmi"},
         "Locations": [{"location": {"coordinates": [4.5, 52.0]}}],
         "Datastreams@iot.count": 3},
    ]}

    with patch("mcp_server.server._get_client", return_value=mock_client):
        result = await sources_resource()

    parsed = json.loads(result)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "knmi"
    assert parsed[0]["station_count"] == 1
    assert "source_attribution" in parsed[0]


@pytest.mark.asyncio
async def test_parameters_resource_returns_json():
    """protects=parameters resource output shape; fails_when=not valid JSON array with all params"""
    from mcp_server.server import parameters_resource

    result = await parameters_resource()
    parsed = json.loads(result)
    assert isinstance(parsed, list)
    assert len(parsed) >= 35  # all canonical parameters
    for param in parsed:
        assert "name" in param
        assert "display_name" in param
        assert "unit_ucum" in param
        assert "aliases" in param


# -- Tools still exist (backwards compat) -----------------------------------

def test_tools_still_registered():
    """protects=backwards compat; fails_when=tools removed; no_claim=tool behavior (tested elsewhere)"""
    from mcp_server.server import mcp

    tool_names = {t.name for t in mcp._tool_manager.list_tools()}
    assert "list_sources" in tool_names
    assert "get_parameters" in tool_names
    assert "get_latest_readings" in tool_names
    assert "get_data_freshness" in tool_names
