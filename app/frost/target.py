"""FROST server target descriptor.

Each target represents a single SensorThings API server endpoint
with its own version, authentication, and human-readable label.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FrostTarget:
    """A single FROST server target with version and auth metadata."""

    url: str
    version: str = "v1.1"
    auth_username: str = ""
    auth_password: str = ""
    auth_token: str = ""
    label: str = ""

    @property
    def is_v2(self) -> bool:
        return self.version.startswith("v2") or self.version.startswith("2")
