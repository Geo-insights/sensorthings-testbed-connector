"""High-water marks of the polling loops: what advances them, and what they drop.

* A reading that got no datastream on the primary FROST target (its entity set
  failed to register, or was deferred) was neither sent nor dead-lettered, so
  its mark stays and the next cycle pushes it if the source serves it again.
* A mark is the latest timestamp pushed for its key, whatever order the source
  lists its readings in (Sensor.Community: newest first).
* Exact repeats within one fetch are pushed once; same-time readings with other
  values or of another sensor type (co-located sensors share a sensor_id) all are.
* POST /connector/ohnics-push runs its blocking FROST calls in the threadpool.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import time
import types
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import app.main as main
import app.routes.connector as connector
from app.models import SensorReading
from app.sources import sensor_community

PRIMARY = "https://frost.test/FROST-Server/v2.0"
SECONDARY = "https://other.test/FROST-Server/v1.1"
NO_DATASTREAM = "No datastream mapping available for this sensor; dynamic lookup also failed."
T9 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


class _Stop(BaseException):
    """Ends the polling loop from inside a test."""


def _reading(sensor_id: str, prop: str, at: datetime, value: float = 1.0) -> SensorReading:
    return SensorReading(
        sensor_id=sensor_id, sensor_name=f"{sensor_id} sensor", observed_property=prop, unit="1", value=value,
        timestamp=at,
    )


def _sc_entry(
    timestamp: str, p1: float, p2: float, location: str = "4711", sensor_type: str = "SDS011"
) -> dict[str, Any]:
    """One entry of the Sensor.Community API: a particulate sensor's PM10 (P1) and PM2.5 (P2)."""
    return {
        "location": {"id": location},
        "sensor": {"sensor_type": {"name": sensor_type}},
        "timestamp": timestamp,
        "sensordatavalues": [{"value_type": "P1", "value": str(p1)}, {"value_type": "P2", "value": str(p2)}],
    }


def _station(number: str) -> dict[str, Any]:
    """A Luchtmeetnet-like entity set: every station has site_key "luchtmeetnet"."""
    return {
        "site_key": "luchtmeetnet",
        "thing": {"name": f"Luchtmeetnet {number}"},
        "sensors": [{"sensor_id": f"luchtmeetnet-{number}", "observed_properties": ["no2"]}],
    }


