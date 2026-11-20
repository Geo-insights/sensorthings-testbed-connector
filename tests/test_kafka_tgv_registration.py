"""TGV Kafka readings go only to the TGV entity sets registered on the primary FROST target.

Production (2026-11-20): frost.geo-insights.nl had no TGV Things or Datastreams;
nothing registered entity_configs/tgv_*.yaml there after the primary moved off
sta.wbd-rd.nl. The old warm-up's bare property keys sent TGV readings into
Sensor.Community, KNMI, Ohnics and Samen Meten datastreams (about 3.2M rows).
These tests pin:

* coverage: every (sensor_id, observed property) the Kafka path emits has a
  datastream in the TGV entity sets, under the key its readings carry;
* registration: the Kafka loop registers the TGV entity sets before it pushes,
  retries the sets that failed, leaves a batch uncommitted while no set is
  registered, and finds the entities again after a restart instead of creating
  them twice;
* routing: a datastream id kept from the old server (entity cache or
  SENSORTHINGS_DATASTREAM_IDS_JSON) never receives a TGV reading, and the push
  path's property and sensor checks accept the TGV datastreams.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
import yaml

import app.main as main
from app.models import SensorReading
from app.pipeline import kafka_tgv
from app.pipeline.kafka_tgv import (
    TGV_THING_PROPERTIES,
    AvroUnionDeserializer,
    TGVDecapsulator,
    TGVMeasurementParser,
    TGVPrimaryRegistration,
    TGVRegistrationPending,
    entity_set_datastream_keys,
    registration_problem,
    tgv_datastream_key,
    tgv_entity_sets,
)
from app.services import sensorthings_client as stc
from app.services.sensorthings_client import SensorThingsClient
from app.sources.tgv_kafka_mapping import _DEFAULT_DEVICE_MAPPING, avro_batch_to_sensor_readings
from app.sta.canonical import resolve

BASE_URL = "https://frost.test/FROST-Server/v2.0"
DEVICE = "Weather Station"
T0 = datetime(2026, 11, 20, 22, 0, tzinfo=UTC)
ENTITY_CONFIGS = Path(__file__).resolve().parents[1] / "entity_configs"
THING_NAMES = ["Climate Davis", "Hitteplein", "TGV Office Lab", "Weather Climatics"]

# (sensor_id, observed_property, thing_name) of every TGV row on frost.geo-insights.nl,
# from a read-only scan of the PARAMETERS of OBSERVATIONS IDs 1..22,647,473 on
# 2026-11-20 (device_eui 'Weather Station', location 'tgv' throughout).
PRODUCTION_LABELS = {
    ("tgv-officelab-climate", "humidity", "TGV Office Lab"),
    ("tgv-officelab-climate", "temperature", "TGV Office Lab"),
    ("tgv-climate-davis-weather-station", "precipitation", "Climate Davis"),
    ("tgv-climate-davis-weather-station", "wind_speed", "Climate Davis"),
    ("tgv-climate-davis-weather-station", "wind_gust", "Climate Davis"),
    ("tgv-climate-davis-weather-station", "wind_direction", "Climate Davis"),
    ("tgv-climate-davis-weather-station", "air_temperature", "Climate Davis"),
    ("tgv-weather-climatics-rooftop-station", "air_pressure", "Weather Climatics"),
    ("tgv-hitteplein-climate-station", "solar_radiation", "Hitteplein"),
    ("tgv-hitteplein-climate-station", "relative_humidity", "Hitteplein"),
}

# Datastreams 163-184 on frost.geo-insights.nl (read-only query, 2026-11-20): the ids
# the TGV keys had on the old primary name these datastreams of other sources now.
PRODUCTION_DATASTREAMS_163_184: dict[int, tuple[str, str, str]] = {
    163: ("GEO_SensorCommunity 81543 sensor - Air temperature", "sensor_community-81543", "temperature"),
    164: ("GEO_SensorCommunity 81543 sensor - Air pressure", "sensor_community-81543", "pressure"),
    165: ("GEO_SensorCommunity 81543 sensor - Relative humidity", "sensor_community-81543", "humidity"),
    166: ("GEO_KNMI KNMI-52.93-4.78 sensor - Wind direction", "knmi-7", "wind_direction"),
    167: ("GEO_KNMI KNMI-52.93-4.78 sensor - Wind gust speed", "knmi-7", "wind_gust"),
    168: ("GEO_Ohnics de-4073 sensor - PM2.5 concentration", "ohnics-de-4073", "pm25"),
    169: ("GEO_KNMI KNMI-52.93-4.78 sensor - Air pressure", "knmi-7", "air_pressure"),
    170: ("GEO_Ohnics de-4073 sensor - Air temperature", "ohnics-de-4073", "air_temperature"),
    171: ("GEO_SensorCommunity 77815 sensor - Noise level (LAeq)", "sensor_community-77815", "noise_laeq"),
    172: ("GEO_KNMI KNMI-52.93-4.78 sensor - Visibility", "knmi-7", "visibility"),
    173: ("GEO_KNMI KNMI-52.93-4.78 sensor - Solar radiation", "knmi-7", "solar_radiation"),
    174: ("GEO_Ohnics de-4076 sensor - PM2.5 concentration", "ohnics-de-4076", "pm25"),
    175: ("GEO_KNMI KNMI-52.93-4.78 sensor - Precipitation", "knmi-7", "precipitation"),
    176: ("GEO_SensorCommunity 13066 sensor - Air temperature", "sensor_community-13066", "temperature"),
    177: ("GEO_Ohnics de-4076 sensor - Air temperature", "ohnics-de-4076", "air_temperature"),
    178: ("GEO_SensorCommunity 13066 sensor - Relative humidity", "sensor_community-13066", "humidity"),
    179: ("GEO_KNMI KNMI-52.32-4.79 sensor - Air temperature", "knmi-8", "temperature"),
    180: ("GEO_KNMI KNMI-52.32-4.79 sensor - Ground temperature", "knmi-8", "ground_temperature"),
    181: ("GEO_Ohnics de-4097 sensor - PM2.5 concentration", "ohnics-de-4097", "pm25"),
    182: ("GEO_Ohnics de-4097 sensor - Air temperature", "ohnics-de-4097", "air_temperature"),
    183: ("GEO_KNMI KNMI-52.32-4.79 sensor - Relative humidity", "knmi-8", "humidity"),
    184: ("GEO_SensorCommunity 85855 sensor - PM2.5 concentration", "sensor_community-85855", "pm2_5"),
}
STALE_IDS = {str(ds_id) for ds_id in PRODUCTION_DATASTREAMS_163_184}

# A SENSORTHINGS_DATASTREAM_IDS_JSON still pointing TGV keys at old-server ids,
# where possible at a datastream of the same property (only the sensor differs).
STALE_ENV_MAPPING: dict[str, str] = {
    "tgv-officelab-climate::temperature": "163",
    "tgv-officelab-climate::humidity": "165",
    "tgv-officelab-climate::co2": "171",
    "tgv-officelab-climate::pressure": "164",
    "tgv-climate-davis-weather-station::air_temperature": "170",
    "tgv-climate-davis-weather-station::wind_speed": "167",
    "tgv-climate-davis-weather-station::wind_direction": "166",
    "tgv-climate-davis-weather-station::precipitation": "175",
    "tgv-climate-davis-weather-station::wind_gust": "167",
    "tgv-climate-davis-weather-station::dew_point": "176",
    "tgv-climate-davis-weather-station::heat_index": "179",
    "tgv-climate-davis-weather-station::wind_chill": "182",
    "tgv-climate-davis-weather-station::rain_rate": "175",
    "tgv-climate-davis-weather-station::uv_index": "172",
    "tgv-climate-davis-weather-station::wet_bulb_temperature": "177",
    "tgv-climate-davis-weather-station::evapotranspiration": "180",
    "tgv-weather-climatics-rooftop-station::air_pressure": "169",
    "tgv-hitteplein-climate-station::solar_radiation": "173",
    "tgv-hitteplein-climate-station::relative_humidity": "183",
    # bare sensor ids and stream keys, the push path's fallbacks
    "tgv-officelab-climate": "163",
    "tgv-climate-davis-weather-station": "170",
    "tgv-weather-climatics-rooftop-station": "169",
    "tgv-hitteplein-climate-station": "173",
    "temperature": "163",
    "humidity": "165",
    "air_temperature": "177",
    "precipitation": "175",
    "wind_gust": "167",
    "wind_direction": "166",
    "air_pressure": "169",
    "solar_radiation": "173",
    "relative_humidity": "183",
}


# ---------------------------------------------------------------------------
# Synthetic Kafka input: GreenVillageRecords as the Avro deserializer hands them over
# ---------------------------------------------------------------------------


def _measurement_ids(mapping: dict[str, dict[str, str]] | None = None) -> list[str]:
    return [key.split("/", 1)[1] for key in (mapping or _DEFAULT_DEVICE_MAPPING)]


def _record(minutes: int = 0, measurement_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """One Davis console record: every measurement the device mapping knows, at T0 + minutes."""
    ids = list(measurement_ids) if measurement_ids is not None else _measurement_ids()
    return {
        "device_id": DEVICE,
        "timestamp": int((T0 + timedelta(minutes=minutes)).timestamp() * 1000),
        "measurements": [
            {"measurement_id": m, "value": {"float": 10.0 + n}, "unit": None, "measurement_description": None}
            for n, m in enumerate(ids)
        ],
    }


def _readings(*records: dict[str, Any], mapping: dict[str, dict[str, str]] | None = None) -> list[SensorReading]:
    """The SensorReadings the Kafka loop maps the records to."""
    return avro_batch_to_sensor_readings(list(records), override_mapping=dict(mapping or _DEFAULT_DEVICE_MAPPING))


def _key(reading: SensorReading) -> str:
    return f"{reading.sensor_id}::{reading.observed_property}"


def _production_mapping() -> dict[str, dict[str, str]]:
    """The default mapping's entries for the ten streams production emits."""
    wanted = {(sensor, prop) for sensor, prop, _ in PRODUCTION_LABELS}
    return {
        key: entry
        for key, entry in _DEFAULT_DEVICE_MAPPING.items()
        if (entry["sensor_id"], entry["observed_property"]) in wanted
    }


