"""Batched polling cycles: a source that pushes a cycle in several batches.

A Samen Meten cycle reads 11,000+ datastreams and can take two hours, so its
readings go to FROST in batches while it fetches. Before each batch's push the
polling loop registers the entity sets of that batch's readings, and after the
cycle's last batch the rest. Each delivery counts for the source's freshness,
and the longest wait for data in a cycle that delivered widens its stall
threshold (tests/test_stall_threshold.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import types
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import app.main as main
import app.services.alerting as alerting
import app.services.samen_meten_source as samen_meten_source
from app.models import SensorReading
from app.services.health_monitor import health_monitor
from app.services.rest_polling_source import RestPollingSource
from app.services.samen_meten_source import SamenMetenPollingSource

SOURCE = "batches_test_source"  # not a real source: keeps health_monitor state apart
T0 = datetime(2026, 11, 26, 14, 0, tzinfo=UTC)


class _Stop(BaseException):
    """Ends the polling loop from inside a test."""


@dataclass
class _Reading:
    sensor_id: str
    observed_property: str
    timestamp: datetime
    value: float = 1.0


def _set(name: str) -> dict[str, Any]:
    """An entity set: Thing ``name`` with one sensor (``<name>-sensor``) observing pm10."""
    return {
        "site_key": SOURCE,
        "thing": {"name": name},
        "sensors": [{"sensor_id": f"{name}-sensor", "observed_properties": ["pm10"]}],
    }


def _reading(name: str, minutes: int = 0) -> _Reading:
    return _Reading(f"{name}-sensor", "pm10", T0 + timedelta(minutes=minutes))


class _Clock:
    """Stands in for time.monotonic in app.main; the fake source advances it."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def monotonic(self) -> float:
        return self.now


class _BatchedSource:
    """Serves scripted cycles; a cycle is a list of batches (seconds its fetch takes, readings).

    Its entity sets become known with its first fetch (dynamic discovery, as
    Samen Meten's), and it names a batch's own sets (``entity_sets_for``).
    """

    source_name = SOURCE

    def __init__(self, cycles: list[list[tuple[float, list]]], sets: list[dict], harness: _Harness) -> None:
        self._cycles = list(cycles)
        self._all_sets = sets
        self._known: list[dict] = []
        self._harness = harness

    def poll_interval(self) -> int:
        return 300

    def entity_sets(self) -> list[dict[str, Any]]:
        return list(self._known)

    def entity_sets_for(self, readings: list[_Reading]) -> list[dict[str, Any]]:
        sensor_ids = {reading.sensor_id for reading in readings}
        return [s for s in self._known if s["sensors"][0]["sensor_id"] in sensor_ids]

    async def fetch_reading_batches(self):
        if not self._cycles:
            raise _Stop
        batches = self._cycles.pop(0)
        self._known = list(self._all_sets)
        self._harness.events.append(("fetch",))
        try:
            for seconds, readings in batches:
                self._harness.clock.now += seconds
                yield readings
        finally:
            self._harness.events.append(("fetch closed",))


