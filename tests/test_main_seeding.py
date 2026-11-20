"""Seeding the polling loops' high-water marks from FROST after a restart.

``_seed_timestamps_from_frost`` pages through all of a source's Datastreams in id
order and reads each one's newest phenomenonTime, so readings FROST already
holds are not posted again.
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

BASE = "https://frost.test/FROST-Server/v1.1"
BASE_V2 = "https://frost.test/FROST-Server/v2.0"
AUTH = {"Authorization": "Basic dGVzdDp0ZXN0"}
EXPAND = "Observations($select=phenomenonTime;$orderby=phenomenonTime desc;$top=1)"
SC_FILTER = "Thing/properties/source eq 'sensor_community'"


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
    ds_time: Any = None,
    newest: Any = None,
    *,
    v2: bool = False,
    expanded: bool = True,
) -> dict[str, Any]:
    """A Datastream row: its own phenomenonTime and, when expanded, its newest Observation."""
    props: dict[str, Any] = {"site_key": "x"}
    if sensor_id:
        props["sensor_id"] = sensor_id
    if prop:
        props["observed_property"] = prop
    row: dict[str, Any] = {("id" if v2 else "@iot.id"): ds_id, "properties": props}
    if ds_time is not None:
        row["phenomenonTime"] = ds_time
    if expanded:
        row["Observations"] = [] if newest is None else [{"phenomenonTime": newest}]
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


# -- paging -----------------------------------------------------------------------


def test_pages_through_every_datastream_in_id_order(monkeypatch):
    _use_target(monkeypatch, BASE)
    fake = _serve(
        monkeypatch,
        _page([_ds(3, "sc-1", "pm10", _interval("09:00", "10:00"), _instant("10:00")),
               _ds(8, "sc-1", "pm2_5", _interval("09:00", "10:05"), _instant("10:05"))], more=True),
        _page([_ds(11, "sc-2", "pm10", _interval("09:00", "10:10"), _instant("10:10")),
               _ds(14, "sc-2", "pm2_5", _interval("09:00", "10:15"), _instant("10:15"))], more=True),
        _page([_ds(20, "sc-3", "pm10", _interval("09:00", "10:20"), _instant("10:20"))]),
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
    assert fake.calls[0]["params"] == {
        "$filter": SC_FILTER,
        "$select": "@iot.id,properties,phenomenonTime",
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
        _page([_ds(7, "knmi-06344", "temperature", {"start": _instant("08:00"), "end": _instant("10:00")},
                   {"start": _instant("10:00")}, v2=True)], more=True, v2=True),
        _page([_ds(9, "knmi-06344", "humidity", {"start": _instant("10:10")}, {"start": _instant("10:10")},
                   v2=True)], v2=True),
    )

    seeded = main._seed_timestamps_from_frost("knmi_weather", page_size=1)

    assert seeded == {
        "knmi-06344::temperature": _utc("2026-11-20T10:00:00"),
        "knmi-06344::humidity": _utc("2026-11-20T10:10:00"),
    }
    knmi_filter = "Thing/properties/source eq 'knmi_weather'"
    assert fake.calls[0]["params"]["$select"] == "id,properties,phenomenonTime"
    assert fake.calls[0]["params"]["$orderby"] == "id asc"
    assert fake.calls[1]["params"]["$filter"] == f"{knmi_filter} and id gt 7"


def test_v2_url_without_a_target_stack_still_pages(monkeypatch):
    # SENSORTHINGS_BASE_URL defaults to v1.1 field names; FROST's v2.0 endpoint
    # accepts them in queries but answers with "id" and "@nextLink".
    _use_target(monkeypatch, BASE_V2, version=None)
    fake = _serve(
        monkeypatch,
        _page([_ds(4, "sc-1", "pm10", None, {"start": _instant("10:00")}, v2=True)], more=True, v2=True),
        _page([_ds(6, "sc-2", "pm10", None, {"start": _instant("10:05")}, v2=True)], v2=True),
    )

    seeded = main._seed_timestamps_from_frost("sensor_community", page_size=1)

    assert set(seeded) == {"sc-1::pm10", "sc-2::pm10"}
    assert fake.calls[1]["params"]["$filter"] == f"{SC_FILTER} and @iot.id gt 4"


def test_string_ids_are_quoted_in_the_continuation(monkeypatch):
    _use_target(monkeypatch, BASE)
    fake = _serve(
        monkeypatch,
        _page([_ds("a'1", "sc-1", "pm10", None, _instant("10:00"))], more=True),
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


# -- newest timestamp per key -----------------------------------------------------


def test_keeps_the_newest_timestamp_per_key(monkeypatch, caplog):
    _use_target(monkeypatch, BASE)
    _serve(
        monkeypatch,
        _page(
            [
                _ds(1, "sc-5", "pm10", _interval("09:00", "10:00"), _instant("10:00")),
                # Same key on another Datastream whose phenomenonTime lags its newest
                # Observation (concurrent inserts): the Observation counts, and wins.
                _ds(2, "sc-5", "pm10", _interval("09:00", "10:10"), _instant("10:30")),
                # ...and an older Datastream does not overwrite it.
                _ds(3, "sc-5", "pm10", _interval("09:00", "10:20"), _instant("10:20")),
                # An interval Observation that ends after the newest one starts.
                _ds(4, "sc-6", "pm10", _interval("09:00", "11:00"), _instant("10:50")),
                # No phenomenonTime on the Datastream, an Observation all the same.
                _ds(5, "sc-7", "pm10", None, _instant("10:20")),
                _ds(6, "sc-8", "pm10"),  # no Observations yet
                _ds(7, None, "pm10", _interval("10:00", "10:00"), _instant("10:00")),  # no sensor_id
                _ds(8, "sc-9", None, _interval("10:00", "10:00"), _instant("10:00")),  # no observed_property
                _ds(9, "sc-10", "pm10", "not a time"),
                _ds(10, "sc-11", "pm10", "2026-11-20T10:40:00", expanded=False),  # no offset: UTC
                "not a datastream",
            ]
        ),
    )

    with caplog.at_level(logging.INFO, logger="connector.events"):
        seeded = main._seed_timestamps_from_frost("sensor_community")

    assert seeded == {
        "sc-5::pm10": _utc("2026-11-20T10:30:00"),
        "sc-6::pm10": _utc("2026-11-20T11:00:00"),
        "sc-7::pm10": _utc("2026-11-20T10:20:00"),
        "sc-11::pm10": _utc("2026-11-20T10:40:00"),
    }
    assert (
        "Seeded 4 timestamps from FROST for sensor_community (11 datastreams in 1 page(s), "
        "2 without observations, 3 without sensor_id/observed_property, "
        "1 with a phenomenonTime behind their newest Observation, "
    ) in caplog.text


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


# -- partial failure, fallback and the time bound ----------------------------------


def test_failed_page_keeps_the_pages_already_read(monkeypatch, caplog):
    _use_target(monkeypatch, BASE)
    fake = _serve(
        monkeypatch,
        _page([_ds(1, "sc-1", "pm10", _interval("10:00", "10:00"), _instant("10:00"))], more=True),
        requests.ConnectionError("connection reset"),
        requests.ConnectionError("connection reset"),
    )

    with caplog.at_level(logging.INFO, logger="connector.events"):
        seeded = main._seed_timestamps_from_frost("sensor_community", page_size=1)

    assert seeded == {"sc-1::pm10": _utc("2026-11-20T10:00:00")}
    # Page 2 is asked again without the expansion before the sweep gives up.
    assert [call["params"]["$filter"] for call in fake.calls] == [SC_FILTER] + [f"{SC_FILTER} and @iot.id gt 1"] * 2
    assert "$expand" not in fake.calls[2]["params"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Seeded 1 timestamps from FROST for sensor_community, incomplete (page 2 failed: ConnectionError" in warnings[0]


def test_failed_expansion_falls_back_to_the_datastream_time(monkeypatch, caplog):
    # A FROST without an index on (DATASTREAM_ID, PHENOMENON_TIME_START) can take
    # longer than the request timeout to expand a page.
    _use_target(monkeypatch, BASE)
    fake = _serve(
        monkeypatch,
        requests.Timeout("read timed out"),
        _page([_ds(1, "sc-1", "pm10", _interval("09:00", "10:00"), expanded=False)], more=True),
        _page([_ds(2, "sc-2", "pm10", _interval("09:00", "10:05"), expanded=False)]),
    )

    with caplog.at_level(logging.INFO, logger="connector.events"):
        seeded = main._seed_timestamps_from_frost("sensor_community", page_size=1)

    assert seeded == {"sc-1::pm10": _utc("2026-11-20T10:00:00"), "sc-2::pm10": _utc("2026-11-20T10:05:00")}
    assert ["$expand" in call["params"] for call in fake.calls] == [True, False, False]
    assert [call["params"]["$filter"] for call in fake.calls] == [SC_FILTER, SC_FILTER, f"{SC_FILTER} and @iot.id gt 1"]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].startswith("Seeded 2 timestamps from FROST for sensor_community (2 datastreams in 2 page(s)")
    assert "newest Observations not expanded from page 1 on (Timeout: read timed out)" in warnings[0]


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

    assert len(fake.calls) == 2
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
        return _page([_ds(n, f"sc-{n}", "pm10", _interval("10:00", "10:00"), _instant("10:00"))], more=True)

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


def test_time_budget_also_bounds_the_fallback(monkeypatch, caplog):
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


def test_stops_when_a_page_gives_no_id_to_continue_after(monkeypatch, caplog):
    _use_target(monkeypatch, BASE)
    fake = _serve(monkeypatch, _page([_ds(None, "sc-1", "pm10", None, _instant("10:00"))], more=True))

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