# ---------------------------------------------------------------------------
# Coverage: entity_configs/tgv_*.yaml against what the Kafka path emits
# ---------------------------------------------------------------------------


def test_the_tgv_entity_sets_are_the_four_tgv_yaml_files() -> None:
    files = sorted(ENTITY_CONFIGS.glob("tgv_*.yaml"))
    names = sorted(yaml.safe_load(path.read_text(encoding="utf-8"))["thing"]["name"] for path in files)

    assert len(files) == 4
    assert sorted(_thing(s) for s in tgv_entity_sets()) == names == THING_NAMES


def test_every_tgv_thing_keeps_the_cleanup_contract() -> None:
    """The cleanup deletes TGV-labelled rows in datastreams whose Thing lacks site 'tgv' and source 'kafka'."""
    assert TGV_THING_PROPERTIES == {"site": "tgv", "source": "kafka"}
    for entity_set in tgv_entity_sets():
        properties = entity_set["thing"]["properties"]
        assert properties["site"] == "tgv" and properties["source"] == "kafka", _thing(entity_set)


def test_yaml_observed_properties_are_canonical_keys() -> None:
    """register_entity_set files a datastream under the YAML key; readings carry the canonical key."""
    for entity_set in tgv_entity_sets():
        for sensor in entity_set["sensors"]:
            for observed_property in sensor["observed_properties"]:
                canonical = resolve(observed_property)
                assert canonical is not None and canonical.value == observed_property, observed_property
        assert all(canonical == reported for canonical, reported in entity_set_datastream_keys(entity_set).items())


