"""Entity ID cache with JSON file persistence.

Tracks mappings from entity names to FROST @iot.id values so that
repeated registrations can be resolved without querying the server.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_COLLECTIONS = (
    "things",
    "locations",
    "sensors",
    "observed_properties",
    "datastreams",
    "projects",
    "actuators",
    "tasking_capabilities",
)


class EntityCache:
    """In-memory + JSON-persisted cache of entity name → @iot.id mappings."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict[str, str]] = self._load()

    # -- Public API --------------------------------------------------------

    def get(self, collection: str, name: str) -> str | None:
        return self._data.get(collection, {}).get(name)

    def put(self, collection: str, name: str, entity_id: str) -> None:
        self._data.setdefault(collection, {})[name] = entity_id
        self._persist()

    def drop(self, collection: str, name: str) -> None:
        entries = self._data.get(collection, {})
        if name in entries:
            del entries[name]
            self._persist()

    def all(self) -> dict[str, dict[str, str]]:
        return self._data

    def collection(self, name: str) -> dict[str, str]:
        return self._data.get(name, {})

    # -- Persistence -------------------------------------------------------

    def _load(self) -> dict[str, dict[str, str]]:
        default: dict[str, dict[str, str]] = {c: {} for c in _COLLECTIONS}
        if not self._path.exists():
            return default
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        if not isinstance(data, dict):
            return default
        for key in default:
            entries = data.get(key)
            if isinstance(entries, dict):
                default[key] = {str(n): str(i) for n, i in entries.items()}
        return default

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True),
            encoding="utf-8",
        )
