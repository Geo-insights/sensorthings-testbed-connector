"""Seeding the polling loops' high-water marks from FROST after a restart.

``_seed_timestamps_from_frost`` pages through all of a source's Datastreams in id
order and expands each one's newest Observation with its parameters, where
push_observations recorded the sensor_id and observed_property of the reading it
was. The mark goes to that label, so readings FROST already holds are not posted
again, and readings it does not hold are never dropped for another key's row.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import types
from datetime import UTC, datetime
from typing import Any

import pytest
import requests

import app.main as main
from app.frost.odata_fields import odata_fields_for
from app.models import SensorReading
from app.sources import ohnics, samen_meten

BASE = "https://frost.test/FROST-Server/v1.1"
BASE_V2 = "https://frost.test/FROST-Server/v2.0"
AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}
EXPAND = "Observations($select=phenomenonTime,parameters;$orderby=phenomenonTime desc;$top=1),Thing($select=name)"
SC_FILTER = "Thing/properties/source eq 'sensor_community'"
OWN = ("own", "label")  # sentinel: the newest Observation is labelled with its Datastream's own key


class _Response:
    def __init__(self, body: Any = None, status: int = 200) -> None:
        self._body = body
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeFrost:
    """Answers consecutive GETs with scripted responses and records each request."""

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, str] | None = None, headers: Any = None, timeout: Any = None) -> Any:
        self.calls.append({"url": url, "params": dict(params or {}), "headers": headers, "timeout": timeout})
        assert self._responses, f"unexpected request to {url} with {params}"
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _use_target(monkeypatch: pytest.MonkeyPatch, base: str | None, version: str | None = "1.1", prefix: str = "") -> None:
    """Point the client at ``base``; ``version=None`` leaves the URL without a target stack."""
    monkeypatch.setattr(main.client, "_http", types.SimpleNamespace(primary_base_url=base))
    stack = types.SimpleNamespace(fields=odata_fields_for(version)) if version else None
    monkeypatch.setattr(main.client, "_target_stack_for_url", lambda url: stack)
    monkeypatch.setattr(main.client, "_headers_for_url", lambda url: dict(AUTH))
    monkeypatch.setattr(
        main, "settings", dataclasses.replace(main.settings, entity_name_prefix=prefix, datastreams_path="/Datastreams")
    )


def _serve(monkeypatch: pytest.MonkeyPatch, *responses: Any) -> _FakeFrost:
    fake = _FakeFrost(*responses)
    monkeypatch.setattr(main.requests, "get", fake.get)
    return fake


def _ds(
    ds_id: Any,
    sensor_id: str | None,
    prop: str | None,
    newest: Any = None,
    *,
    label: tuple[Any, Any] | None = OWN,
    ds_time: Any = None,
    v2: bool = False,
    expanded: bool = True,
    thing: str | None = None,
    written_thing: str | None = None,
) -> dict[str, Any]:
    """A Datastream row with its newest Observation (phenomenonTime ``newest``) expanded.

    ``label`` is the (sensor_id, observed_property) that push_observations wrote
    into that Observation's parameters: by default the Datastream's own, None
    for an Observation without parameters. ``ds_time`` is the Datastream's own
    phenomenonTime, which FROST's triggers keep over all of its rows. ``thing``
    is the name of the Datastream's Thing (expanded), ``written_thing`` the
    ``thing_name`` push_observations wrote into the Observation's parameters.
    """
    props: dict[str, Any] = {"site_key": "x"}
    if sensor_id:
        props["sensor_id"] = sensor_id
    if prop:
        props["observed_property"] = prop
    row: dict[str, Any] = {("id" if v2 else "@iot.id"): ds_id, "properties": props}
    if ds_time is not None:
        row["phenomenonTime"] = ds_time
    if expanded and thing is not None:
        row["Thing"] = {("id" if v2 else "@iot.id"): 1, "name": thing}
    if expanded:
        row["Observations"] = []
        if newest is not None:
            observation: dict[str, Any] = {"phenomenonTime": newest}
            if label is OWN:
                label = (sensor_id, prop)
            if label is not None:
                observation["parameters"] = {"sensor_id": label[0], "observed_property": label[1], "unit": "ug/m3"}
                if written_thing is not None:
                    observation["parameters"]["thing_name"] = written_thing
            row["Observations"].append(observation)
    return row


def _page(rows: list[Any], more: bool = False, *, v2: bool = False) -> _Response:
    body: dict[str, Any] = {"value": rows}
    if more:
        root = BASE_V2 if v2 else BASE
        body["@nextLink" if v2 else "@iot.nextLink"] = f"{root}/Datastreams?$top=2&$skip=2"
    return _Response(body)


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _interval(start: str, end: str) -> str:
    return f"2026-11-20T{start}Z/2026-11-20T{end}Z"


def _instant(at: str) -> str:
    return f"2026-11-20T{at}Z"


def _reading(sensor_id: str, prop: str, at: str, value: float = 1.0) -> SensorReading:
    return SensorReading(
        sensor_id=sensor_id, sensor_name=f"{sensor_id} sensor", observed_property=prop, unit="1", value=value,
        timestamp=_utc(f"2026-11-20T{at}"),
    )


def _stored(
    readings: list[Any], datastream_of: dict[str, tuple[str, str]], thing: str | None = None,
) -> list[dict[str, Any]]:
    """The Datastream rows FROST serves after ``readings`` were pushed.

    ``datastream_of`` routes a reading's ``sensor_id::observed_property`` to the
    (sensor_id, observed_property) in its Datastream's properties. Each
    Datastream's newest Observation is labelled with its reading's own keys and
    Thing name, as push_observations writes them into the parameters; ``thing``
    is the name of the Datastream's Thing on FROST (with the entity name prefix).
    """
    newest: dict[tuple[str, str], Any] = {}
    for reading in readings:
        target = datastream_of[f"{reading.sensor_id}::{reading.observed_property}"]
        if target not in newest or reading.timestamp > newest[target].timestamp:
            newest[target] = reading
    return [
        _ds(n, sensor_id, prop, reading.timestamp.isoformat(), label=(reading.sensor_id, reading.observed_property),
            thing=thing, written_thing=reading.thing_name)
        for n, ((sensor_id, prop), reading) in enumerate(sorted(newest.items()), start=1)
    ]


# -- paging -----------------------------------------------------------------------


def test_pages_through_every_datastream_in_id_order(monkeypatch):
    _use_target(monkeypatch, BASE)
    fake = _serve(
        monkeypatch,
        _page([_ds(3, "sc-1", "pm10", _instant("10:00")), _ds(8, "sc-1", "pm2_5", _instant("10:05"))], more=True),
        _page([_ds(11, "sc-2", "pm10", _instant("10:10")), _ds(14, "sc-2", "pm2_5", _instant("10:15"))], more=True),
        _page([_ds(20, "sc-3", "pm10", _instant("10:20"))]),
    )

    seeded = main._seed_timestamps_from_frost("sensor_community", page_size=2)

    assert seeded == {
        "sc-1::pm10": _utc("2026-11-20T10:00:00"),
        "sc-1::pm2_5": _utc("2026-11-20T10:05:00"),
        "sc-2::pm10": _utc("2026-11-20T10:10:00"),
        "sc-2::pm2_5": _utc("2026-11-20T10:15:00"),
        "sc-3::pm10": _utc("2026-11-20T10:20:00"),
    }
    assert [call["url"] for call in fake.calls] == [f"{BASE}/Datastreams"] * 3
    # The newest Observation with its labels; not the Datastream's own phenomenonTime.
    assert fake.calls[0]["params"] == {
        "$filter": SC_FILTER,
        "$select": "@iot.id,properties",
        "$orderby": "@iot.id asc",
        "$top": "2",
        "$expand": EXPAND,
    }
    # Each next page continues after the last id of the previous one.
    assert fake.calls[1]["params"] == {**fake.calls[0]["params"], "$filter": f"{SC_FILTER} and @iot.id gt 8"}
    assert fake.calls[2]["params"] == {**fake.calls[0]["params"], "$filter": f"{SC_FILTER} and @iot.id gt 14"}
    assert all(call["headers"] == AUTH for call in fake.calls)


def test_reads_sensorthings_v2_responses(monkeypatch):
    _use_target(monkeypatch, BASE_V2, version="2.0")
    fake = _serve(
        monkeypatch,
        _page([_ds(7, "knmi-06344", "temperature", {"start": _instant("10:00")}, v2=True)], more=True, v2=True),
        _page([_ds(9, "knmi-06344", "humidity", {"start": _instant("10:00"), "end": _instant("10:10")}, v2=True)],
              v2=True),
    )

    seeded = main._seed_timestamps_from_frost("knmi_weather", page_size=1)

    assert seeded == {
        "knmi-06344::temperature": _utc("2026-11-20T10:00:00"),
        "knmi-06344::humidity": _utc("2026-11-20T10:10:00"),
    }
    knmi_filter = "Thing/properties/source eq 'knmi_weather'"
    assert fake.calls[0]["params"]["$select"] == "id,properties"
    assert fake.calls[0]["params"]["$orderby"] == "id asc"
    assert fake.calls[1]["params"]["$filter"] == f"{knmi_filter} and id gt 7"


def test_v2_url_without_a_target_stack_still_pages(monkeypatch):
    # Production declares its /v2.0 URL without a version: the connector asks with
    # v1.1 field names, which FROST accepts, and FROST answers with "id" and "@nextLink".
    _use_target(monkeypatch, BASE_V2, version=None)
    fake = _serve(
        monkeypatch,
        _page([_ds(4, "sc-1", "pm10", {"start": _instant("10:00")}, v2=True)], more=True, v2=True),
        _page([_ds(6, "sc-2", "pm10", {"start": _instant("10:05")}, v2=True)], v2=True),
    )

    seeded = main._seed_timestamps_from_frost("sensor_community", page_size=1)

    assert set(seeded) == {"sc-1::pm10", "sc-2::pm10"}
    assert fake.calls[1]["params"]["$filter"] == f"{SC_FILTER} and @iot.id gt 4"


def test_string_ids_are_quoted_in_the_continuation(monkeypatch):
    _use_target(monkeypatch, BASE)
    fake = _serve(
        monkeypatch,
        _page([_ds("a'1", "sc-1", "pm10", _instant("10:00"))], more=True),
        _page([]),
    )

    main._seed_timestamps_from_frost("sensor_community", page_size=1)

    assert fake.calls[1]["params"]["$filter"] == f"{SC_FILTER} and @iot.id gt 'a''1'"


def test_filter_selects_the_source_and_the_entity_name_prefix(monkeypatch):
    _use_target(monkeypatch, BASE, prefix=" GEO_ ")
    fake = _serve(monkeypatch, _page([]))

    main._seed_timestamps_from_frost("o'source")

    assert fake.calls[0]["params"]["$filter"] == "startswith(name,'GEO_') and Thing/properties/source eq 'o''source'"


def test_source_name_is_matched_in_lower_case(monkeypatch):
    # The Ohnics debug routes pass "Ohnics"; Things carry properties.source "ohnics".
    _use_target(monkeypatch, BASE)
    fake = _serve(monkeypatch, _page([]))

    main._seed_timestamps_from_frost("Ohnics")

    assert fake.calls[0]["params"]["$filter"] == "Thing/properties/source eq 'ohnics'"


# -- whose mark ------------------------------------------------------------------


def test_marks_come_from_the_labels_of_the_newest_observations(monkeypatch, caplog):
    _use_target(monkeypatch, BASE)
    _serve(
        monkeypatch,
        _page(
            [
                _ds(1, "sc-5", "pm10", _instant("10:00")),
                # Rows of one key in several Datastreams: the newest counts...
                _ds(2, "sc-5", "pm10", _instant("10:30")),
                # ...and an older one does not overwrite it.
                _ds(3, "sc-5", "pm10", _instant("10:20")),
                # An interval Observation counts with its end.
                _ds(4, "sc-6", "pm10", _interval("09:00", "11:00")),
                # A time without an offset is UTC.
                _ds(5, "sc-7", "pm10", "2026-11-20T10:40:00"),
                # The Datastream's own phenomenonTime, later than its newest Observation (FROST keeps
                # it over every row: a foreign interval reading's end, say), does not count.
                _ds(6, "sc-8", "pm10", _instant("10:10"), ds_time=_interval("09:00", "12:00")),
                # No Observations: no mark, whatever the Datastream's phenomenonTime says.
                _ds(7, "sc-9", "pm10", ds_time=_interval("09:00", "12:00")),
                _ds(8, "sc-10", "pm10", ds_time=_interval("09:00", "12:00"), expanded=False),
                # A newest Observation without a complete label marks nothing.
                _ds(9, "sc-11", "pm10", _instant("10:50"), label=None),
                _ds(10, "sc-12", "pm10", _instant("10:50"), label=("sc-12", None)),
                _ds(11, "sc-13", "pm10", _instant("10:50"), label=(" ", "pm10")),
                _ds(12, "sc-14", "pm10", "not a time"),
                # A Datastream without a key of its own: its newest Observation's label counts.
                _ds(13, None, None, _instant("10:45"), label=("sc-15", "pm10")),
                "not a datastream",
            ]
        ),
    )

    with caplog.at_level(logging.INFO, logger="connector.events"):
        seeded = main._seed_timestamps_from_frost("sensor_community")

    assert seeded == {
        "sc-5::pm10": _utc("2026-11-20T10:30:00"),
        "sc-6::pm10": _utc("2026-11-20T11:00:00"),
        "sc-7::pm10": _utc("2026-11-20T10:40:00"),
        "sc-8::pm10": _utc("2026-11-20T10:10:00"),
        "sc-15::pm10": _utc("2026-11-20T10:45:00"),
    }
    assert (
        "Seeded 5 timestamps from FROST for sensor_community (14 datastreams in 1 page(s); newest Observation "
        "labelled with the datastream's key: 6, with another key: 1, unlabelled: 3, none: 4; "
    ) in caplog.text


def test_foreign_newest_row_drops_nothing(monkeypatch):
    """A TGV reading routed into an Ohnics Datastream (before the routing fix) is its newest row.

    It marks the TGV key, which no Ohnics reading has, and not the Ohnics one:
    the 10:55 Ohnics reading FROST does not hold yet is pushed. Before, the
    10:57 TGV row became the Ohnics key's mark and dropped it.
    """
    _use_target(monkeypatch, BASE)
    _serve(
        monkeypatch,
        _page([
            _ds(1, "ohnics-DELFT-1", "air_temperature", _instant("10:57"),
                label=("tgv-weatherstation-1", "air_temperature"), ds_time=_interval("08:00", "10:57")),
            _ds(2, "ohnics-DELFT-1", "pm25", _instant("10:50"), label=("ohnics-DELFT-1", "pm2_5")),
        ]),
    )

    seeded = main._seed_timestamps_from_frost("ohnics")

    assert seeded == {
        "tgv-weatherstation-1::air_temperature": _utc("2026-11-20T10:57:00"),
        "ohnics-DELFT-1::pm2_5": _utc("2026-11-20T10:50:00"),
    }
    fresh = ohnics.parse_sensor_readings({"Name": "DELFT-1", "P2": 3.0, "T": 9.5, "Timestamp": "2026-11-20T10:55:00Z"})
    kept = main._dedup_readings(fresh, seeded)
    assert sorted((r.observed_property, r.timestamp) for r in kept) == [
        ("air_temperature", _utc("2026-11-20T10:55:00")),
        ("pm2_5", _utc("2026-11-20T10:55:00")),
    ]


class _FakeSamenMeten:
    """The Samen Meten STA: one Thing (id 4711) with a PM2.5 and a PM10 Datastream."""

    async def get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        if path.startswith("/Things"):
            return {"value": [{
                "@iot.id": 4711, "name": "OHN_gm-2138",
                "Locations": [{"location": {"type": "Point", "coordinates": [4.3, 52.0]}}],
                "Datastreams": [
                    {"@iot.id": 9001, "name": "OHN_gm-2138-pm25", "ObservedProperty": {"name": "pm25"}},
                    {"@iot.id": 9002, "name": "OHN_gm-2138-pm10", "ObservedProperty": {"name": "pm10"}},
                ],
            }]}
        return {"value": [
            {"phenomenonTime": "2026-11-20T09:00:00Z", "result": 7.5},
            {"phenomenonTime": "2026-11-20T09:10:00Z", "result": 8.0},
        ]}


def test_samen_meten_restart_reposts_nothing(monkeypatch):
    """Samen Meten keys its Datastreams and its readings on the RIVM Thing id (#45).

    The newest row of each Datastream carries the Datastream's own key
    (sm-<id>::<property>), so the restart marks exactly the readings' keys: no
    "older sensor id" mark, and the look-back posts nothing twice.
    """
    from app.services.samen_meten_source import SamenMetenPollingSource

    source = SamenMetenPollingSource()
    source._client = _FakeSamenMeten()
    readings = asyncio.run(source._do_fetch_readings())
    entity_set = source.entity_sets()[0]
    (sensor,) = entity_set["sensors"]
    assert {r.sensor_id for r in readings} == {sensor["sensor_id"]} == {"sm-4711"}
    assert {r.thing_name for r in readings} == {entity_set["thing"]["name"]} == {"SamenMeten OHN_gm-2138"}
    _use_target(monkeypatch, BASE, prefix="GEO_")
    _serve(monkeypatch, _page(_stored(readings, {
        f"sm-4711::{prop}": (sensor["sensor_id"], prop) for prop in sensor["observed_properties"]
    }, thing=f"GEO_{entity_set['thing']['name']}")))

    seeded = main._seed_timestamps_from_frost("samen_meten")

    newest = _utc("2026-11-20T09:10:00")
    assert seeded == {"sm-4711::pm2_5": newest, "sm-4711::pm10": newest}
    # The restarted loop's look-back serves the same Observations again: none is posted twice.
    assert main._dedup_readings(readings, seeded) == []
    later = samen_meten.parse_observations(
        [{"phenomenonTime": "2026-11-20T09:20:00Z", "result": 9.0}], sensor_id="sm-4711",
        sensor_name="SamenMeten OHN_gm-2138 sensor", observed_property="pm2_5", thing_name="SamenMeten OHN_gm-2138",
    )
    assert main._dedup_readings(later, seeded) == later


class _FakeOhnics:
    async def get(self, url: str, params: dict[str, str] | None = None) -> list[dict[str, Any]]:
        return [{"Name": "de-1", "Lat": 52.0, "Long": 4.36, "P2": 3.0, "T": 9.5, "Timestamp": "2026-11-20T10:00:00Z"}]


def test_ohnics_restart_reposts_nothing_although_its_datastreams_say_pm25(monkeypatch):
    """Ohnics' PM2.5 Datastreams on FROST say "pm25" (registered so until 2026-11-24); its readings say
    "pm2_5", and so does what the source registers now. Seeding must still match them."""
    from app.services.ohnics_source import OhnicsPollingSource

    source = OhnicsPollingSource()
    source._client = _FakeOhnics()
    source._prefix = "de-"
    readings = asyncio.run(source._do_fetch_readings())
    (sensor,) = source.entity_sets()[0]["sensors"]
    assert sorted(sensor["observed_properties"]) == ["air_temperature", "pm2_5"]
    assert sorted(r.observed_property for r in readings) == ["air_temperature", "pm2_5"]
    _use_target(monkeypatch, BASE, prefix="GEO_")
    _serve(monkeypatch, _page(_stored(readings, {
        "ohnics-de-1::pm2_5": ("ohnics-de-1", "pm25"),
        "ohnics-de-1::air_temperature": ("ohnics-de-1", "air_temperature"),
    }, thing=f"GEO_{source.entity_sets()[0]['thing']['name']}")))

    seeded = main._seed_timestamps_from_frost("ohnics")

    assert seeded == {
        "ohnics-de-1::pm2_5": _utc("2026-11-20T10:00:00"),
        "ohnics-de-1::air_temperature": _utc("2026-11-20T10:00:00"),
    }
    assert main._dedup_readings(readings, seeded) == []


def test_labels_are_compared_and_marked_canonical(monkeypatch, caplog):
    # A row labelled "pm25" by an earlier connector marks the canonical key, and counts as the
    # Datastream's own; an observed property that is not canonical stays as it is.
    _use_target(monkeypatch, BASE)
    _serve(
        monkeypatch,
        _page([
            _ds(1, "ohnics-de-2", "pm25", _instant("10:00"), label=("ohnics-de-2", "pm25")),
            _ds(2, "ohnics-de-2", "PM2.5", _instant("10:05"), label=("ohnics-de-2", "pm2_5")),
            _ds(3, "x-1", "not_canonical", _instant("10:00")),
        ]),
    )

    with caplog.at_level(logging.INFO, logger="connector.events"):
        seeded = main._seed_timestamps_from_frost("ohnics")

    assert seeded == {"ohnics-de-2::pm2_5": _utc("2026-11-20T10:05:00"), "x-1::not_canonical": _utc("2026-11-20T10:00:00")}
    assert "labelled with the datastream's key: 3, with another key: 0," in caplog.text


KNMI_A = "KNMI KNMI-52.1-5.18"  # parameters.thing_name of station A's readings
KNMI_B = "KNMI KNMI-52.46-4.56"
WIGOS_A = "knmi-0-20000-0-06260"
WIGOS_B = "knmi-0-20000-0-06225"


def test_knmi_datastreams_moved_to_wigos_ids_keep_their_marks(monkeypatch, caplog):
    """scripts/migrate_knmi_sensor_ids.py moves KNMI Datastreams from knmi-<position> to WIGOS ids.

    Their Observations keep the old label until the first reading with the new
    id lands. The newest row is the Datastream's own Thing's (parameters.thing_name
    is the Thing's name without the GEO_ prefix) and property, so the WIGOS key
    is marked too: the restarted loop posts nothing again. Without that mark
    every KNMI Datastream's latest value was posted once more under the WIGOS
    label, a row duplicate cleanup cannot pair with the original.
    """
    _use_target(monkeypatch, BASE, prefix="GEO_")
    _serve(
        monkeypatch,
        _page([
            _ds(1, WIGOS_A, "temperature", _instant("10:00"), label=("knmi-5", "temperature"),
                thing=f"GEO_{KNMI_A}", written_thing=KNMI_A),
            _ds(2, WIGOS_A, "humidity", _instant("10:00"), label=("knmi-5", "humidity"),
                thing=f"GEO_{KNMI_A}", written_thing=KNMI_A),
            # The same Thing's row of another property (misplaced) does not mark the Datastream's key.
            _ds(3, WIGOS_A, "ground_temperature", _instant("10:00"), label=("knmi-5", "temperature"),
                thing=f"GEO_{KNMI_A}", written_thing=KNMI_A),
            # A row written without thing_name in its parameters marks nothing extra.
            _ds(4, WIGOS_B, "temperature", _instant("09:50"), label=("knmi-7", "temperature"),
                thing=f"GEO_{KNMI_B}"),
        ]),
    )

    with caplog.at_level(logging.INFO, logger="connector.events"):
        seeded = main._seed_timestamps_from_frost("knmi_weather")

    ten = _utc("2026-11-20T10:00:00")
    assert seeded == {
        "knmi-5::temperature": ten,
        "knmi-5::humidity": ten,
        f"{WIGOS_A}::temperature": ten,
        f"{WIGOS_A}::humidity": ten,
        "knmi-7::temperature": _utc("2026-11-20T09:50:00"),
    }
    assert "with another key: 4," in caplog.text
    assert "of the other keys, the datastream's Thing and property under an older sensor id: 2;" in caplog.text
    # The restarted loop's look-back serves 10:00 again under the WIGOS id: nothing is posted twice.
    again = [_reading(WIGOS_A, "temperature", "10:00:00"), _reading(WIGOS_A, "humidity", "10:00:00")]
    assert main._dedup_readings(again, seeded) == []
    later = [_reading(WIGOS_A, "temperature", "10:10:00"), _reading(WIGOS_A, "ground_temperature", "10:00:00"),
             _reading(WIGOS_B, "temperature", "09:50:00")]
    assert main._dedup_readings(later, seeded) == later


def test_without_an_entity_name_prefix_thing_names_compare_as_they_are(monkeypatch):
    _use_target(monkeypatch, BASE)
    _serve(monkeypatch, _page([
        _ds(1, WIGOS_A, "temperature", _instant("10:00"), label=("knmi-5", "temperature"),
            thing=KNMI_A, written_thing=KNMI_A),
        _ds(2, WIGOS_B, "temperature", _instant("10:00"), label=("knmi-7", "temperature"),
            thing=f"GEO_{KNMI_B}", written_thing=KNMI_B),
    ]))

    seeded = main._seed_timestamps_from_frost("knmi_weather")

    assert f"{WIGOS_A}::temperature" in seeded
    assert f"{WIGOS_B}::temperature" not in seeded  # GEO_ is not the prefix here: another name


def test_another_stations_row_never_marks_the_datastream_key(monkeypatch):
    """Station B's reading in station A's Datastream (the old position shift put it there).

    Same property, an older KNMI id, but parameters.thing_name names B: A's key
    gets no mark and A's next reading is posted.
    """
    _use_target(monkeypatch, BASE, prefix="GEO_")
    _serve(monkeypatch, _page([
        _ds(1, WIGOS_A, "temperature", _instant("10:00"), label=("knmi-1", "temperature"),
            thing=f"GEO_{KNMI_A}", written_thing=KNMI_B),
    ]))

    seeded = main._seed_timestamps_from_frost("knmi_weather")

    assert seeded == {"knmi-1::temperature": _utc("2026-11-20T10:00:00")}
    fresh = [_reading(WIGOS_A, "temperature", "09:55:00")]
    assert main._dedup_readings(fresh, seeded) == fresh


def test_foreign_rows_never_mark_the_datastream_key(monkeypatch):
    """A TGV row in an Ohnics Datastream and a misrouted Sensor.Community row, both of the
    Datastream's own observed property: their Things are others, so only their own keys are marked."""
    _use_target(monkeypatch, BASE, prefix="GEO_")
    _serve(monkeypatch, _page([
        _ds(1, "ohnics-DELFT-1", "air_temperature", _instant("10:57"),
            label=("tgv-weatherstation-1", "air_temperature"),
            thing="GEO_Ohnics DELFT-1", written_thing="TGV weatherstation 1"),
        _ds(2, "sc-10", "pm10", _instant("10:40"), label=("sc-11", "P1"),
            thing="GEO_SensorCommunity 10", written_thing="SensorCommunity 11"),
    ]))

    seeded = main._seed_timestamps_from_frost("ohnics")

    assert seeded == {
        "tgv-weatherstation-1::air_temperature": _utc("2026-11-20T10:57:00"),
        "sc-11::pm10": _utc("2026-11-20T10:40:00"),
    }
    fresh = [_reading("ohnics-DELFT-1", "air_temperature", "10:55:00"), _reading("sc-10", "pm10", "10:30:00")]
    assert main._dedup_readings(fresh, seeded) == fresh


def test_rows_labelled_with_a_key_decide_its_mark_over_an_older_sensor_id(monkeypatch):
    """A key that rows are labelled with keeps the mark of those rows, even an older one.

    Datastream 1 (WIGOS_A) holds A's 10:30 row under knmi-5; datastream 2 holds
    a 10:00 row labelled WIGOS_A. 10:00 is the mark: what is newer may be posted
    again, never dropped.
    """
    _use_target(monkeypatch, BASE, prefix="GEO_")
    _serve(monkeypatch, _page([
        _ds(1, WIGOS_A, "temperature", _instant("10:30"), label=("knmi-5", "temperature"),
            thing=f"GEO_{KNMI_A}", written_thing=KNMI_A),
        _ds(2, "knmi-5", "temperature", _instant("10:00"), label=(WIGOS_A, "temperature"),
            thing=f"GEO_{KNMI_A}", written_thing=KNMI_A),
    ]))

    seeded = main._seed_timestamps_from_frost("knmi_weather")

    assert seeded[f"{WIGOS_A}::temperature"] == _utc("2026-11-20T10:00:00")
    assert seeded["knmi-5::temperature"] == _utc("2026-11-20T10:30:00")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-20T09:00:00Z/2026-11-20T10:00:00Z", "2026-11-20T10:00:00"),
        ("2026-11-20T10:00:00.250Z/2026-11-20T10:00:00.250Z", "2026-11-20T10:00:00.250"),
        ("2026-11-20T10:00:00Z", "2026-11-20T10:00:00"),
        ("2026-11-20T11:00:00+01:00", "2026-11-20T10:00:00"),
        ({"start": "2026-11-20T09:00:00Z", "end": "2026-11-20T10:00:00Z"}, "2026-11-20T10:00:00"),
        ({"start": "2026-11-20T10:00:00Z"}, "2026-11-20T10:00:00"),
        ({}, None),
        (None, None),
        ("", None),
        ("garbage", None),
        (42, None),
    ],
)
def test_phenomenon_time_end(raw, expected):
    parsed = main._phenomenon_time_end(raw)
    if expected is None:
        assert parsed is None
    else:
        assert parsed == _utc(expected)
        assert parsed.tzinfo is not None