def test_datastream_keys_are_canonical_whatever_the_yaml_spells() -> None:
    """A YAML alias still routes: the reading's key and the registered key meet at the canonical name."""
    entity_set = {"sensors": [{"sensor_id": "tgv-x", "observed_properties": ["rh", "air_temperature", "made_up"]}]}

    assert tgv_datastream_key("tgv-x", "rh") == "tgv-x::relative_humidity"
    assert entity_set_datastream_keys(entity_set) == {
        "tgv-x::relative_humidity": "tgv-x::rh",
        "tgv-x::air_temperature": "tgv-x::air_temperature",
        "tgv-x::made_up": "tgv-x::made_up",
    }


def test_datastream_names_are_unique() -> None:
    """register_entity_set also finds a datastream by name: two keys with one name would share a datastream."""
    names: list[str] = []
    for entity_set in tgv_entity_sets():
        for sensor in entity_set["sensors"]:
            for observed_property in sensor["observed_properties"]:
                meta = resolve(observed_property).meta  # type: ignore[union-attr]
                names.append(f"{sensor['name']} - {meta.display_name}")
    assert len(names) == len(set(names)) == 19


def test_every_default_mapping_stream_has_a_datastream_in_its_things_entity_set() -> None:
    owner = {key: _thing(s) for s in tgv_entity_sets() for key in entity_set_datastream_keys(s)}
    readings = _readings(_record())

    assert len(readings) == len(_DEFAULT_DEVICE_MAPPING) == 17
    for reading in readings:
        assert _key(reading) in owner, _key(reading)
        assert owner[_key(reading)] == reading.thing_name


def test_the_pipeline_parser_emits_the_same_keys() -> None:
    """app.pipeline's TGVMeasurementParser maps measurements like the Kafka loop's mapper."""
    parser = TGVMeasurementParser(_DEFAULT_DEVICE_MAPPING)
    record = _record()
    parsed = [parser.parse(m) for m in TGVDecapsulator().decapsulate(AvroUnionDeserializer().deserialize(record))]

    assert [(_key(r), r.thing_name, r.stream_key) for r in parsed] == [
        (_key(r), r.thing_name, r.stream_key) for r in _readings(record)
    ]


def test_every_stream_production_emits_has_a_datastream_in_its_things_entity_set() -> None:
    owner = {key: _thing(s) for s in tgv_entity_sets() for key in entity_set_datastream_keys(s)}

    for sensor_id, observed_property, thing_name in PRODUCTION_LABELS:
        assert owner.get(f"{sensor_id}::{observed_property}") == thing_name
    readings = _readings(_record(), mapping=_production_mapping())
    assert {(r.sensor_id, r.observed_property, r.thing_name) for r in readings} == PRODUCTION_LABELS


def _thing(entity_set: dict[str, Any]) -> str:
    return str(entity_set["thing"]["name"])


# ---------------------------------------------------------------------------
# Registration: the parts of SensorThingsClient it uses, scripted
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ScriptedFrost:
    """register_entity_set and friends; each Thing name answers with its scripted outcomes, then "ok"."""

    def __init__(self, base_url: str | None = BASE_URL) -> None:
        self.base_url = base_url
        self.down = False
        self._datastream_ids: dict[str, str] = {}
        self.outcomes: dict[str, list[Any]] = {}
        self.registrations: list[str] = []
        self.things: dict[str, dict[str, Any]] = {}
        self._next_id = 5000

    def _primary_base_url(self) -> str | None:
        return self.base_url

    def _prefixed_name(self, name: str) -> str:
        return f"GEO_{name}"

    def _fetch_entity_by_id(self, path: str, entity_id: str) -> dict[str, Any] | None:
        assert path == "/Things"
        return self.things.get(str(entity_id))

    def _id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def register_entity_set(self, entity_set: dict[str, Any]) -> dict[str, Any]:
        name = _thing(entity_set)
        self.registrations.append(name)
        if self.down:
            raise requests.ConnectionError("FROST down")
        queue = self.outcomes.get(name)
        outcome = queue.pop(0) if queue else "ok"
        if outcome == "raise":
            raise requests.ConnectionError("FROST down")
        if isinstance(outcome, dict):
            return outcome
        thing_id = self._id()
        properties = dict(entity_set["thing"]["properties"])
        if outcome == "no-source":
            properties.pop("source")
        self.things[thing_id] = {"@iot.id": int(thing_id), "properties": properties}
        datastream_ids = {key: self._id() for key in entity_set_datastream_keys(entity_set).values()}
        self._datastream_ids.update(datastream_ids)  # as register_entity_set does
        return {
            "mode": "live",
            "site_key": entity_set["site_key"],
            "primary_ok": True,
            "thing_id": thing_id,
            "thing_status": "created:201",
            "location_id": self._id(),
            "location_status": "created:201",
            "sensor_ids": {s["sensor_id"]: self._id() for s in entity_set["sensors"]},
            "datastream_ids": datastream_ids,
            "datastream_results": [{"datastream_key": k, "status": "created:201"} for k in datastream_ids],
        }


def _set_of(name: str) -> dict[str, Any]:
    return next(s for s in tgv_entity_sets() if _thing(s) == name)


def _keys_of(name: str) -> set[str]:
    return set(entity_set_datastream_keys(_set_of(name)))


