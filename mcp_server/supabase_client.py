"""Read-only async PostgREST client for Supabase.

Queries the bgi_catalog and geo_context schemas via the Supabase REST API
with the anon key. Circuit breaker pattern mirrors frost_client.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from mcp_server.config import settings

logger = logging.getLogger(__name__)


class SupabaseCircuitOpen(Exception):
    """Raised when the Supabase circuit breaker is open."""


class SupabaseReadClient:
    """Async read-only PostgREST client for Supabase."""

    def __init__(
        self,
        url: str = settings.supabase_project_url,
        anon_key: str = settings.supabase_anon_key,
        timeout: float = settings.frost_timeout_seconds,
        cb_threshold: int = settings.circuit_breaker_threshold,
        cb_cooldown: float = settings.circuit_breaker_cooldown_seconds,
    ) -> None:
        self._base_url = url.rstrip("/")
        self._rest_url = f"{self._base_url}/rest/v1"
        self._timeout = timeout

        self._client = httpx.AsyncClient(
            headers={
                "apikey": anon_key,
                "Authorization": f"Bearer {anon_key}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout),
        )

        # Circuit breaker state
        self._cb_threshold = cb_threshold
        self._cb_cooldown = cb_cooldown
        self._cb_failures = 0
        self._cb_open_since: float | None = None

    def _check_circuit(self) -> None:
        if self._cb_open_since is None:
            return
        elapsed = time.monotonic() - self._cb_open_since
        if elapsed < self._cb_cooldown:
            raise SupabaseCircuitOpen(
                f"Supabase circuit open ({self._cb_failures} failures, "
                f"{self._cb_cooldown - elapsed:.0f}s until retry)"
            )
        logger.info("Supabase circuit breaker half-open, allowing probe request")

    def _record_success(self) -> None:
        if self._cb_failures > 0:
            logger.info("Supabase probe succeeded, closing circuit breaker")
        self._cb_failures = 0
        self._cb_open_since = None

    def _record_failure(self) -> None:
        self._cb_failures += 1
        if self._cb_failures >= self._cb_threshold:
            self._cb_open_since = time.monotonic()
            logger.warning(
                "Supabase circuit breaker OPEN after %d consecutive failures",
                self._cb_failures,
            )

    async def get(
        self,
        table: str,
        *,
        schema: str = "bgi_catalog",
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """GET rows from a PostgREST table endpoint.

        Returns parsed JSON list. Raises on HTTP or circuit errors.
        """
        self._check_circuit()
        url = f"{self._rest_url}/{table}"
        headers = {"Accept-Profile": schema}
        try:
            resp = await self._client.get(
                url, params=params or {}, headers=headers,
            )
            resp.raise_for_status()
            self._record_success()
            data = resp.json()
            return data if isinstance(data, list) else [data]
        except (httpx.HTTPError, Exception):
            self._record_failure()
            raise

    async def rpc(
        self,
        function_name: str,
        *,
        schema: str = "bgi_catalog",
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Call a Supabase RPC function via PostgREST."""
        self._check_circuit()
        url = f"{self._rest_url}/rpc/{function_name}"
        headers = {"Content-Profile": schema}
        try:
            resp = await self._client.post(
                url, json=params or {}, headers=headers,
            )
            resp.raise_for_status()
            self._record_success()
            return resp.json()
        except (httpx.HTTPError, Exception):
            self._record_failure()
            raise

    async def close(self) -> None:
        await self._client.aclose()
