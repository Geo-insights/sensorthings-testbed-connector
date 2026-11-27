"""Where a Samen Meten Datastream starts reading (#47).

Each cycle asks RIVM for a Datastream's observations after a time. It used to
be the Datastream's last fetched observation, else one hour before the cycle's
start: after a restart every Datastream read only that hour, and RIVM publishes
some values hours late, so those were never read (11-26: the 13:00-16:00 hours
around a restart stayed nearly empty). Now a Datastream without a fetched
timestamp starts at the polling loop's high-water mark for its sensor and
property, the newest observation in FROST, and only without one
SAMEN_METEN_LOOKBACK_HOURS (6) before the cycle.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import types
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

import app.main as main
import app.services.samen_meten_source as samen_meten_source
from app.services.samen_meten_source import SamenMetenPollingSource

FETCHED = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)
MARK = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


class _RecordingClient:
    """Stands in for AsyncAPIClient: records each Datastream's $filter and returns no observations."""

    def __init__(self) -> None:
        self.filters: dict[int, str] = {}

    async def get(self, path: str, params: dict | None = None) -> dict:
        ds = int(re.search(r"Datastreams\(([0-9]+)\)", path).group(1))  # type: ignore[union-attr]
        self.filters[ds] = (params or {})["$filter"]
        return {"value": []}


def _source(monkeypatch, *, lookback_hours: int = 6) -> tuple[SamenMetenPollingSource, _RecordingClient]:
    monkeypatch.setattr(
        samen_meten_source,
        "settings",
        dataclasses.replace(samen_meten_source.settings, samen_meten_lookback_hours=lookback_hours),
    )
    source = SamenMetenPollingSource()
    for thing_id, ds_id, prop in ((1, 10, "pm2_5"), (2, 20, "pm2_5"), (3, 30, "temperature")):
        source._discovered_things[thing_id] = {
            "name": f"LTD_{thing_id}", "lat": 52.0, "lon": 4.3,
            "datastreams": [{"ds_id": ds_id, "observed_property": prop}],
        }
    client = _RecordingClient()
    source._client = client
    return source, client


def _since(filter_text: str) -> datetime:
    match = re.fullmatch(r"phenomenonTime gt ([0-9T:-]+)Z", filter_text)
    assert match, filter_text
    return datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC)


def test_fetched_timestamp_then_mark_then_lookback(monkeypatch):
    """protects=#47: a Datastream reads after its last fetched observation, else after the loop's mark for its sensor
    and property (FROST's newest), else SAMEN_METEN_LOOKBACK_HOURS before the cycle;
    fails_when=a restarted connector reads only the last hour for every Datastream again;
    no_claim=what RIVM returns."""
    source, client = _source(monkeypatch)
    source._last_ts[10] = FETCHED
    source.use_marks({"sm-1::pm2_5": MARK, "sm-2::pm2_5": MARK})
    before = datetime.now(UTC).replace(microsecond=0)

    asyncio.run(source._do_fetch_readings())

    assert _since(client.filters[10]) == FETCHED  # its own fetch wins over the older mark
    assert _since(client.filters[20]) == MARK
    looked_back = _since(client.filters[30])
    assert before - timedelta(hours=6, minutes=1) <= looked_back <= before - timedelta(hours=6) + timedelta(seconds=5)


def test_the_marks_are_the_loops_live_dict(monkeypatch):
    """protects=marks the loop advances after handing them over count on the next cycle;
    fails_when=the source keeps a copy taken at startup;
    no_claim=how the loop advances marks (test_main_seeding)."""
    source, client = _source(monkeypatch)
    marks: dict[str, datetime] = {}
    source.use_marks(marks)
    marks["sm-3::temperature"] = MARK

    asyncio.run(source._do_fetch_readings())

    assert _since(client.filters[30]) == MARK


def test_a_mark_in_another_time_zone_is_sent_as_utc(monkeypatch):
    """protects=the $filter time is UTC ('Z') whatever zone the mark carries;
    fails_when=a +01:00 timestamp is printed as is with a 'Z' (an hour off);
    no_claim=naive timestamps from elsewhere (they count as UTC)."""
    source, client = _source(monkeypatch)
    source.use_marks({"sm-2::pm2_5": datetime(2026, 11, 26, 13, 0, tzinfo=timezone(timedelta(hours=1)))})

    asyncio.run(source._do_fetch_readings())

    assert _since(client.filters[20]) == MARK


@pytest.mark.parametrize("hours", [0, -3])
def test_a_lookback_that_is_not_positive_uses_six_hours(monkeypatch, hours):
    """protects=SAMEN_METEN_LOOKBACK_HOURS=0 (or less) falls back to 6 hours;
    fails_when=0 reads from the cycle's start (nothing) or a negative value reads from the future;
    no_claim=config validation."""
    source, client = _source(monkeypatch, lookback_hours=hours)
    before = datetime.now(UTC).replace(microsecond=0)

    asyncio.run(source._do_fetch_readings())

    looked_back = _since(client.filters[30])
    assert before - timedelta(hours=6, minutes=1) <= looked_back <= before - timedelta(hours=6) + timedelta(seconds=5)


# ---------------------------------------------------------------------------
# The polling loop hands its marks over
# ---------------------------------------------------------------------------


class _Stop(BaseException):
    """Ends the polling loop from inside a test."""


class _MarkTaker:
    source_name = "lookback_test_source"

    def __init__(self) -> None:
        self.marks: Any = None

    def poll_interval(self) -> int:
        return 300

    def entity_sets(self) -> list:
        return []

    def use_marks(self, marks: dict) -> None:
        self.marks = marks

    async def fetch_readings(self) -> list:
        raise _Stop


def test_the_loop_hands_the_seeded_marks_to_the_source(monkeypatch):
    """protects=the polling loop gives a source with use_marks its seeded dict before the first fetch;
    fails_when=the seed stays in the loop and Samen Meten falls back to the look-back after every restart;
    no_claim=what the seed contains (test_main_seeding)."""
    seeded = {"sm-2::pm2_5": MARK}
    monkeypatch.setattr(main, "_seed_timestamps_from_frost", lambda source_name: seeded)
    monkeypatch.setattr(main, "asyncio", types.SimpleNamespace(sleep=None))
    source = _MarkTaker()

    with pytest.raises(_Stop):
        asyncio.run(main._polling_ingest_loop(source))

    assert source.marks is seeded


def test_sources_without_use_marks_are_left_alone():
    """protects=only a source that asks for the marks gets them;
    fails_when=handing them over fails on every other source;
    no_claim=anything else about those sources."""
    main._share_marks(object(), {"k": MARK})