def test_the_first_batch_registers_every_tgv_entity_set_before_anything_is_pushed() -> None:
    frost = _ScriptedFrost()
    registration = TGVPrimaryRegistration(client=frost)  # type: ignore[arg-type]
    readings = _readings(_record())

    routable = registration.prepare_push(readings)

    assert sorted(frost.registrations) == THING_NAMES
    assert routable == readings
    routes = registration.datastream_ids()
    assert len(routes) == 19
    assert {_key(r) for r in readings} <= set(routes)
    assert all(frost._datastream_ids[key] == ds_id for key, ds_id in routes.items())


def test_registered_sets_are_not_registered_again() -> None:
    frost = _ScriptedFrost()
    registration = TGVPrimaryRegistration(client=frost)  # type: ignore[arg-type]

    for minutes in (0, 1, 2):
        assert len(registration.prepare_push(_readings(_record(minutes)))) == 17

    assert sorted(frost.registrations) == THING_NAMES


def test_registered_ids_are_pinned_over_any_other_mapping() -> None:
    frost = _ScriptedFrost()
    registration = TGVPrimaryRegistration(client=frost)  # type: ignore[arg-type]
    registration.prepare_push(_readings(_record()))
    routes = registration.datastream_ids()
    frost._datastream_ids.update({key: STALE_ENV_MAPPING.get(key, "163") for key in routes})

    registration.prepare_push(_readings(_record(1)))

    assert {key: frost._datastream_ids[key] for key in routes} == routes


def test_a_failed_set_is_retried_after_its_delay_and_its_readings_wait(caplog) -> None:
    clock = _Clock()
    frost = _ScriptedFrost()
    frost.outcomes["Climate Davis"] = ["raise", "raise"]
    registration = TGVPrimaryRegistration(client=frost, clock=clock)  # type: ignore[arg-type]
    davis = _keys_of("Climate Davis")

    with caplog.at_level(logging.WARNING, logger="connector.events"):
        first = registration.prepare_push(_readings(_record(0)))
        second = registration.prepare_push(_readings(_record(1)))

    assert {_key(r) for r in first} == {_key(r) for r in second} == {_key(r) for r in _readings(_record())} - davis
    assert frost.registrations.count("Climate Davis") == 1  # no new attempt within 30 s
    not_pushed = [r.getMessage() for r in caplog.records if "readings not pushed" in r.getMessage()]
    assert len(not_pushed) == 1  # once per key, then at debug level
    assert "entity set 'Climate Davis' not registered yet" in not_pushed[0]
    assert any("'GEO_Climate Davis' not registered" in r.getMessage() for r in caplog.records)

    clock.advance(30)
    assert {_key(r) for r in registration.prepare_push(_readings(_record(2)))}.isdisjoint(davis)
    assert frost.registrations.count("Climate Davis") == 2  # failed again: next attempt after 60 s

    clock.advance(59)
    registration.prepare_push(_readings(_record(3)))
    assert frost.registrations.count("Climate Davis") == 2

    clock.advance(1)
    assert len(registration.prepare_push(_readings(_record(4)))) == 17
    assert frost.registrations.count("Climate Davis") == 3
    assert frost.registrations.count("Hitteplein") == 1


def test_the_retry_delay_doubles_up_to_five_minutes() -> None:
    clock = _Clock()
    frost = _ScriptedFrost()
    frost.outcomes["Hitteplein"] = ["raise"] * 8
    registration = TGVPrimaryRegistration(client=frost, clock=clock)  # type: ignore[arg-type]
    registration.prepare_push(_readings(_record()))

    delays = []
    for _ in range(7):
        attempts = frost.registrations.count("Hitteplein")
        waited = 0
        while frost.registrations.count("Hitteplein") == attempts:
            clock.advance(10)
            waited += 10
            registration.prepare_push([])
        delays.append(waited)

    assert delays == [30, 60, 120, 240, 300, 300, 300]


def test_a_batch_is_held_while_no_set_is_registered(monkeypatch) -> None:
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "app.services.alerting.send_alert", lambda event, message, **kw: alerts.append((event, kw["level"]))
    )
    clock = _Clock()
    frost = _ScriptedFrost()
    frost.down = True
    registration = TGVPrimaryRegistration(client=frost, clock=clock)  # type: ignore[arg-type]
    readings = _readings(_record())

    with pytest.raises(TGVRegistrationPending) as held:
        registration.prepare_push(readings)
    # Three failures in a row: the target is down, the fourth set waits for the next batch.
    assert frost.registrations == THING_NAMES[:3]
    assert "4 pending" in str(held.value)
    assert frost._datastream_ids == {}

    with pytest.raises(TGVRegistrationPending):
        registration.prepare_push(readings)
    assert frost.registrations == [*THING_NAMES[:3], "Weather Climatics"]  # the deferred set's turn
    with pytest.raises(TGVRegistrationPending):
        registration.prepare_push(readings)  # all four wait for their retry delay
    assert len(frost.registrations) == 4
    assert alerts == [("kafka.tgv_registration", "critical")]  # once per hold

    frost.down = False
    clock.advance(30)
    assert registration.prepare_push(readings) == readings
    assert alerts == [("kafka.tgv_registration", "critical"), ("kafka.tgv_registration", "info")]


def test_nothing_to_hold_without_readings() -> None:
    frost = _ScriptedFrost()
    frost.down = True
    registration = TGVPrimaryRegistration(client=frost)  # type: ignore[arg-type]

    assert registration.prepare_push([]) == []
    assert len(frost.registrations) == 3  # registration is still tried


def test_the_hold_is_not_mistaken_for_a_kafka_auth_failure() -> None:
    frost = _ScriptedFrost(base_url="https://unauthorized.example/authentication/v2.0")
    frost.down = True
    registration = TGVPrimaryRegistration(client=frost)  # type: ignore[arg-type]

    with pytest.raises(TGVRegistrationPending) as held:
        registration.prepare_push(_readings(_record()))

    assert not main._is_kafka_auth_error(held.value)


