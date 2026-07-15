"""Integration tests: entity registration against a real FROST server."""

from __future__ import annotations

import pytest

from app.frost.cache import EntityCache
from app.frost.entity_manager import EntityManager
from app.frost.http_client import FrostHTTPClient


pytestmark = pytest.mark.integration


@pytest.fixture()
def frost_client(frost_url, tmp_path):
    """Create a FrostHTTPClient pointed at the ephemeral FROST instance."""
    return FrostHTTPClient(
        base_urls=[frost_url],
        auth_user="",
        auth_password="",
        auth_token="",
        timeout=10,
    )


@pytest.fixture()
def entity_manager(frost_client, tmp_path):
    cache = EntityCache(cache_path=str(tmp_path / "cache.json"))
    return EntityManager(http=frost_client, cache=cache)


def test_create_thing(entity_manager, frost_client):
    """Create a Thing and verify it exists on the server."""
    thing_id = entity_manager.get_or_create(
        entity_type="things",
        name="Integration Test Thing",
        payload={
            "name": "Integration Test Thing",
            "description": "Created by integration test",
            "properties": {"test": True},
        },
    )
    assert isinstance(thing_id, int)

    # Verify it exists on the server
    resp = frost_client.get(f"v1.1/Things({thing_id})")
    assert resp["name"] == "Integration Test Thing"


def test_idempotent_creation(entity_manager):
    """get_or_create with the same name returns the same ID."""
    payload = {
        "name": "Idempotent Thing",
        "description": "Should only be created once",
    }
    id1 = entity_manager.get_or_create("things", "Idempotent Thing", payload)
    id2 = entity_manager.get_or_create("things", "Idempotent Thing", payload)
    assert id1 == id2


def test_stale_cache_recovery(frost_client, tmp_path):
    """If the cache has a stale ID, get_or_create re-discovers the correct one."""
    cache = EntityCache(cache_path=str(tmp_path / "cache.json"))
    em = EntityManager(http=frost_client, cache=cache)

    # Create a thing
    payload = {
        "name": "Stale Cache Thing",
        "description": "For stale cache test",
    }
    real_id = em.get_or_create("things", "Stale Cache Thing", payload)

    # Poison the cache with a fake ID
    cache.put("things", "Stale Cache Thing", 999999)

    # get_or_create should detect the stale cache and recover
    recovered_id = em.get_or_create("things", "Stale Cache Thing", payload)
    assert recovered_id == real_id


def test_full_entity_set(entity_manager, frost_client):
    """Register a complete entity hierarchy: Thing → Location, Sensor, ObservedProperty, Datastream."""
    thing_id = entity_manager.get_or_create("things", "Full Test Thing", {
        "name": "Full Test Thing",
        "description": "Integration test entity set",
    })

    # Location linked to Thing
    location_id = entity_manager.get_or_create("locations", "Full Test Location", {
        "name": "Full Test Location",
        "description": "Test location",
        "encodingType": "application/geo+json",
        "location": {"type": "Point", "coordinates": [4.3517, 52.0116]},
        "Things": [{"@iot.id": thing_id}],
    })

    sensor_id = entity_manager.get_or_create("sensors", "Full Test Sensor", {
        "name": "Full Test Sensor",
        "description": "Test sensor",
        "encodingType": "application/pdf",
        "metadata": "n/a",
    })

    op_id = entity_manager.get_or_create("observedproperties", "Full Test Property", {
        "name": "Full Test Property",
        "description": "Test observed property",
        "definition": "https://example.com/test",
    })

    ds_id = entity_manager.get_or_create("datastreams", "Full Test Datastream", {
        "name": "Full Test Datastream",
        "description": "Test datastream",
        "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
        "unitOfMeasurement": {"name": "Celsius", "symbol": "°C", "definition": "http://unitsofmeasure.org/ucum.html#para-30"},
        "Thing": {"@iot.id": thing_id},
        "Sensor": {"@iot.id": sensor_id},
        "ObservedProperty": {"@iot.id": op_id},
    })

    # Verify all entities exist
    for entity_type, eid in [("Things", thing_id), ("Locations", location_id),
                              ("Sensors", sensor_id), ("ObservedProperties", op_id),
                              ("Datastreams", ds_id)]:
        resp = frost_client.get(f"v1.1/{entity_type}({eid})")
        assert "@iot.id" in resp or "name" in resp