@pytest.mark.parametrize(
    ("sensor_id", "observed_property", "expected"),
    [
        ("ohnics-de-1", "pm25", "ohnics-de-1::pm2_5"),
        ("ohnics-de-1", "PM2.5", "ohnics-de-1::pm2_5"),
        ("ohnics-de-1", " pm2_5 ", "ohnics-de-1::pm2_5"),
        ("sc-1", "P1", "sc-1::pm10"),
        ("knmi-0", "temperature", "knmi-0::temperature"),  # not merged with air_temperature
        ("knmi-0", "air_temperature", "knmi-0::air_temperature"),
        ("x-1", "not_canonical", "x-1::not_canonical"),
        (4711, "pm10", "4711::pm10"),
        ("sc-1", None, None),
        (None, "pm10", None),
        ("", "pm10", None),
        ("sc-1", "  ", None),
    ],
)
def test_mark_key(sensor_id, observed_property, expected):
    assert main._mark_key(sensor_id, observed_property) == expected


# -- partial failure, the retry and the time bound ---------------------------------


def test_failed_page_is_asked_again_with_a_fifth_of_its_rows(monkeypatch, caplog):
    _use_target(monkeypatch, BASE)
    fake = _serve(
        monkeypatch,
        _page([_ds(1, "sc-1", "pm10", _instant("10:00"))], more=True),
        requests.Timeout("read timed out"),
        _page([_ds(2, "sc-2", "pm10", _instant("10:05"))], more=True),
        _page([_ds(3, "sc-3", "pm10", _instant("10:10"))]),
    )

    with caplog.at_level(logging.INFO, logger="connector.events"):
        seeded = main._seed_timestamps_from_frost("sensor_community", page_size=10)

    assert set(seeded) == {"sc-1::pm10", "sc-2::pm10", "sc-3::pm10"}
    assert [call["params"]["$top"] for call in fake.calls] == ["10", "10", "2", "2"]
    assert [call["params"]["$filter"] for call in fake.calls] == [
        SC_FILTER, f"{SC_FILTER} and @iot.id gt 1", f"{SC_FILTER} and @iot.id gt 1", f"{SC_FILTER} and @iot.id gt 2",
    ]
    # Never without the labels: a Datastream's own phenomenonTime cannot tell whose row is newest.
    assert all(call["params"]["$expand"] == EXPAND for call in fake.calls)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].startswith("Seeded 3 timestamps from FROST for sensor_community (3 datastreams in 3 page(s);")
    assert warnings[0].endswith("; page 2 asked again with 2 rows after Timeout: read timed out)")