@pytest.mark.parametrize(
    "outcome",
    [
        "raise",
        "no-source",
        {"mode": "preview"},
        {"mode": "live", "primary_ok": False, "primary_error": "Primary target unreachable (see logs)."},
        "thing-missing",
        "location-missing",
        "sensor-missing",
        "datastream-missing",
        "datastream-absent",
    ],
)
def test_an_incomplete_registration_does_not_count(outcome: Any) -> None:
    frost = _ScriptedFrost()
    if isinstance(outcome, str) and outcome.endswith(("-missing", "-absent")):
        other = _ScriptedFrost()
        complete = other.register_entity_set(_set_of("Hitteplein"))
        frost.things.update(other.things)  # the Thing itself is fine
        if outcome == "thing-missing":
            complete["thing_id"] = None
        elif outcome == "location-missing":
            complete["location_id"] = None
        elif outcome == "sensor-missing":
            complete["sensor_ids"] = {"tgv-hitteplein-climate-station": None}
        elif outcome == "datastream-missing":
            complete["datastream_ids"]["tgv-hitteplein-climate-station::solar_radiation"] = None
        else:  # skipped by register_entity_set: no entry at all
            del complete["datastream_ids"]["tgv-hitteplein-climate-station::solar_radiation"]
        outcome = complete
    frost.outcomes["Hitteplein"] = [outcome]
    registration = TGVPrimaryRegistration(client=frost)  # type: ignore[arg-type]

    routable = registration.prepare_push(_readings(_record()))

    assert not ({_key(r) for r in routable} & _keys_of("Hitteplein"))
    assert not (set(registration.datastream_ids()) & _keys_of("Hitteplein"))
    assert len(routable) == 15


def test_registration_problem_reads_the_thing_back() -> None:
    entity_set = _set_of("Weather Climatics")
    frost = _ScriptedFrost()
    result = frost.register_entity_set(entity_set)
    thing_id = str(result["thing_id"])

    assert registration_problem(entity_set, result, lambda i: frost.things.get(i)) is None
    assert registration_problem(entity_set, result, lambda _id: None) == f"Thing {thing_id} could not be read back"
    wrong = {"properties": {"site": "tgv", "source": "manual"}}
    assert "properties" in str(registration_problem(entity_set, result, lambda _id: wrong))


def test_without_a_frost_target_readings_pass_unchanged() -> None:
    frost = _ScriptedFrost(base_url=None)
    registration = TGVPrimaryRegistration(client=frost)  # type: ignore[arg-type]
    readings = _readings(_record())

    assert registration.prepare_push(readings) is readings
    assert frost.registrations == []


def test_readings_no_tgv_entity_set_defines_are_left_out(caplog) -> None:
    frost = _ScriptedFrost()
    registration = TGVPrimaryRegistration(client=frost)  # type: ignore[arg-type]
    custom = {
        "*/Outside Temperature": _DEFAULT_DEVICE_MAPPING["*/Outside Temperature"],
        "*/Soil Moisture": {"thing_name": "Climate Davis", "sensor_id": "tgv-climate-davis-weather-station", "observed_property": "soil_moisture"},
        "*/Inside Temperature": {"thing_name": "Somewhere", "sensor_id": "tgv-somewhere", "observed_property": "temperature"},
    }

    with caplog.at_level(logging.WARNING, logger="connector.events"):
        routable = registration.prepare_push(_readings(_record(measurement_ids=_measurement_ids(custom)), mapping=custom))

    assert [_key(r) for r in routable] == ["tgv-climate-davis-weather-station::air_temperature"]
    message = next(r.getMessage() for r in caplog.records if "readings not pushed" in r.getMessage())
    assert "2 of 3 readings" in message
    assert "tgv-climate-davis-weather-station::soil_moisture (in no TGV entity set)" in message
    assert "tgv-somewhere::temperature (in no TGV entity set)" in message


def test_a_new_primary_target_gets_the_entity_sets_registered_again() -> None:
    frost = _ScriptedFrost()
    registration = TGVPrimaryRegistration(client=frost)  # type: ignore[arg-type]
    registration.prepare_push(_readings(_record()))
    old_routes = registration.datastream_ids()

    frost.base_url = "https://frost-new.test/FROST-Server/v1.1"
    registration.prepare_push(_readings(_record(1)))

    assert sorted(frost.registrations) == sorted(THING_NAMES * 2)
    assert set(registration.datastream_ids().values()).isdisjoint(old_routes.values())


