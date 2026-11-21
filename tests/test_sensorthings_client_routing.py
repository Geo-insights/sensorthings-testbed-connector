"""Push routing: a reading only ever goes to a datastream of its own observed property.

Production (2026-11-20): Sensor.Community 'Air pressure' datastreams held PM10,
PM2.5 and humidity readings, some from other locations. Two routes led there:

* the bare ``sensor_id`` fallback in push_observations took the first
  datastream the warm-up listed for the sensor, whatever it observed;
* cached ``sensor_id::observed_property`` entries carried over from the
  previous primary FROST server kept integer ids that exist on the new server
  as unrelated datastreams, and the warm-up only purged ids that don't exist.

These tests pin the guards: the warm-up purges mappings that contradict the
listed datastream's properties and only adds unambiguous fallback keys, and the
push path refuses any mapping to a datastream of another property or sensor.
Fallbacks that cannot cross properties keep working.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from app.models import SensorReading
from app.services import sensorthings_client as stc
from app.services.sensor_community_source import SensorCommunityPollingSource
from app.services.sensorthings_client import SensorThingsClient
from app.sources.sensor_community import build_entity_set, parse_sensor_data

BASE_URL = "https://example.test/v1.1"


def _settings(tmp_path: Any, **overrides: Any) -> SimpleNamespace:
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
        default_project_name="",
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


def _reading(sensor_id: str, observed_property: str, *, stream_key: str | None = None) -> SensorReading:
    return SensorReading(
        sensor_id=sensor_id,
        sensor_name=f"{sensor_id} sensor",
        observed_property=observed_property,
        unit="1",
        value=1.0,
        timestamp="2026-11-20T12:00:00Z",
        thing_name=sensor_id,
        stream_key=stream_key,
    )


def _sc(location: int, observed_property: str) -> SensorReading:
    """A Sensor.Community reading as app/sources/sensor_community.py builds it (no stream_key)."""
    return _reading(f"sensor_community-{location}", observed_property)


def _ds(ds_id: int, sensor_id: str | None, observed_property: str | None, stream_key: str | None = None) -> dict[str, Any]:
    """A Datastream as the warm-up lists it ($select=id,name,properties)."""
    props: dict[str, Any] = {}
    if sensor_id:
        props["sensor_id"] = sensor_id
    if observed_property:
        props["observed_property"] = observed_property
    if stream_key or observed_property:
        props["streamKey"] = stream_key or observed_property
    return {"@iot.id": ds_id, "name": f"DS {ds_id}", "properties": props}


class _Response:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.ok = True
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._body


@pytest.fixture
def make_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    """Build a client from an entity cache, env mappings and a warm-up listing."""

    def _make(
        *,
        cache: dict[str, str] | None = None,
        env: dict[str, str] | None = None,
        listing: list[dict[str, Any]] | None = None,
        next_link: str | None = None,
        max_datastreams: int = 10000,
    ) -> SensorThingsClient:
        cache_path = tmp_path / "registered_entities.json"
        if cache is not None:
            cache_path.write_text(json.dumps({"datastreams": cache}), encoding="utf-8")
        monkeypatch.setattr(
            stc, "settings", _settings(tmp_path, registered_entities_path=str(cache_path), datastream_ids=dict(env or {}))
        )
        client = SensorThingsClient()
        if listing is not None:
            body: dict[str, Any] = {"value": listing}
            if next_link:
                body["@iot.nextLink"] = next_link
            monkeypatch.setattr(stc.requests, "get", lambda *args, **kwargs: _Response(body))
            client.warm_datastream_cache(max_datastreams=max_datastreams)
        return client

    return _make


@pytest.fixture
def push(monkeypatch: pytest.MonkeyPatch):
    """Push readings without HTTP; returns (result, posts, live_lookups).

    ``posts`` lists (datastream id, observed property of the reading) per POST.
    ``live`` maps ``sensor_id::observed_property`` to what the live lookup finds.
    """

    def _push(
        client: SensorThingsClient, readings: list[SensorReading], live: dict[str, str] | None = None
    ) -> tuple[dict[str, Any], list[tuple[str, str]], list[str]]:
        posts: list[tuple[str, str]] = []
        lookups: list[str] = []

        def fake_post(endpoint: str, payload: dict[str, Any], datastream_id: Any, base_url: str | None = None) -> Any:
            posts.append((str(datastream_id), payload["parameters"]["observed_property"]))
            return SimpleNamespace(ok=True, status_code=201, text="created")

        def fake_live(base_url: str, reading: SensorReading) -> str | None:
            key = f"{reading.sensor_id}::{reading.observed_property}"
            lookups.append(key)
            return (live or {}).get(key)

        monkeypatch.setattr(client, "_post_observation", fake_post)
        monkeypatch.setattr(client, "_resolve_datastream_id_live", fake_live)
        result = client.push_observations(readings, source="test")
        return result, sorted(posts), lookups

    return _push


def _unresolved(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        f"{r['sensor_id']}::{r['observed_property']}": r
        for r in result["results"]
        if not r.get("ok") and "observed_property" in r
    }


# ---------------------------------------------------------------------------
# No cross-property routing
# ---------------------------------------------------------------------------


def test_bare_sensor_fallback_never_puts_other_properties_into_air_pressure(make_client, push) -> None:
    """The production case: location 93182 had only its Air pressure datastream."""
    client = make_client(listing=[_ds(1386, "sensor_community-93182", "pressure")])
    assert client._datastream_ids["sensor_community-93182"] == "1386"  # sole datastream: fallback entry kept

    result, posts, lookups = push(
        client, [_sc(93182, "pressure"), _sc(93182, "pm10"), _sc(93182, "pm2_5"), _sc(93182, "humidity")]
    )

    assert posts == [("1386", "pressure")]
    unresolved = _unresolved(result)
    assert set(unresolved) == {
        "sensor_community-93182::pm10",
        "sensor_community-93182::pm2_5",
        "sensor_community-93182::humidity",
    }
    for entry in unresolved.values():
        assert entry["refused"] == ["datastream 1386 observes pressure"]
    # Each still gets the property-specific live lookup before it is given up.
    assert sorted(lookups) == sorted(unresolved)


def test_no_bare_sensor_entry_when_the_sensor_has_several_datastreams(make_client, push) -> None:
    client = make_client(
        listing=[_ds(1386, "sensor_community-93182", "pressure"), _ds(1387, "sensor_community-93182", "temperature")]
    )

    assert "sensor_community-93182" not in client._datastream_ids
    result, posts, _ = push(client, [_sc(93182, "pm10"), _sc(93182, "temperature")])

    assert posts == [("1387", "temperature")]
    assert set(_unresolved(result)) == {"sensor_community-93182::pm10"}


def test_each_reading_of_a_mixed_batch_goes_to_its_own_datastream(make_client, push) -> None:
    client = make_client(
        listing=[
            _ds(1, "sensor_community-93182", "pm10"),
            _ds(2, "sensor_community-93182", "pm2_5"),
            _ds(3, "sensor_community-93182", "pressure"),
            _ds(4, "sensor_community-91154", "pm10"),
        ]
    )

    result, posts, _ = push(
        client,
        [
            _sc(93182, "pm10"),
            _sc(93182, "pm2_5"),
            _sc(93182, "pressure"),
            _sc(93182, "humidity"),
            _sc(91154, "pm10"),
            _sc(91154, "pressure"),
        ],
    )

    assert posts == [("1", "pm10"), ("2", "pm2_5"), ("3", "pressure"), ("4", "pm10")]
    assert set(_unresolved(result)) == {"sensor_community-93182::humidity", "sensor_community-91154::pressure"}


# ---------------------------------------------------------------------------
# Stale ids from a previous FROST target (the cross-location rows)
# ---------------------------------------------------------------------------


def test_warm_up_replaces_a_cached_id_that_now_names_another_locations_datastream(make_client, push, tmp_path) -> None:
    """91154's pm10 key kept an old-server id that is 93182's Air pressure on the new server."""
    client = make_client(
        cache={"sensor_community-91154::pm10": "1386", "sensor_community-93182::pressure": "1386"},
        listing=[_ds(1386, "sensor_community-93182", "pressure"), _ds(2001, "sensor_community-91154", "pm10")],
    )

    assert client._datastream_ids["sensor_community-91154::pm10"] == "2001"
    assert client._datastream_ids["sensor_community-93182::pressure"] == "1386"
    persisted = json.loads((tmp_path / "registered_entities.json").read_text(encoding="utf-8"))["datastreams"]
    assert persisted.get("sensor_community-91154::pm10") != "1386"
    assert persisted["sensor_community-93182::pressure"] == "1386"

    _, posts, _ = push(client, [_sc(91154, "pm10")])
    assert posts == [("2001", "pm10")]


def test_warm_up_drops_a_cached_id_of_another_property_at_the_same_location(make_client) -> None:
    client = make_client(
        cache={"sensor_community-93182::pm10": "1386"},
        listing=[_ds(1386, "sensor_community-93182", "pressure"), _ds(1390, "sensor_community-93182", "humidity")],
    )

    assert "sensor_community-93182::pm10" not in client._datastream_ids


def test_push_refuses_a_mapping_that_contradicts_the_listing(make_client, push) -> None:
    """A wrong id that shows up after the warm-up is refused at push time too."""
    client = make_client(listing=[_ds(1386, "sensor_community-93182", "pressure")])
    client._datastream_ids["sensor_community-91154::pm10"] = "1386"

    result, posts, lookups = push(client, [_sc(91154, "pm10")], live={"sensor_community-91154::pm10": "2001"})

    assert posts == [("2001", "pm10")]
    assert lookups == ["sensor_community-91154::pm10"]
    assert result["results"][0]["ok"] is True


def test_refusal_is_logged_once(make_client, push, caplog) -> None:
    client = make_client(listing=[_ds(1386, "sensor_community-93182", "pressure")])

    with caplog.at_level(logging.WARNING, logger="app.services.sensorthings_client"):
        push(client, [_sc(93182, "pm10")])
        push(client, [_sc(93182, "pm10")])

    refusals = [r for r in caplog.records if r.getMessage().startswith("Not routing sensor_community-93182 pm10")]
    assert len(refusals) == 1


# ---------------------------------------------------------------------------
# Legitimate fallbacks keep working
# ---------------------------------------------------------------------------


def test_full_key_is_trusted_when_nothing_contradicts_it(make_client, push) -> None:
    """No warm-up listing (e.g. FROST unreachable at startup): cached keys work as before."""
    client = make_client(cache={"sensor_community-93182::pm10": "50"})

    _, posts, lookups = push(client, [_sc(93182, "pm10")])

    assert posts == [("50", "pm10")]
    assert lookups == []


def test_single_datastream_sensor_fallback_without_known_property(make_client, push) -> None:
    """An env mapping of a bare sensor id to its one datastream (legacy bridge config)."""
    client = make_client(env={"legacy-gauge": "77"})

    _, posts, _ = push(client, [_reading("legacy-gauge", "water_level")])

    assert posts == [("77", "water_level")]


def test_bare_fallback_with_unknown_property_is_refused_for_a_multi_datastream_sensor(make_client, push) -> None:
    client = make_client(env={"legacy::temperature": "77", "legacy": "78"})

    result, posts, _ = push(client, [_reading("legacy", "temperature"), _reading("legacy", "humidity")])

    assert posts == [("77", "temperature")]
    refused = _unresolved(result)["legacy::humidity"]["refused"]
    assert refused == ["sensor legacy has 2 datastreams and datastream 78's observed property is unknown"]


def test_bare_fallback_follows_the_listed_property(make_client, push) -> None:
    """A datastream listed without sensor_id, mapped by an env bare key: property decides."""
    client = make_client(env={"bro-W1": "55"}, listing=[_ds(55, None, "groundwater_level")])

    result, posts, _ = push(client, [_reading("bro-W1", "water_level"), _reading("bro-W1", "temperature")])

    assert posts == [("55", "water_level")]  # groundwater_level resolves to canonical water_level
    assert set(_unresolved(result)) == {"bro-W1::temperature"}


def test_warm_up_maps_canonical_names_to_datastreams_registered_under_aliases(make_client, push) -> None:
    """Ohnics registered 'pm25' datastreams; its readings carry canonical 'pm2_5'."""
    client = make_client(
        listing=[
            _ds(70, "ohnics-de-4034", "pm25", stream_key="pm2_5"),
            _ds(71, "ohnics-de-4034", "air_temperature"),
            _ds(80, "ohnics-de-4094", "pm25"),
            _ds(81, "ohnics-de-4094", "air_temperature"),
        ]
    )

    _, posts, lookups = push(
        client,
        [
            _reading("ohnics-de-4034", "pm2_5", stream_key="pm2_5"),
            _reading("ohnics-de-4094", "pm2_5", stream_key="pm2_5"),
            _reading("ohnics-de-4094", "air_temperature", stream_key="air_temperature"),
        ],
    )

    assert posts == [("70", "pm2_5"), ("80", "pm2_5"), ("81", "air_temperature")]
    assert lookups == []


def test_explicit_stream_key_mapping_still_routes(make_client, push) -> None:
    """A configured stream-key mapping names one stream, so it is trusted without a listing."""
    client = make_client(env={"Inside Temperature": "12"})

    _, posts, _ = push(client, [_reading("tgv-officelab-climate", "temperature", stream_key="Inside Temperature")])

    assert posts == [("12", "temperature")]


def test_stream_key_fallback_never_takes_another_sensors_datastream(make_client, push) -> None:
    client = make_client(listing=[_ds(10, "sensor_community-1", "pm2_5")])
    assert client._datastream_ids["pm2_5"] == "10"  # unique stream key on the server

    result, posts, _ = push(client, [_reading("ohnics-de-1", "pm2_5", stream_key="pm2_5")])

    assert posts == []
    assert _unresolved(result)["ohnics-de-1::pm2_5"]["refused"] == ["datastream 10 belongs to sensor sensor_community-1"]


def test_no_bare_stream_key_entry_when_several_datastreams_share_it(make_client) -> None:
    client = make_client(listing=[_ds(10, "sensor_community-1", "pm2_5"), _ds(11, "sensor_community-2", "pm2_5")])

    assert "pm2_5" not in client._datastream_ids
    assert client._datastream_ids["sensor_community-1::pm2_5"] == "10"
    assert client._datastream_ids["sensor_community-2::pm2_5"] == "11"


# ---------------------------------------------------------------------------
# Unresolved readings
# ---------------------------------------------------------------------------


def test_unresolved_readings_are_counted_and_not_pushed_or_dead_lettered(make_client, push, tmp_path) -> None:
    client = make_client(listing=[_ds(1386, "sensor_community-93182", "pressure")])

    result, posts, _ = push(client, [_sc(93182, "pm10"), _sc(93182, "pm2_5")])

    assert posts == []
    assert result["sent"] == 0
    assert all(r["ok"] is False and "datastream" in r["message"] for r in result["results"])
    assert stc._validation_summarize_results(result["results"]) == (2, 0, "unresolved_datastream")
    assert not (tmp_path / "failed_observations.jsonl").exists()


def test_unresolved_reading_found_by_the_live_lookup_is_pushed(make_client, push) -> None:
    client = make_client(listing=[_ds(1386, "sensor_community-93182", "pressure")])

    _, posts, _ = push(client, [_sc(93182, "pm10")], live={"sensor_community-93182::pm10": "1400"})

    assert posts == [("1400", "pm10")]


# ---------------------------------------------------------------------------
# Partial listing
# ---------------------------------------------------------------------------


def test_partial_listing_purges_nothing_and_adds_no_fallback_entries(make_client) -> None:
    client = make_client(
        cache={"s1::temperature": "999"},
        listing=[_ds(1, "s2", "humidity")],
        next_link=f"{BASE_URL}/Datastreams?$skip=1",
        max_datastreams=1,
    )

    assert client._datastream_ids["s1::temperature"] == "999"  # may be beyond the first page
    assert client._datastream_ids["s2::humidity"] == "1"
    assert "s2" not in client._datastream_ids
    assert "humidity" not in client._datastream_ids


# ---------------------------------------------------------------------------
# Warm-up listing
# ---------------------------------------------------------------------------


def test_warm_up_lists_datastreams_in_id_order(make_client, monkeypatch) -> None:
    """Unordered, FROST lists in storage order, which its triggers keep changing."""
    client = make_client()
    calls: list[dict[str, Any] | None] = []

    def fake_get(url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> _Response:
        calls.append(params)
        return _Response({"value": [_ds(2, "s1", "humidity"), _ds(1, "s1", "temperature")]})

    monkeypatch.setattr(stc.requests, "get", fake_get)
    client.warm_datastream_cache()

    assert calls and calls[0] is not None
    assert calls[0]["$orderby"] == "id asc"
    assert client._datastream_ids["s1::temperature"] == "1"
    assert client._datastream_ids["s1::humidity"] == "2"


def test_complete_empty_listing_purges_every_cached_id(make_client, push, tmp_path) -> None:
    """A fresh primary (no datastreams yet) can't hold any id from the old target's cache."""
    client = make_client(cache={"sensor_community-91154::pm10": "1386"}, listing=[])

    assert client._datastream_ids == {}
    persisted = json.loads((tmp_path / "registered_entities.json").read_text(encoding="utf-8"))["datastreams"]
    assert persisted == {}
    _, posts, lookups = push(client, [_sc(91154, "pm10")])
    assert posts == []
    assert lookups == ["sensor_community-91154::pm10"]


# ---------------------------------------------------------------------------
# Readings of other sources (TGV on the primary had no datastreams of its own)
# ---------------------------------------------------------------------------


def _tgv(observed_property: str) -> SensorReading:
    """A TGV Kafka reading as app/pipeline/kafka_tgv.py builds it (stream_key = canonical name)."""
    reading = _reading("tgv-officelab-climate", observed_property, stream_key=observed_property)
    return reading.model_copy(update={"device_eui": "officelab-climate", "location": "tgv"})


def test_tgv_reading_never_lands_in_another_sources_datastream_of_its_property(make_client, push) -> None:
    """Production: TGV humidity/temperature filled Sensor.Community and KNMI datastreams.

    The warm-up used to map the bare stream key "humidity" to the first datastream
    listed with that streamKey, whatever its sensor. Without a TGV datastream on
    the server the reading must stay unresolved.
    """
    client = make_client(
        listing=[
            _ds(10, "sensor_community-93182", "humidity"),
            _ds(11, "knmi-0", "humidity"),
            _ds(12, "sensor_community-93182", "temperature"),
        ]
    )
    assert "humidity" not in client._datastream_ids

    result, posts, lookups = push(client, [_tgv("humidity"), _tgv("temperature")])

    assert posts == []
    assert sorted(lookups) == ["tgv-officelab-climate::humidity", "tgv-officelab-climate::temperature"]
    assert set(_unresolved(result)) == {"tgv-officelab-climate::humidity", "tgv-officelab-climate::temperature"}


def test_unresolved_keys_are_warned_once_per_target(make_client, push, caplog) -> None:
    client = make_client(listing=[_ds(1386, "sensor_community-93182", "pressure")])

    with caplog.at_level(logging.DEBUG, logger="app.services.sensorthings_client"):
        push(client, [_sc(93182, "pm10")])
        push(client, [_sc(93182, "pm10")])
        push(client, [_sc(93182, "pm10"), _sc(93182, "pm2_5")])

    summaries = [r for r in caplog.records if "have no datastream of their own observed property" in r.getMessage()]
    assert [r.levelno for r in summaries] == [logging.WARNING, logging.DEBUG, logging.WARNING]
    assert "sensor_community-93182::pm10" in summaries[0].getMessage()
    assert "1 new: sensor_community-93182::pm2_5" in summaries[2].getMessage()


# Reading shapes of the other sources that call push_observations, each with the
# datastream its connector registration creates. All must keep routing.
_OTHER_SOURCES = [
    # TGV Kafka: canonical stream_key and a device EUI.
    (_tgv("temperature"), _ds(201, "tgv-officelab-climate", "temperature")),
    # Ohnics: datastreams registered before the pm25 -> pm2_5 rename.
    (_reading("ohnics-de-4034", "pm2_5"), _ds(202, "ohnics-de-4034", "pm25")),
    # KNMI: every station shares sensor_id knmi-0 (a separate bug); full key still routes.
    (_reading("knmi-0", "wind_speed"), _ds(203, "knmi-0", "wind_speed")),
    (_reading("levellog-1001", "water_level"), _ds(204, "levellog-1001", "water_level")),
    (_reading("mjs-6421", "soil_moisture"), _ds(205, "mjs-6421", "soil_moisture")),
    (_reading("bro-GMW000000038946-1", "water_level"), _ds(206, "bro-GMW000000038946-1", "water_level")),
    (_reading("luchtmeetnet-NL10938", "no2"), _ds(207, "luchtmeetnet-NL10938", "no2")),
    # MQTT bridge / demo readings: entity-set sensor ids and property keys.
    (_reading("tgv-hitteplein-climate-station", "air_temperature"), _ds(208, "tgv-hitteplein-climate-station", "air_temperature")),
]


@pytest.mark.parametrize(("reading", "datastream"), _OTHER_SOURCES, ids=lambda v: getattr(v, "sensor_id", None))
def test_other_sources_keep_routing_by_full_key(make_client, push, reading, datastream) -> None:
    client = make_client(listing=[datastream, _ds(299, "sensor_community-93182", reading.observed_property)])

    result, posts, lookups = push(client, [reading])

    assert posts == [(str(datastream["@iot.id"]), reading.observed_property)]
    assert lookups == []
    assert result["results"][0]["ok"] is True


def test_samen_meten_keeps_routing_through_the_live_lookup(make_client, push) -> None:
    """Samen Meten readings carry sm-<remote thing id>; its datastreams sm-<thing name>."""
    client = make_client(listing=[_ds(300, "sm-ssk_lh001", "pm10"), _ds(301, "sm-ssk_lh002", "pm10")])

    _, posts, lookups = push(client, [_reading("sm-4711", "pm10")], live={"sm-4711::pm10": "300"})

    assert lookups == ["sm-4711::pm10"]
    assert posts == [("300", "pm10")]


# ---------------------------------------------------------------------------
# Integration: a real FROST-Server 2.8 (python -m pytest -m integration)
# ---------------------------------------------------------------------------

_FROST_IMAGE = "fraunhoferiosb/frost-server:2.8"
_POSTGIS_IMAGE = "postgis/postgis:16-3.4-alpine"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="module")
def frost28() -> Iterator[str]:
    """v1.1 base URL of a throwaway FROST 2.8 + PostGIS, or $SC_ROUTING_FROST_URL.

    Own containers under unique names (no compose project to collide with), torn
    down afterwards. maxTop=2 makes FROST page every listing, so the warm-up has
    to follow its nextLinks.
    """
    existing = os.getenv("SC_ROUTING_FROST_URL", "").strip().rstrip("/")
    if existing:
        yield existing
        return
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    suffix = uuid.uuid4().hex[:8]
    net, db, frost = f"scroute-it-{suffix}", f"scroute-it-db-{suffix}", f"scroute-it-frost-{suffix}"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    root = f"http://localhost:{port}/FROST-Server"
    try:
        _docker("network", "create", net)
        _docker(
            "run", "-d", "--name", db, "--network", net,
            "-e", "POSTGRES_DB=sensorthings", "-e", "POSTGRES_USER=sensorthings", "-e", "POSTGRES_PASSWORD=sensorthings",
            _POSTGIS_IMAGE,
        )
        _docker(
            "run", "-d", "--name", frost, "--network", net, "-p", f"127.0.0.1:{port}:8080",
            "-e", f"serviceRootUrl={root}",
            "-e", "persistence_db_driver=org.postgresql.Driver",
            "-e", f"persistence_db_url=jdbc:postgresql://{db}:5432/sensorthings",
            "-e", "persistence_db_username=sensorthings", "-e", "persistence_db_password=sensorthings",
            "-e", "persistence_autoUpdateDatabase=true", "-e", "maxTop=2",
            _FROST_IMAGE,
        )
        deadline = time.monotonic() + 240
        while True:
            try:
                if requests.get(f"{root}/v1.1/Things", timeout=5).status_code == 200:
                    break
            except requests.RequestException:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"FROST at {root} not ready")
            time.sleep(2)
        yield f"{root}/v1.1"
    finally:
        _docker("rm", "-f", frost, db, check=False)
        _docker("network", "rm", net, check=False)


def _all_pages(url: str, params: dict[str, str]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    next_url: str | None = url
    while next_url:
        body = requests.get(next_url, params=params, timeout=15).json()
        values.extend(body.get("value", []))
        next_url, params = body.get("@iot.nextLink"), {}
    return values


def _labels(base_url: str, ds_id: str) -> list[str]:
    """observed_property labels (PARAMETERS) of a datastream's observations, sorted."""
    obs = _all_pages(f"{base_url}/Datastreams({ds_id})/Observations", {"$select": "parameters"})
    return sorted(str((o.get("parameters") or {}).get("observed_property")) for o in obs)


