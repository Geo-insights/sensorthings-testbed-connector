"""Per-target FROST stack bundling HTTP client, cache, and entity manager.

Each configured FROST server target gets its own stack so that entity IDs,
authentication, and API version can be managed independently.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.frost.cache import EntityCache
from app.frost.entity_manager import EntityManager
from app.frost.http_client import FrostHTTPClient
from app.frost.target import FrostTarget


def _slug(url: str) -> str:
    """Derive a filesystem-safe slug from a URL."""
    slug = re.sub(r"https?://", "", url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", slug).strip("_")
    return slug[:80] or "default"


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
