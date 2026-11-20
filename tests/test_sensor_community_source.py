"""SensorCommunityPollingSource discovery: one location, several physical sensors.

Sensor.Community returns one entry per physical sensor, and several sensors can
share a ``location.id`` (an SDS011 for PM next to a BME280 for temperature,
humidity and pressure). Every property a location reports must get a datastream
of its own, otherwise its readings have nowhere correct to go. These tests pin
the union of observed properties across the entries of one response and across
polls.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.models import SensorReading
from app.services.sensor_community_source import SensorCommunityPollingSource

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(location_id: int, sensor_type: str, values: dict[str, str], lat: str = "52.01", lon: str = "4.36") -> dict[str, Any]:
    return {
        "id": 1,
        "sensor": {"id": 2, "sensor_type": {"name": sensor_type}},
        "location": {"id": location_id, "latitude": lat, "longitude": lon},
        "sensordatavalues": [{"value_type": k, "value": v} for k, v in values.items()],
        "timestamp": "2026-11-20 12:00:00",
    }


def _sds011(location_id: int) -> dict[str, Any]:
    return _entry(location_id, "SDS011", {"P1": "15.3", "P2": "8.1"})


def _bme280(location_id: int) -> dict[str, Any]:
    return _entry(location_id, "BME280", {"temperature": "18.5", "humidity": "72.3", "pressure": "101325.00"})


def _bmp280(location_id: int) -> dict[str, Any]:
    return _entry(location_id, "BMP280", {"temperature": "18.4", "pressure": "101300.00"})


def _dht22(location_id: int) -> dict[str, Any]:
    return _entry(location_id, "DHT22", {"temperature": "18.9", "humidity": "70.0"})


def _poll(source: SensorCommunityPollingSource, response: Any) -> list[SensorReading]:
    async def fake_get(url: str, *args: Any, **kwargs: Any) -> Any:
        return response

    source._client.get = fake_get
    return asyncio.run(source._do_fetch_readings())


def _sets_by_location(source: SensorCommunityPollingSource) -> dict[str, dict[str, Any]]:
    return {s["thing"]["properties"]["location_id"]: s for s in source.entity_sets()}


def _properties(entity_set: dict[str, Any]) -> list[str]:
    (sensor,) = entity_set["sensors"]
    return list(sensor["observed_properties"])


def _datastream_keys(source: SensorCommunityPollingSource) -> set[str]:
    """``sensor_id::observed_property`` of every datastream the entity sets register."""
    return {
        f"{sensor['sensor_id']}::{prop}"
        for entity_set in source.entity_sets()
        for sensor in entity_set["sensors"]
        for prop in sensor["observed_properties"]
    }


# ---------------------------------------------------------------------------
# Union across the entries of one response
# ---------------------------------------------------------------------------


def test_properties_of_every_entry_at_a_location_are_merged() -> None:
    source = SensorCommunityPollingSource()

    _poll(source, [_sds011(93182), _bme280(93182)])

    sets = _sets_by_location(source)
    assert list(sets) == ["93182"]
    assert _properties(sets["93182"]) == ["pm10", "pm2_5", "temperature", "humidity", "pressure"]
    assert set(sets["93182"]["observed_properties"]) == {"pm10", "pm2_5", "temperature", "humidity", "pressure"}


def test_merge_does_not_depend_on_which_entry_comes_first() -> None:
    pm_first = SensorCommunityPollingSource()
    climate_first = SensorCommunityPollingSource()

    _poll(pm_first, [_sds011(93182), _bmp280(93182), _dht22(93182)])
    _poll(climate_first, [_bmp280(93182), _dht22(93182), _sds011(93182)])

    expected = {"pm10", "pm2_5", "temperature", "humidity", "pressure"}
    assert set(_properties(_sets_by_location(pm_first)["93182"])) == expected
    assert set(_properties(_sets_by_location(climate_first)["93182"])) == expected


def test_every_reading_has_a_datastream_of_its_own_property() -> None:
    """The routing invariant: nothing a location reports lacks a datastream."""
    source = SensorCommunityPollingSource()

    readings = _poll(
        source,
        [_bmp280(93182), _sds011(93182), _dht22(93182), _sds011(91154), _bme280(91154), _sds011(5)],
    )

    assert readings
    assert {f"{r.sensor_id}::{r.observed_property}" for r in readings} <= _datastream_keys(source)


def test_locations_stay_separate() -> None:
    source = SensorCommunityPollingSource()

    _poll(source, [_sds011(93182), _bme280(91154)])

    sets = _sets_by_location(source)
    assert _properties(sets["93182"]) == ["pm10", "pm2_5"]
    assert _properties(sets["91154"]) == ["temperature", "humidity", "pressure"]
    assert sets["93182"]["sensors"][0]["sensor_id"] == "sensor_community-93182"
    assert sets["91154"]["sensors"][0]["sensor_id"] == "sensor_community-91154"


# ---------------------------------------------------------------------------
# Union across polls
# ---------------------------------------------------------------------------


def test_properties_seen_in_a_later_poll_are_added() -> None:
    source = SensorCommunityPollingSource()

    _poll(source, [_bme280(93182)])
    assert _properties(_sets_by_location(source)["93182"]) == ["temperature", "humidity", "pressure"]

    _poll(source, [_sds011(93182)])
    assert _properties(_sets_by_location(source)["93182"]) == ["temperature", "humidity", "pressure", "pm10", "pm2_5"]


def test_a_sensor_missing_from_a_later_poll_keeps_its_properties() -> None:
    source = SensorCommunityPollingSource()

    _poll(source, [_sds011(93182), _bme280(93182)])
    _poll(source, [_sds011(93182)])  # the BME280 skipped a poll

    assert set(_properties(_sets_by_location(source)["93182"])) == {"pm10", "pm2_5", "temperature", "humidity", "pressure"}


def test_repeated_polls_do_not_duplicate_properties() -> None:
    source = SensorCommunityPollingSource()

    for _ in range(3):
        _poll(source, [_sds011(93182), _bme280(93182)])

    props = _properties(_sets_by_location(source)["93182"])
    assert len(props) == len(set(props)) == 5


def test_a_location_first_seen_in_a_later_poll_is_added() -> None:
    source = SensorCommunityPollingSource()

    _poll(source, [_sds011(93182)])
    _poll(source, [_sds011(93182), _dht22(91154)])

    assert set(_sets_by_location(source)) == {"93182", "91154"}


def test_entity_sets_already_returned_are_not_changed_by_later_polls() -> None:
    source = SensorCommunityPollingSource()

    _poll(source, [_sds011(93182)])
    before = source.entity_sets()
    _poll(source, [_bme280(93182)])

    assert _properties(before[0]) == ["pm10", "pm2_5"]
    assert len(_properties(source.entity_sets()[0])) == 5


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_location_without_a_known_property_gets_no_placeholder_datastreams() -> None:
    source = SensorCommunityPollingSource()

    _poll(source, [_entry(777, "Radiation", {"counts_per_minute": "21", "pressure_at_sealevel": "101900"})])
    assert source.entity_sets() == []

    _poll(source, [_sds011(777)])
    assert _properties(_sets_by_location(source)["777"]) == ["pm10", "pm2_5"]


def test_coordinates_come_from_a_later_entry_when_the_first_has_none() -> None:
    source = SensorCommunityPollingSource()

    _poll(source, [_entry(93182, "SDS011", {"P1": "1", "P2": "2"}, lat="", lon=""), _bme280(93182)])

    (entity_set,) = source.entity_sets()
    assert entity_set["location"]["location"]["coordinates"] == [4.36, 52.01]


def test_malformed_entries_are_skipped() -> None:
    source = SensorCommunityPollingSource()

    _poll(
        source,
        [
            "not a dict",
            {"location": None, "sensordatavalues": [{"value_type": "P1", "value": "1"}]},
            {"location": {"latitude": "52"}, "sensordatavalues": [{"value_type": "P1", "value": "1"}]},
            {"location": {"id": 1}, "sensordatavalues": [{"value_type": "counts_per_minute", "value": "3"}]},
            {"location": {"id": 2}, "sensordatavalues": ["x", {"value_type": "P2", "value": "3"}]},
        ],
    )

    assert set(_sets_by_location(source)) == {"2"}
    assert _properties(_sets_by_location(source)["2"]) == ["pm2_5"]


def test_non_list_response_discovers_nothing() -> None:
    source = SensorCommunityPollingSource()

    assert _poll(source, {"error": "rate limited"}) == []
    assert source.entity_sets() == []
