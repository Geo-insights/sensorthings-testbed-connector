"""Read-only async FROST OData client for the MCP server.

Queries Things, Datastreams, and Observations via the SensorThings v1.1 API.
Includes a circuit breaker that trips after consecutive failures to avoid
hammering an unreachable FROST instance.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from mcp_server.config import settings

logger = logging.getLogger(__name__)


class CircuitOpen(Exception):
    """Raised when the circuit breaker is open (FROST unreachable)."""


class FrostReadClient:
    """Async read-only client for FROST SensorThings OData API."""

    def __init__(
        self,
        base_url: str = settings.frost_base_url,
        username: str = settings.frost_auth_username,
        password: str = settings.frost_auth_password,
        timeout: float = settings.frost_timeout_seconds,
        cb_threshold: int = settings.circuit_breaker_threshold,
        cb_cooldown: float = settings.circuit_breaker_cooldown_seconds,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

        headers: dict[str, str] = {"Accept": "application/json"}
        if username:
            cred = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {cred}"

        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout),
        )

        # Circuit breaker state
        self._cb_threshold = cb_threshold
        self._cb_cooldown = cb_cooldown
        self._cb_failures = 0
        self._cb_open_since: float | None = None

    # -- Circuit breaker ---------------------------------------------------

    def _check_circuit(self) -> None:
        if self._cb_open_since is None:
            return
        elapsed = time.monotonic() - self._cb_open_since
        if elapsed < self._cb_cooldown:
            raise CircuitOpen(
                f"FROST circuit open ({self._cb_failures} failures, "
                f"{self._cb_cooldown - elapsed:.0f}s until retry)"
            )
        # Half-open: allow one attempt
        logger.info("Circuit breaker half-open, allowing probe request")

    def _record_success(self) -> None:
        if self._cb_failures > 0:
            logger.info("FROST probe succeeded, closing circuit breaker")
        self._cb_failures = 0
        self._cb_open_since = None

    def _record_failure(self) -> None:
        self._cb_failures += 1
        if self._cb_failures >= self._cb_threshold:
            self._cb_open_since = time.monotonic()
            logger.warning(
                "Circuit breaker OPEN after %d consecutive failures", self._cb_failures
            )

    # -- OData queries -----------------------------------------------------

    async def get(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """GET a FROST OData endpoint. Returns parsed JSON dict.

        Raises CircuitOpen if the breaker is tripped, or httpx errors on failure.
        """
        self._check_circuit()
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            self._record_success()
            return resp.json()  # type: ignore[no-any-return]
        except (httpx.HTTPError, Exception):
            self._record_failure()
            raise

    async def get_things(
        self,
        top: int = 1000,
        skip: int = 0,
        expand: str | None = None,
        filter_: str | None = None,
        count: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, str] = {"$top": str(top), "$skip": str(skip)}
        if expand:
            params["$expand"] = expand
        if filter_:
            params["$filter"] = filter_
        if count:
            params["$count"] = "true"
        return await self.get("Things", params)

    async def get_datastreams(
        self,
        top: int = 1000,
        skip: int = 0,
        expand: str | None = None,
        filter_: str | None = None,
        count: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, str] = {"$top": str(top), "$skip": str(skip)}
        if expand:
            params["$expand"] = expand
        if filter_:
            params["$filter"] = filter_
        if count:
            params["$count"] = "true"
        return await self.get("Datastreams", params)

    async def get_observations(
        self,
        top: int = 100,
        skip: int = 0,
        expand: str | None = None,
        filter_: str | None = None,
        orderby: str | None = None,
        count: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, str] = {"$top": str(top), "$skip": str(skip)}
        if expand:
            params["$expand"] = expand
        if filter_:
            params["$filter"] = filter_
        if orderby:
            params["$orderby"] = orderby
        if count:
            params["$count"] = "true"
        return await self.get("Observations", params)

    async def close(self) -> None:
        await self._client.aclose()
