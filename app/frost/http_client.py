"""FROST server HTTP client.

Handles authentication, URL building, retries, and raw HTTP
operations against a FROST SensorThings API endpoint.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any

import requests

from app.exceptions import FrostConnectionError, FrostRequestError, ObservationUploadError

logger = logging.getLogger(__name__)


class FrostHTTPClient:
    """Low-level HTTP client for FROST server interactions."""

    def __init__(
        self,
        base_urls: list[str],
        auth_username: str = "",
        auth_password: str = "",
        auth_token: str = "",
        timeout: float = 15.0,
    ) -> None:
        self._base_urls = [url.rstrip("/") for url in base_urls if url.strip()]
        self._auth_username = auth_username
        self._auth_password = auth_password
        self._auth_token = auth_token
        self._timeout = timeout
        # Reuse TCP connections (TLS handshake, keep-alive) across requests
        self._session = requests.Session()
        self._session.headers.update(self.headers())

    # -- URL helpers -------------------------------------------------------

    @property
    def base_urls(self) -> list[str]:
        return list(self._base_urls)

    @property
    def primary_base_url(self) -> str | None:
        return self._base_urls[0] if self._base_urls else None

    def has_targets(self) -> bool:
        return bool(self._base_urls)

    def endpoint(self, path: str, base_url: str | None = None) -> str | None:
        base = base_url or self.primary_base_url
        if not base:
            return None
        return f"{base.rstrip('/')}{path}"

    # -- Auth --------------------------------------------------------------

    def headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._auth_username:
            credentials = f"{self._auth_username}:{self._auth_password}"
            encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
            h["Authorization"] = f"Basic {encoded}"
        elif self._auth_token:
            h["Authorization"] = f"Bearer {self._auth_token}"
        return h

    # -- Core HTTP methods -------------------------------------------------

    def get(self, url: str, params: dict[str, str] | None = None) -> requests.Response:
        try:
            return self._session.get(url, params=params, timeout=self._timeout)
        except requests.RequestException as exc:
            raise FrostConnectionError(url, cause=exc) from exc

    def post(self, url: str, payload: dict[str, Any]) -> requests.Response:
        try:
            return self._session.post(url, json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            raise FrostConnectionError(url, cause=exc) from exc

    def patch(self, url: str, payload: dict[str, Any]) -> requests.Response:
        try:
            return self._session.patch(url, json=payload, timeout=self._timeout)
        except requests.RequestException as exc:
            raise FrostConnectionError(url, cause=exc) from exc

    # -- Response helpers --------------------------------------------------

    @staticmethod
    def extract_iot_id(response: requests.Response) -> str | None:
        """Extract @iot.id from a FROST response (body or Location header)."""
        try:
            body = response.json()
            if isinstance(body, dict):
                if body.get("@iot.id") is not None:
                    return str(body["@iot.id"])
                if body.get("id") is not None:
                    return str(body["id"])
        except ValueError:
            pass
        location = response.headers.get("Location", "")
        match = re.search(r"\(([^)]+)\)$", location)
        if match:
            return match.group(1).strip("'")
        return None

    @staticmethod
    def extract_first_iot_id(body: Any) -> str | None:
        """Extract the first @iot.id from a collection response."""
        if isinstance(body, dict):
            if body.get("@iot.id") is not None:
                return str(body["@iot.id"])
            value = body.get("value")
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, dict):
                    if first.get("@iot.id") is not None:
                        return str(first["@iot.id"])
                    if first.get("id") is not None:
                        return str(first["id"])
        return None

    # -- Retry logic -------------------------------------------------------

    def post_with_retry(
        self,
        url: str,
        payload: dict[str, Any],
        datastream_id: str,
        max_attempts: int = 3,
        initial_wait: float = 2.0,
    ) -> requests.Response:
        """POST with exponential-backoff retry.  Raises ObservationUploadError on exhaustion."""
        current_wait = initial_wait
        last_response: requests.Response | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = self._session.post(url, json=payload, timeout=self._timeout)
                last_response = response
                if response.ok:
                    return response
                logger.warning(
                    "Observation push failed",
                    extra={"datastream_id": datastream_id, "status_code": response.status_code, "attempt": attempt},
                )
            except requests.RequestException as exc:
                logger.warning(
                    "Observation push raised an exception",
                    extra={
                        "datastream_id": datastream_id,
                        "status_code": getattr(getattr(exc, "response", None), "status_code", None),
                        "attempt": attempt,
                    },
                )
                last_response = None

            if attempt < max_attempts:
                time.sleep(current_wait)
                current_wait *= 2

        if last_response is None:
            raise ObservationUploadError(
                datastream_id=datastream_id,
                attempts=max_attempts,
                last_error="All retry attempts failed without a response.",
            )
        return last_response
