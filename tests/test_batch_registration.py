"""Tests for FROST JSON Batch entity registration and capabilities discovery."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.frost.entity_manager import EntityManager
from app.frost.target_stack import TargetCapabilities, _parse_capabilities


# ---------------------------------------------------------------------------
# TargetCapabilities discovery
# ---------------------------------------------------------------------------

def test_parse_capabilities_frost_v11():
    """Parse a typical FROST v1.1 landing page with JSON Batch support."""
    body = {
        "value": [
            {"name": "Things", "url": "http://frost/v1.1/Things"},
            {"name": "Sensors", "url": "http://frost/v1.1/Sensors"},
            {"name": "Projects", "url": "http://frost/v1.1/Projects"},
        ],
        "serverSettings": {
            "conformance": [
                "http://www.opengis.net/spec/iot_sensing/1.1/req/batch-request/batch-request",
                "http://www.opengis.net/spec/iot_sensing/1.1/req/data-array/data-array",
                "https://fraunhoferiosb.github.io/FROST-Server/extensions/JsonBatchRequest.html",
                "http://www.opengis.net/spec/iot_sensing/1.1/req/create-update-delete",
            ],
        },
    }
    caps = _parse_capabilities(body)
    assert caps.json_batch is True
    assert caps.data_array is True
    assert caps.projects is True
    assert caps.mqtt is False
    assert len(caps.conformance) == 4
    assert "Things" in caps.collections


def test_parse_capabilities_no_batch():
    """Server without batch support gets json_batch=False."""
    body = {
        "value": [{"name": "Things", "url": "http://example/Things"}],
        "serverSettings": {
            "conformance": [
                "http://www.opengis.net/spec/iot_sensing/1.1/req/create-update-delete",
            ],
        },
    }
    caps = _parse_capabilities(body)
    assert caps.json_batch is False
    assert caps.data_array is False


def test_parse_capabilities_empty_body():
    """Empty landing page returns all-False capabilities."""
    caps = _parse_capabilities({})
    assert caps.json_batch is False
    assert caps.data_array is False
    assert caps.conformance == []
    assert caps.collections == []


def test_parse_capabilities_missing_server_settings():
    body = {
        "value": [{"name": "Datastreams", "url": "http://frost/Datastreams"}],
    }
    caps = _parse_capabilities(body)
    assert caps.json_batch is False
    assert caps.conformance == []


# ---------------------------------------------------------------------------
# EntityManager batch operations
# ---------------------------------------------------------------------------

def _make_em() -> tuple[EntityManager, MagicMock]:
    """Create an EntityManager with a mocked HTTP client."""
    http = MagicMock()
    cache = MagicMock()
    cache.get.return_value = None
    em = EntityManager(http, cache)
    return em, http


def test_batch_find_by_name_parses_collection_response():
    em, http = _make_em()
    http.post_batch.return_value = {
        "responses": [
            {
                "id": "thing",
                "status": 200,
                "body": {"value": [{"@iot.id": 42, "name": "My Thing"}]},
            },
            {
                "id": "sensor",
                "status": 200,
                "body": {"value": []},
            },
        ],
    }
    http.extract_iot_id_from_body.return_value = None
    http.extract_first_iot_id.side_effect = lambda body: (
        str(body["value"][0]["@iot.id"]) if body.get("value") else None
    )

    entries = [
        ("thing", "/Things", "My Thing"),
        ("sensor", "/Sensors", "Missing Sensor"),
    ]
    result = em.batch_find_by_name(entries)

    assert result["thing"] == "42"
    assert result["sensor"] is None
    http.post_batch.assert_called_once()


def test_batch_find_by_name_escapes_single_quotes():
    em, http = _make_em()
    http.post_batch.return_value = {"responses": []}

    em.batch_find_by_name([("x", "/Things", "O'Brien's Lab")])

    call_args = http.post_batch.call_args[0][0]
    assert "O''Brien''s Lab" in call_args[0]["url"]


def test_batch_create_updates_cache():
    em, http = _make_em()
    cache = em._cache
    http.post_batch.return_value = {
        "responses": [
            {
                "id": "thing",
                "status": 201,
                "body": {"@iot.id": 99, "name": "New Thing"},
            },
        ],
    }
    http.extract_iot_id_from_body.side_effect = lambda body: (
        str(body["@iot.id"]) if "@iot.id" in body else None
    )
    http.extract_first_iot_id.return_value = None

    entries = [("thing", "/Things", "things", {"name": "New Thing"})]
    result = em.batch_create(entries)

    assert result["thing"] == "99"
    cache.put.assert_called_once_with("things", "New Thing", "99")


def test_batch_find_empty_entries():
    em, http = _make_em()
    result = em.batch_find_by_name([])
    assert result == {}
    http.post_batch.assert_not_called()


def test_batch_create_empty_entries():
    em, http = _make_em()
    result = em.batch_create([])
    assert result == {}
    http.post_batch.assert_not_called()


def test_batch_find_handles_none_response():
    em, http = _make_em()
    http.post_batch.return_value = None

    entries = [("thing", "/Things", "X")]
    result = em.batch_find_by_name(entries)
    assert result["thing"] is None