# ---------------------------------------------------------------------------
# The real client against FROST in memory: idempotency and stale ids
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.headers = headers or {}
        self.text = "" if body is None else json.dumps(body)

    def json(self) -> Any:
        if self._body is None:
            raise ValueError("no body")
        return self._body

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _MemoryFrost:
    """SensorThings v1.1 in memory, served through the client's FrostHTTPClient.get/post.

    Answers ``name eq '...'`` filters, GET by id and POST (201 + Location, as
    FROST does); any other filter finds nothing.
    """

    def __init__(self) -> None:
        self.entities: dict[str, dict[int, dict[str, Any]]] = {}
        self.next_id = 1
        self.created: list[tuple[str, str]] = []  # (collection, name)
        self.down = False
        self.refuse: set[str] = set()  # names whose creation fails with HTTP 500

    def add(self, collection: str, entity: dict[str, Any], entity_id: int | None = None) -> int:
        if entity_id is None:
            entity_id = self.next_id
        self.next_id = max(self.next_id, entity_id + 1)
        self.entities.setdefault(collection, {})[entity_id] = dict(entity)
        return entity_id

    def _split(self, url: str) -> str:
        assert url.startswith(BASE_URL), url
        return url[len(BASE_URL):]

    def get(self, url: str, params: dict[str, str] | None = None) -> _Response:
        if self.down:
            raise requests.ConnectionError("FROST down")
        path = self._split(url)
        match = re.fullmatch(r"/(\w+)\((\d+)\)", path)
        if match:
            entity = self.entities.get(match[1], {}).get(int(match[2]))
            return _Response(200, {"@iot.id": int(match[2]), **entity}) if entity else _Response(404, {"message": "Nothing found."})
        values = [{"@iot.id": i, **e} for i, e in sorted(self.entities.get(path.strip("/"), {}).items())]
        query = params or {}
        if "$filter" in query:
            name = re.fullmatch(r"name eq '(.*)'", query["$filter"])
            values = [v for v in values if name is not None and v.get("name") == name[1]]
        return _Response(200, {"value": values[: int(query.get("$top", len(values) or 1))]})

    def post(self, url: str, payload: dict[str, Any]) -> _Response:
        if self.down:
            raise requests.ConnectionError("FROST down")
        collection = self._split(url).strip("/")
        if payload.get("name") in self.refuse:
            return _Response(500, {"message": "refused"})
        entity_id = self.add(collection, payload)
        self.created.append((collection, str(payload.get("name"))))
        return _Response(201, None, {"Location": f"{BASE_URL}/{collection}({entity_id})"})

    def listing(self) -> dict[str, Any]:
        """Datastreams as the warm-up lists them ($select=id,name,properties, ordered by id)."""
        return {
            "value": [
                {"@iot.id": i, "name": e.get("name"), "properties": e.get("properties", {})}
                for i, e in sorted(self.entities.get("Datastreams", {}).items())
            ]
        }

    def names(self, collection: str) -> list[str]:
        return sorted(str(e.get("name")) for e in self.entities.get(collection, {}).values())

    def datastream(self, ds_id: str) -> dict[str, Any]:
        return self.entities["Datastreams"][int(ds_id)]

    def thing_of(self, ds_id: str) -> dict[str, Any]:
        return self.entities["Things"][int(self.datastream(ds_id)["Thing"]["@iot.id"])]


