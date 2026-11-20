"""The polling loop registers the entity sets a source discovers after startup.

Before each push the loop registers the sets that are new since its last
registration (or whose registration failed), and only those.
"""

from __future__ import annotations

import asyncio
import types
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import app.main as main

SOURCE = "registration_test_source"  # not a real source: keeps health_monitor state apart
T0 = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


class _Stop(BaseException):
    """Ends the polling loop from inside a test."""


@dataclass
class _Reading:
    sensor_id: str
    observed_property: str
    timestamp: datetime


def _set(site: str, *props: str) -> dict[str, Any]:
    """An entity set with one sensor (``<site>-sensor``) observing ``props``."""
    return {
        "site_key": site,
        "thing": {"name": site},
        "sensors": [{"sensor_id": f"{site}-sensor", "observed_properties": list(props or ("pm10",))}],
    }


def _reading(site: str, minutes: int, prop: str = "pm10") -> _Reading:
    return _Reading(f"{site}-sensor", prop, T0 + timedelta(minutes=minutes))


def _complete(entity_set: dict[str, Any]) -> dict[str, Any]:
    """What ``client.register_entity_set`` returns after registering a whole set."""
    sensor = entity_set["sensors"][0]
    return {
        "mode": "live",
        "site_key": entity_set["site_key"],
        "primary_ok": True,
        "thing_id": "1",
        "location_id": "2",
        "sensor_ids": {sensor["sensor_id"]: "3"},
        "datastream_ids": {f"{sensor['sensor_id']}::{p}": "4" for p in sensor["observed_properties"]},
    }


class _Source:
    """Each fetch serves the next scripted cycle: the entity sets known after it, and its readings."""

    source_name = SOURCE

    def __init__(self, cycles: list[tuple[list[dict], list[_Reading]]], events: list, static: list | None = None):
        self._cycles = list(cycles)
        self._events = events
        self._sets = list(static or [])

    def poll_interval(self) -> int:
        return 300

    def entity_sets(self) -> list[dict[str, Any]]:
        return list(self._sets)

    async def fetch_readings(self) -> list[_Reading]:
        if not self._cycles:
            raise _Stop
        self._sets, readings = self._cycles.pop(0)
        self._events.append(("fetch",))
        return readings


