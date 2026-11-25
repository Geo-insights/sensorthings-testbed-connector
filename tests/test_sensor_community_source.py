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
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from app.models import SensorReading
from app.services import sensor_community_source as source_module
from app.services.sensor_community_source import SensorCommunityPollingSource
from app.sta.canonical import resolve


@pytest.fixture(autouse=True)
def _owners_file_in_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every source of a test reads and writes its owner map (#39) under tmp_path, never data/."""
    path = tmp_path / "sc_sensor_owners.json"
    monkeypatch.setattr(source_module, "settings", replace(source_module.settings, sensor_community_owners_path=str(path)))
    return path

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
    """Observed properties of the set's location sensor (``sensor_community-{loc}``)."""
    location_sensor_id = f"sensor_community-{entity_set['thing']['properties']['location_id']}"
    (sensor,) = [s for s in entity_set["sensors"] if s["sensor_id"] == location_sensor_id]
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


# ---------------------------------------------------------------------------
# One datastream per sensor type, persisted owner map (#39)
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures"


def _climate(location_id: int, sensor_type: str, temperature: str, humidity: str) -> dict[str, Any]:
    return _entry(location_id, sensor_type, {"temperature": temperature, "humidity": humidity})


def _ids_by_type(readings: list[SensorReading]) -> dict[tuple[str, str], str]:
    """(sensor type, observed property) → sensor_id; the type is the third word of the SC sensor name."""
    return {(r.sensor_name.split(" ")[2], r.observed_property): r.sensor_id for r in readings}


def test_two_types_at_one_location_get_one_datastream_each() -> None:
    """protects=AC1: BME280 + DHT22 at L, both temperature + humidity: BME280 owns both, DHT22 gets L-dht22,
    entity_sets lists 2 sensors and 4 datastream keys, and no key carries two types;
    fails_when=both types share sensor_community-L, the owner depends on entry order, or a reading lacks a datastream;
    no_claim=FROST accepts the registration (see the integration test)."""
    source = SensorCommunityPollingSource()

    readings = _poll(source, [_climate(6591, "DHT22", "18.9", "70.0"), _climate(6591, "BME280", "18.5", "72.3")])

    assert source.owners == {"6591": {"temperature": "BME280", "humidity": "BME280"}}
    assert _ids_by_type(readings) == {
        ("BME280", "temperature"): "sensor_community-6591",
        ("BME280", "humidity"): "sensor_community-6591",
        ("DHT22", "temperature"): "sensor_community-6591-dht22",
        ("DHT22", "humidity"): "sensor_community-6591-dht22",
    }
    (entity_set,) = source.entity_sets()
    assert [s["sensor_id"] for s in entity_set["sensors"]] == ["sensor_community-6591", "sensor_community-6591-dht22"]
    assert _datastream_keys(source) == {
        "sensor_community-6591::temperature",
        "sensor_community-6591::humidity",
        "sensor_community-6591-dht22::temperature",
        "sensor_community-6591-dht22::humidity",
    }
    keys = [f"{r.sensor_id}::{r.observed_property}" for r in readings]
    assert len(keys) == len(set(keys)) == 4  # one value per datastream and timestamp


def test_owner_survives_a_restart_without_flipping() -> None:
    """protects=AC2: a new source instance reads the persisted owners; a first batch with only DHT22 at L
    keeps DHT22 on L-dht22;
    fails_when=owners are not persisted or not loaded, so DHT22 takes over the location datastream after a restart;
    no_claim=behaviour when the owners file is lost (see the missing-file test)."""
    first = SensorCommunityPollingSource()
    _poll(first, [_climate(6591, "BME280", "18.5", "72.3"), _climate(6591, "DHT22", "18.9", "70.0")])

    restarted = SensorCommunityPollingSource()
    readings = _poll(restarted, [_climate(6591, "DHT22", "19.0", "69.0")])

    assert {r.sensor_id for r in readings} == {"sensor_community-6591-dht22"}
    assert restarted.owners == {"6591": {"temperature": "BME280", "humidity": "BME280"}}
    assert "sensor_community-6591-dht22::temperature" in _datastream_keys(restarted)


def test_a_better_ranked_type_appearing_later_does_not_take_over(_owners_file_in_tmp: Path) -> None:
    """protects=AC3: L first seen with only DHT22 (owner DHT22); a BME280 appearing later gets L-bme280,
    DHT22 keeps the location id, and the stored owner does not change;
    fails_when=owners are re-ranked on every batch, so the location datastream switches instruments;
    no_claim=properties no type owned yet (BME280's pressure, owned by BME280 as first reporter)."""
    source = SensorCommunityPollingSource()
    _poll(source, [_climate(6591, "DHT22", "18.9", "70.0")])

    readings = _poll(source, [_climate(6591, "DHT22", "18.8", "70.1"), _bme280(6591)])

    ids = _ids_by_type(readings)
    assert ids[("DHT22", "temperature")] == ids[("DHT22", "humidity")] == "sensor_community-6591"
    assert ids[("BME280", "temperature")] == ids[("BME280", "humidity")] == "sensor_community-6591-bme280"
    assert ids[("BME280", "pressure")] == "sensor_community-6591"
    stored = json.loads(_owners_file_in_tmp.read_text(encoding="utf-8"))
    assert stored == {
        "version": 1,
        "owners": {"6591": {"temperature": "DHT22", "humidity": "DHT22", "pressure": "BME280"}},
    }
    assert {f"{r.sensor_id}::{r.observed_property}" for r in readings} <= _datastream_keys(source)


def test_single_type_locations_of_the_nl_feed_are_unchanged() -> None:
    """protects=AC4: on a recorded NL feed sample (tests/fixtures/sensor_community_nl_sample.json, 2026-11-25),
    every location whose properties each come from one sensor type has exactly the sensor ids, readings, entity
    sets and datastream keys of the connector before #39 (sensor_community_nl_sample_baseline.json, recorded at
    fc7d019); where two types report one property only non-owner readings move, to keys of their own;
    fails_when=the owner map changes ids, names, order or datastreams of single-type locations, or a mixed
    location still writes two types into one datastream;
    no_claim=the sample covers every sensor type combination of the live feed."""
    entries = json.loads((_FIXTURES / "sensor_community_nl_sample.json").read_text(encoding="utf-8"))
    baseline = json.loads((_FIXTURES / "sensor_community_nl_sample_baseline.json").read_text(encoding="utf-8"))
    types_per_pair: dict[tuple[str, str], set[str]] = {}
    for entry in entries:
        for sdv in entry["sensordatavalues"]:
            canonical = resolve(sdv["value_type"])
            if canonical is not None:
                pair = (str(entry["location"]["id"]), canonical.value)
                types_per_pair.setdefault(pair, set()).add(entry["sensor"]["sensor_type"]["name"])
    mixed = {loc for (loc, _prop), types in types_per_pair.items() if len(types) > 1}
    single = {loc for loc, _prop in types_per_pair} - mixed
    assert len(single) >= 15 and len(mixed) >= 5  # the sample covers both kinds

    source = SensorCommunityPollingSource()
    readings = _poll(source, entries)

    fields = baseline["reading_fields"]
    rows = [[r.timestamp.isoformat() if f == "timestamp" else getattr(r, f) for f in fields] for r in readings]
    location_of = fields.index("location")
    sensor_id_of = fields.index("sensor_id")
    for row, before in zip(rows, baseline["readings"], strict=True):
        if row[location_of] in single:
            assert row == before
        else:  # mixed: the same reading; only a non-owner's sensor_id changes, to its type's own id
            assert row[:sensor_id_of] + row[sensor_id_of + 1 :] == before[:sensor_id_of] + before[sensor_id_of + 1 :]
            assert row[sensor_id_of] == before[sensor_id_of] or row[sensor_id_of].startswith(f"{before[sensor_id_of]}-")

    sets = source.entity_sets()
    before_sets = baseline["entity_sets"]
    location_ids = [s["thing"]["properties"]["location_id"] for s in sets]
    assert location_ids == [s["thing"]["properties"]["location_id"] for s in before_sets]
    for entity_set, before_set in zip(sets, before_sets, strict=True):
        if entity_set["thing"]["properties"]["location_id"] in single:
            assert entity_set == before_set
        else:  # the location sensor is today's; type sensors come after it
            assert entity_set["sensors"][0] == before_set["sensors"][0]
            assert len(entity_set["sensors"]) > 1

    def keys_of(entity_sets: list[dict[str, Any]], locations: set[str]) -> set[str]:
        return {
            f"{sensor['sensor_id']}::{prop}"
            for s in entity_sets
            if s["thing"]["properties"]["location_id"] in locations
            for sensor in s["sensors"]
            for prop in sensor["observed_properties"]
        }

    assert keys_of(sets, single) == keys_of(before_sets, single)
    names_per_key_and_time: dict[tuple[str, Any], set[str]] = {}
    for r in readings:
        key = f"{r.sensor_id}::{r.observed_property}"
        assert key in _datastream_keys(source)
        names_per_key_and_time.setdefault((key, r.timestamp), set()).add(r.sensor_name)
    assert all(len(names) == 1 for names in names_per_key_and_time.values())  # one sensor type per datastream


def test_missing_owners_file_starts_empty_with_a_warning(
    _owners_file_in_tmp: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """protects=AC5: no owners file (first deploy) → empty map, a WARNING, polling works and writes the file;
    fails_when=a missing file raises, or is not created once owners are assigned;
    no_claim=owners chosen after a lost file equal the lost ones."""
    with caplog.at_level("WARNING", logger="connector.sensor_community"):
        source = SensorCommunityPollingSource()

    assert source.owners == {}
    assert any("not found" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert _poll(source, [_sds011(93182)])
    assert json.loads(_owners_file_in_tmp.read_text(encoding="utf-8")) == {
        "version": 1,
        "owners": {"93182": {"pm10": "SDS011", "pm2_5": "SDS011"}},
    }


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        '{"version": 2, "owners": {"1": {"pm10": "SDS011"}}}',
        '["SDS011"]',
        '{"version": 1, "owners": []}',
    ],
    ids=["invalid-json", "other-version", "not-an-object", "owners-not-a-map"],
)
def test_corrupt_owners_file_starts_empty_with_a_warning(
    _owners_file_in_tmp: Path, caplog: pytest.LogCaptureFixture, content: str
) -> None:
    """protects=AC5: an unreadable owners file → empty map, WARNING, no crash, the next assignment rewrites it valid;
    fails_when=a corrupt file raises at source init or poll, or a foreign version is trusted;
    no_claim=the corrupt file is kept for inspection."""
    _owners_file_in_tmp.write_text(content, encoding="utf-8")

    with caplog.at_level("WARNING", logger="connector.sensor_community"):
        source = SensorCommunityPollingSource()

    assert source.owners == {}
    assert [r for r in caplog.records if r.levelname == "WARNING"]
    assert _poll(source, [_sds011(5)])
    assert json.loads(_owners_file_in_tmp.read_text(encoding="utf-8")) == {
        "version": 1,
        "owners": {"5": {"pm10": "SDS011", "pm2_5": "SDS011"}},
    }


def test_owners_file_is_written_atomically_and_only_when_an_owner_is_added(
    _owners_file_in_tmp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """protects=AC5: the file is replaced via a temp file in its own directory (os.replace), only when owners
    are added; a failed replace leaves the previous file intact, no temp file behind, and polling working;
    fails_when=the file is written in place (a crash leaves it truncated), rewritten every poll, or a write error
    breaks the poll;
    no_claim=durability of the directory entry across power loss."""
    real_replace = source_module.os.replace
    replaced: list[tuple[Path, Path]] = []

    def spy_replace(src: Any, dst: Any) -> None:
        replaced.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(source_module.os, "replace", spy_replace)
    source = SensorCommunityPollingSource()

    _poll(source, [_sds011(93182), _bme280(93182)])
    _poll(source, [_sds011(93182), _bme280(93182)])  # nothing new: no write

    assert len(replaced) == 1
    ((src, dst),) = replaced
    assert dst == _owners_file_in_tmp and src.parent == _owners_file_in_tmp.parent and src != dst
    before = _owners_file_in_tmp.read_bytes()

    def failing_replace(src: Any, dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(source_module.os, "replace", failing_replace)
    readings = _poll(source, [_sds011(91154)])  # a new owner: a write that fails

    assert readings
    assert source.owners["91154"] == {"pm10": "SDS011", "pm2_5": "SDS011"}  # kept in memory
    assert _owners_file_in_tmp.read_bytes() == before
    assert sorted(p.name for p in _owners_file_in_tmp.parent.iterdir()) == [_owners_file_in_tmp.name]