def _settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        registered_entities_path=str(tmp_path / "registered_entities.json"),
        failed_observations_path=str(tmp_path / "failed_observations.jsonl"),
        datastream_ids={},
        sensorthings_base_url=BASE_URL,
        auth_username="write",
        auth_password="secret",
        auth_token="",
        entity_name_prefix="GEO_",
        connector_name="UrbanAdapt SensorThings Connector",
        things_path="/Things",
        locations_path="/Locations",
        sensors_path="/Sensors",
        observed_properties_path="/ObservedProperties",
        datastreams_path="/Datastreams",
        observations_path="/Observations",
        projects_path="/Projects",
        actuators_path="/Actuators",
        tasking_capabilities_path="/TaskingCapabilities",
        tasks_path="/Tasks",
        default_project_name="GI-Fieldlab",
        default_project_id="",
        default_project_description="",
        default_project_public=True,
        site_project_configs={},
        site_tasking_configs={},
        tasking_allowed_commands=set(),
        request_timeout_seconds=15.0,
        frost_targets=(),
        frost_batch_push_enabled=False,
        frost_v2_batch_push_enabled=False,
        frost_batch_max_observations=500,
        frost_batch_timeout_seconds=60,
        frost_batch_max_concurrency=4,
        frost_cb_failure_threshold=3,
        frost_cb_cooldown_seconds=600,
        failed_dlq_max_bytes=50_000_000,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _World:
    """FROST in memory with production's datastreams 163-184, and connector processes started against it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.monkeypatch = monkeypatch
        self.tmp_path = tmp_path
        self.frost = _MemoryFrost()
        for ds_id, (name, sensor_id, observed_property) in PRODUCTION_DATASTREAMS_163_184.items():
            self.frost.add(
                "Datastreams",
                {"name": name, "properties": {"sensor_id": sensor_id, "observed_property": observed_property, "streamKey": observed_property}},
                ds_id,
            )
        self.frost.next_id = 13618  # production: 13,617 datastreams
        self.posts: list[tuple[str, str]] = []  # (datastream id, reading key) per observation POST

    @property
    def cache_path(self) -> Path:
        return self.tmp_path / "registered_entities.json"

    def start(
        self, *, env: dict[str, str] | None = None, cache: dict[str, str] | None = None, warm_up: bool = True
    ) -> SensorThingsClient:
        """A connector process: settings, client, entity cache, the startup warm-up."""
        if cache is not None:
            self.cache_path.write_text(json.dumps({"datastreams": cache}), encoding="utf-8")
        self.monkeypatch.setattr(stc, "settings", _settings(self.tmp_path, datastream_ids=dict(env or {})))
        client = SensorThingsClient()
        self.monkeypatch.setattr(client._http, "get", self.frost.get)
        self.monkeypatch.setattr(client._http, "post", self.frost.post)

        def warm_up_get(url: str, **_kwargs: Any) -> _Response:
            if not warm_up or self.frost.down:
                raise requests.ConnectionError("FROST unreachable at startup")
            return _Response(200, self.frost.listing())

        self.monkeypatch.setattr(stc.requests, "get", warm_up_get)
        client.warm_datastream_cache()

        def post_observation(endpoint: str, payload: dict[str, Any], datastream_id: Any, base_url: str | None = None) -> Any:
            parameters = payload["parameters"]
            self.posts.append((str(datastream_id), f"{parameters['sensor_id']}::{parameters['observed_property']}"))
            return SimpleNamespace(ok=True, status_code=201, text="created", headers={})

        self.monkeypatch.setattr(client, "_post_observation", post_observation)
        return client

    def push(self, client: SensorThingsClient, registration: TGVPrimaryRegistration, readings: list[SensorReading]) -> dict[str, Any]:
        """What _consume_and_push_once does with a mapped batch."""
        return client.push_observations(registration.prepare_push(readings), source="kafka")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _World:
    return _World(monkeypatch, tmp_path)


def _created(frost: _MemoryFrost) -> Counter[str]:
    return Counter(collection for collection, _ in frost.created)


def _assert_tgv_routing(world: _World, registration: TGVPrimaryRegistration, readings: list[SensorReading]) -> None:
    """Each reading was posted once, to its own registered TGV datastream, and none to an old-server id."""
    routes = registration.datastream_ids()
    assert sorted(world.posts) == sorted((routes[_key(r)], _key(r)) for r in readings)
    for ds_id, key in world.posts:
        assert ds_id not in STALE_IDS
        properties = world.frost.datastream(ds_id)["properties"]
        assert f"{properties['sensor_id']}::{properties['observed_property']}" == key
        assert {k: world.frost.thing_of(ds_id)["properties"].get(k) for k in TGV_THING_PROPERTIES} == TGV_THING_PROPERTIES


def test_registration_creates_the_tgv_entities_once(world: _World) -> None:
    client = world.start()
    registration = TGVPrimaryRegistration(client=client)
    readings = _readings(_record())

    result = world.push(client, registration, readings)

    assert result["total_sent"] == 17
    _assert_tgv_routing(world, registration, readings)
    assert _created(world.frost) == {
        "Projects": 1, "Things": 4, "Locations": 4, "Sensors": 4, "ObservedProperties": 16, "Datastreams": 19,
    }
    assert world.frost.names("Things") == [f"GEO_{name}" for name in THING_NAMES]
    for thing in world.frost.entities["Things"].values():
        assert {k: thing["properties"][k] for k in TGV_THING_PROPERTIES} == TGV_THING_PROPERTIES


@pytest.mark.parametrize("cache", ["kept", "lost"])
def test_a_restart_finds_the_tgv_entities_and_creates_nothing(world: _World, cache: str) -> None:
    first_client = world.start()
    first = TGVPrimaryRegistration(client=first_client)
    world.push(first_client, first, _readings(_record()))
    routes = first.datastream_ids()
    created = list(world.frost.created)
    if cache == "lost":
        world.cache_path.unlink()

    client = world.start()  # the warm-up now lists the TGV datastreams too
    registration = TGVPrimaryRegistration(client=client)
    world.posts.clear()
    readings = _readings(_record(5))
    world.push(client, registration, readings)

    assert world.frost.created == created
    assert registration.datastream_ids() == routes
    _assert_tgv_routing(world, registration, readings)
    # The push path's property and sensor checks know the TGV datastreams and accept them.
    for reading in readings:
        route = client._datastream_routes[routes[_key(reading)]]
        assert route.sensor_id == reading.sensor_id
        assert client._route_refusal(routes[_key(reading)], reading, "key", client._datastream_ids) is None


@pytest.mark.parametrize("warm_up", [True, False], ids=["warm-up-listed", "warm-up-failed"])
@pytest.mark.parametrize("cache", [None, "stale"], ids=["no-cache", "stale-cache"])
def test_old_server_ids_never_receive_a_tgv_reading(world: _World, warm_up: bool, cache: str | None) -> None:
    stale_cache = {k: v for k, v in STALE_ENV_MAPPING.items() if "::" in k} if cache else None
    client = world.start(env=STALE_ENV_MAPPING, cache=stale_cache, warm_up=warm_up)
    if not warm_up:
        # Nothing contradicts the stale mappings: the client alone would use them.
        reading = _readings(_record(measurement_ids=["Inside Temperature"]))[0]
        assert client._resolve_cached_datastream_id(client._datastream_ids, reading)[0] == "163"

    registration = TGVPrimaryRegistration(client=client)
    readings = [*_readings(_record()), *_readings(_record(1), mapping=_production_mapping())]
    result = world.push(client, registration, readings)

    assert result["total_sent"] == 27
    _assert_tgv_routing(world, registration, readings)


@pytest.mark.parametrize("warm_up", [True, False], ids=["warm-up-listed", "warm-up-failed"])
def test_readings_of_a_set_that_failed_to_register_go_nowhere(world: _World, warm_up: bool) -> None:
    client = world.start(env=STALE_ENV_MAPPING, warm_up=warm_up)
    world.frost.refuse.add("GEO_Climate Davis")
    registration = TGVPrimaryRegistration(client=client)
    readings = _readings(_record())

    result = world.push(client, registration, readings)

    davis = [r for r in readings if r.thing_name == "Climate Davis"]
    others = [r for r in readings if r.thing_name != "Climate Davis"]
    assert len(davis) == 12 and result["total_sent"] == 5
    _assert_tgv_routing(world, registration, others)
    if not warm_up:
        # The stale mappings are still there; the readings never reach them.
        assert client._datastream_ids["tgv-climate-davis-weather-station::precipitation"] == "175"


def test_while_frost_is_down_nothing_is_pushed_and_the_batch_is_held(world: _World) -> None:
    world.frost.down = True
    client = world.start(env=STALE_ENV_MAPPING)
    registration = TGVPrimaryRegistration(client=client)

    with pytest.raises(TGVRegistrationPending):
        world.push(client, registration, _readings(_record()))

    assert world.posts == []
    assert world.frost.created == []


@pytest.mark.parametrize("mapping_key", sorted(_DEFAULT_DEVICE_MAPPING))
def test_each_tgv_stream_routes_to_its_own_datastream_only(world: _World, mapping_key: str) -> None:
    """Per Kafka stream: the push path takes its registered datastream and refuses the old-server id."""
    first = TGVPrimaryRegistration(client=world.start())
    first.prepare_push(_readings(_record()))
    routes = first.datastream_ids()
    client = world.start(env=STALE_ENV_MAPPING)  # restart: the warm-up lists both
    TGVPrimaryRegistration(client=client).prepare_push([])

    reading = _readings(_record(measurement_ids=[mapping_key.split("/", 1)[1]]))[0]
    ds_id, refused = client._resolve_cached_datastream_id(client._datastream_ids, reading)

    assert ds_id == routes[_key(reading)]
    assert refused == []
    stale = STALE_ENV_MAPPING[_key(reading)]
    assert client._route_refusal(stale, reading, "key", client._datastream_ids) is not None


# ---------------------------------------------------------------------------
# The Kafka loop: registration before the push, an uncommitted batch while held
# ---------------------------------------------------------------------------


class _FakeKafka:
    """One topic partition: records, the group's committed offset, and consumers reading from it."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records
        self.committed = 0
        self.events: list[Any] = []
        kafka = self

        class Consumer:
            def __init__(self) -> None:
                self.position = kafka.committed

            def connect(self) -> None:
                kafka.events.append("connect")
                self.position = kafka.committed

            def consume_batch(self, max_messages: int = 100, timeout: float = 5.0) -> list[dict[str, Any]]:
                batch = kafka.records[self.position : self.position + max_messages]
                self.position += len(batch)
                return batch

            def commit(self) -> None:
                kafka.committed = self.position
                kafka.events.append(("commit", kafka.committed))

            def close(self) -> None:
                kafka.events.append("close")

        self.Consumer = Consumer


