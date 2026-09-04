"""Tests for the FROST HTTP layer: http_client, entity_manager, and cache."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.exceptions import FrostConnectionError, ObservationUploadError
from app.frost.cache import EntityCache
from app.frost.entity_manager import EntityManager
from app.frost.http_client import FrostHTTPClient


# ---------------------------------------------------------------------------
# EntityCache
# ---------------------------------------------------------------------------


class TestEntityCache:
    def test_put_and_get(self, tmp_path: Path):
        cache = EntityCache(tmp_path / "cache.json")
        cache.put("things", "MyThing", "42")
        assert cache.get("things", "MyThing") == "42"

    def test_get_missing_returns_none(self, tmp_path: Path):
        cache = EntityCache(tmp_path / "cache.json")
        assert cache.get("things", "Unknown") is None

    def test_drop(self, tmp_path: Path):
        cache = EntityCache(tmp_path / "cache.json")
        cache.put("things", "T1", "1")
        cache.drop("things", "T1")
        assert cache.get("things", "T1") is None

    def test_persists_to_file(self, tmp_path: Path):
        path = tmp_path / "cache.json"
        cache = EntityCache(path)
        cache.put("sensors", "S1", "10")

        # Reload from disk
        cache2 = EntityCache(path)
        assert cache2.get("sensors", "S1") == "10"

    def test_loads_existing_file(self, tmp_path: Path):
        path = tmp_path / "cache.json"
        path.write_text(json.dumps({"things": {"T1": "5"}, "sensors": {}}))
        cache = EntityCache(path)
        assert cache.get("things", "T1") == "5"

    def test_handles_corrupt_file(self, tmp_path: Path):
        path = tmp_path / "cache.json"
        path.write_text("not json!")
        cache = EntityCache(path)
        assert cache.get("things", "T1") is None

    def test_collection(self, tmp_path: Path):
        cache = EntityCache(tmp_path / "cache.json")
        cache.put("datastreams", "ds1", "100")
        cache.put("datastreams", "ds2", "101")
        assert cache.collection("datastreams") == {"ds1": "100", "ds2": "101"}


# ---------------------------------------------------------------------------
# FrostHTTPClient
# ---------------------------------------------------------------------------


class TestFrostHTTPClient:
    def test_base_urls(self):
        client = FrostHTTPClient(base_urls=["http://a", "http://b"])
        assert client.base_urls == ["http://a", "http://b"]

    def test_primary_base_url(self):
        client = FrostHTTPClient(base_urls=["http://a", "http://b"])
        assert client.primary_base_url == "http://a"

    def test_primary_base_url_empty(self):
        client = FrostHTTPClient(base_urls=[])
        assert client.primary_base_url is None

    def test_has_targets(self):
        assert FrostHTTPClient(base_urls=["http://a"]).has_targets()
        assert not FrostHTTPClient(base_urls=[]).has_targets()

    def test_endpoint(self):
        client = FrostHTTPClient(base_urls=["http://frost:8080/v1.1"])
        assert client.endpoint("/Things") == "http://frost:8080/v1.1/Things"

    def test_endpoint_with_override(self):
        client = FrostHTTPClient(base_urls=["http://frost:8080/v1.1"])
        assert client.endpoint("/Things", "http://other/v1.1") == "http://other/v1.1/Things"

    def test_endpoint_no_targets(self):
        client = FrostHTTPClient(base_urls=[])
        assert client.endpoint("/Things") is None

    def test_headers_basic_auth(self):
        client = FrostHTTPClient(base_urls=[], auth_username="user", auth_password="pass")
        headers = client.headers()
        assert "Basic" in headers["Authorization"]

    def test_headers_bearer_auth(self):
        client = FrostHTTPClient(base_urls=[], auth_token="tok123")
        headers = client.headers()
        assert headers["Authorization"] == "Bearer tok123"

    def test_headers_no_auth(self):
        client = FrostHTTPClient(base_urls=[])
        headers = client.headers()
        assert "Authorization" not in headers

    def test_extract_iot_id_from_body(self):
        response = MagicMock()
        response.json.return_value = {"@iot.id": 42, "name": "T1"}
        response.headers = {}
        assert FrostHTTPClient.extract_iot_id(response) == "42"

    def test_extract_iot_id_from_location_header(self):
        response = MagicMock()
        response.json.side_effect = ValueError
        response.headers = {"Location": "http://frost/Things(99)"}
        assert FrostHTTPClient.extract_iot_id(response) == "99"

    def test_extract_first_iot_id(self):
        body = {"value": [{"@iot.id": 7}]}
        assert FrostHTTPClient.extract_first_iot_id(body) == "7"

    def test_extract_first_iot_id_empty(self):
        assert FrostHTTPClient.extract_first_iot_id({"value": []}) is None

    def test_post_with_retry_success(self):
        response = MagicMock()
        response.ok = True

        client = FrostHTTPClient(base_urls=["http://frost"])
        with patch.object(client._session, "post", return_value=response) as mock_post:
            result = client.post_with_retry("http://frost/Observations", {}, "ds1")
            assert result.ok
            assert mock_post.call_count == 1

    @patch("app.frost.http_client.requests.post")
    @patch("app.frost.http_client.time.sleep")
    def test_post_with_retry_exhaustion(self, mock_sleep, mock_post):
        import requests as req
        mock_post.side_effect = req.ConnectionError("timeout")

        client = FrostHTTPClient(base_urls=["http://frost"])
        with pytest.raises(ObservationUploadError) as exc_info:
            client.post_with_retry("http://frost/Observations", {}, "ds1", max_attempts=2, initial_wait=0.01)
        assert exc_info.value.datastream_id == "ds1"
        assert exc_info.value.attempts == 2

    def test_get_raises_frost_connection_error(self):
        import requests as req
        client = FrostHTTPClient(base_urls=["http://unreachable"])
        with patch("app.frost.http_client.requests.get", side_effect=req.ConnectionError("refused")):
            with pytest.raises(FrostConnectionError):
                client.get("http://unreachable/Things")


# ---------------------------------------------------------------------------
# EntityManager
# ---------------------------------------------------------------------------


class TestEntityManager:
    def _make_manager(self, tmp_path: Path) -> tuple[EntityManager, FrostHTTPClient, EntityCache]:
        http = FrostHTTPClient(base_urls=["http://frost/v1.1"])
        cache = EntityCache(tmp_path / "cache.json")
        manager = EntityManager(http, cache)
        return manager, http, cache

    def test_get_or_create_returns_cached(self, tmp_path: Path):
        manager, http, cache = self._make_manager(tmp_path)
        cache.put("things", "T1", "5")

        # Mock server confirming name matches
        with patch.object(manager, "fetch_by_id", return_value={"name": "T1"}):
            iot_id, status = manager.get_or_create("/Things", "T1", {"name": "T1"}, "things")
        assert iot_id == "5"
        assert status == "cached"

    def test_get_or_create_drops_stale_cache(self, tmp_path: Path):
        manager, http, cache = self._make_manager(tmp_path)
        cache.put("things", "T1", "5")

        # Server says name is different → stale
        with patch.object(manager, "fetch_by_id", return_value={"name": "Wrong"}):
            with patch.object(manager, "find_by_name", return_value="42"):
                iot_id, status = manager.get_or_create("/Things", "T1", {"name": "T1"}, "things")
        assert iot_id == "42"
        assert status == "existing"
        assert cache.get("things", "T1") == "42"

    def test_get_or_create_creates_new(self, tmp_path: Path):
        manager, http, cache = self._make_manager(tmp_path)

        mock_response = MagicMock()
        mock_response.json.return_value = {"@iot.id": 99}
        mock_response.headers = {}
        mock_response.status_code = 201

        with patch.object(manager, "find_by_name", return_value=None):
            with patch.object(http, "post", return_value=mock_response):
                iot_id, status = manager.get_or_create("/Things", "T1", {"name": "T1"}, "things")
        assert iot_id == "99"
        assert "created" in status
        assert cache.get("things", "T1") == "99"

    def test_find_by_filter(self, tmp_path: Path):
        manager, http, cache = self._make_manager(tmp_path)

        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.json.return_value = {"value": [{"@iot.id": 7}]}

        with patch.object(http, "get", return_value=mock_response):
            result = manager.find_by_filter("http://frost/v1.1/Datastreams", "name eq 'test'")
        assert result == "7"