class _Frost:
    """Fakes register_entity_set and push_observations the way the client behaves.

    A reading of a sensor whose entity set is not registered gets no datastream:
    push_observations leaves it unresolved (not sent, not dead-lettered).
    ``flag`` adds "unresolved": True as the routing follow-up does; "flag-only"
    also changes the message, so only the flag tells.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, flag: str = "message") -> None:
        self.flag = flag
        self.sensors: set[str] = set()
        self.stored: list[tuple[str, str, datetime]] = []
        self.failures: dict[str, int] = {}  # Thing name -> registrations still to fail
        self.push_errors = 0  # pushes still to raise
        self.pushes: list[list[str]] = []
        monkeypatch.setattr(main.client, "register_entity_set", self.register)
        monkeypatch.setattr(main.client, "push_observations", self.push)
        monkeypatch.setattr(main.client, "_primary_base_url", lambda: PRIMARY)
        monkeypatch.setattr(main, "_seed_timestamps_from_frost", lambda source_name: {})

        async def no_sleep(_seconds: float) -> None:
            return None

        monkeypatch.setattr(main, "asyncio", types.SimpleNamespace(sleep=no_sleep))

    def register(self, entity_set: dict[str, Any]) -> dict[str, Any]:
        name = entity_set["thing"]["name"]
        if self.failures.get(name):
            self.failures[name] -= 1
            return {"mode": "live", "primary_ok": False, "primary_error": "Primary target unreachable (see logs)."}
        (sensor,) = entity_set["sensors"]
        self.sensors.add(sensor["sensor_id"])
        return {
            "mode": "live", "primary_ok": True, "thing_id": "1", "location_id": "2",
            "sensor_ids": {sensor["sensor_id"]: "3"},
            "datastream_ids": {f"{sensor['sensor_id']}::{p}": "4" for p in sensor["observed_properties"]},
        }

    def push(self, readings: list[SensorReading], *, source: str) -> dict[str, Any]:
        self.pushes.append(sorted(f"{r.sensor_id}@{r.timestamp:%H:%M}" for r in readings))
        if self.push_errors:
            self.push_errors -= 1
            raise RuntimeError("connection reset by FROST")
        results: list[dict[str, Any]] = []
        for r in readings:
            if r.sensor_id in self.sensors:
                self.stored.append((r.sensor_id, r.observed_property, r.timestamp))
                results.append({"ok": True, "status_code": 201, "datastream_id": "4", "sensor_id": r.sensor_id})
                continue
            entry: dict[str, Any] = {
                "sensor_id": r.sensor_id, "observed_property": r.observed_property, "ok": False,
                "message": "no datastream" if self.flag == "flag-only" else NO_DATASTREAM,
            }
            if self.flag != "message":
                entry["unresolved"] = True
            results.append(entry)
        sent = sum(1 for entry in results if entry["ok"])
        return {"mode": "live", "endpoint": f"{PRIMARY}/Observations", "sent": sent, "total_sent": sent,
                "results": results}


class _Source:
    """Discovers its entity sets with its first fetch; each fetch serves the next scripted readings."""

    source_name = "luchtmeetnet"

    def __init__(self, sets: list[dict[str, Any]], fetches: list[list[SensorReading]]) -> None:
        self._sets = sets
        self._fetches = list(fetches)
        self._discovered = False

    def poll_interval(self) -> int:
        return 3600

    def entity_sets(self) -> list[dict[str, Any]]:
        return list(self._sets) if self._discovered else []

    async def fetch_readings(self) -> list[SensorReading]:
        if not self._fetches:
            raise _Stop
        self._discovered = True
        return self._fetches.pop(0)


def _run(source: _Source) -> None:
    with pytest.raises(_Stop):
        asyncio.run(main._polling_ingest_loop(source))


# -- readings without a datastream keep their marks ---------------------------------


@pytest.mark.parametrize("flag", ["message", "flag", "flag-only"])
def test_readings_of_deferred_new_sets_are_pushed_once_registered(monkeypatch, flag):
    """Luchtmeetnet right after deploy, no Datastreams yet, and three registrations fail in a row.

    NL05 and NL06 are deferred, NL02-NL04 failed: their 09:00 readings get no
    datastream. The next cycle registers them, and its 3 h look-back serves the
    09:00 readings again with the 10:00 ones: all are pushed, each once.
    """
    frost = _Frost(monkeypatch, flag)
    numbers = [f"NL0{n}" for n in range(1, 7)]
    frost.failures = {"Luchtmeetnet NL02": 1, "Luchtmeetnet NL03": 1, "Luchtmeetnet NL04": 1}
    first = [_reading(f"luchtmeetnet-{n}", "no2", T9) for n in numbers]
    second = first + [_reading(f"luchtmeetnet-{n}", "no2", T9 + timedelta(hours=1)) for n in numbers]

    _run(_Source([_station(n) for n in numbers], [first, second]))

    assert frost.sensors == {f"luchtmeetnet-{n}" for n in numbers}
    assert sorted(frost.stored) == sorted(
        (f"luchtmeetnet-{n}", "no2", at) for n in numbers for at in (T9, T9 + timedelta(hours=1))
    )
    # NL01's 09:00 reading went through in the first cycle and is not posted again.
    assert "luchtmeetnet-NL01@09:00" not in frost.pushes[1]


def test_push_that_raises_advances_no_mark(monkeypatch):
    frost = _Frost(monkeypatch)
    frost.push_errors = 1
    readings = [_reading("luchtmeetnet-NL01", "no2", T9)]

    _run(_Source([_station("NL01")], [readings, readings]))

    assert frost.pushes == [["luchtmeetnet-NL01@09:00"], ["luchtmeetnet-NL01@09:00"]]
    assert frost.stored == [("luchtmeetnet-NL01", "no2", T9)]


def test_dead_lettered_readings_advance_their_marks(monkeypatch):
    # A failed POST goes to the DLQ, which replays it: posting it again would duplicate it.
    frost = _Frost(monkeypatch)
    readings = [_reading("luchtmeetnet-NL01", "no2", T9)]

    def push_failing(batch: list[SensorReading], *, source: str) -> dict[str, Any]:
        frost.pushes.append([f"{r.sensor_id}@{r.timestamp:%H:%M}" for r in batch])
        return {"mode": "live", "sent": 0, "total_sent": 0, "results": [
            {"ok": False, "status_code": 503, "error": "Service Unavailable", "dead_lettered": True}
        ]}

    monkeypatch.setattr(main.client, "push_observations", push_failing)

    _run(_Source([_station("NL01")], [readings, readings]))

    assert frost.pushes == [["luchtmeetnet-NL01@09:00"]]


def _result(*entries: dict[str, Any]) -> dict[str, Any]:
    return {"mode": "live", "sent": 0, "total_sent": 0, "results": list(entries)}


def _unresolved(sensor_id: str, prop: str, **extra: Any) -> dict[str, Any]:
    return {"sensor_id": sensor_id, "observed_property": prop, "ok": False, "message": NO_DATASTREAM, **extra}


@pytest.mark.parametrize(
    ("entry", "unresolved"),
    [
        (_unresolved("a", "no2"), True),
        (_unresolved("a", "no2", refused=["17"]), True),
        (_unresolved("a", "no2", unresolved=True), True),
        ({"sensor_id": "a", "observed_property": "no2", "ok": False, "unresolved": True, "message": "x"}, True),
        ({"ok": False, "status_code": 400, "error": "Bad Request", "message": "Invalid"}, False),
        ({"ok": False, "status_code": 503, "error": "No datastream mapping (sic)"}, False),
        ({"ok": True, "message": NO_DATASTREAM}, False),
        ({"unresolved": "yes", "ok": True}, False),
        ("not an entry", False),
        (None, False),
    ],
)
def test_is_unresolved(entry, unresolved):
    assert main._is_unresolved(entry) is unresolved


def test_marks_follow_the_primary_target_only(monkeypatch):
    monkeypatch.setattr(main.client, "_primary_base_url", lambda: PRIMARY)
    readings = [_reading("k", "no2", T9), _reading("j", "no2", T9), _reading("m", "pm2_5", T9)]
    result = {
        "mode": "live", "multi_target": True, "sent": 2, "total_sent": 2,
        "targets": [
            {"base_url": SECONDARY, "results": [_unresolved("j", "no2")]},
            {"base_url": PRIMARY + "/", "results": [_unresolved("k", "no2"), {"ok": True}]},
        ],
    }

    assert [r.sensor_id for r in main._readings_to_mark(readings, result)] == ["j", "m"]


def test_marks_of_every_target_count_when_the_primary_is_not_listed(monkeypatch):
    monkeypatch.setattr(main.client, "_primary_base_url", lambda: PRIMARY)
    readings = [_reading("k", "no2", T9), _reading("j", "no2", T9), _reading("m", "no2", T9)]
    result = {"mode": "live", "multi_target": True, "targets": [
        {"base_url": SECONDARY, "results": [_unresolved("j", "no2")]},
        {"base_url": "https://third.test/v1.1", "results": [_unresolved("k", "no2")]},
    ]}

    assert [r.sensor_id for r in main._readings_to_mark(readings, result)] == ["m"]


@pytest.mark.parametrize(
    "result",
    [
        {"mode": "preview", "message": "No SensorThings server configured.", "preview": {}},
        {"total_sent": 3},
        None,
        _result({"ok": True}),
    ],
)
def test_without_unresolved_entries_every_mark_advances(result):
    readings = [_reading("k", "no2", T9), _reading("j", "no2", T9)]

    assert main._readings_to_mark(readings, result) == readings


def test_unresolved_entries_match_readings_by_their_canonical_key():
    readings = [_reading("ohnics-de-1", "pm2_5", T9), _reading("ohnics-de-1", "air_temperature", T9)]

    marked = main._readings_to_mark(readings, _result(_unresolved("ohnics-de-1", "pm25")))

    assert [r.observed_property for r in marked] == ["air_temperature"]


# -- marks are the latest timestamp per key ------------------------------------------


def test_marks_take_the_newest_timestamp_of_a_sensor_community_batch():
    # The API lists a sensor's measurements newest first.
    readings = sensor_community.parse_sensor_data(
        [_sc_entry("2026-11-20 10:05:00", 11, 7), _sc_entry("2026-11-20 10:02:30", 12, 8)]
    )
    marks: dict[str, datetime] = {"sensor_community-4711::pm10": datetime(2026, 11, 20, 10, 3, tzinfo=UTC)}

    main._update_timestamps(readings, marks)

    assert marks == {
        "sensor_community-4711::pm10": datetime(2026, 11, 20, 10, 5, tzinfo=UTC),
        "sensor_community-4711::pm2_5": datetime(2026, 11, 20, 10, 5, tzinfo=UTC),
    }
    # An older reading never moves a mark back.
    main._update_timestamps(readings[-2:], marks)
    assert marks["sensor_community-4711::pm10"] == datetime(2026, 11, 20, 10, 5, tzinfo=UTC)


def test_overlapping_sensor_community_fetches_post_each_reading_once(monkeypatch):
    frost = _Frost(monkeypatch)
    location = {
        "site_key": "sensor_community_4711",
        "thing": {"name": "SensorCommunity 4711"},
        "sensors": [{"sensor_id": "sensor_community-4711", "observed_properties": ["pm10", "pm2_5"]}],
    }
    fetches = [
        sensor_community.parse_sensor_data([_sc_entry("2026-11-20 10:05:00", 11, 7),
                                            _sc_entry("2026-11-20 10:02:30", 12, 8)]),
        sensor_community.parse_sensor_data([_sc_entry("2026-11-20 10:07:30", 13, 9),
                                            _sc_entry("2026-11-20 10:05:00", 11, 7)]),
    ]
    source = _Source([location], fetches)
    source.source_name = "sensor_community"

    _run(source)

    # The second fetch repeats 10:05, which the first one pushed: only 10:07:30 is new.
    assert frost.pushes[1] == ["sensor_community-4711@10:07", "sensor_community-4711@10:07"]
    assert len(frost.stored) == len(set(frost.stored)) == 6


# -- repeats within one fetch ----------------------------------------------------------


def test_exact_repeats_in_one_fetch_are_pushed_once_and_co_located_sensors_all():
    readings = sensor_community.parse_sensor_data([
        _sc_entry("2026-11-20 10:10:00", 14, 10),
        _sc_entry("2026-11-20 10:10:00", 14, 10),  # the API lists the same sensor twice
        _sc_entry("2026-11-20 10:10:00", 20, 15),  # a second SDS011 at the location: same key and time
        _sc_entry("2026-11-20 10:10:00", 14, 10, sensor_type="SPS30"),  # another type, same values
        _sc_entry("2026-11-20 10:10:00", 14, 10, location="4712"),  # another location: another key
    ])

    new = main._dedup_readings(readings, {})

    assert sorted((r.sensor_id, r.observed_property, r.value) for r in new) == [
        ("sensor_community-4711", "pm10", 14.0),
        ("sensor_community-4711", "pm10", 14.0),
        ("sensor_community-4711", "pm10", 20.0),
        ("sensor_community-4711", "pm2_5", 10.0),
        ("sensor_community-4711", "pm2_5", 10.0),
        ("sensor_community-4711", "pm2_5", 15.0),
        ("sensor_community-4712", "pm10", 14.0),
        ("sensor_community-4712", "pm2_5", 10.0),
    ]


def test_repeats_are_only_dropped_within_one_batch():
    reading = _reading("sensor_community-4711", "pm10", T9, 14.0)

    assert main._dedup_readings([reading], {}) == [reading]
    assert main._dedup_readings([reading], {}) == [reading]


# -- POST /connector/ohnics-push ------------------------------------------------------


def test_ohnics_push_route_keeps_the_event_loop_serving(monkeypatch):
    """Each FROST call of the route (register, seed, push) blocks; none may hold up the event loop."""
    from app.services.ohnics_source import OhnicsPollingSource

    reading = _reading("ohnics-de-1", "pm2_5", T9)
    calls: list[str] = []

    async def fetch(self: Any) -> list[SensorReading]:
        return [reading]

    def blocking(name: str, value: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            time.sleep(0.4)  # a FROST that answers slowly
            return value
        return call

    monkeypatch.setattr(OhnicsPollingSource, "fetch_readings", fetch)
    monkeypatch.setattr(OhnicsPollingSource, "entity_sets", lambda self: [{"site_key": "delft"}])
    monkeypatch.setattr(main, "_seed_timestamps_from_frost", blocking("seed", {}))
    monkeypatch.setattr(connector.client, "register_entity_set", blocking("register", {"mode": "live"}))
    monkeypatch.setattr(connector.client, "push_observations", blocking("push", {"total_sent": 1}))
    monkeypatch.setattr(connector, "settings", dataclasses.replace(connector.settings, ohnics_enabled=True))

    async def scenario() -> tuple[dict[str, Any], list[float]]:
        ticks: list[float] = []

        async def ticker() -> None:
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        started = time.monotonic()
        response = await connector.ohnics_push()
        ended = time.monotonic()
        task.cancel()
        moments = [started, *(tick for tick in ticks if started < tick < ended), ended]
        return response, [later - earlier for earlier, later in itertools.pairwise(moments)]

    response, gaps = asyncio.run(scenario())

    assert calls == ["register", "seed", "push"]
    assert response["pushed"] == 1 and response["new"] == 1
    assert sum(gaps) >= 1.2
    # Other coroutines kept running throughout: the loop never stalled for a whole FROST call.
    assert max(gaps) < 0.25
