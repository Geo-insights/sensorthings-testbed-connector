"""Tests for the MCP Apps extension and caching hints (revision 2026-07-28).

protects: ui:// resource registration, tool->UI binding, cache hint config,
          graceful degradation (structured result unchanged by the extension)
fails_when: UI resource missing/wrong MIME type, _meta.ui.resourceUri dropped,
            cache hints unset or scoped private, a non-cacheable method hinted
no_claim: host-side iframe rendering, postMessage bridge behaviour (browser-only)
"""

from __future__ import annotations

import pytest
from mcp.server.apps import APP_MIME_TYPE, EXTENSION_ID

from mcp_server.apps import HISTORY_CHART_URI, READ_ONLY_ANNOTATIONS, apps
from mcp_server.server import mcp

# -- ui:// resource --------------------------------------------------------


@pytest.mark.asyncio
async def test_ui_resource_registered_with_app_mime_type():
    """protects=ui resource registration; fails_when=missing or wrong MIME; no_claim=rendering"""
    resources = await mcp.list_resources()
    ui = next((r for r in resources if str(r.uri) == HISTORY_CHART_URI), None)
    assert ui is not None, "history chart ui:// resource not registered"
    assert ui.mime_type == APP_MIME_TYPE == "text/html;profile=mcp-app"


@pytest.mark.asyncio
async def test_ui_resource_serves_html():
    """protects=UI bundle contents; fails_when=empty or non-HTML payload; no_claim=JS correctness"""
    contents = await mcp.read_resource(HISTORY_CHART_URI)
    body = "".join(c.content for c in contents if isinstance(c.content, str))
    assert body.lstrip().lower().startswith("<!doctype html>")
    # The bridge the ext-apps spec requires the app to speak.
    assert "ui/initialize" in body
    assert "ui/notifications/tool-result" in body


@pytest.mark.asyncio
async def test_ui_bundle_declares_no_external_origins():
    """protects=deny-all CSP posture; fails_when=a CDN dependency creeps in; no_claim=host CSP enforcement

    The bundle is deliberately dependency-free so the host's default CSP
    (``default-src 'none'``) suffices and no ``csp.resource_domains`` is needed.
    """
    contents = await mcp.read_resource(HISTORY_CHART_URI)
    body = "".join(c.content for c in contents if isinstance(c.content, str))
    assert "<script src=" not in body.lower(), "external script would need a CSP origin"
    assert "cdn." not in body.lower(), "external CDN would need a CSP origin"


# -- tool -> UI binding ----------------------------------------------------


@pytest.mark.asyncio
async def test_history_tool_bound_to_ui_resource():
    """protects=_meta.ui.resourceUri binding; fails_when=binding dropped; no_claim=host honouring it"""
    tools = await mcp.list_tools()
    tool = next((t for t in tools if t.name == "get_readings_history"), None)
    assert tool is not None, "get_readings_history not registered"
    assert tool.meta is not None
    assert tool.meta["ui"]["resourceUri"] == HISTORY_CHART_URI


@pytest.mark.asyncio
async def test_history_tool_keeps_schema_and_annotations():
    """protects=graceful degradation; fails_when=Apps strips outputSchema/annotations; no_claim=none

    An MCP Apps tool must still be a fully usable plain tool for clients that
    did not negotiate the extension (SEP-2133).
    """
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "get_readings_history")
    assert tool.output_schema is not None, "structured output lost"
    assert tool.title == "Get Historical Readings"
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True


@pytest.mark.asyncio
async def test_all_twelve_tools_still_registered():
    """protects=no tool lost to the extension snapshot; fails_when=count regresses; no_claim=none

    MCPServer snapshots extension tool bindings at construction, so a handler
    registered after the server is built is silently dropped. This pins that.
    """
    tools = await mcp.list_tools()
    assert len(tools) == 12, f"expected 12 tools, got {len(tools)}: {[t.name for t in tools]}"


def test_extension_capability_advertised():
    """protects=capability negotiation; fails_when=extension not advertised; no_claim=client support"""
    caps = mcp._lowlevel_server.get_capabilities(
        notification_options=None, experimental_capabilities=None
    )
    assert caps.extensions is not None
    assert EXTENSION_ID in caps.extensions
    assert EXTENSION_ID == "io.modelcontextprotocol/ui"


def test_shared_annotations_are_read_only():
    """protects=annotation posture; fails_when=a write hint appears; no_claim=none"""
    assert READ_ONLY_ANNOTATIONS.read_only_hint is True
    assert READ_ONLY_ANNOTATIONS.destructive_hint is False
    assert READ_ONLY_ANNOTATIONS.idempotent_hint is True


def test_apps_extension_identifier():
    """protects=extension id; fails_when=id drifts from the spec constant; no_claim=none"""
    assert apps.identifier == "io.modelcontextprotocol/ui"


# -- caching hints (SEP-2549) ----------------------------------------------


def test_cache_hints_set_for_discovery_methods():
    """protects=cache hints present; fails_when=hints unset (ttl=0 default); no_claim=client caching"""
    hints = mcp._lowlevel_server.cache_hints
    for method in ("server/discover", "tools/list", "resources/list", "resources/read"):
        assert method in hints, f"{method} has no cache hint (defaults to ttl_ms=0)"
        assert hints[method].ttl_ms > 0, f"{method} ttl_ms must be > 0 to be cacheable"


def test_cache_hints_are_public_scope():
    """protects=shared caching across agents; fails_when=scope private; no_claim=none

    None of the hinted results is user-specific, so "public" is correct and lets
    one agent's fetch serve another. Access control never rests on cacheScope:
    premium gating happens in tools/call, which is not a cacheable method.
    """
    hints = mcp._lowlevel_server.cache_hints
    for method, hint in hints.items():
        assert hint.scope == "public", f"{method} should be public, got {hint.scope}"


def test_tools_call_is_not_cached():
    """protects=live sensor data freshness; fails_when=tools/call gets a hint; no_claim=none

    tools/call is not a cacheable method in the 2026-07-28 revision. Hinting it
    would be rejected by the SDK, and caching readings would serve stale data.
    """
    assert "tools/call" not in mcp._lowlevel_server.cache_hints


def test_static_catalogs_cached_longer_than_readings():
    """protects=TTL ordering; fails_when=resources/read outlives tools/list; no_claim=none"""
    hints = mcp._lowlevel_server.cache_hints
    # Tool/resource lists change only on deploy; resource bodies track upstream data.
    assert hints["tools/list"].ttl_ms >= hints["resources/read"].ttl_ms
