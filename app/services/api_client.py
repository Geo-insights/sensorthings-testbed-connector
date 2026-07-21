"""Shared async HTTP client helpers for REST API polling sources."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger("connector.api_client")


class AsyncAPIClient:
    """Thin async HTTP wrapper with retry and timeout."""

    def __init__(
        self,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        max_retries: int = 3,
        verify_ssl: bool = True,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = headers or {}
        self._timeout = timeout
        self._max_retries = max_retries
        self._verify_ssl = verify_ssl

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET request with retries. Returns parsed JSON."""
        url = f"{self._base_url}{path}" if self._base_url else path
        async with httpx.AsyncClient(verify=self._verify_ssl, timeout=self._timeout) as client:
            for attempt in range(1, self._max_retries + 1):
                try:
                    resp = await client.get(url, params=params, headers=self._headers)
                    resp.raise_for_status()
                    return resp.json()
                except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                    if attempt == self._max_retries:
                        raise
                    wait = 2 ** attempt
                    logger.warning("%s GET %s attempt %d failed: %s (retry in %ds)", self.__class__.__name__, path, attempt, exc, wait)
                    import asyncio
                    await asyncio.sleep(wait)
        return None  # unreachable

    async def post(self, path: str, data: dict[str, Any] | str | None = None, json_body: Any = None, params: dict[str, Any] | None = None) -> Any:
        """POST request with retries. Returns parsed JSON."""
        url = f"{self._base_url}{path}" if self._base_url else path
        async with httpx.AsyncClient(verify=self._verify_ssl, timeout=self._timeout) as client:
            for attempt in range(1, self._max_retries + 1):
                try:
                    if json_body is not None:
                        resp = await client.post(url, json=json_body, params=params, headers=self._headers)
                    elif isinstance(data, str):
                        resp = await client.post(url, content=data.encode(), params=params, headers=self._headers)
                    elif isinstance(data, dict):
                        resp = await client.post(url, data=data, params=params, headers=self._headers)
                    else:
                        resp = await client.post(url, params=params, headers=self._headers)
                    resp.raise_for_status()
                    return resp.json()
                except (httpx.HTTPStatusError, httpx.RequestError) as exc:
                    if attempt == self._max_retries:
                        raise
                    wait = 2 ** attempt
                    logger.warning("POST %s attempt %d failed: %s (retry in %ds)", path, attempt, exc, wait)
                    import asyncio
                    await asyncio.sleep(wait)
        return None  # unreachable


class OAuth2ClientCredentials:
    """Manages OAuth2 client credentials token lifecycle."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, scope: str = "") -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._token: str | None = None
        self._expires_at: float = 0

    async def get_token(self) -> str:
        """Return a valid access token, refreshing if expired."""
        if self._token and time.monotonic() < self._expires_at:
            return self._token

        async with httpx.AsyncClient(timeout=15.0) as client:
            form_data = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            }
            if self._scope:
                form_data["scope"] = self._scope

            resp = await client.post(
                self._token_url,
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            data = resp.json()

        self._token = data["access_token"]
        expires_in = int(data.get("expires_in", 1800))
        self._expires_at = time.monotonic() + expires_in - 60  # refresh 60s early
        logger.info("OAuth2 token refreshed (expires_in=%ds)", expires_in)
        return self._token