class _Harness:
    """Fakes the FROST client, the seed, asyncio.sleep, the clock and alerts; logs what the loop does."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple] = []
        self.clock = _Clock()
        self.raise_on_push: set[str] = set()  # sensor ids whose push raises
        self.ages: list[float] = []  # what source_age_seconds returns after each cycle, if set
        self.alerts: list[dict] = []
        monkeypatch.setattr(main.client, "register_entity_set", self._register)
        monkeypatch.setattr(main.client, "push_observations", self._push)
        monkeypatch.setattr(main, "_seed_timestamps_from_frost", self._seed)
        monkeypatch.setattr(main, "asyncio", types.SimpleNamespace(sleep=self._sleep))
        monkeypatch.setattr(main, "time", types.SimpleNamespace(monotonic=self.clock.monotonic))
        monkeypatch.setattr(alerting, "send_alert", self._alert)
        for state in ("_source_last_success", "_source_success_counts", "_source_last_error", "_source_longest_wait"):
            monkeypatch.setattr(health_monitor, state, {})
        real_age = health_monitor.source_age_seconds
        monkeypatch.setattr(
            health_monitor, "source_age_seconds", lambda name: self.ages.pop(0) if self.ages else real_age(name)
        )

    def _register(self, entity_set: dict[str, Any]) -> dict[str, Any]:
        self.events.append(("register", entity_set["thing"]["name"]))
        sensor = entity_set["sensors"][0]
        return {
            "mode": "live",
            "primary_ok": True,
            "thing_id": "1",
            "location_id": "2",
            "sensor_ids": {sensor["sensor_id"]: "3"},
            "datastream_ids": {f"{sensor['sensor_id']}::pm10": "4"},
        }

    def _push(self, readings: list[_Reading], *, source: str) -> dict[str, Any]:
        sensor_ids = sorted(r.sensor_id for r in readings)
        self.events.append(("push", sensor_ids))
        if self.raise_on_push & set(sensor_ids):
            raise RuntimeError("FROST refused the batch")
        return {"total_sent": len(readings)}

    def _seed(self, source_name: str) -> dict[str, datetime]:
        return {}

    async def _sleep(self, seconds: float) -> None:
        self.events.append(("sleep", seconds))

    def _alert(self, event: str, message: str, **kwargs: Any) -> bool:
        self.alerts.append({"event": event, **kwargs.get("context", {})})
        return True

    def run(self, source: _BatchedSource) -> list[tuple]:
        with pytest.raises(_Stop):
            asyncio.run(main._polling_ingest_loop(source))
        return self.events


@pytest.fixture()
def harness(monkeypatch) -> _Harness:
    return _Harness(monkeypatch)


def _register_and_push(events: list[tuple]) -> list[tuple]:
    return [event for event in events if event[0] in ("register", "push", "fetch closed")]


# ---------------------------------------------------------------------------
# The polling loop
# ---------------------------------------------------------------------------


def test_each_batch_is_pushed_after_registering_its_own_sets(harness):
    """protects=a batch waits only for the registration of its own readings' sets; the rest follow the last batch;
    fails_when=the loop registers every set the source knows before the first push (5,099 for Samen Meten
    after a restart, 20 minutes) or never registers the sets without readings;
    no_claim=what FROST does with the registrations."""
    a, b, c = _set("a"), _set("b"), _set("c")
    source = _BatchedSource([[(60, [_reading("a")]), (60, [_reading("b")])]], [a, b, c], harness)

    events = harness.run(source)

    assert _register_and_push(events) == [
        ("register", "a"),
        ("push", ["a-sensor"]),
        ("register", "b"),
        ("push", ["b-sensor"]),
        ("fetch closed",),
        ("register", "c"),
    ]


def test_a_push_that_raises_ends_the_cycle_and_its_fetch(harness, caplog):
    """protects=a push that raises stops the cycle's fetch at once and the loop goes on with the next cycle;
    fails_when=the fetch keeps running (or leaks its tasks) after the loop stopped reading its batches;
    no_claim=the marks of the refused batch (test_main_registration covers marks)."""
    a, b, c = _set("a"), _set("b"), _set("c")
    harness.raise_on_push = {"b-sensor"}
    source = _BatchedSource(
        [
            [(60, [_reading("a")]), (60, [_reading("b")]), (60, [_reading("c")])],
            [(60, [_reading("c", 5)])],
        ],
        [a, b, c],
        harness,
    )

    with caplog.at_level(logging.ERROR, logger="connector.events"):
        events = harness.run(source)

    pushes_and_closes = [event for event in events if event[0] in ("push", "fetch closed", "fetch")]
    assert pushes_and_closes == [
        ("fetch",),
        ("push", ["a-sensor"]),
        ("push", ["b-sensor"]),
        ("fetch closed",),
        ("fetch",),
        ("push", ["c-sensor"]),
        ("fetch closed",),
    ]
    (failure,) = [r for r in caplog.records if r.getMessage() == f"{SOURCE} polling cycle failed"]
    assert failure.exc_info is not None
    assert "FROST refused the batch" in str(failure.exc_info[1])


def test_every_batch_counts_for_freshness(harness):
    """protects=each delivered batch records a source success, so a long cycle keeps its source fresh;
    fails_when=success is recorded once per cycle (at its end);
    no_claim=the wall-clock age itself."""
    source = _BatchedSource([[(60, [_reading("a")]), (60, [_reading("b")])]], [_set("a"), _set("b")], harness)

    harness.run(source)

    assert health_monitor._source_success_counts[SOURCE] == 2


def test_the_longest_wait_for_data_in_a_cycle_is_recorded(harness):
    """protects=the cycle's longest stretch without data (start to first push, between pushes, last push to end);
    fails_when=the wait is measured as the whole cycle, or misses the stretch before the first push;
    no_claim=how the threshold uses it (tests/test_stall_threshold.py)."""
    source = _BatchedSource(
        [[(100, [_reading("a")]), (300, [_reading("b")]), (50, [])]],
        [_set("a"), _set("b")],
        harness,
    )

    harness.run(source)

    assert health_monitor.source_longest_wait(SOURCE) == 300


def test_a_cycle_that_delivered_nothing_keeps_the_last_wait(harness):
    """protects=a stall cannot widen its own threshold;
    fails_when=a cycle without data records its (long) duration as the wait;
    no_claim=alerting during that cycle."""
    source = _BatchedSource(
        [[(100, [_reading("a")])], [(5_000, [])]],
        [_set("a")],
        harness,
    )

    harness.run(source)

    assert health_monitor.source_longest_wait(SOURCE) == 100


def test_the_watchdog_alerts_past_the_widened_threshold(harness, monkeypatch):
    """protects=the in-loop stall alert compares the age with poll + grace + 2 x the longest wait;
    fails_when=the watchdog keeps the fixed poll + grace (1,200 s for Samen Meten, whose cycles take hours);
    no_claim=where the alert goes."""
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, freshness_grace_seconds=600))
    source = _BatchedSource([[(300, [_reading("a")])], [(300, [_reading("a", 5)])]], [_set("a")], harness)
    harness.ages = [1_400.0, 1_600.0]  # threshold 300 + 600 + 2 x 300 = 1,500

    harness.run(source)

    assert harness.alerts == [
        {"event": f"{SOURCE}.stall", "age_seconds": 1_600.0, "threshold_seconds": 1_500, "api_url": "unknown"}
    ]


# ---------------------------------------------------------------------------
# RestPollingSource: batches with health bookkeeping
# ---------------------------------------------------------------------------


def _sensor_reading(sensor_id: str) -> SensorReading:
    return SensorReading(
        sensor_id=sensor_id, sensor_name=sensor_id, observed_property="pm10", unit="ug/m3", value=1.0, timestamp=T0
    )


class _RestBatches(RestPollingSource):
    """Yields scripted batches; an exception in the script is raised where it stands."""

    source_name = "rest_batches_test_source"

    def __init__(self, script: list[Any]) -> None:
        super().__init__()
        self.script = script
        self.closed = False
        self.empty_alerts = 0

    def is_enabled(self) -> bool:
        return True

    def poll_interval(self) -> int:
        return 60

    def entity_sets(self) -> list[dict[str, Any]]:
        return []

    def _emit_empty_cycle_alert(self) -> None:
        self.empty_alerts += 1

    async def _do_fetch_readings(self) -> list[SensorReading]:
        raise AssertionError("this source yields batches")

    async def _do_fetch_reading_batches(self):
        try:
            for step in self.script:
                if isinstance(step, Exception):
                    raise step
                yield step
        finally:
            self.closed = True


def test_a_fetch_failing_part_way_keeps_the_earlier_batches_and_records_the_error(monkeypatch):
    """protects=batches yielded before a fetch error stand; the error is recorded and ends the cycle;
    fails_when=the error escapes into the polling loop or drops the batches already yielded;
    no_claim=the readings the failed part would have given."""
    monkeypatch.setattr(health_monitor, "_source_last_error", {})
    source = _RestBatches([[_sensor_reading("x")], RuntimeError("RIVM 502"), [_sensor_reading("y")]])

    readings = asyncio.run(source.fetch_readings())

    assert [r.sensor_id for r in readings] == ["x"]
    assert "RIVM 502" in health_monitor._source_last_error[source.source_name]


def test_the_empty_cycle_alert_looks_at_the_whole_cycle():
    """protects=a cycle counts as empty only when none of its batches had readings;
    fails_when=an empty first batch raises the alert;
    no_claim=the alert's text."""
    source = _RestBatches([[_sensor_reading("x")]])
    asyncio.run(source.fetch_readings())  # the source has delivered before
    source.script = [[], [_sensor_reading("y")]]
    asyncio.run(source.fetch_readings())
    assert source.empty_alerts == 0

    source.script = [[], []]
    asyncio.run(source.fetch_readings())
    assert source.empty_alerts == 1


