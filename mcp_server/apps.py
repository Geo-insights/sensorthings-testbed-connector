"""MCP Apps extension (``io.modelcontextprotocol/ui``) for Geo-Insights.

Binds tools to interactive ``ui://`` HTML resources that supporting hosts render
in a sandboxed iframe. UI-bound tools still return their normal
``structuredContent``, so clients without MCP Apps support get the same
structured result and the model gets the text summary unchanged — the extension
is purely additive (SEP-2133 requires graceful degradation).

UI-bound tool handlers live here rather than in ``server.py`` because
``MCPServer.__init__`` snapshots an extension's tool bindings: a handler
registered after the server is constructed is silently dropped. Keeping each
handler next to the UI it renders also keeps the pairing obvious.

The UI bundles are dependency-free: hand-rolled inline SVG rather than a CDN
charting library. That keeps the default deny-all CSP intact (no
``csp.resource_domains`` needed), removes a third-party load that could fail
inside the iframe, and avoids pinning an external version.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import httpx
from mcp.server.apps import Apps
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from mcp_server import tools as tool_impls
from mcp_server.errors import classify_frost_error
from mcp_server.frost_client import CircuitOpen
from mcp_server.middleware import request_logger
from mcp_server.schemas import HistoryResult

logger = logging.getLogger(__name__)

_UI_DIR = Path(__file__).parent / "ui"

# ``ui://`` resource URIs. The scheme is mandated by the ext-apps spec; the host
# serves them as ``text/html;profile=mcp-app``.
HISTORY_CHART_URI = "ui://geo-insights/history-chart.html"

# Shared annotations for every Geo-Insights tool: all are read-only queries
# against external data sources. Defined here (rather than in ``server.py``)
# because UI-bound tools are declared in this module and need it at decoration
# time; ``server.py`` imports it for the rest.
READ_ONLY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


def _load(filename: str) -> str:
    """Read a UI bundle from ``mcp_server/ui/``."""
    return (_UI_DIR / filename).read_text(encoding="utf-8")


apps = Apps()

apps.add_html_resource(
    HISTORY_CHART_URI,
    _load("history_chart.html"),
    name="history-chart",
    title="Readings History Chart",
    description=(
        "Interactive time-series chart of environmental readings per station, "
        "with a toggleable legend and hover readout."
    ),
    prefers_border=False,
)


@apps.tool(
    resource_uri=HISTORY_CHART_URI,
    title="Get Historical Readings",
    annotations=READ_ONLY_ANNOTATIONS,
)
async def get_readings_history(
    lat: float,
    lon: float,
    parameter: str,
    start: str,
    end: str | None = None,
    radius_km: float = 10.0,
    limit: int = 100,
) -> HistoryResult:
    """Get historical environmental readings near a location over a time range.

    Use this for trend analysis, comparisons, and time-series queries.
    Unlike get_latest_readings (which returns only the most recent value),
    this returns multiple observations per station within the time window.

    Args:
        lat: Latitude in WGS84 (e.g. 51.92 for Rotterdam)
        lon: Longitude in WGS84 (e.g. 4.47 for Rotterdam)
        parameter: Parameter name or alias (required — e.g. "temperature",
                   "pm25", "no2"). Use get_parameters() for valid names.
        start: Start of time range in ISO 8601 (e.g. "2026-11-01T00:00:00Z")
        end: End of time range in ISO 8601 (default: now)
        radius_km: Search radius in kilometers (default 10, max 50)
        limit: Maximum readings per station (default 100, max 1000)

    Returns stations grouped by location, each with a time-ordered array of
    readings, plus parameter info and the actual time range covered.

    Hosts that support MCP Apps render this as an interactive chart; other
    clients receive the same structured result as plain data.

    Invalid coordinates, unrecognized parameters, or unparseable dates return
    a structured error. Data source failures include cause and retry timing.
    """
    # Imported at call time: ``server.py`` imports this module, so a top-level
    # import would be circular. Mirrors ``freshness.py``.
    from mcp_server.server import _get_client

    start_t = time.monotonic()
    try:
        result = await tool_impls.get_readings_history(
            _get_client(), lat, lon, parameter, start, end, radius_km, limit
        )
        request_logger.log("get_readings_history", int((time.monotonic() - start_t) * 1000), "", "", "ok")
        return result
    except ToolError:
        request_logger.log("get_readings_history", int((time.monotonic() - start_t) * 1000), "", "", "validation_error")
        raise
    except (CircuitOpen, httpx.HTTPError, Exception) as exc:
        request_logger.log("get_readings_history", int((time.monotonic() - start_t) * 1000), "", "", "error")
        raise ToolError(json.dumps(classify_frost_error(exc))) from exc