def _sc_entry(location_id: int, sensor_type: str, values: dict[str, str]) -> dict[str, Any]:
    return {
        "sensor": {"sensor_type": {"name": sensor_type}},
        "location": {"id": location_id, "latitude": "52.01", "longitude": "4.36"},
        "sensordatavalues": [{"value_type": k, "value": v} for k, v in values.items()],
        "timestamp": "2026-11-20 12:05:00",
    }


@pytest.mark.integration
def test_routing_end_to_end_on_frost_28(frost28, monkeypatch, tmp_path) -> None:
    """The production failure and its fix against a real server.

    Location A (93182 in production) was first seen through its BME280 entry,
    so only its temperature/pressure/humidity datastreams exist. B's (91154)
    PM10 key kept the id it had on the previous primary, which is A's Air
    pressure datastream here.
    """
    salt = uuid.uuid4().int % 1000  # unique Things when SC_ROUTING_FROST_URL is reused
    loc_a, loc_b = 93182000 + salt, 91154000 + salt
    monkeypatch.setattr(
        stc, "settings",
        _settings(tmp_path, sensorthings_base_url=frost28, auth_username="", auth_password=""),
    )
    registrar = SensorThingsClient()
    for loc in (loc_a, loc_b):
        entity_set = build_entity_set(loc, 52.0, 4.3, ["temperature", "pressure", "humidity"])
        assert registrar.register_entity_set(entity_set)["primary_ok"]
    pressure_a = registrar._datastream_ids[f"sensor_community-{loc_a}::pressure"]
    humidity_a = registrar._datastream_ids[f"sensor_community-{loc_a}::humidity"]

    cache_path = tmp_path / "registered_entities.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    cache["datastreams"][f"sensor_community-{loc_b}::pm10"] = pressure_a
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    client = SensorThingsClient()
    listing_calls: list[str] = []
    real_get = requests.get

    def counting_get(url: str, *args: Any, **kwargs: Any) -> Any:
        if "/Datastreams" in url and "Observations" not in url:
            listing_calls.append(url)
        return real_get(url, *args, **kwargs)

    monkeypatch.setattr(stc.requests, "get", counting_get)
    client.warm_datastream_cache()
    monkeypatch.setattr(stc.requests, "get", real_get)

    assert len(listing_calls) >= 3  # maxTop=2: the ordered listing comes in pages
    assert f"sensor_community-{loc_b}::pm10" not in client._datastream_ids
    assert f"sensor_community-{loc_a}" not in client._datastream_ids  # three datastreams: no bare entry

    # One poll, readings as the Sensor.Community mapper builds them.
    entries = [
        _sc_entry(loc_a, "BME280", {"temperature": "8.1", "pressure": "1012.0", "humidity": "81"}),
        _sc_entry(loc_a, "SDS011", {"P1": "12.5", "P2": "7.25"}),
        _sc_entry(loc_b, "SDS011", {"P1": "20.5", "P2": "11.0"}),
    ]
    readings = parse_sensor_data(entries)
    result = client.push_observations(readings, source="sensor_community")

    assert result["sent"] == 3  # A's temperature, pressure and humidity
    assert set(_unresolved(result)) == {
        f"sensor_community-{loc_a}::pm10", f"sensor_community-{loc_a}::pm2_5",
        f"sensor_community-{loc_b}::pm10", f"sensor_community-{loc_b}::pm2_5",
    }
    assert _labels(frost28, pressure_a) == ["pressure"]
    assert _labels(frost28, humidity_a) == ["humidity"]

    # Discovery merges every entry of the poll; the sets are registered before the
    # push (app/main.py) and PM readings get datastreams of their own.
    source = SensorCommunityPollingSource()
    source._discover(entries)
    for entity_set in source.entity_sets():
        assert client.register_entity_set(entity_set)["primary_ok"]

    pm_readings = [r for r in readings if r.observed_property in {"pm10", "pm2_5"}]
    result = client.push_observations(pm_readings, source="sensor_community")

    assert result["sent"] == 4
    for loc in (loc_a, loc_b):
        for prop in ("pm10", "pm2_5"):
            ds_id = client._datastream_ids[f"sensor_community-{loc}::{prop}"]
            assert ds_id not in {pressure_a, humidity_a}
            assert _labels(frost28, ds_id) == [prop]
    assert _labels(frost28, pressure_a) == ["pressure"]