def test_page_failing_twice_stops_the_sweep_with_the_pages_already_read(monkeypatch, caplog):
    _use_target(monkeypatch, BASE)
    fake = _serve(
        monkeypatch,
        _page([_ds(1, "sc-1", "pm10", _instant("10:00"))], more=True),
        requests.ConnectionError("connection reset"),
        requests.ConnectionError("connection reset"),
    )

    with caplog.at_level(logging.INFO, logger="connector.events"):
        seeded = main._seed_timestamps_from_frost("sensor_community", page_size=1)

    assert seeded == {"sc-1::pm10": _utc("2026-11-20T10:00:00")}
    assert [call["params"]["$filter"] for call in fake.calls] == [SC_FILTER] + [f"{SC_FILTER} and @iot.id gt 1"] * 2
    assert [call["params"]["$top"] for call in fake.calls] == ["1", "1", "1"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Seeded 1 timestamps from FROST for sensor_community, incomplete (page 2 failed: ConnectionError" in warnings[0]


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_Response({"error": "unauthorized"}, status=401), "page 1 failed: HTTP 401"),
        (_Response(ValueError("Expecting value")), "page 1 failed: ValueError: Expecting value"),
        (_Response(["not", "an", "object"]), "page 1 failed: TypeError"),
        (_Response({"value": "nope"}), "page 1 failed: TypeError: 'value' is not a list"),
        (requests.Timeout("read timed out"), "page 1 failed: Timeout"),
    ],
)
def test_failed_first_page_seeds_nothing_and_never_raises(monkeypatch, caplog, response, reason):
    _use_target(monkeypatch, BASE)
    fake = _serve(monkeypatch, response, response)

    with caplog.at_level(logging.WARNING, logger="connector.events"):
        assert main._seed_timestamps_from_frost("sensor_community") == {}

    assert [call["params"]["$top"] for call in fake.calls] == ["500", "100"]
    assert "Seeded 0 timestamps from FROST for sensor_community, incomplete" in caplog.text
    assert reason in caplog.text


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