@pytest.fixture
def kafka_cycle(monkeypatch: pytest.MonkeyPatch):
    """_consume_and_push_once with the scripted client registering and push_observations recorded."""
    frost = _ScriptedFrost()
    clock = _Clock()
    pushes: list[list[str]] = []
    registration = TGVPrimaryRegistration(client=frost, clock=clock)  # type: ignore[arg-type]
    monkeypatch.setattr(kafka_tgv, "tgv_primary", registration)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, frost_async_push_enabled=False))

    def push(readings: list[SensorReading], *, source: str) -> dict[str, Any]:
        assert source == "kafka"
        assert set(registration.datastream_ids()) >= {_key(r) for r in readings}  # registered first
        pushes.append(sorted(_key(r) for r in readings))
        return {"total_sent": len(readings)}

    monkeypatch.setattr(main.client, "push_observations", push)
    monkeypatch.setattr("app.services.alerting.send_alert", lambda *_a, **_kw: False)
    return SimpleNamespace(frost=frost, clock=clock, pushes=pushes, registration=registration)


def test_a_kafka_cycle_registers_then_pushes_then_commits(kafka_cycle) -> None:
    kafka = _FakeKafka([_record(0), _record(1)])
    consumer = kafka.Consumer()

    summary = main._consume_and_push_once(consumer)

    assert summary["pulled"] == 2 and summary["mapped"] == 34 and summary["pushed"] == 34
    assert sorted(kafka_cycle.frost.registrations) == THING_NAMES
    assert len(kafka_cycle.pushes) == 1 and len(kafka_cycle.pushes[0]) == 34
    assert kafka.events == [("commit", 2)]


def test_a_kafka_cycle_hands_the_registered_readings_to_the_frost_worker(kafka_cycle, monkeypatch) -> None:
    queued: list[list[SensorReading]] = []
    worker = SimpleNamespace(enqueue=lambda readings, _timeout: queued.append(readings) or True)
    monkeypatch.setattr("app.services.frost_worker.frost_worker", worker)
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, frost_async_push_enabled=True))
    kafka_cycle.frost.outcomes["Hitteplein"] = ["raise"]
    kafka = _FakeKafka([_record(0)])

    summary = main._consume_and_push_once(kafka.Consumer())

    assert summary["pushed"] == "queued"
    assert len(queued) == 1 and len(queued[0]) == 15  # Hitteplein's two streams wait for its registration
    assert kafka.events == [("commit", 1)]


def test_a_held_kafka_cycle_pushes_and_commits_nothing(kafka_cycle) -> None:
    kafka_cycle.frost.down = True
    kafka = _FakeKafka([_record(0)])

    with pytest.raises(TGVRegistrationPending):
        main._consume_and_push_once(kafka.Consumer())

    assert kafka_cycle.pushes == []
    assert kafka.events == []


class _Stop(BaseException):
    """Ends the Kafka loop from inside a test."""


def test_the_kafka_loop_redelivers_a_held_batch_once_frost_is_back(kafka_cycle, monkeypatch) -> None:
    """FROST down at startup: the batch stays uncommitted, a new consumer gets it again, then it is pushed."""
    kafka = _FakeKafka([_record(0), _record(1), _record(2)])
    kafka_cycle.frost.down = True
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        kafka_cycle.clock.advance(seconds)
        kafka_cycle.frost.down = False  # back after the first backoff
        if len(sleeps) >= 3:
            raise _Stop

    health = SimpleNamespace(
        record_source_success=lambda *_a: None,
        record_source_error=lambda *_a: None,
        source_age_seconds=lambda *_a: 0.0,
    )
    monkeypatch.setattr("app.services.kafka_tgv_consumer.KafkaTGVConsumer", kafka.Consumer)
    monkeypatch.setattr("app.services.health_monitor.health_monitor", health)
    monkeypatch.setattr(main, "asyncio", SimpleNamespace(sleep=sleep, wait_for=asyncio.wait_for))
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(main.settings, frost_async_push_enabled=False, kafka_max_lag_seconds=0)
    )

    with pytest.raises(_Stop):
        asyncio.run(main._kafka_ingest_loop())

    # connect, held (closed without commit), reconnect, the same three records pushed and committed
    assert kafka.events[:4] == ["connect", "close", "connect", ("commit", 3)]
    assert [len(push) for push in kafka_cycle.pushes] == [51]
    assert sleeps[0] == 30  # the loop's backoff after a failed cycle
    assert sorted(kafka_cycle.frost.registrations) == sorted([*THING_NAMES[:3], *THING_NAMES])
