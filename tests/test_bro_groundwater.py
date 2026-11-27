"""BRO groundwater source, against the response shapes of the live BRO GM API (checked 2026-11-27).

Until the rewrite the source parsed a JSON API that does not exist: the well search is namespaced XML (the parser
found 0 of 959 wells), the GLD list endpoint answered 404, and dossiers are XML. The fixtures below are trimmed
copies of real responses (well GMW000000007878, dossier GLD000000046233 near Delft).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest

import app.services.api_client as api_client
import app.services.bro_groundwater_source as bro_source
from app.services.bro_groundwater_source import BROGroundwaterPollingSource
from app.services.rest_polling_source import RestPollingSource
from app.sources.bro_groundwater import (
    ObservationSummary,
    Point,
    Tube,
    Well,
    build_entity_set,
    merge_points,
    parse_points,
    parse_relations,
    parse_summary,
    parse_wells,
    readings_for,
    sensor_id_for,
)

_NS = ('xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:brocom="http://www.broservices.nl/xsd/brocommon/3.0" '
       'xmlns="http://www.broservices.nl/xsd/dsgmw/1.1"')


def _gmw(bro_id: str, lat: float, lon: float, *, removed: str = "nee", deregistered: str = "nee",
         pos: bool = True) -> str:
    location = (f'<brocom:standardizedLocation srsName="urn:ogc:def:crs:EPSG::4258"><gml:pos>{lat} {lon}</gml:pos>'
                f'</brocom:standardizedLocation>') if pos else ""
    return (f"<dispatchDocument><GMW_C><brocom:broId>{bro_id}</brocom:broId>"
            f"<brocom:deregistered>{deregistered}</brocom:deregistered>{location}"
            '<brocom:deliveredLocation srsName="http://www.opengis.net/def/crs/EPSG/0/28992"><gml:pos>83204.000 448440.000'
            f"</gml:pos></brocom:deliveredLocation><removed>{removed}</removed></GMW_C></dispatchDocument>")


def _search(*wells: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?><dispatchCharacteristicsResponse {_NS}>'
            f"<brocom:responseType>dispatch</brocom:responseType>{''.join(wells)}</dispatchCharacteristicsResponse>")


RELATIONS = {
    "gmwBroId": "GMW000000007878",
    "monitoringTubeReferences": [
        {"tubeNumber": 1, "screenTopPosition": -4.06, "gmnReferences": [],
         "gldReferences": [{"broId": "GLD000000046233", "url": "https://publiek.broservices.nl/gm/gld/v1/objects/GLD000000046233"}]},
        {"tubeNumber": 2, "gldReferences": []},
    ],
    "characteristics": None,
}

SUMMARY = [
    {"observationId": "_a", "startDate": "31-12-2023", "endDate": "19-02-2025", "observationType": "reguliereMeting",
     "observationStatus": "volledigBeoordeeld"},
    {"observationId": "_b", "startDate": "11-04-2025", "endDate": "31-12-2025", "observationType": "reguliereMeting",
     "observationStatus": "voorlopig"},
    {"observationId": "_c", "startDate": "01-06-2025", "endDate": "01-06-2025", "observationType": "controlemeting",
     "observationStatus": "voorlopig"},
]


def _tvp(time: str, value: str, quality: str = "goedgekeurd") -> str:
    return (f"<wml2:point><wml2:MeasurementTVP><wml2:time>{time}</wml2:time><wml2:value uom=\"m\">{value}</wml2:value>"
            "<wml2:metadata><wml2:TVPMeasurementMetadata><wml2:qualifier><swe:Category>"
            '<swe:codeSpace xlink:href="urn:bro:gld:StatusQualityControl"/>'
            f"<swe:value>{quality}</swe:value></swe:Category></wml2:qualifier></wml2:TVPMeasurementMetadata>"
            "</wml2:metadata></wml2:MeasurementTVP></wml2:point>")


def _observation(*points: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><dispatchDataResponse xmlns:wml2="http://www.opengis.net/waterml/2.0" '
            'xmlns:swe="http://www.opengis.net/swe/2.0" xmlns:xlink="http://www.w3.org/1999/xlink">'
            f"<wml2:MeasurementTimeseries>{''.join(points)}</wml2:MeasurementTimeseries></dispatchDataResponse>")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseWells:
    def test_namespaced_wells_with_their_standardized_location(self):
        """protects=wells come out of the namespaced XML, at their EPSG:4258 position (not the RD delivered one);
        fails_when=the parser looks for un-namespaced broId again (0 wells) or reads the RD coordinates;
        no_claim=which wells BRO holds."""
        xml = _search(_gmw("GMW2", 52.02, 4.35), _gmw("GMW1", 52.019661020, 4.341227630))

        assert parse_wells(xml, 500) == [Well("GMW1", 52.01966102, 4.34122763), Well("GMW2", 52.02, 4.35)]

    def test_removed_deregistered_and_unplaced_wells_are_left_out(self):
        xml = _search(_gmw("GMW1", 52.0, 4.3, removed="ja"), _gmw("GMW2", 52.0, 4.3, deregistered="ja"),
                      _gmw("GMW3", 52.0, 4.3, pos=False), _gmw("GMW4", 52.0, 4.3))

        assert [w.bro_id for w in parse_wells(xml, 500)] == ["GMW4"]

    def test_max_wells_keeps_the_first_by_bro_id(self, caplog):
        xml = _search(*(_gmw(f"GMW{i}", 52.0, 4.3) for i in (3, 1, 2)))

        with caplog.at_level(logging.WARNING):
            assert [w.bro_id for w in parse_wells(xml, 2)] == ["GMW1", "GMW2"]
        assert "BRO_MAX_WELLS" in caplog.text


def test_relations_give_the_tubes_with_a_dossier():
    """protects=a well's tubes and their GLD ids come from gmw-relations; tubes without a GLD are left out;
    fails_when=the source asks the GLD API for a well's dossiers (404) or registers tubes without data;
    no_claim=GMN or GAR references."""
    assert parse_relations(RELATIONS) == [Tube(1, ("GLD000000046233",))]
    assert parse_relations({"monitoringTubeReferences": None}) == []


def test_summary_dates_are_day_month_year():
    summary = parse_summary([*SUMMARY, {"startDate": "01-01-2020"}])

    assert summary[1] == ObservationSummary("_b", date(2025, 4, 11), date(2025, 12, 31), "reguliereMeting", "voorlopig")
    assert len(summary) == 3


def test_points_are_utc_with_their_quality_and_nil_values_skipped():
    """protects=WaterML points: the offset time to UTC, the value in m, the StatusQualityControl;
    fails_when=times are kept as local wall clock or a nil value raises;
    no_claim=other WaterML metadata."""
    xml = _observation(_tvp("2025-04-11T00:00:00+02:00", "-0.923"), _tvp("2025-04-11T01:00:00+02:00", ""),
                       _tvp("2025-12-31T23:00:00+01:00", "-0.932", "afgekeurd"))

    assert parse_points(xml) == [
        Point(datetime(2025, 4, 10, 22, 0, tzinfo=UTC), -0.923, "goedgekeurd"),
        Point(datetime(2025, 12, 31, 22, 0, tzinfo=UTC), -0.932, "afgekeurd"),
    ]


def test_merge_prefers_the_assessed_value_and_drops_rejected_points():
    t = datetime(2025, 5, 1, tzinfo=UTC)
    merged = merge_points([
        ("volledigBeoordeeld", [Point(t, -1.0, "goedgekeurd")]),
        ("voorlopig", [Point(t, -1.1, "goedgekeurd"), Point(t + timedelta(hours=1), -1.2, "afgekeurd"),
                       Point(t + timedelta(hours=2), -1.3, "onbeslist")]),
    ])

    assert merged == [Point(t, -1.0, "goedgekeurd"), Point(t + timedelta(hours=2), -1.3, "onbeslist")]


def test_readings_are_canonical_groundwater_levels_per_tube():
    t = datetime(2025, 5, 1, tzinfo=UTC)
    (reading,) = readings_for([Point(t, -0.9, "onbeslist"), Point(t, -1.0, "afgekeurd")], "GMW1", 2)

    assert (reading.sensor_id, reading.observed_property, reading.unit, reading.observed_property_name,
            reading.quality, reading.thing_name) == (
        "bro-GMW1-2", "water_level", "m", "Groundwater level", "uncertain", "BRO GMW1")


def test_entity_set_has_one_sensor_per_tube():
    es = build_entity_set(Well("GMW1", 52.01, 4.34), [2, 1])

    assert [s["sensor_id"] for s in es["sensors"]] == [sensor_id_for("GMW1", 1), sensor_id_for("GMW1", 2)]
    assert es["location"]["location"]["coordinates"] == [4.34, 52.01]
    assert es["thing"]["properties"]["source"] == "bro_groundwater"
    assert all(s["observed_properties"] == ["water_level"] for s in es["sensors"])


# ---------------------------------------------------------------------------
# The polling source
# ---------------------------------------------------------------------------


class _FakeBro:
    """Stands in for AsyncAPIClient against BRO: scripted responses, every request recorded."""

    def __init__(self, *, wells: str, relations: dict[str, Any], summaries: dict[str, list],
                 observations: dict[str, str], failing: set[str] | None = None) -> None:
        self.wells, self.relations, self.summaries, self.observations = wells, relations, summaries, observations
        self.failing = failing or set()
        self.calls: list[tuple[str, Any]] = []

    async def post_text(self, path: str, json_body: Any = None) -> str:
        self.calls.append((path, json_body))
        return self.wells

    async def get(self, path: str, params: dict | None = None) -> Any:
        self.calls.append((path, params))
        if any(f in path for f in self.failing):
            raise httpx.ConnectError("BRO unreachable")
        if path.startswith("/v1/gmw-relations/"):
            return self.relations[path.rsplit("/", 1)[1]]
        return self.summaries[path.split("/")[4]]

    async def get_text(self, path: str, params: dict | None = None) -> str:
        self.calls.append((path, params))
        return self.observations[path.rsplit("/", 1)[1]]


def _settings(monkeypatch, **changes: Any) -> None:
    base = {"bro_enabled": True, "bro_bbox": "52.0,4.3,52.1,4.4", "bro_max_wells": 500, "bro_initial_days": 30}
    monkeypatch.setattr(bro_source, "settings", dataclasses.replace(bro_source.settings, **{**base, **changes}))


def _fake(**overrides: Any) -> _FakeBro:
    args: dict[str, Any] = {
        "wells": _search(_gmw("GMW000000007878", 52.0197, 4.3412), _gmw("GMW000000009999", 52.03, 4.36)),
        "relations": {"GMW000000007878": RELATIONS, "GMW000000009999": {"monitoringTubeReferences": []}},
        "summaries": {"GLD000000046233": SUMMARY},
        "observations": {
            "_a": _observation(_tvp("2025-01-01T00:00:00+01:00", "-0.80")),
            "_b": _observation(_tvp("2025-11-30T00:00:00+01:00", "-0.90"), _tvp("2025-12-15T00:00:00+01:00", "-0.91"),
                               _tvp("2025-12-31T23:00:00+01:00", "-0.93")),
        },
    }
    args.update(overrides)
    return _FakeBro(**args)


def _run(source: BROGroundwaterPollingSource) -> list[Any]:
    return asyncio.run(source._do_fetch_readings())


def test_first_sight_reads_the_last_initial_days_of_the_dossier(monkeypatch):
    """protects=a tube with no fetched point and no FROST mark gets the last BRO_INITIAL_DAYS of its own data,
    via the time-filtered observation endpoint, and only regular observations reaching that far are fetched;
    fails_when=the source asks for the hour before now (BRO is months behind: nothing, ever) or downloads whole
    dossiers (52 MB);
    no_claim=what BRO does with the margin (the loop's dedup drops repeats)."""
    _settings(monkeypatch)
    source = BROGroundwaterPollingSource()
    source._client = fake = _fake()

    readings = _run(source)

    # Newest end 31-12-2025, 30 days back: from 2025-12-01 (points from 30 Nov are before it).
    assert [(r.timestamp, r.value) for r in readings] == [
        (datetime(2025, 12, 14, 23, 0, tzinfo=UTC), -0.91), (datetime(2025, 12, 31, 22, 0, tzinfo=UTC), -0.93)]
    fetched = [(path, params) for path, params in fake.calls if "/observations/" in path]
    assert [path.rsplit("/", 1)[1] for path, _ in fetched] == ["_b"]  # _a ends earlier, _c is a control measurement
    assert fetched[0][1]["startTVPTime"] == "2025-11-30T00:00:00"
    assert set(fetched[0][1]) == {"startTVPTime", "endTVPTime"} and "Z" not in fetched[0][1]["endTVPTime"]


def test_a_frost_mark_is_where_a_tube_starts(monkeypatch):
    """protects=after a restart a tube resumes at its newest row in FROST (use_marks), not its first-sight window;
    fails_when=marks are ignored and every restart re-reads (or skips) a window;
    no_claim=how the loop seeds the marks."""
    _settings(monkeypatch)
    source = BROGroundwaterPollingSource()
    source._client = _fake()
    source.use_marks({"bro-GMW000000007878-1::water_level": datetime(2025, 12, 20, tzinfo=UTC)})

    readings = _run(source)

    assert [r.timestamp for r in readings] == [datetime(2025, 12, 31, 22, 0, tzinfo=UTC)]


def test_the_next_cycle_starts_after_the_newest_point_fetched(monkeypatch):
    _settings(monkeypatch)
    source = BROGroundwaterPollingSource()
    source._client = fake = _fake()
    _run(source)
    fake.calls.clear()

    assert _run(source) == []
    (fetch,) = [params for path, params in fake.calls if "/observations/" in path]
    assert fetch["startTVPTime"] == "2025-12-30T22:00:00"  # newest point minus the one-day margin
    assert not [path for path, _ in fake.calls if "gmw-relations" in path or "searches" in path]  # cached


def test_entity_sets_are_the_wells_with_a_dossier(monkeypatch):
    _settings(monkeypatch)
    source = BROGroundwaterPollingSource()
    source._client = _fake()
    readings = _run(source)

    assert [es["thing"]["name"] for es in source.entity_sets()] == ["BRO GMW000000007878"]
    assert source.entity_sets_for(readings) == source.entity_sets()
    assert source.entity_sets_for([]) == []


def test_a_failing_well_is_skipped_and_the_rest_go_on(monkeypatch, caplog):
    _settings(monkeypatch)
    source = BROGroundwaterPollingSource()
    source._client = _fake(failing={"GMW000000009999"})

    with caplog.at_level(logging.WARNING):
        readings = _run(source)

    assert readings and "GMW000000009999 skipped" in caplog.text
    assert "GMW000000009999" not in source._tubes  # tried again next cycle


def test_batches_hold_a_few_wells_each(monkeypatch):
    _settings(monkeypatch)
    monkeypatch.setattr(bro_source, "_WELLS_PER_BATCH", 1)
    source = BROGroundwaterPollingSource()
    source._client = _fake(relations={"GMW000000007878": RELATIONS, "GMW000000009999": RELATIONS})

    async def batches() -> list[list[Any]]:
        return [b async for b in source._do_fetch_reading_batches()]

    assert [{r.sensor_id for r in b} for b in asyncio.run(batches())] == [
        {"bro-GMW000000007878-1"}, {"bro-GMW000000009999-1"}]


@pytest.mark.parametrize("bbox", ["", "52.0,4.3"])
def test_no_or_bad_bbox_discovers_nothing(monkeypatch, caplog, bbox):
    _settings(monkeypatch, bro_bbox=bbox)
    source = BROGroundwaterPollingSource()
    source._client = fake = _fake()

    with caplog.at_level(logging.WARNING):
        assert _run(source) == []
    assert fake.calls == [] and "BRO_BBOX" in caplog.text


def test_source_polls_daily_by_default():
    """protects=BRO is polled once a day by default (BRO_POLL_SECONDS unset), and the source is disabled by default;
    fails_when=the hourly default comes back (BRO is months behind: 24 empty polls a day);
    no_claim=a value set in the environment."""
    defaults = {f.name: f.default for f in dataclasses.fields(bro_source.settings)}
    source = BROGroundwaterPollingSource()

    assert defaults["bro_poll_seconds"] == 86400 and defaults["bro_enabled"] is False
    assert isinstance(source, RestPollingSource) and source.uses_dynamic_discovery
    assert source.source_name == "bro_groundwater"


# ---------------------------------------------------------------------------
# AsyncAPIClient text methods (BRO answers XML)
# ---------------------------------------------------------------------------


def test_text_methods_return_the_body_and_retry(monkeypatch):
    """protects=get_text/post_text return the raw body (XML) and retry like get/post;
    fails_when=they parse JSON or give up on the first 503;
    no_claim=the wait between retries (sleep is skipped here)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        if len(seen) == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="<xml/>")

    real = httpx.AsyncClient
    monkeypatch.setattr(api_client.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    client = api_client.AsyncAPIClient(base_url="https://bro.test")

    assert asyncio.run(client.get_text("/x")) == "<xml/>"
    assert asyncio.run(client.post_text("/y", json_body={"a": 1})) == "<xml/>"
    assert seen == ["GET", "GET", "POST"]