def test_time_budget_bounds_the_sweep(monkeypatch, caplog):
    _use_target(monkeypatch, BASE)
    clock = _Clock()
    monkeypatch.setattr(main, "time", clock)
    calls: list[Any] = []

    def slow_get(url, params=None, headers=None, timeout=None):
        calls.append(timeout)
        clock.now += 4.0
        n = len(calls)
        return _page([_ds(n, f"sc-{n}", "pm10", _instant("10:00"))], more=True)

    monkeypatch.setattr(main.requests, "get", slow_get)

    with caplog.at_level(logging.WARNING, logger="connector.events"):
        seeded = main._seed_timestamps_from_frost(
            "sensor_community", page_size=1, time_budget_seconds=10.0, request_timeout_seconds=30.0
        )

    # Pages start at t=0, 4 and 8 s; the next one would start past the 10 s budget.
    assert set(seeded) == {"sc-1::pm10", "sc-2::pm10", "sc-3::pm10"}
    # No request may outlive the budget.
    assert calls == [10.0, 6.0, 2.0]
    assert "incomplete (time budget of 10s used up; 3 datastreams in 3 page(s)" in caplog.text


def test_time_budget_also_bounds_the_retry(monkeypatch, caplog):
    _use_target(monkeypatch, BASE)
    clock = _Clock()
    monkeypatch.setattr(main, "time", clock)
    calls: list[Any] = []

    def timing_out_get(url, params=None, headers=None, timeout=None):
        calls.append(timeout)
        clock.now += timeout
        raise requests.Timeout("read timed out")

    monkeypatch.setattr(main.requests, "get", timing_out_get)

    with caplog.at_level(logging.WARNING, logger="connector.events"):
        assert main._seed_timestamps_from_frost("sensor_community", time_budget_seconds=20.0) == {}

    assert calls == [20.0]
    assert "incomplete (time budget of 20s used up; 0 datastreams in 0 page(s)" in caplog.text
    assert "page 1 asked again with 100 rows after Timeout: read timed out" in caplog.text


