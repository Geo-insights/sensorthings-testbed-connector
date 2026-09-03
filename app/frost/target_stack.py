"""Per-target FROST stack bundling HTTP client, cache, and entity manager.

Each configured FROST server target gets its own stack so that entity IDs,
authentication, and API version can be managed independently.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.frost.cache import EntityCache
from app.frost.entity_manager import EntityManager
from app.frost.http_client import FrostHTTPClient
from app.frost.target import FrostTarget

logger = logging.getLogger(__name__)


def _slug(url: str) -> str:
    """Derive a filesystem-safe slug from a URL."""
    slug = re.sub(r"https?://", "", url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", slug).strip("_")
    return slug[:80] or "default"


@dataclass
class TargetCapabilities:
    """Capabilities discovered from the STA landing page conformance list."""

    json_batch: bool = False
    data_array: bool = False
    mqtt: bool = False
    projects: bool = False
    tasking: bool = False
    conformance: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)


def _parse_capabilities(body: dict[str, Any]) -> TargetCapabilities:
    """Extract capability flags from a STA landing page response."""
    collections: list[str] = []
    conformance: list[str] = []
    if isinstance(body.get("value"), list):
        collections = [
            item.get("name") for item in body["value"]
            if isinstance(item, dict) and item.get("name")
        ]
    server_settings = body.get("serverSettings")
    if isinstance(server_settings, dict) and isinstance(server_settings.get("conformance"), list):
        conformance = [str(item) for item in server_settings["conformance"]]

    def _advertises(token: str) -> bool:
        t = token.lower()
        return any(t in c.lower() for c in conformance) or any(t in n.lower() for n in collections)

    return TargetCapabilities(
        json_batch=_advertises("JsonBatchRequest") or _advertises("batch-request"),
        data_array=_advertises("data-array") or _advertises("dataArray") or _advertises("DataArrayValue"),
        mqtt=_advertises("mqtt"),
        projects=_advertises("Projects"),
        tasking=_advertises("Tasking") or _advertises("Tasks"),
        conformance=conformance,
        collections=collections,
    )


class TargetStack:
    """FROST HTTP + Cache + EntityManager for a single target server."""

    def __init__(self, target: FrostTarget, cache_dir: Path) -> None:
        self.target = target
        self.http = FrostHTTPClient(
            base_urls=[target.url],
            auth_username=target.auth_username,
            auth_password=target.auth_password,
            auth_token=target.auth_token,
        )
        label = target.label or _slug(target.url)
        cache_file = cache_dir / f"entities_{label}.json"
        self.cache = EntityCache(cache_file)
        self.entity_manager = EntityManager(self.http, self.cache)
        self.capabilities = self._discover()

    @property
    def base_url(self) -> str:
        return self.target.url

    @property
    def version(self) -> str:
        return self.target.version

    @property
    def is_v2(self) -> bool:
        return self.target.is_v2

    @property
    def label(self) -> str:
        return self.target.label or _slug(self.target.url)

    def _discover(self) -> TargetCapabilities:
        """Probe the STA landing page for conformance classes.

        Returns an all-False TargetCapabilities on failure so the caller
        falls back to the sequential registration path.
        """
        body = self.http.get_landing_page()
        if body is None:
            logger.info("Could not discover capabilities for %s — assuming none", self.label)
            return TargetCapabilities()
        caps = _parse_capabilities(body)
        logger.info(
            "Discovered capabilities for %s: json_batch=%s data_array=%s (%d conformance classes)",
            self.label, caps.json_batch, caps.data_array, len(caps.conformance),
        )
        return caps