class _Harness:
    """Fakes the FROST client, the seed and asyncio.sleep, and logs what the loop does in order."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple] = []
        self.seed: dict[str, datetime] = {}
        # site_key -> scripted outcomes of its next registrations ("ok", "raise", or a result dict)
        self.outcomes: dict[str, list[Any]] = {}
        monkeypatch.setattr(main.client, "register_entity_set", self._register)
        monkeypatch.setattr(main.client, "push_observations", self._push)
        monkeypatch.setattr(main, "_seed_timestamps_from_frost", self._seed)
        monkeypatch.setattr(main, "asyncio", types.SimpleNamespace(sleep=self._sleep))

    def _register(self, entity_set: dict[str, Any]) -> dict[str, Any]:
        site = entity_set["site_key"]
        self.events.append(("register", site))
        queue = self.outcomes.get(site)
        outcome = queue.pop(0) if queue else "ok"
        if outcome == "raise":
            raise RuntimeError("FROST unreachable")
        if isinstance(outcome, dict):
            return outcome
        return _complete(entity_set)

    def _push(self, readings: list[_Reading], *, source: str) -> dict[str, Any]:
        self.events.append(("push", sorted(f"{r.sensor_id}@{r.timestamp:%H:%M}" for r in readings)))
        return {"total_sent": len(readings)}

    def _seed(self, source_name: str) -> dict[str, datetime]:
        self.events.append(("seed",))
        return dict(self.seed)

    async def _sleep(self, seconds: float) -> None:
        self.events.append(("sleep", seconds))

    def run(self, source: _Source) -> list[tuple]:
        with pytest.raises(_Stop):
            asyncio.run(main._polling_ingest_loop(source))
        return self.events

    def registrations(self) -> list[str]:
        return [event[1] for event in self.events if event[0] == "register"]


@pytest.fixture()
def harness(monkeypatch) -> _Harness:
    return _Harness(monkeypatch)


def _cycles(*events: tuple) -> list[list[tuple]]:
    """Split an event log into polling cycles, one per fetch."""
    cycles: list[list[tuple]] = []
    for event in events:
        if event[0] == "fetch":
            cycles.append([])
        elif cycles:
            cycles[-1].append(event)
    return cycles


def test_new_entity_sets_are_registered_before_the_push_that_needs_them(harness):
    a, b = _set("site-a"), _set("site-b")
    source = _Source(
        [
            ([a], [_reading("site-a", 0)]),
            ([a, b], [_reading("site-a", 5), _reading("site-b", 5)]),
        ],
        harness.events,
    )

    events = harness.run(source)

    first, second = _cycles(*events)
    assert ("register", "site-a") in first
    assert first.index(("register", "site-a")) < first.index(("push", ["site-a-sensor@10:00"]))
    # Only the set discovered since, and before the push that carries its readings.
    assert [e for e in second if e[0] in ("register", "push")] == [
        ("register", "site-b"),
        ("push", ["site-a-sensor@10:05", "site-b-sensor@10:05"]),
    ]


def test_registered_entity_sets_are_not_registered_again(harness):
    a, b = _set("site-a"), _set("site-b")
    source = _Source(
        [([a, b], [_reading("site-a", m), _reading("site-b", m)]) for m in (0, 5, 10)],
        harness.events,
    )

    events = harness.run(source)

    assert harness.registrations() == ["site-a", "site-b"]
    assert [len([e for e in cycle if e[0] == "push"]) for cycle in _cycles(*events)] == [1, 1, 1]


def test_static_entity_sets_are_registered_at_startup_only(harness):
    a = _set("site-a")
    source = _Source([([a], [_reading("site-a", m)]) for m in (0, 5)], harness.events, static=[a])

    events = harness.run(source)

    assert events[0] == ("register", "site-a")
    assert harness.registrations() == ["site-a"]


@pytest.mark.parametrize(
    "failure",
    [
        "raise",
        {"mode": "live", "primary_ok": False, "primary_error": "Primary target unreachable (see logs)."},
        {**_complete(_set("site-b")), "datastream_ids": {"site-b-sensor::pm10": None}},
        {**_complete(_set("site-b")), "sensor_ids": {"site-b-sensor": None}, "datastream_ids": {}},
        {**_complete(_set("site-b")), "location_id": None},
    ],
    ids=["raises", "primary-failed", "datastream-missing", "sensor-missing", "location-missing"],
)
def test_failed_registration_is_retried_next_cycle(harness, failure):
    a, b = _set("site-a"), _set("site-b")
    harness.outcomes["site-b"] = [failure]
    source = _Source(
        [([a, b], [_reading("site-a", m), _reading("site-b", m)]) for m in (0, 5, 10)],
        harness.events,
    )

    events = harness.run(source)

    first, second, third = _cycles(*events)
    assert [e for e in first if e[0] == "register"] == [("register", "site-a"), ("register", "site-b")]
    # The push goes ahead (as before): one set registered is enough.
    assert first[-2][0] == "push"
    assert [e for e in second if e[0] in ("register", "push")] == [
        ("register", "site-b"),
        ("push", ["site-a-sensor@10:05", "site-b-sensor@10:05"]),
    ]
    assert [e for e in third if e[0] == "register"] == []


def test_entity_set_that_gains_an_observed_property_is_registered_again(harness):
    source = _Source(
        [
            ([_set("site-a", "pm10")], [_reading("site-a", 0)]),
            ([_set("site-a", "pm10", "temperature")], [_reading("site-a", 5), _reading("site-a", 5, "temperature")]),
            ([_set("site-a", "temperature", "pm10")], [_reading("site-a", 10)]),
        ],
        harness.events,
    )

    harness.run(source)

    assert harness.registrations() == ["site-a", "site-a"]


def test_failures_in_a_row_defer_the_rest_to_the_next_cycle(harness):
    sets = [_set(f"site-{n}") for n in range(1, 6)]
    for site in ("site-1", "site-2", "site-3"):
        harness.outcomes[site] = ["raise"]  # three failures in a row: FROST looks down
    source = _Source([(sets, [_reading("site-1", m)]) for m in (0, 5)], harness.events)

    events = harness.run(source)

    first, second = _cycles(*events)
    assert [e[1] for e in first if e[0] == "register"] == ["site-1", "site-2", "site-3"]
    # Sets not tried yet go first, those that failed after them.
    assert [e[1] for e in second if e[0] == "register"] == ["site-4", "site-5", "site-1", "site-2", "site-3"]


def test_skipped_push_waits_for_the_next_cycle(harness):
    # Nothing registered and nothing seeded: the push is skipped, and the loop
    # still sleeps before it fetches again.
    harness.outcomes["site-a"] = ["raise", "raise"]
    source = _Source([([_set("site-a")], [_reading("site-a", m)]) for m in (0, 5)], harness.events)

    events = harness.run(source)

    assert not [e for e in events if e[0] == "push"]
    assert [e for e in events if e[0] in ("fetch", "sleep")] == [("fetch",), ("sleep", 300), ("fetch",), ("sleep", 300)]


def test_seeds_again_after_the_first_registration_when_nothing_was_seeded(harness):
    source = _Source([([_set("site-a")], [_reading("site-a", m)]) for m in (0, 5)], harness.events)

    events = harness.run(source)

    first, second = _cycles(*events)
    assert events[0] == ("seed",)
    assert first[:2] == [("register", "site-a"), ("seed",)]
    assert ("seed",) not in second


def test_does_not_seed_again_when_the_startup_seed_found_marks(harness):
    harness.seed = {"site-a-sensor::pm10": T0}
    source = _Source([([_set("site-a")], [_reading("site-a", 0), _reading("site-a", 5)])], harness.events)

    events = harness.run(source)

    assert events.count(("seed",)) == 1
    # The seeded mark still drops the reading FROST already holds.
    assert ("push", ["site-a-sensor@10:05"]) in events


def test_eager_registration_retries_what_failed(harness):
    a, b = _set("site-a"), _set("site-b")
    harness.outcomes["site-b"] = ["raise"]
    source = _Source([([a, b], [_reading("site-a", 0)])], harness.events, static=[a, b])

    events = harness.run(source)

    startup = events[: events.index(("fetch",))]
    assert startup == [("register", "site-a"), ("register", "site-b"), ("sleep", 10), ("register", "site-b"), ("seed",)]
    assert [e for e in _cycles(*events)[0] if e[0] == "register"] == []


@pytest.mark.parametrize(
    ("result", "complete"),
    [
        (_complete(_set("s", "pm10", "pm2_5")), True),
        # Non-canonical observed properties are skipped by the client, not failed.
        ({**_complete(_set("s")), "datastream_ids": {}}, True),
        ({"mode": "preview", "message": "No SensorThings server configured."}, True),
        ({**_complete(_set("s")), "primary_ok": False}, False),
        ({**_complete(_set("s")), "thing_id": None}, False),
        ({**_complete(_set("s")), "location_id": None}, False),
        ({**_complete(_set("s")), "sensor_ids": {"s-sensor": None}}, False),
        ({**_complete(_set("s", "pm10", "pm2_5")), "datastream_ids": {"s-sensor::pm10": "4", "s-sensor::pm2_5": None}}, False),
        (None, False),
        ("unexpected", False),
    ],
)
def test_registration_complete(result, complete):
    assert main._registration_complete(result) is complete