def test_stops_when_a_page_gives_no_id_to_continue_after(monkeypatch, caplog):
    _use_target(monkeypatch, BASE)
    fake = _serve(monkeypatch, _page([_ds(None, "sc-1", "pm10", _instant("10:00"))], more=True))

    with caplog.at_level(logging.WARNING, logger="connector.events"):
        seeded = main._seed_timestamps_from_frost("sensor_community", page_size=1)

    assert seeded == {"sc-1::pm10": _utc("2026-11-20T10:00:00")}
    assert len(fake.calls) == 1
    assert "incomplete (page 1 gives no id to continue after" in caplog.text


def test_no_target_configured_seeds_nothing(monkeypatch):
    _use_target(monkeypatch, None)
    fake = _serve(monkeypatch)

    assert main._seed_timestamps_from_frost("sensor_community") == {}
    assert fake.calls == []


# -- polling loop wiring ---------------------------------------------------------


class _Stop(BaseException):
    """Ends the polling loop from inside a test."""


def test_polling_loop_seeds_with_the_source_name(monkeypatch):
    seeded_for: list[str] = []

    def fake_seed(source_name: str) -> dict:
        seeded_for.append(source_name)
        return {}

    class _Source:
        source_name = "sensor_community"

        def poll_interval(self) -> int:
            return 0

        def entity_sets(self) -> list:
            return []

        async def fetch_readings(self) -> list:
            raise _Stop

    monkeypatch.setattr(main, "_seed_timestamps_from_frost", fake_seed)

    with pytest.raises(_Stop):
        asyncio.run(main._polling_ingest_loop(_Source()))

    # Not "Sensor_community": Datastream names never started with that.
    assert seeded_for == ["sensor_community"]


def test_seeded_marks_drop_exactly_what_frost_holds(monkeypatch):
    """A restart in the middle of a look-back window: older readings are dropped, newer ones pushed."""
    _use_target(monkeypatch, BASE)
    _serve(monkeypatch, _page([_ds(1, "luchtmeetnet-NL01", "no2", _instant("09:00"))]))
    seeded = main._seed_timestamps_from_frost("luchtmeetnet")
    hours = [_reading("luchtmeetnet-NL01", "no2", f"{9 + h:02d}:00:00") for h in (-1, 0, 1)]

    assert [r.timestamp for r in main._dedup_readings(hours, seeded)] == [_utc("2026-11-20T10:00:00")]
