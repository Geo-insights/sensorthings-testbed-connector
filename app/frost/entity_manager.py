"""Entity CRUD operations against a FROST server.

Provides idempotent get-or-create semantics with cache integration.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.frost.cache import EntityCache
from app.frost.http_client import FrostHTTPClient

logger = logging.getLogger(__name__)

# Cached entity IDs are assumed valid for this many seconds without re-checking
# against the FROST server. Avoids an HTTP GET per entity on every registration.
_VALIDATION_TTL_SECONDS = 600  # 10 minutes


class EntityManager:
    """Idempotent entity CRUD using FrostHTTPClient + EntityCache."""

    def __init__(self, http: FrostHTTPClient, cache: EntityCache) -> None:
        self._http = http
        self._cache = cache
        # In-memory only: tracks when each (collection, name) was last validated
        self._validated_at: dict[str, float] = {}

    # -- Lookup ------------------------------------------------------------

    def fetch_by_id(self, path: str, entity_id: str) -> dict[str, Any] | None:
        """GET a single entity by its @iot.id. Returns None on any error."""
        endpoint = self._http.endpoint(f"{path}({entity_id})")
        if not endpoint:
            return None
        try:
            response = self._http.get(endpoint)
        except Exception:
            return None
        if not response.ok:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def find_by_name(self, path: str, name: str) -> str | None:
        """Search for an entity by name via OData $filter. Returns @iot.id or None."""
        endpoint = self._http.endpoint(path)
        if not endpoint:
            return None
        try:
            response = self._http.get(endpoint, params={"$filter": f"name eq '{name}'", "$top": "1"})
        except Exception:
            return None
        if not response.ok:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return self._http.extract_first_iot_id(body)

    def cached_id_matches(
        self, path: str, entity_id: str, expected_name: str | None, cache_key: str = ""
    ) -> bool:
        """Verify that a cached entity ID still maps to the expected name on the server.

        Skips the HTTP check when the entry was validated less than
        ``_VALIDATION_TTL_SECONDS`` ago (avoids ~30 redundant GETs per cycle).
        """
        if not expected_name:
            return True
        # Fast path: recently validated
        vkey = f"{path}:{cache_key or entity_id}"
        last = self._validated_at.get(vkey)
        if last is not None and (time.monotonic() - last) < _VALIDATION_TTL_SECONDS:
            return True
        # Slow path: actually check the server
        entity = self.fetch_by_id(path, entity_id)
        if not entity:
            self._validated_at.pop(vkey, None)
            return False
        if str(entity.get("name", "")).strip() != expected_name.strip():
            self._validated_at.pop(vkey, None)
            return False
        self._validated_at[vkey] = time.monotonic()
        return True

    # -- Idempotent create -------------------------------------------------

    def get_or_create(
        self,
        path: str,
        name: str,
        payload: dict[str, Any],
        collection: str,
    ) -> tuple[str | None, str]:
        """Get existing or create new entity. Returns (iot_id, status_label).

        Status labels: "cached", "existing", "created:<status_code>"
        """
        # 1. Check cache
        cached_id = self._cache.get(collection, name)
        if cached_id:
            expected_name = str(payload.get("name", "")).strip() or None
            if self.cached_id_matches(path, cached_id, expected_name, cache_key=name):
                return cached_id, "cached"
            self._cache.drop(collection, name)

        # 2. Search server by cache key
        existing_id = self.find_by_name(path, name)
        if existing_id:
            self._cache.put(collection, name, existing_id)
            return existing_id, "existing"

        # 2b. If the cache key differs from the payload name (e.g. datastream
        #     keys like "sensor_id::property"), also search by the actual entity
        #     name so we don't create duplicates when the local cache is empty.
        payload_name = str(payload.get("name", "")).strip()
        if payload_name and payload_name != name:
            existing_id = self.find_by_name(path, payload_name)
            if existing_id:
                self._cache.put(collection, name, existing_id)
                return existing_id, "existing"

        # 3. Create new
        endpoint = self._http.endpoint(path)
        if not endpoint:
            return None, "no_endpoint"
        try:
            response = self._http.post(endpoint, payload)
        except Exception as exc:
            logger.warning("Failed to create entity %s at %s: %s", name, path, exc)
            return None, f"error:{exc}"
        entity_id = self._http.extract_iot_id(response)
        if entity_id:
            self._cache.put(collection, name, entity_id)
        return entity_id, f"created:{response.status_code}"

    # -- Batch operations (FROST JSON Batch) --------------------------------

    def batch_find_by_name(
        self,
        entries: list[tuple[str, str, str]],
    ) -> dict[str, str | None]:
        """Find multiple entities by name in a single ``/$batch`` request.

        *entries* is a list of ``(ref_id, path, name)`` tuples.

        Returns a dict of ``ref_id → @iot.id`` for found entities. Missing
        entities map to ``None``.
        """
        if not entries:
            return {}

        requests_payload: list[dict[str, Any]] = []
        for ref_id, path, name in entries:
            safe_name = name.replace("'", "''")
            requests_payload.append({
                "id": ref_id,
                "method": "get",
                "url": f"{path.lstrip('/')}?$filter=name eq '{safe_name}'&$top=1",
            })

        body = self._http.post_batch(requests_payload)
        return self._extract_batch_ids(entries, body)

    def batch_create(
        self,
        entries: list[tuple[str, str, str, dict[str, Any]]],
    ) -> dict[str, str | None]:
        """Create multiple entities in a single ``/$batch`` request.

        *entries* is a list of ``(ref_id, path, collection, payload)`` tuples.
        Payloads may use ``$ref_id`` back-references for linked entity IDs.

        Returns a dict of ``ref_id → @iot.id`` for created entities. Updates
        the local cache on success.
        """
        if not entries:
            return {}

        requests_payload: list[dict[str, Any]] = []
        for ref_id, path, _collection, payload in entries:
            requests_payload.append({
                "id": ref_id,
                "method": "post",
                "url": path.lstrip("/"),
                "body": payload,
            })

        body = self._http.post_batch(requests_payload)
        result = self._extract_batch_ids(
            [(ref_id, path, "") for ref_id, path, _, _ in entries],
            body,
        )

        # Update cache for successfully created entities
        for ref_id, _path, collection, payload in entries:
            iot_id = result.get(ref_id)
            if iot_id:
                cache_key = str(payload.get("name", ref_id))
                self._cache.put(collection, cache_key, iot_id)

        return result

    def _extract_batch_ids(
        self,
        entries: list[tuple[str, str, str]],
        body: dict[str, Any] | None,
    ) -> dict[str, str | None]:
        """Parse a batch response body and extract @iot.id per ref_id."""
        result: dict[str, str | None] = {ref_id: None for ref_id, _, _ in entries}
        if not body or "responses" not in body:
            return result

        for resp in body["responses"]:
            ref_id = resp.get("id", "")
            status = resp.get("status", 0)
            resp_body = resp.get("body")
            if not isinstance(resp_body, dict):
                continue

            iot_id: str | None = None
            if 200 <= status < 300:
                # POST → direct @iot.id; GET collection → first item's @iot.id
                iot_id = self._http.extract_iot_id_from_body(resp_body)
                if iot_id is None:
                    iot_id = self._http.extract_first_iot_id(resp_body)

            if iot_id and ref_id in result:
                result[ref_id] = iot_id

        return result

    # -- OData filter queries ----------------------------------------------

    def find_by_filter(self, endpoint: str, odata_filter: str) -> str | None:
        """Query a collection endpoint with an OData $filter, return first @iot.id."""
        try:
            response = self._http.get(endpoint, params={"$filter": odata_filter, "$top": "1"})
        except Exception:
            return None
        if not response.ok:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return self._http.extract_first_iot_id(body)