def test_closing_the_batches_closes_the_source_fetch_at_once():
    """protects=when the polling loop stops reading batches, the source's own generator closes right away;
    fails_when=the inner fetch is left to the garbage collector (its tasks keep fetching);
    no_claim=which tasks the source cancels (the Samen Meten tests below)."""
    source = _RestBatches([[_sensor_reading("x")], [_sensor_reading("y")]])

    async def take_one_and_close() -> None:
        batches = source.fetch_reading_batches()
        await anext(batches)
        await batches.aclose()

    asyncio.run(take_one_and_close())

    assert source.closed


# ---------------------------------------------------------------------------
# Samen Meten: batches while it fetches
# ---------------------------------------------------------------------------


class _SlowClient:
    """Stands in for AsyncAPIClient: Datastream 20 answers only once ``release`` is set."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.cancelled: list[str] = []

    async def get(self, path: str, params: dict | None = None) -> dict:
        if "Datastreams(20)" in path:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.append(path)
                raise
        return {"value": [{"phenomenonTime": "2026-11-26T14:00:00Z", "result": 7.5}]}


def _samen_meten(monkeypatch) -> tuple[SamenMetenPollingSource, _SlowClient]:
    monkeypatch.setattr(
        samen_meten_source,
        "settings",
        dataclasses.replace(samen_meten_source.settings, samen_meten_batch_seconds=0.05),
    )
    source = SamenMetenPollingSource()
    for thing_id, ds_id in ((1, 10), (2, 20)):
        source._discovered_things[thing_id] = {
            "name": f"LTD_{thing_id}",
            "lat": 52.0,
            "lon": 4.3,
            "datastreams": [{"ds_id": ds_id, "observed_property": "pm2_5"}],
        }
    client = _SlowClient()
    source._client = client
    return source, client


def test_samen_meten_yields_batches_while_slow_datastreams_are_still_fetching(monkeypatch):
    """protects=readings fetched so far go out every samen_meten_batch_seconds, before the slow fetches end;
    fails_when=the source waits for all 11,000+ datastreams before it yields anything;
    no_claim=RIVM's real response times."""
    source, client = _samen_meten(monkeypatch)

    async def scenario() -> tuple[list, list]:
        batches = source._do_fetch_reading_batches()
        # A source that waits for every fetch would never yield before the release.
        first = await asyncio.wait_for(anext(batches), timeout=5)
        client.release.set()
        rest = [reading async for batch in batches for reading in batch]
        return first, rest

    first, rest = asyncio.run(scenario())

    assert {r.sensor_id for r in first} == {"sm-1"}
    assert {r.sensor_id for r in rest} == {"sm-2"}


def test_samen_meten_stops_fetching_when_its_batches_are_closed(monkeypatch):
    """protects=closing the batches cancels the Datastream fetches still running;
    fails_when=fetch tasks outlive the cycle that started them;
    no_claim=the marks of what was not fetched (its _last_ts stays, so the next cycle fetches it)."""
    source, client = _samen_meten(monkeypatch)

    async def scenario() -> None:
        batches = source._do_fetch_reading_batches()
        await asyncio.wait_for(anext(batches), timeout=5)
        await batches.aclose()

    asyncio.run(scenario())

    assert client.cancelled == ["/Datastreams(20)/Observations"]
    assert 20 not in source._last_ts


def test_samen_meten_names_the_entity_sets_of_a_batch(monkeypatch):
    """protects=entity_sets_for gives the sets of the readings' Things only;
    fails_when=a batch registers every discovered Thing;
    no_claim=the contents of an entity set (tests/test_samen_meten.py)."""
    source, _ = _samen_meten(monkeypatch)

    sets = source.entity_sets_for([_sensor_reading("sm-2")])

    assert [s["sensors"][0]["sensor_id"] for s in sets] == ["sm-2"]
    assert len(source.entity_sets()) == 2
