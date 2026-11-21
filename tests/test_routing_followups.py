"""Routing follow-ups (review of 2026-11-20 on the routing fix).

* The warm-up listing pages by id from the connector's own base URL, so it
  lists every datastream on FROST's /v2.0 path, which answers ``@nextLink``
  even to a target declared as v1.1 (production: frost.geo-insights.nl).
  Before, it read only ``@iot.nextLink`` there: one page of 1,000 datastreams,
  taken as the complete listing, purged every other cached mapping.
* A failed warm-up is a warning, is retried from the push path with backoff,
  and shows in /health; before, it was a debug line and the route guard stayed
  off for the life of the process.
* Lines in the dead-letter queue get the route check push_observations does:
  lines the pre-fix connector wrote carry the datastream its wrong routing chose.
* Counting a sensor's datastreams reads a per-sensor index instead of iterating
  the id map, which registrations on other threads change.
* The monitoring MQTT bridge resolves datastreams the way push_observations does.
* Readings without a datastream are flagged ``"unresolved": True``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from app.frost.cache import EntityCache
from app.frost.entity_manager import EntityManager
from app.frost.http_client import FrostHTTPClient
from app.frost.odata_fields import odata_fields_for
from app.models import SensorReading
from app.services import monitoring_mqtt_bridge as bridge
from app.services import sensorthings_client as stc
from app.services.sensorthings_client import SensorThingsClient
from tests.test_sensorthings_client_routing import _ds, _reading, _sc, _settings

V2_BASE = "https://frost.example/FROST-Server/v2.0"  # production: /v2.0, no declared version
SERVER_ROOT = "http://frost-internal:8080/FROST-Server/v2.0"  # where FROST's own next links point


# ---------------------------------------------------------------------------
# A FROST Datastreams listing
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, body: Any, status_code: int = 200) -> None:
        self._body = body
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = json.dumps(body)

    def raise_for_status(self) -> None:
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)  # type: ignore[arg-type]

    def json(self) -> Any:
        return self._body


class FakeListing:
    """FROST 2.8's answer to the warm-up's Datastreams requests.

    Rows in id order; honours ``$top`` (capped at ``max_top``) and
    ``$filter=id gt N``; adds a next link in ``next_link_key``'s convention when
    more rows match, pointing at the server's own root. Records every request.
    """

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        next_link_key: str = "@nextLink",
        max_top: int = 1000,
        id_key: str = "id",
    ) -> None:
        self.rows = sorted(rows, key=lambda row: row[id_key])
        self.next_link_key = next_link_key
        self.max_top = max_top
        self.id_key = id_key
        self.calls: list[tuple[str, dict[str, str], dict[str, str]]] = []

    def __call__(self, url: str, params: dict[str, str] | None = None, headers: Any = None, **kwargs: Any) -> _Response:
        params = dict(params or {})
        self.calls.append((url, params, dict(headers or {})))
        top = min(int(params.get("$top", "100")), self.max_top)
        rows = self.rows
        if "$filter" in params:
            match = re.fullmatch(r"id gt (\d+|'[^']*')", params["$filter"])
            assert match, params["$filter"]
            literal = match.group(1)
            after: Any = int(literal) if literal.isdigit() else literal.strip("'")
            rows = [row for row in rows if row[self.id_key] > after]
        body: dict[str, Any] = {"value": rows[:top]}
        if len(rows) > top:
            body[self.next_link_key] = f"{SERVER_ROOT}/Datastreams?$top={top}&$skip={top}&$skipFilter=..."
        return _Response(body)


def _v2_row(ds_id: int, sensor_id: str, observed_property: str) -> dict[str, Any]:
    """A Datastream as FROST's /v2.0 path lists it: ``id``, whatever the $select said."""
    row = _ds(ds_id, sensor_id, observed_property)
    row["id"] = row.pop("@iot.id")
    return row


def _population(n: int) -> list[dict[str, Any]]:
    """n datastreams: Sensor.Community locations with pm10 and pm2_5."""
    return [_v2_row(i, f"sensor_community-{(i - 1) // 2}", ("pm10", "pm2_5")[(i - 1) % 2]) for i in range(1, n + 1)]


def _cache_of(rows: list[dict[str, Any]]) -> dict[str, str]:
    """The id map as registration leaves it: each key to its own datastream."""
    return {f"{r['properties']['sensor_id']}::{r['properties']['observed_property']}": str(r["id"]) for r in rows}


