"""Sensor.Community: one datastream per sensor type, against a real FROST (#39).

A BME280 and a DHT22 at one location both report temperature and humidity at
the same timestamp. Before #39 both were written into the location's
datastreams, two values per datastream and phenomenonTime. With the owner map
the BME280 (better ranked) writes to the location datastreams and the DHT22 to
datastreams of its own Sensor.

Runs on the docker compose FROST of ``tests/integration/conftest.py``::

    python -m pytest tests/integration/test_sc_type_datastreams_frost.py -m integration
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
import requests

from app.services import sensorthings_client as stc
from app.services.sensor_community_source import SensorCommunityPollingSource
from app.services.sensorthings_client import SensorThingsClient
from tests.test_sensorthings_client_routing import _settings

pytestmark = pytest.mark.integration


def _entry(location_id: int, sensor_type: str, values: dict[str, str]) -> dict[str, Any]:
    return {
        "sensor": {"id": 1, "sensor_type": {"name": sensor_type}},
        "location": {"id": location_id, "latitude": "52.01", "longitude": "4.36"},
        "sensordatavalues": [{"value_type": k, "value": v} for k, v in values.items()],
        "timestamp": "2026-11-25 12:00:00",
    }


def _get(url: str, **params: str) -> Any:
    response = requests.get(url, params=params, timeout=30)
    assert response.status_code == 200, response.text
    return response.json()


def test_two_types_at_one_location_write_two_datastreams_without_conflicts(
    frost_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """protects=AC6: registering L with BME280 + DHT22 and pushing one batch gives 2 temperature datastreams
    (location + DHT22) and 0 rows sharing datastream + phenomenonTime with different values;
    fails_when=both types' readings land in one datastream (conflict rows), the DHT22 Sensor/datastreams are not
    registered, or a reading is not pushed;
    no_claim=FROST v2.0 targets, batch push, or history written before #39."""
    location = 6591_000_000 + uuid.uuid4().int % 100_000  # a Thing of its own on a reused server
    v11 = f"{frost_url}/v1.1"
    monkeypatch.setattr(stc, "settings", _settings(tmp_path, sensorthings_base_url=v11, auth_username="", auth_password=""))
    client = SensorThingsClient()
    source = SensorCommunityPollingSource(owners_path=tmp_path / "sc_sensor_owners.json")
    batch = [
        _entry(location, "DHT22", {"temperature": "18.9", "humidity": "70.0"}),
        _entry(location, "BME280", {"temperature": "18.5", "humidity": "72.3", "pressure": "101325.00"}),
    ]

    async def fake_get(url: str, *args: Any, **kwargs: Any) -> Any:
        return batch

    source._client.get = fake_get  # type: ignore[method-assign]
    readings = asyncio.run(source._do_fetch_readings())
    for entity_set in source.entity_sets():  # as app/main.py does before every push
        assert client.register_entity_set(entity_set)["primary_ok"]
    result = client.push_observations(readings, source="sensor_community")

    assert result["sent"] == len(readings) == 5
    (thing,) = _get(f"{v11}/Things", **{"$filter": f"name eq 'GEO_SensorCommunity {location}'"})["value"]
    datastreams = _get(
        f"{v11}/Things({thing['@iot.id']})/Datastreams", **{"$expand": "Sensor($select=name,properties)", "$top": "100"}
    )["value"]
    temperature = {
        ds["name"]: ds for ds in datastreams if (ds.get("properties") or {}).get("observed_property") == "temperature"
    }
    assert set(temperature) == {
        f"GEO_SensorCommunity {location} sensor - Air temperature",
        f"GEO_SensorCommunity {location} DHT22 sensor - Air temperature",
    }
    dht22 = temperature[f"GEO_SensorCommunity {location} DHT22 sensor - Air temperature"]
    assert dht22["properties"]["sensor_id"] == f"sensor_community-{location}-dht22"
    assert dht22["Sensor"]["properties"] == {"sensor_type": "DHT22"}

    values: dict[str, list[float]] = {}
    conflicts: list[tuple[str, str, set[float]]] = []
    for ds in datastreams:
        observations = _get(f"{v11}/Datastreams({ds['@iot.id']})/Observations", **{"$top": "1000"})["value"]
        by_time: dict[str, set[float]] = {}
        for obs in observations:
            by_time.setdefault(obs["phenomenonTime"], set()).add(obs["result"])
            values.setdefault(ds["name"], []).append(obs["result"])
        conflicts += [(ds["name"], time, results) for time, results in by_time.items() if len(results) > 1]
    assert conflicts == []
    assert values[f"GEO_SensorCommunity {location} sensor - Air temperature"] == [18.5]
    assert values[f"GEO_SensorCommunity {location} DHT22 sensor - Air temperature"] == [18.9]
    assert sum(len(v) for v in values.values()) == 5
