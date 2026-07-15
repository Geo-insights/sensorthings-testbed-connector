"""Integration tests: observation push against a real FROST server."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import requests

from app.frost.cache import EntityCache
from app.frost.entity_manager import EntityManager
from app.frost.http_client import FrostHTTPClient


pytestmark = pytest.mark.integration


@pytest.fixture()
def frost_stack(frost_url, tmp_path):
    """Set up a complete FROST entity hierarchy and return (http_client, datastream_id)."""
    http = FrostHTTPClient(
        base_urls=[frost_url],
        auth_user="",
        auth_password="",
        auth_token="",
        timeout=10,
    )
    cache = EntityCache(cache_path=str(tmp_path / "cache.json"))
    em = EntityManager(http=http, cache=cache)

    thing_id = em.get_or_create("things", "Obs Push Thing", {
        "name": "Obs Push Thing",
        "description": "For observation push tests",
    })
    sensor_id = em.get_or_create("sensors", "Obs Push Sensor", {
        "name": "Obs Push Sensor",
        "description": "Test sensor",
        "encodingType": "application/pdf",
        "metadata": "n/a",
    })
    op_id = em.get_or_create("observedproperties", "Obs Push Property", {
        "name": "Obs Push Property",
        "description": "Test property",
        "definition": "https://example.com/test",
    })
    ds_id = em.get_or_create("datastreams", "Obs Push Datastream", {
        "name": "Obs Push Datastream",
        "description": "Test datastream",
        "observationType": "http://www.opengis.net/def/observationType/OGC-OM/2.0/OM_Measurement",
        "unitOfMeasurement": {"name": "Celsius", "symbol": "°C", "definition": "http://unitsofmeasure.org/ucum.html#para-30"},
        "Thing": {"@iot.id": thing_id},
        "Sensor": {"@iot.id": sensor_id},
        "ObservedProperty": {"@iot.id": op_id},
    })

    return http, ds_id


def test_push_single_observation(frost_url, frost_stack):
    """Push one observation and verify it appears on the server."""
    http, ds_id = frost_stack
    now = datetime.now(UTC).isoformat()

    payload = {
        "phenomenonTime": now,
        "resultTime": now,
        "result": 21.5,
        "Datastream": {"@iot.id": ds_id},
    }
    http.post(f"v1.1/Observations", payload)

    # Verify via direct query
    resp = requests.get(
        f"{frost_url}/v1.1/Datastreams({ds_id})/Observations?$count=true",
        timeout=10,
    )
    data = resp.json()
    assert data["@iot.count"] >= 1
    assert any(obs["result"] == 21.5 for obs in data["value"])


def test_push_batch_observations(frost_url, frost_stack):
    """Push a batch of observations and verify count."""
    http, ds_id = frost_stack
    batch_size = 10

    for i in range(batch_size):
        now = datetime.now(UTC).isoformat()
        http.post(f"v1.1/Observations", {
            "phenomenonTime": now,
            "resultTime": now,
            "result": 20.0 + i * 0.1,
            "Datastream": {"@iot.id": ds_id},
        })

    resp = requests.get(
        f"{frost_url}/v1.1/Datastreams({ds_id})/Observations?$count=true",
        timeout=10,
    )
    data = resp.json()
    # At least batch_size observations (may have more from other tests)
    assert data["@iot.count"] >= batch_size


def test_post_with_retry_succeeds(frost_stack):
    """post_with_retry on a healthy server succeeds on first attempt."""
    http, ds_id = frost_stack
    now = datetime.now(UTC).isoformat()

    result = http.post_with_retry(
        path=f"v1.1/Observations",
        payload={
            "phenomenonTime": now,
            "resultTime": now,
            "result": 99.9,
            "Datastream": {"@iot.id": ds_id},
        },
        max_retries=3,
    )
    assert result.status_code == 201