@pytest.fixture
def client_for(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A client of a production-shaped primary (``V2_BASE``) with a cached id map."""

    def _make(cache: dict[str, str] | None = None, **overrides: Any) -> SensorThingsClient:
        cache_path = tmp_path / "registered_entities.json"
        if cache is not None:
            cache_path.write_text(json.dumps({"datastreams": cache}), encoding="utf-8")
        values: dict[str, Any] = dict(
            sensorthings_base_url=V2_BASE, registered_entities_path=str(cache_path), failed_dlq_max_age_hours=48,
            frost_replay_dedup_probe=False,
        )
        values.update(overrides)
        monkeypatch.setattr(stc, "settings", _settings(tmp_path, **values))
        return SensorThingsClient()

    return _make


def _persisted(tmp_path: Path) -> dict[str, str]:
    return json.loads((tmp_path / "registered_entities.json").read_text(encoding="utf-8"))["datastreams"]


# ---------------------------------------------------------------------------
# 1. The warm-up lists every datastream
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("next_link_key", ["@nextLink", "@iot.nextLink"], ids=["v2.0-answer", "v1.1-answer"])
def test_warm_up_lists_every_datastream_and_purges_nothing_valid(client_for, monkeypatch, tmp_path, next_link_key) -> None:
    """Production: 13,617 datastreams; FROST's /v2.0 path answers "@nextLink" to the v1.1-declared target."""
    rows = _population(2500)
    listing = FakeListing(rows, next_link_key=next_link_key)
    monkeypatch.setattr(stc.requests, "get", listing)
    client = client_for(cache=_cache_of(rows))

    client.warm_datastream_cache()

    assert set(client._datastream_routes) == {str(r["id"]) for r in rows}
    assert _persisted(tmp_path) == _cache_of(rows)  # nothing valid purged
    assert client.route_status()["complete"] is True
    # Three pages, each asked from the configured base URL, never at the server's next link.
    assert [call[0] for call in listing.calls] == [f"{V2_BASE}/Datastreams"] * 3
    assert [call[1].get("$filter") for call in listing.calls] == [None, "id gt 1000", "id gt 2000"]
    assert all(call[1]["$orderby"] == "id asc" for call in listing.calls)
    assert all(call[2].get("Authorization", "").startswith("Basic ") for call in listing.calls)


def test_bare_keys_are_judged_on_the_whole_listing(client_for, monkeypatch) -> None:
    """A sensor whose second datastream is on page two gets no bare sensor-id entry."""
    rows = [*_population(1000), _v2_row(1001, "knmi-0", "temperature"), _v2_row(1002, "knmi-0", "humidity")]
    rows[0]["properties"]["sensor_id"] = "knmi-0"  # first page: one knmi-0 datastream
    monkeypatch.setattr(stc.requests, "get", FakeListing(rows))
    client = client_for()

    client.warm_datastream_cache()

    assert "knmi-0" not in client._datastream_ids
    assert client._datastream_ids["knmi-0::humidity"] == "1002"


def test_the_route_guard_covers_datastreams_beyond_the_first_page(client_for, monkeypatch) -> None:
    """A mapping to another sensor's datastream of another property, id 2,400."""
    rows = _population(2500)
    monkeypatch.setattr(stc.requests, "get", FakeListing(rows))
    client = client_for()
    client.warm_datastream_cache()
    client._datastream_ids["sensor_community-999999::humidity"] = "2400"
    posts: list[str] = []
    monkeypatch.setattr(client, "_post_observation", lambda ep, pl, ds, base_url=None: posts.append(str(ds)) or SimpleNamespace(ok=True, status_code=201, text="ok"))
    monkeypatch.setattr(client, "_resolve_datastream_id_live", lambda *a, **k: None)

    result = client.push_observations([_sc(999999, "humidity")], source="sensor_community")

    assert posts == []
    assert result["results"][0]["refused"] == ["datastream 2400 belongs to sensor sensor_community-1199"]


def test_a_full_last_page_without_next_link_is_confirmed_by_one_more_request(client_for, monkeypatch) -> None:
    listing = FakeListing(_population(1000))
    monkeypatch.setattr(stc.requests, "get", listing)
    client = client_for()

    client.warm_datastream_cache()

    assert [call[1].get("$filter") for call in listing.calls] == [None, "id gt 1000"]
    assert client.route_status() | {"loaded_at": None} == {
        "routes_loaded": True, "route_count": 1000, "complete": True, "last_error": None,
        "failed_attempts": 0, "loaded_at": None, "next_retry_in_seconds": None,
    }


def test_a_server_max_top_below_the_page_size_is_followed_by_its_next_links(client_for, monkeypatch) -> None:
    listing = FakeListing(_population(250), max_top=100)
    monkeypatch.setattr(stc.requests, "get", listing)
    client = client_for(cache=_cache_of(_population(250)))

    client.warm_datastream_cache()

    assert [call[1].get("$filter") for call in listing.calls] == [None, "id gt 100", "id gt 200"]
    assert len(client._datastream_routes) == 250
    assert client.route_status()["complete"] is True


def test_string_ids_are_quoted_in_the_continuation_filter(client_for, monkeypatch) -> None:
    rows = [_v2_row(i, f"s{i}", "pm10") for i in range(1, 4)]
    for row in rows:
        row["id"] = f"ds-{row['id']:04d}"
    listing = FakeListing(rows, max_top=2)
    monkeypatch.setattr(stc.requests, "get", listing)
    client = client_for()

    client.warm_datastream_cache()

    assert [call[1].get("$filter") for call in listing.calls] == [None, "id gt 'ds-0002'"]
    assert set(client._datastream_routes) == {"ds-0001", "ds-0002", "ds-0003"}


def test_a_listing_out_of_id_order_counts_as_partial_and_purges_nothing(client_for, monkeypatch, tmp_path) -> None:
    """A server that ignores $orderby: id paging could skip datastreams."""
    page = {"value": [_v2_row(5, "s5", "pm10"), _v2_row(3, "s3", "pm10")], "@nextLink": f"{SERVER_ROOT}/Datastreams?$skip=2"}
    calls: list[Any] = []
    monkeypatch.setattr(stc.requests, "get", lambda url, params=None, **kw: calls.append(params) or _Response(page))
    client = client_for(cache={"gone::pm10": "77"})

    client.warm_datastream_cache()

    assert len(calls) == 1
    assert client.route_status()["complete"] is False
    assert _persisted(tmp_path) == {"gone::pm10": "77"}


def test_max_datastreams_stops_a_partial_listing(client_for, monkeypatch, tmp_path) -> None:
    listing = FakeListing(_population(30), max_top=10)
    monkeypatch.setattr(stc.requests, "get", listing)
    client = client_for(cache={"gone::pm10": "777"})

    client.warm_datastream_cache(max_datastreams=20)

    assert len(listing.calls) == 2
    assert client.route_status()["complete"] is False
    assert _persisted(tmp_path) == {"gone::pm10": "777"}  # missing from a partial listing proves nothing


def test_a_registration_during_the_listing_is_not_purged(client_for, monkeypatch, tmp_path) -> None:
    """A datastream created after its page was read is not in the listing."""
    rows = _population(4)
    listing = FakeListing(rows)

    def get_while_registering(url: str, params: dict[str, str] | None = None, **kwargs: Any) -> _Response:
        response = listing(url, params, **kwargs)
        client._datastream_ids["sensor_community-new::pm10"] = "5"  # register_entity_set on another thread
        return response

    monkeypatch.setattr(stc.requests, "get", get_while_registering)
    client = client_for(cache={**_cache_of(rows), "old-target::pm10": "4711"})

    client.warm_datastream_cache()

    assert client._datastream_ids["sensor_community-new::pm10"] == "5"
    assert "old-target::pm10" not in client._datastream_ids  # stale: purged as before
    assert "old-target::pm10" not in _persisted(tmp_path)


# ---------------------------------------------------------------------------
# 4. A failed warm-up is reported and retried
# ---------------------------------------------------------------------------


def _down(*args: Any, **kwargs: Any) -> Any:
    raise requests.ConnectionError("FROST unreachable")


def test_a_failed_warm_up_is_a_warning_with_its_cause(client_for, monkeypatch, caplog, tmp_path) -> None:
    monkeypatch.setattr(stc.requests, "get", _down)
    client = client_for(cache={"sensor_community-1::pm10": "1386"})

    with caplog.at_level(logging.INFO, logger="app.services.sensorthings_client"):
        assert client.warm_datastream_cache() == 0

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "ConnectionError: FROST unreachable" in warnings[0].getMessage()
    assert "retrying from the push path in 60s" in warnings[0].getMessage()
    status = client.route_status()
    assert status["routes_loaded"] is False and status["route_count"] == 0
    assert status["last_error"] == "ConnectionError: FROST unreachable"
    assert 0 < status["next_retry_in_seconds"] <= 60
    assert _persisted(tmp_path) == {"sensor_community-1::pm10": "1386"}  # nothing purged


def test_an_http_error_names_its_status(client_for, monkeypatch) -> None:
    monkeypatch.setattr(stc.requests, "get", lambda *a, **k: _Response({"message": "busy"}, 503))
    client = client_for()

    client.warm_datastream_cache()

    assert client.route_status()["last_error"] == "HTTP 503"


@pytest.fixture
def no_http_push(monkeypatch: pytest.MonkeyPatch):
    """Patch a client's POST and live lookup; returns the posted (datastream, property) pairs."""

    def _patch(client: SensorThingsClient, live: dict[str, str] | None = None) -> list[tuple[str, str]]:
        posts: list[tuple[str, str]] = []

        def fake_post(endpoint: str, payload: dict[str, Any], datastream_id: Any, base_url: str | None = None) -> Any:
            posts.append((str(datastream_id), payload["parameters"]["observed_property"]))
            return SimpleNamespace(ok=True, status_code=201, text="created")

        monkeypatch.setattr(client, "_post_observation", fake_post)
        monkeypatch.setattr(
            client, "_resolve_datastream_id_live",
            lambda base_url, reading: (live or {}).get(f"{reading.sensor_id}::{reading.observed_property}"),
        )
        return posts

    return _patch


def test_the_push_path_retries_the_listing_once_the_backoff_has_passed(client_for, monkeypatch, no_http_push) -> None:
    """FROST down at startup, back later: the next push lists and checks its mappings."""
    monkeypatch.setattr(stc.requests, "get", _down)
    # A stale id from the previous target: 1386 is location 93182's pressure datastream now.
    client = client_for(cache={"sensor_community-91154::pm10": "1386"})
    client.warm_datastream_cache()
    posts = no_http_push(client)
    listing = FakeListing([_v2_row(1386, "sensor_community-93182", "pressure")])
    monkeypatch.setattr(stc.requests, "get", listing)

    client.push_observations([_sc(91154, "pm10")], source="sensor_community")
    assert listing.calls == []  # backoff not passed: unchecked, as before
    assert posts == [("1386", "pm10")]

    client._route_listing.retry_at = 0.0  # the backoff has passed
    result = client.push_observations([_sc(91154, "pm10")], source="sensor_community")

    assert len(listing.calls) == 1
    assert posts == [("1386", "pm10")]  # nothing more: the stale mapping was purged
    assert result["results"][0]["unresolved"] is True
    assert client.route_status()["routes_loaded"] is True

    client.push_observations([_sc(91154, "pm10")], source="sensor_community")
    assert len(listing.calls) == 1  # listed once


def test_retries_back_off_up_to_fifteen_minutes(client_for, monkeypatch) -> None:
    monkeypatch.setattr(stc.requests, "get", _down)
    clock = {"now": 1000.0}
    monkeypatch.setattr(
        stc, "time", SimpleNamespace(monotonic=lambda: clock["now"], perf_counter=time.perf_counter, time=time.time)
    )
    client = client_for()
    client.warm_datastream_cache()
    delays = []
    for _ in range(7):
        delays.append(client._route_listing.retry_at - clock["now"])
        clock["now"] = client._route_listing.retry_at
        client._retry_route_listing_if_due()

    assert delays == [60, 120, 240, 480, 900, 900, 900]
    assert client.route_status()["failed_attempts"] == 8


def test_no_retry_while_the_circuit_is_open(client_for, monkeypatch) -> None:
    monkeypatch.setattr(stc.requests, "get", _down)
    client = client_for()
    client.warm_datastream_cache()
    client._route_listing.retry_at = 0.0
    calls: list[Any] = []
    monkeypatch.setattr(stc.requests, "get", lambda *a, **k: calls.append(a) or _Response({"value": []}))
    monkeypatch.setattr(stc.observation_breaker, "snapshot", lambda: {V2_BASE: {"open": True}})

    client._retry_route_listing_if_due()

    assert calls == []


def test_a_retry_does_not_wait_for_a_listing_on_another_thread(client_for, monkeypatch) -> None:
    monkeypatch.setattr(stc.requests, "get", _down)
    client = client_for()
    client.warm_datastream_cache()
    client._route_listing.retry_at = 0.0
    calls: list[Any] = []
    monkeypatch.setattr(stc.requests, "get", lambda *a, **k: calls.append(a) or _Response({"value": []}))

    with client._warmup_lock:
        client._retry_route_listing_if_due()

    assert calls == []


def test_health_reports_the_route_guard(client_for, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import health as health_route

    monkeypatch.setattr(stc.requests, "get", _down)
    client = client_for()
    client.warm_datastream_cache()
    monkeypatch.setattr(health_route, "client", client)

    routes = TestClient(app).get("/health").json()["datastream_routes"]

    assert routes["routes_loaded"] is False
    assert routes["route_count"] == 0
    assert routes["last_error"] == "ConnectionError: FROST unreachable"

    monkeypatch.setattr(stc.requests, "get", FakeListing(_population(3)))
    client.warm_datastream_cache()
    routes = TestClient(app).get("/health").json()["datastream_routes"]
    assert (routes["routes_loaded"], routes["route_count"], routes["last_error"]) == (True, 3, None)


# ---------------------------------------------------------------------------
# 2. DLQ replay gets the route check
# ---------------------------------------------------------------------------


def _line(datastream_id: int, sensor_id: str, observed_property: str, **label: Any) -> dict[str, Any]:
    """A dead-letter line as push_observations writes it for the primary target."""
    parameters = {
        "sensor_id": sensor_id, "sensor_name": f"{sensor_id} sensor", "observed_property": observed_property,
        "unit": "1", "location": "x", "thing_name": label.pop("thing_name", sensor_id),
        "device_eui": label.pop("device_eui", None), "stream_key": label.pop("stream_key", None),
    }
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "datastream_id": str(datastream_id),
        "endpoint": f"{V2_BASE}/Observations",
        "payload": {
            "phenomenonTime": "2026-11-20T12:00:00Z", "result": 81.0, "resultQuality": ["good"],
            "Datastream": {"@iot.id": datastream_id}, "parameters": parameters,
        },
        "status_code": None,
        "error": "circuit_open",
    }


@pytest.fixture
def replay(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Write DLQ lines, replay them; returns (summary, posted (datastream, property) pairs, lines left)."""

    def _replay(
        client: SensorThingsClient, lines: list[dict[str, Any]], *, live: dict[str, str] | None = None,
        status: int = 201, max_lines: int | None = None,
    ) -> tuple[dict[str, Any], list[tuple[Any, str | None]], list[dict[str, Any]]]:
        dlq = tmp_path / "failed_observations.jsonl"
        dlq.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
        posted: list[tuple[Any, str | None]] = []

        def fake_post(url: str, json: dict[str, Any] | None = None, **kwargs: Any) -> _Response:
            label = json.get("parameters") or json.get("properties") or {}
            datastream = json["Datastream"]
            posted.append((datastream.get("@iot.id", datastream.get("id")), label.get("observed_property")))
            return _Response({}, status)

        monkeypatch.setattr(stc.requests, "post", fake_post)
        monkeypatch.setattr(
            client, "_resolve_datastream_id_live",
            lambda base_url, reading: (live or {}).get(f"{reading.sensor_id}::{reading.observed_property}"),
        )
        summary = client.replay_failed_observations(max_lines)
        left = [json.loads(line) for line in dlq.read_text(encoding="utf-8").splitlines() if line.strip()]
        return summary, posted, left

    return _replay


def _listed_client(client_for, monkeypatch, rows: list[dict[str, Any]], cache: dict[str, str] | None = None) -> SensorThingsClient:
    monkeypatch.setattr(stc.requests, "get", FakeListing(rows))
    client = client_for(cache=cache)
    client.warm_datastream_cache()
    return client


def test_replay_drops_a_line_misrouted_into_another_propertys_datastream(client_for, monkeypatch, replay, caplog) -> None:
    """The reviewer's repro: a humidity reading in the location's pm10 datastream, none of its own."""
    client = _listed_client(client_for, monkeypatch, [_v2_row(723, "sensor_community-5", "pm10")])

    with caplog.at_level(logging.WARNING, logger="app.services.sensorthings_client"):
        summary, posted, left = replay(client, [_line(723, "sensor_community-5", "humidity")])

    assert posted == []
    assert left == []
    assert (summary["replayed"], summary["dropped"], summary["route_dropped"]) == (0, 1, 1)
    assert summary["results"][0]["route_refused"] == "datastream 723 observes pm10"
    message = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("DLQ replay route check"))
    assert "1 line(s) named a datastream of another sensor or observed property: 0 rewritten" in message
    assert "1 dropped" in message


def test_replay_rewrites_a_line_to_the_readings_own_datastream(client_for, monkeypatch, replay) -> None:
    client = _listed_client(
        client_for, monkeypatch,
        [_v2_row(723, "sensor_community-5", "pm10"), _v2_row(724, "sensor_community-5", "pm2_5")],
    )

    summary, posted, _left = replay(client, [_line(723, "sensor_community-5", "pm2_5")])

    assert posted == [(724, "pm2_5")]
    assert summary["route_rewritten"] == 1
    assert summary["results"][0] == {"datastream_id": "724", "ok": True, "status_code": 201, "rerouted_from": "723"}


def test_replay_rewrites_a_tgv_line_out_of_a_knmi_datastream(client_for, monkeypatch, replay) -> None:
    """Pre-fix: TGV humidity followed the bare stream key into the first humidity datastream listed."""
    rows = [_v2_row(11, "knmi-0", "humidity"), _v2_row(900, "tgv-officelab-climate", "humidity")]
    client = _listed_client(client_for, monkeypatch, rows)
    tgv = _line(11, "tgv-officelab-climate", "humidity", stream_key="humidity", device_eui="officelab-climate")

    summary, posted, _left = replay(client, [tgv])

    assert posted == [(900, "humidity")]
    assert summary["route_rewritten"] == 1


def test_replay_finds_the_own_datastream_by_the_live_lookup_or_drops_it(client_for, monkeypatch, replay) -> None:
    client = _listed_client(client_for, monkeypatch, [_v2_row(11, "knmi-0", "humidity"), _v2_row(12, "knmi-0", "temperature")])
    found = _line(11, "tgv-officelab-climate", "humidity", stream_key="humidity", device_eui="officelab-climate")
    missing = _line(12, "tgv-officelab-climate", "temperature", stream_key="temperature", device_eui="officelab-climate")

    summary, posted, left = replay(client, [found, missing], live={"tgv-officelab-climate::humidity": "950"})

    assert posted == [(950, "humidity")]
    assert (summary["route_rewritten"], summary["route_dropped"]) == (1, 1)
    assert left == []


def test_replay_keeps_samen_meten_lines_their_live_lookup_confirms(client_for, monkeypatch, replay) -> None:
    """Samen Meten readings carry sm-<remote thing id>; their datastreams sm-<thing name>."""
    client = _listed_client(client_for, monkeypatch, [_v2_row(300, "sm-ssk_lh001", "pm10")])

    summary, posted, _left = replay(client, [_line(300, "sm-4711", "pm10")], live={"sm-4711::pm10": "300"})

    assert posted == [(300, "pm10")]
    assert (summary["route_checked"], summary["route_rewritten"], summary["route_dropped"]) == (1, 0, 0)


def test_replay_of_a_correct_line_is_unchanged(client_for, monkeypatch, replay, caplog) -> None:
    client = _listed_client(client_for, monkeypatch, [_v2_row(723, "sensor_community-5", "pm10")])

    with caplog.at_level(logging.WARNING, logger="app.services.sensorthings_client"):
        summary, posted, _left = replay(client, [_line(723, "sensor_community-5", "pm10")])

    assert posted == [(723, "pm10")]
    assert summary["route_checked"] == 1
    assert not [r for r in caplog.records if r.getMessage().startswith("DLQ replay route check")]


def test_replay_counts_lines_without_a_label_as_unchecked(client_for, monkeypatch, replay, caplog) -> None:
    client = _listed_client(client_for, monkeypatch, [_v2_row(723, "sensor_community-5", "pm10")])
    line = _line(723, "sensor_community-5", "humidity")
    del line["payload"]["parameters"]

    with caplog.at_level(logging.WARNING, logger="app.services.sensorthings_client"):
        summary, posted, _left = replay(client, [line])

    assert posted == [(723, None)]  # today's behaviour
    assert summary["route_unchecked"] == 1
    assert any("1 line(s) without a sensor_id/observed_property label" in r.getMessage() for r in caplog.records)


def test_replay_of_a_datastream_the_listing_does_not_know(client_for, monkeypatch, replay) -> None:
    client = _listed_client(client_for, monkeypatch, [_v2_row(723, "sensor_community-5", "pm10")])

    summary, posted, _left = replay(client, [_line(5000, "sensor_community-9", "pm10")])

    assert posted == [(5000, "pm10")]
    assert summary["route_unknown"] == 1


def test_a_rewritten_line_that_stays_queued_keeps_its_new_datastream(client_for, monkeypatch, replay) -> None:
    client = _listed_client(
        client_for, monkeypatch,
        [_v2_row(723, "sensor_community-5", "pm10"), _v2_row(724, "sensor_community-5", "pm2_5")],
    )

    _summary, posted, left = replay(client, [_line(723, "sensor_community-5", "pm2_5")], status=503)

    assert posted == [(724, "pm2_5")]
    assert [(line["datastream_id"], line["payload"]["Datastream"]) for line in left] == [("724", {"@iot.id": 724})]


def test_refused_lines_count_against_the_pass_limit(client_for, monkeypatch, replay) -> None:
    client = _listed_client(client_for, monkeypatch, [_v2_row(723, "sensor_community-5", "pm10")])
    lines = [_line(723, "sensor_community-5", "humidity") for _ in range(3)] + [_line(723, "sensor_community-5", "pm10")]

    summary, posted, left = replay(client, lines, max_lines=2)

    assert summary["route_dropped"] == 2
    assert posted == []
    assert len(left) == 2


def test_replay_checks_a_v2_payload(client_for, monkeypatch, replay) -> None:
    client = _listed_client(
        client_for, monkeypatch,
        [_v2_row(723, "sensor_community-5", "pm10"), _v2_row(724, "sensor_community-5", "pm2_5")],
    )
    line = _line(723, "sensor_community-5", "pm2_5")
    payload = line["payload"]
    payload["properties"] = payload.pop("parameters")
    payload["Datastream"] = {"id": 723}

    summary, posted, _left = replay(client, [line])

    assert posted == [(724, "pm2_5")]
    assert summary["route_rewritten"] == 1


def test_replay_lines_of_another_target_are_not_checked(client_for, monkeypatch, replay) -> None:
    client = _listed_client(client_for, monkeypatch, [_v2_row(723, "sensor_community-5", "pm10")])
    line = _line(723, "sensor_community-5", "humidity")
    line["endpoint"] = "https://other.example/v1.1/Observations"

    summary, posted, _left = replay(client, [line])

    assert posted == [(723, "humidity")]
    assert summary["route_dropped"] == 0


def test_replay_retries_a_failed_listing_before_checking(client_for, monkeypatch, replay) -> None:
    monkeypatch.setattr(stc.requests, "get", _down)
    client = client_for()
    client.warm_datastream_cache()
    client._route_listing.retry_at = 0.0
    monkeypatch.setattr(stc.requests, "get", FakeListing([_v2_row(723, "sensor_community-5", "pm10")]))

    summary, posted, _left = replay(client, [_line(723, "sensor_community-5", "humidity")])

    assert client.route_status()["routes_loaded"] is True
    assert posted == []
    assert summary["route_dropped"] == 1


# ---------------------------------------------------------------------------
# 3. Counting a sensor's datastreams while registrations write
# ---------------------------------------------------------------------------


def test_push_survives_a_concurrent_registration(client_for, monkeypatch, no_http_push) -> None:
    """The reviewer's repro: the bare sensor-id check iterated the shared id map.

    A reading reaches that check through its bare sensor id when the
    datastream's property is unknown (an env mapping, or no listing yet).
    register_entity_set on another polling loop's thread adds keys meanwhile;
    the iteration raised "dictionary changed size during iteration" and lost
    the whole batch.
    """
    client = client_for(cache={"legacy": "78", "legacy::temperature": "77"})
    for i in range(30_000):  # production-sized id map
        client._datastream_ids[f"sensor_community-{i}::pm10"] = str(i)
    no_http_push(client)
    reading = _reading("legacy", "humidity")
    stop = threading.Event()

    def registration() -> None:
        """Keys come and go (registrations, warm-up purges): every write changes the size."""
        i = 0
        while not stop.is_set():
            key = f"sensor_community-new{i % 20_000}::pm2_5"
            if key in client._datastream_ids:
                del client._datastream_ids[key]
            else:
                client._datastream_ids[key] = str(100_000 + i)
            i += 1

    writer = threading.Thread(target=registration)
    writer.start()
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    try:
        for _ in range(100):
            try:
                results.append(client.push_observations([reading], source="test"))
            except RuntimeError as exc:
                errors.append(str(exc))
                break
    finally:
        stop.set()
        writer.join()

    assert errors == []
    assert {r["results"][0]["refused"][0] for r in results} == {
        "sensor legacy has 2 datastreams and datastream 78's observed property is unknown"
    }


def test_the_bare_sensor_check_reads_an_index_not_the_whole_map(client_for, monkeypatch, no_http_push) -> None:
    client = client_for(cache={"legacy::temperature": "77", "legacy": "78", "other::pm10": "1"})
    posts = no_http_push(client)

    def no_scan(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the push path iterated the id map")

    monkeypatch.setattr(stc._DatastreamIdMap, "__iter__", no_scan)
    monkeypatch.setattr(stc._DatastreamIdMap, "items", no_scan)

    result = client.push_observations([_reading("legacy", "humidity")], source="test")

    assert posts == []
    assert result["results"][0]["refused"] == [
        "sensor legacy has 2 datastreams and datastream 78's observed property is unknown"
    ]


def test_the_sensor_index_follows_every_write() -> None:
    ids = stc._DatastreamIdMap({"s::pm10": "1", "s::pm2_5": "2", "s": "9", "pm10": "1"})
    assert ids.ids_of_sensor("s") == {"1", "2"}

    ids["s::pm10"] = "3"
    ids.update({"t::pm10": "4"})
    del ids["s::pm2_5"]
    assert ids.pop("missing", None) is None
    assert ids.setdefault("s::humidity", "5") == "5"

    assert ids.ids_of_sensor("s") == {"3", "5"}
    assert ids.ids_of_sensor("t") == {"4"}
    del ids["t::pm10"]
    assert ids.ids_of_sensor("t") == frozenset()
    assert ids == {"s::pm10": "3", "s": "9", "pm10": "1", "s::humidity": "5"}
    ids.clear()
    assert ids.ids_of_sensor("s") == frozenset() and len(ids) == 0


# ---------------------------------------------------------------------------
# 5. Monitoring MQTT bridge
# ---------------------------------------------------------------------------


def test_bridge_refuses_another_propertys_datastream(client_for, monkeypatch) -> None:
    """The reviewer's repro: a DNMS-only location, whose sole datastream got the bare sensor-id key."""
    client = _listed_client(client_for, monkeypatch, [_v2_row(5, "sensor_community-7", "noise_laeq")])
    assert client._datastream_ids["sensor_community-7"] == "5"
    monkeypatch.setattr(bridge, "client", client)
    monkeypatch.setattr(client, "_resolve_datastream_id_live", lambda *a, **k: None)

    assert bridge._resolve_monitoring_payloads([_sc(7, "pm10")]) == []
    assert [p.datastream_id for p in bridge._resolve_monitoring_payloads([_sc(7, "noise_laeq")])] == ["5"]


def test_bridge_falls_back_to_the_live_lookup_after_a_refusal(client_for, monkeypatch) -> None:
    client = _listed_client(client_for, monkeypatch, [_v2_row(5, "sensor_community-7", "noise_laeq")])
    client._datastream_ids["sensor_community-7::pm10"] = "5"  # a stale mapping
    monkeypatch.setattr(bridge, "client", client)
    looked_up: list[str] = []

    def live(base_url: str, reading: SensorReading) -> str:
        looked_up.append(reading.observed_property)
        return "40"

    monkeypatch.setattr(client, "_resolve_datastream_id_live", live)

    payloads = bridge._resolve_monitoring_payloads([_sc(7, "pm10")])

    assert [p.datastream_id for p in payloads] == ["40"]
    assert looked_up == ["pm10"]


# ---------------------------------------------------------------------------
# Unresolved readings are flagged (contract with the seeding follow-up)
# ---------------------------------------------------------------------------


def _is_unresolved(entry: dict[str, Any]) -> bool:
    """How the seeding side recognises an unresolved reading (either version)."""
    return entry.get("unresolved") is True or (
        entry.get("ok") is False and str(entry.get("message", "")).startswith("No datastream mapping")
    )


def test_unresolved_readings_are_flagged(client_for, monkeypatch, no_http_push) -> None:
    client = _listed_client(client_for, monkeypatch, [_v2_row(1386, "sensor_community-93182", "pressure")])
    no_http_push(client)

    result = client.push_observations([_sc(93182, "pressure"), _sc(93182, "pm10")], source="sensor_community")

    unresolved = [r for r in result["results"] if _is_unresolved(r)]
    assert unresolved == [{
        "sensor_id": "sensor_community-93182", "observed_property": "pm10", "ok": False,
        "message": "No datastream mapping available for this sensor; dynamic lookup also failed.",
        "unresolved": True, "refused": ["datastream 1386 observes pressure"],
    }]
    assert next(r for r in result["results"] if not _is_unresolved(r))["ok"] is True


def test_unresolved_readings_are_flagged_while_the_circuit_is_open(client_for, monkeypatch, no_http_push) -> None:
    client = _listed_client(client_for, monkeypatch, [_v2_row(1386, "sensor_community-93182", "pressure")])
    no_http_push(client)
    monkeypatch.setattr(stc.observation_breaker, "allow", lambda target: False)

    result = client.push_observations([_sc(93182, "pm10"), _sc(93182, "pressure")], source="sensor_community")

    assert result["results"] == [r for r in result["results"] if r.get("unresolved") is True]
    assert len(result["results"]) == 1


def test_resolved_readings_carry_no_unresolved_flag(client_for, monkeypatch, no_http_push) -> None:
    client = _listed_client(client_for, monkeypatch, [_v2_row(1386, "sensor_community-93182", "pressure")])
    no_http_push(client)

    result = client.push_observations([_sc(93182, "pressure")], source="sensor_community")

    assert "unresolved" not in result["results"][0]


# ---------------------------------------------------------------------------
# The live lookup picks the same Datastream every time
# ---------------------------------------------------------------------------

KNMI_THING = "GEO_KNMI KNMI-52.36-3.34"


def _knmi_ds(ds_id: int, suffix: str, op_name: str | None, id_key: str = "@iot.id") -> dict[str, Any]:
    row: dict[str, Any] = {id_key: ds_id, "name": f"{KNMI_THING} sensor - {suffix}"}
    if op_name is not None:
        row["ObservedProperty"] = {"name": op_name}
    return row


class _LookupHttp:
    """FrostHTTPClient.get for a Datastreams query: answers ``rows`` in the given (physical) order."""

    def __init__(self, rows: list[dict[str, Any]], status_code: int = 200) -> None:
        self.rows = rows
        self.status_code = status_code
        self.params: list[dict[str, str]] = []

    def get(self, url: str, params: dict[str, str] | None = None) -> _Response:
        self.params.append(dict(params or {}))
        return _Response({"value": list(self.rows)}, self.status_code)

    extract_iot_id_from_body = staticmethod(FrostHTTPClient.extract_iot_id_from_body)


def _manager(http: _LookupHttp, version: str, tmp_path: Path) -> EntityManager:
    return EntityManager(http, EntityCache(tmp_path / "c.json"), fields=odata_fields_for(version))  # type: ignore[arg-type]


STALE = _knmi_ds(5, "Ground temperature", "Ground temperature")
CURRENT = _knmi_ds(9, "Ground surface temperature", "Ground surface temperature")


@pytest.mark.parametrize("rows", [[STALE, CURRENT], [CURRENT, STALE]], ids=["stale-first", "current-first"])
def test_live_lookup_prefers_the_canonical_display_name(rows: list[dict[str, Any]], tmp_path: Path) -> None:
    http = _LookupHttp(rows)
    manager = _manager(http, "v1.1", tmp_path)
    assert manager.find_datastream_by_filter(f"{V2_BASE}/Datastreams", "x", "Ground surface temperature") == "9"
    # The query asks for the matches in id order, newest first, with their ObservedProperty's name.
    assert http.params == [{"$filter": "x", "$orderby": "id desc", "$top": "20",
                            "$expand": "ObservedProperty($select=name)"}]


def test_live_lookup_prefers_the_display_name_over_a_newer_id(tmp_path: Path) -> None:
    newer_stale = _knmi_ds(12, "Ground temperature", "Ground temperature")
    assert _manager(_LookupHttp([newer_stale, CURRENT]), "v1.1", tmp_path).find_datastream_by_filter(
        "e", "x", "Ground surface temperature") == "9"
    # By the Datastream's name when the ObservedProperty is not expanded.
    no_op = [_knmi_ds(12, "Ground temperature", None), _knmi_ds(9, "Ground surface temperature", None)]
    assert _manager(_LookupHttp(no_op), "v1.1", tmp_path).find_datastream_by_filter(
        "e", "x", "Ground surface temperature") == "9"


@pytest.mark.parametrize("preferred", ["", "Soil moisture"])
def test_live_lookup_without_a_preferred_match_takes_the_highest_id(preferred: str, tmp_path: Path) -> None:
    rows = [_knmi_ds(9, "a", "A"), _knmi_ds(12, "b", "B"), _knmi_ds(10, "c", "C")]  # the server ignored $orderby
    assert _manager(_LookupHttp(rows), "v1.1", tmp_path).find_datastream_by_filter("e", "x", preferred) == "12"


@pytest.mark.parametrize("version", ["v1.1", "v2.0"])
def test_live_lookup_reads_ids_in_either_convention(version: str, tmp_path: Path) -> None:
    for id_key in ("@iot.id", "id"):  # FROST's /v2.0 path answers "id", whatever the target declares
        rows = [_knmi_ds(5, "Ground temperature", "Ground temperature", id_key),
                _knmi_ds(9, "Ground surface temperature", "Ground surface temperature", id_key)]
        manager = _manager(_LookupHttp(rows), version, tmp_path)
        assert manager.find_datastream_by_filter("e", "x", "Ground surface temperature") == "9"
        assert manager.find_datastream_by_filter("e", "x") == "9"


def test_live_lookup_finds_nothing_on_errors(tmp_path: Path) -> None:
    assert _manager(_LookupHttp([STALE], status_code=500), "v1.1", tmp_path).find_datastream_by_filter("e", "x") is None
    assert _manager(_LookupHttp([]), "v1.1", tmp_path).find_datastream_by_filter("e", "x") is None


def test_resolve_live_asks_for_the_canonical_display_name(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(stc, "settings", _settings(tmp_path))
    client = SensorThingsClient()
    asked: list[tuple[str, str]] = []

    def fetch(endpoint: str, query_filter: str, base_url: str | None = None, preferred_name: str = "") -> str:
        asked.append((query_filter, preferred_name))
        return "9"

    monkeypatch.setattr(client, "_fetch_datastream_id_by_filter", fetch)
    reading = SensorReading(sensor_id="knmi-0-20000-0-06310", sensor_name="KNMI KNMI-52.36-3.34 sensor",
                            observed_property="ground_temperature", unit="Cel", value=3.0,
                            timestamp="2026-11-20T12:00:00Z", thing_name="KNMI KNMI-52.36-3.34",
                            observed_property_name="Ground temperature")
    assert client._resolve_datastream_id_live("https://example.test/v1.1", reading) == "9"
    assert asked == [(f"Thing/name eq '{KNMI_THING}' and properties/streamKey eq 'ground_temperature'",
                      "Ground surface temperature")]


def test_resolve_live_picks_the_current_ground_temperature_end_to_end(monkeypatch, tmp_path) -> None:
    """Through the client's own entity manager: the stale Datastream, listed first, is not taken."""
    monkeypatch.setattr(stc, "settings", _settings(tmp_path))
    client = SensorThingsClient()
    http = _LookupHttp([STALE, CURRENT])
    monkeypatch.setattr(client._entity_manager, "_http", http)
    reading = SensorReading(sensor_id="knmi-0-20000-0-06310", sensor_name="KNMI KNMI-52.36-3.34 sensor",
                            observed_property="ground_temperature", unit="Cel", value=3.0,
                            timestamp="2026-11-20T12:00:00Z", thing_name="KNMI KNMI-52.36-3.34")
    assert client._resolve_datastream_id_live("https://example.test/v1.1", reading) == "9"
    assert http.params[0]["$orderby"] == "id desc"
