"""scripts/fix_samen_meten_calibrated.py (#41): the three rules, the skips, the RIVM client, apply/revert.

Unit tests use a fake RIVM client (or a fake requests session) and a fake psql that keeps an in-memory
OBSERVATIONS table; no network, no database. tests/integration/test_fix_samen_meten_calibrated_it.py runs the
script's real SQL on the docker test FROST.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import requests

from scripts import cleanup_frost_observations as cleanup
from scripts import fix_samen_meten_calibrated as fix

DAY = "2026-11-25"
DS25, DS10, DS_OTHER = 16094, 16095, 16096
KAL25, KAL10 = 43002, 43004
NAME = "LUC_354724647888301"
COLUMNS = ["PHENOMENON_TIME_START", "RESULT_TYPE", "RESULT_NUMBER", "RESULT_STRING", "PARAMETERS", "DATASTREAM_ID",
           "FEATURE_ID", "ID"]


def _t(hour: int, minute: int = 0) -> datetime:
    return fix.parse_time(f"{DAY}T{hour:02d}:{minute:02d}:00Z")


def _row(obs_id: int, hour: int, value: float | None, *, ds: int = DS25, sid: str | None = "sm-8272",
         text: str | None = None, rtype: int | None = 0) -> fix.Row:
    return fix.Row(obs_id, ds, _t(hour), rtype, value, text if text is not None else
                   (None if value is None else fix.number_text(value)), sid)


def _sm(ds: int = DS25, prop: str = "pm2_5", thing: str = f"GEO_SamenMeten {NAME}") -> fix.SmDatastream:
    return fix.SmDatastream(ds, f"{thing} sensor - {prop}", prop, thing)


def _plan(rows: list[fix.Row], kal: dict[int, list[float]]) -> fix.DsPlan:
    plan = fix.DsPlan(_sm(), rows, kal={_t(h): v for h, v in kal.items()})
    fix.plan_rows(plan)
    return plan


def _deleted(plan: fix.DsPlan) -> dict[int, str]:
    return {d.row.id: d.reason for d in plan.deletes}


class FakeRivm:
    """RIVM as data: Things by id, _kal observations by datastream; failures injectable."""

    def __init__(self, things: dict[int, fix.RivmThing] | None = None,
                 observations: dict[int, dict[int, float]] | None = None, *, failing: set[int] | None = None) -> None:
        self.things = things if things is not None else {
            8272: fix.RivmThing(8272, NAME, ((43001, f"{NAME}-12-pm25"), (KAL25, f"{NAME}-12-pm25_kal"),
                                             (43003, f"{NAME}-12-pm10"), (KAL10, f"{NAME}-12-pm10_kal")))}
        self.obs = observations if observations is not None else {KAL25: {6: 17.234945, 8: 19.5}, KAL10: {6: 28.4}}
        self.failing = failing or set()
        self.calls: list[tuple[int, datetime, datetime]] = []

    def thing(self, thing_id: int) -> fix.RivmThing:
        if thing_id in self.failing:
            raise fix.RivmError(f"Things({thing_id}): timeout (after 4 attempts)")
        if thing_id not in self.things:
            raise fix.RivmNotFound(f"Things({thing_id}): HTTP 404")
        return self.things[thing_id]

    def observations(self, ds_id: int, start: datetime, end: datetime) -> list[tuple[datetime, float]]:
        self.calls.append((ds_id, start, end))
        if ds_id in self.failing:
            raise fix.RivmError(f"Datastreams({ds_id}): HTTP 503 (after 4 attempts)")
        return [(_t(h), v) for h, v in self.obs.get(ds_id, {}).items() if start <= _t(h) < end]


class FakeDb:
    """A fake psql: answers the script's statements from an in-memory OBSERVATIONS table and records sessions.

    rows: id -> dict(ds, hour, num, str, sid). archive / rollback: checked at the moment a DELETE / UPDATE arrives.
    """

    def __init__(self, rows: dict[int, dict[str, Any]], catalogue: list[fix.SmDatastream] | None = None, *,
                 archive: Path | None = None, rollback: Path | None = None, busy: bool = False) -> None:
        self.rows = {i: dict(r) for i, r in rows.items()}
        self.catalogue = catalogue if catalogue is not None else [_sm(DS25), _sm(DS10, "pm10")]
        self.archive, self.rollback, self.busy = archive, rollback, busy
        self.sessions: list[str] = []
        self.unarchived_at_delete: set[int] = set()
        self.unrecorded_at_update: set[int] = set()

    def text(self, obs_id: int) -> str:
        r = self.rows[obs_id]
        return json.dumps({"PHENOMENON_TIME_START": f"{DAY}T{r['hour']:02d}:00:00+00:00", "RESULT_TYPE": 0,
                           "RESULT_NUMBER": r["num"], "RESULT_STRING": r["str"], "PARAMETERS": {"sensor_id": r["sid"]},
                           "DATASTREAM_ID": r["ds"], "FEATURE_ID": 1, "ID": obs_id}, separators=(",", ":"))

    def __call__(self, sql: str) -> list[str]:
        self.sessions.append(sql)
        out: list[str] = []
        if "'t', 'smds'" in sql:
            out += [json.dumps({"t": "smds", "id": d.id, "name": d.name, "prop": d.prop, "thing_name": d.thing_name})
                    for d in self.catalogue]
        if "'t', 'col'" in sql:
            out += [json.dumps({"t": "col", "name": c}) for c in COLUMNS]
        if "pg_stat_activity" in sql and self.busy:
            out.append(f"busy|4242|{cleanup.APP_NAME}|active|00:00:03|end")
        for ids, max_id in re.findall(r"""o\."DATASTREAM_ID" = ANY\('\{([0-9,]*)\}'::bigint\[\]\) AND o\."ID" <= (\d+);""", sql):
            wanted = {int(i) for i in ids.split(",") if i}
            for obs_id, r in sorted(self.rows.items()):
                if r["ds"] in wanted and obs_id <= int(max_id):
                    out.append(json.dumps({"t": "r", "id": obs_id, "ds": r["ds"], "pt": f"{DAY}T{r['hour']:02d}:00:00+00:00",
                                           "type": 0, "num": r["num"], "str": r["str"], "sid": r["sid"]}))
        for ids, ds in re.findall(r"""o\."ID" = ANY\('\{([0-9,]*)\}'::bigint\[\]\) AND o\."DATASTREAM_ID" = (\d+)""", sql):
            for obs_id in (int(i) for i in ids.split(",") if i):
                if obs_id in self.rows and self.rows[obs_id]["ds"] == int(ds):
                    text = self.text(obs_id)
                    out.append(f"cand|{ds}|{obs_id}|{hashlib.md5(text.encode()).hexdigest()}||{text}")
        out += self._updates(sql, "upd", "updated", ("old", "olds"), ("new", "news"))
        out += self._updates(sql, "rev", "reverted", ("new", "news"), ("old", "olds"))
        for chunk in sql.split("SELECT 'rvstate', ")[1:]:
            ds, values = int(chunk.split(",", 1)[0]), self._values(chunk)
            present = [v for v in values if v["id"] in self.rows and self.rows[v["id"]]["ds"] == ds]
            out.append(f"rvstate|{ds}|{len(values)}|{len(present)}|"
                       f"{sum(1 for v in present if self._holds(v, 'new', 'news'))}|"
                       f"{sum(1 for v in present if self._holds(v, 'old', 'olds'))}|end")
        for chunk in sql.split("WITH del AS (")[1:]:
            ds = int(re.search(r'o\."DATASTREAM_ID" = (\d+)', chunk)[1])  # type: ignore[index]
            ids = [int(i) for i in re.search(r"unnest\('\{([0-9,]*)\}'", chunk)[1].split(",")]  # type: ignore[index]
            hashes = re.search(r"'\{([0-9a-f,]*)\}'::text\[\]", chunk)[1].split(",")  # type: ignore[index]
            keep = re.search(r'k\."ID" = ANY\(\'\{([0-9,]*)\}\'', chunk)
            keep_ids = {int(i) for i in keep[1].split(",")} if keep else None
            archived = _archived_ids(self.archive) if self.archive else set()
            count = 0
            for obs_id, digest in zip(ids, hashes, strict=True):
                self.unarchived_at_delete |= {obs_id} - archived
                if obs_id not in self.rows or self.rows[obs_id]["ds"] != ds:
                    continue
                if hashlib.md5(self.text(obs_id).encode()).hexdigest() != digest:
                    continue
                hour = self.rows[obs_id]["hour"]
                if keep_ids is not None and not any(k in self.rows and k != obs_id and self.rows[k]["ds"] == ds
                                                    and self.rows[k]["hour"] == hour for k in keep_ids):
                    continue
                del self.rows[obs_id]
                count += 1
            out.append(f"deleted|{ds}|{len(ids)}|{count}|end")
        out += [f"refreshed|{ds}|end" for ds in re.findall(r"SELECT 'refreshed', (\d+)", sql)]
        return out

    @staticmethod
    def _values(chunk: str) -> list[dict[str, Any]]:
        unnest = re.search(r"unnest\('\{([0-9,]*)\}'::bigint\[\], '\{([^}]*)\}'::double precision\[\], "
                           r"'\{([^}]*)\}'::double precision\[\], ARRAY\[(.*?)\]::text\[\], ARRAY\[(.*?)\]::text\[\]\)",
                           chunk)
        assert unnest is not None
        ids, olds, news, olds_t, news_t = unnest.groups()

        def texts(items: str) -> list[str | None]:
            return [None if m[0] == "NULL" else m[1].replace("''", "'")
                    for m in re.findall(r"(NULL|'((?:[^']|'')*)')", items)]

        return [{"id": int(i), "old": float(o), "new": float(n), "olds": ot, "news": nt}
                for i, o, n, ot, nt in zip(ids.split(","), olds.split(","), news.split(","), texts(olds_t),
                                           texts(news_t), strict=True)]

    def _holds(self, v: dict[str, Any], num: str, text: str) -> bool:
        r = self.rows[v["id"]]
        return bool(r["num"] == v[num] and r["str"] == v[text])

    def _updates(self, sql: str, cte: str, tag: str, match: tuple[str, str], write: tuple[str, str]) -> list[str]:
        out = []
        for chunk in sql.split(f"WITH {cte} AS (")[1:]:
            ds = int(re.search(rf"SELECT '{tag}', (\d+)", chunk)[1])  # type: ignore[index]
            values = self._values(chunk)
            if tag == "updated" and self.rollback is not None:
                recorded = {u.id for u in fix.read_rollback(self.rollback)}
                self.unrecorded_at_update |= {v["id"] for v in values} - recorded
            count = 0
            for v in values:
                if v["id"] in self.rows and self.rows[v["id"]]["ds"] == ds and self._holds(v, *match):
                    self.rows[v["id"]]["num"], self.rows[v["id"]]["str"] = v[write[0]], v[write[1]]
                    count += 1
            out.append(f"{tag}|{ds}|{len(values)}|{count}|end")
        return out

    def writes(self) -> list[str]:
        return [s for s in self.sessions if s.startswith(cleanup.WRITE_SESSION)]


def _archived_ids(archive: Path) -> set[int]:
    if not archive.exists():
        return set()
    return {cleanup.parse_row_line(line).id for line in archive.read_text(encoding="utf-8").splitlines()
            if line.startswith('{"sel":')}


def _db_rows() -> dict[int, dict[str, Any]]:
    """DS25: hour 6 raw/kal/raw (rule 1), hour 8 two raw (rule 2), hour 10 no _kal (rule 3); DS10: hour 6 rule 1."""
    return {
        1: {"ds": DS25, "hour": 6, "num": 17.77, "str": "17.77", "sid": "sm-8272"},
        2: {"ds": DS25, "hour": 6, "num": 17.234945, "str": "17.234945", "sid": "sm-8272"},
        3: {"ds": DS25, "hour": 6, "num": 17.77, "str": "17.77", "sid": "sm-8272"},
        4: {"ds": DS25, "hour": 8, "num": 20.0, "str": "20.0", "sid": "sm-8272"},
        5: {"ds": DS25, "hour": 8, "num": 21.0, "str": "21.0", "sid": "sm-8272"},
        6: {"ds": DS25, "hour": 10, "num": 5.0, "str": "5.0", "sid": "sm-8272"},
        7: {"ds": DS10, "hour": 6, "num": 30.1, "str": "30.1", "sid": "sm-8272"},
        8: {"ds": DS10, "hour": 6, "num": 28.4, "str": "28.4", "sid": "sm-8272"},
        99: {"ds": DS25, "hour": 6, "num": 50.0, "str": "50.0", "sid": "sm-8272"},  # above --max-id
    }


def _main(db: FakeDb, api: FakeRivm, *args: str) -> int:
    return fix.main(["--container", "fake", "--max-id", "50", *args], api=api, run_sql=db)


# ---------------------------------------------------------------------------
# The three rules
# ---------------------------------------------------------------------------


def test_rule1_keeps_lowest_matching_row_and_deletes_the_others() -> None:
    """protects=rule 1 (a row equals _kal: keep the lowest-ID matching row, delete the rest);
    fails_when=a raw row is kept, a later matching twin is kept instead of the first, or anything is updated;
    no_claim=SQL execution (see the integration test)"""
    plan = _plan([_row(1, 6, 17.77), _row(2, 6, 17.234945), _row(3, 6, 17.234945), _row(4, 6, 17.77)],
                 {6: [17.234945]})
    assert plan.keep == [2]
    assert plan.updates == []
    assert _deleted(plan) == {1: fix.DEL_MATCH, 3: fix.DEL_MATCH, 4: fix.DEL_MATCH}
    assert {d.keep for d in plan.deletes} == {2}


def test_rule2_updates_lowest_row_and_deletes_the_others() -> None:
    """protects=rule 2 (_kal exists, no row equals it: lowest-ID row set to _kal, others deleted);
    fails_when=another row is updated, the old value/string is not carried for the rollback, or extras survive;
    no_claim=that FROST serves the new value (integration test)"""
    plan = _plan([_row(5, 8, 21.0), _row(4, 8, 20.0, text="20.00")], {8: [19.5]})
    assert plan.keep == []
    assert plan.updates == [fix.Update(4, DS25, _t(8), 20.0, 19.5, "20.00")]
    assert plan.updates[0].new_string == "19.5"
    assert _deleted(plan) == {5: fix.DEL_UPDATED}


def test_rule3_deletes_every_row_of_an_hour_without_kal() -> None:
    """protects=rule 3 (no _kal value for the hour: delete all its rows, owner decision 2026-11-25);
    fails_when=a row of such an hour is kept or updated, or a row of an hour with _kal is deleted by rule 3;
    no_claim=that RIVM really has no value (the fetch is trusted once it succeeded)"""
    plan = _plan([_row(1, 10, 5.0), _row(2, 10, 6.0), _row(3, 11, 7.0)], {11: [7.0]})
    assert _deleted(plan) == {1: fix.DEL_NO_KAL, 2: fix.DEL_NO_KAL}
    assert plan.keep == [3]


def test_float_tolerance_and_result_string() -> None:
    """protects=equality |diff| < 1e-6 on RESULT_NUMBER and on the RESULT_STRING FROST serves;
    fails_when=a 4e-7 difference counts as different, a 2e-6 one as equal, or a row whose string disagrees is kept;
    no_claim=rounding inside PostgreSQL (values travel as shortest exact text)"""
    assert _plan([_row(1, 6, 12.0)], {6: [12.0000004]}).keep == [1]
    far = _plan([_row(1, 6, 12.0)], {6: [12.000002]})
    assert far.keep == [] and [u.id for u in far.updates] == [1]
    stale = _plan([_row(1, 6, 17.234945, text="17.77")], {6: [17.234945]})
    assert [u.id for u in stale.updates] == [1] and stale.updates[0].old_string == "17.77"


def test_hour_with_several_kal_values_is_left_alone() -> None:
    """protects=an hour where RIVM returns different _kal values is not guessed at;
    fails_when=its rows are updated or deleted, or it is not counted as ambiguous;
    no_claim=how often RIVM does this (unknown)"""
    plan = _plan([_row(1, 6, 17.0), _row(2, 6, 18.0)], {6: [17.0, 18.0]})
    assert plan.actions == 0 and plan.ambiguous_hours == 1 and plan.ambiguous_rows == 2
    assert _plan([_row(1, 6, 17.0), _row(2, 6, 17.0)], {6: [17.0, 17.0]}).keep == [1]


def test_rows_are_bucketed_by_utc_hour() -> None:
    """protects=the unit is (datastream, phenomenonTime hour), RIVM values matched to the hour they fall in;
    fails_when=rows of 06:00 and 06:30 are treated as two hours, or 07:00 matches the 06:00 _kal value;
    no_claim=minute-level RIVM data (RIVM is hourly)"""
    rows = [_row(1, 6, 17.0), fix.Row(2, DS25, _t(6, 30), 0, 18.0, "18.0", "sm-8272"), _row(3, 7, 17.0)]
    plan = _plan(rows, {6: [17.0]})
    assert plan.keep == [1]
    assert _deleted(plan) == {2: fix.DEL_MATCH, 3: fix.DEL_NO_KAL}


# ---------------------------------------------------------------------------
# Skips
# ---------------------------------------------------------------------------


def _build(rows: list[fix.Row], api: FakeRivm, ds: fix.SmDatastream | None = None) -> fix.DsPlan:
    ds = ds or _sm()
    return fix.build_plans({ds.id: ds}, {ds.id: rows}, Counter(), api, 2)[0]


def test_name_cross_check_mismatch_skips_datastream() -> None:
    """protects=the RIVM Thing must carry the name in our thing_name minus 'GEO_SamenMeten ';
    fails_when=a datastream is repaired against another sensor's _kal series, or one without the prefix is not skipped;
    no_claim=renames on the RIVM side over time"""
    rows = [_row(1, 6, 17.77)]
    plan = _build(rows, FakeRivm(), _sm(thing="GEO_SamenMeten SOMETHING_ELSE"))
    assert plan.skip == fix.SKIP_NAME and plan.actions == 0
    assert _build(rows, FakeRivm(), _sm(thing=NAME)).skip == fix.SKIP_NAME
    assert _build(rows, FakeRivm()).skip is None


def test_multiple_rivm_ids_or_foreign_sensor_id_skip_datastream() -> None:
    """protects=the RIVM Thing comes from the rows' sensor_id 'sm-<id>', one per datastream;
    fails_when=a datastream with rows of two RIVM Things (or a TGV/old-style label) is repaired, or the datastream's
    own sensor_id ('sm-luc_...') is used; no_claim=which of the ids would have been right"""
    api = FakeRivm()
    assert _build([_row(1, 6, 1.0), _row(2, 7, 1.0, sid="sm-8273")], api).skip == fix.SKIP_SEVERAL_IDS
    assert _build([_row(1, 6, 1.0), _row(2, 7, 1.0, sid="tgv-officelab")], api).skip == fix.SKIP_SEVERAL_IDS
    assert _build([_row(1, 6, 1.0, sid="sm-luc_354724647888301")], api).skip == fix.SKIP_NO_ID
    assert _build([_row(1, 6, 1.0, sid=None)], api).skip == fix.SKIP_NO_ID
    assert api.calls == []


def test_missing_kal_datastream_skips_and_several_take_the_lowest() -> None:
    """protects=a Thing without '-pm25_kal'/'-pm10_kal' datastream is skipped; of several the lowest id is used (#38);
    fails_when=the raw series is used as reference, or the suffix of the other PM property is matched;
    no_claim=ObservedProperty-based matching (RIVM often lacks it; the issue specifies the name suffix)"""
    raw_only = fix.RivmThing(8272, NAME, ((43001, f"{NAME}-12-pm25"), (KAL10, f"{NAME}-12-pm10_kal")))
    plan = _build([_row(1, 6, 17.77)], FakeRivm({8272: raw_only}))
    assert plan.skip == fix.SKIP_NO_KAL and plan.kal_ds is None
    several = fix.RivmThing(8272, NAME, ((43050, f"{NAME}-13-PM25_KAL"), (KAL25, f"{NAME}-12-pm25_kal")))
    plan = _build([_row(1, 6, 17.234945)], FakeRivm({8272: several}))
    assert plan.skip is None and plan.kal_ds == KAL25 and "several _kal" in plan.detail
    assert _build([_row(1, 6, 28.4, ds=DS10)], FakeRivm(), _sm(DS10, "pm10")).kal_ds == KAL10


def test_fetch_failure_or_unknown_thing_skips_datastream() -> None:
    """protects=a datastream whose RIVM Thing or _kal series cannot be read after retries is skipped, not emptied;
    fails_when=a failed fetch counts as 'no _kal value' and plans rule-3 deletions of every row;
    no_claim=RIVM availability"""
    rows = [_row(1, 6, 17.77), _row(2, 7, 1.0)]
    plan = _build(rows, FakeRivm(failing={KAL25}))
    assert plan.skip == fix.SKIP_RIVM_ERROR and plan.actions == 0 and "503" in plan.detail
    assert _build(rows, FakeRivm(failing={8272})).skip == fix.SKIP_RIVM_ERROR
    assert _build(rows, FakeRivm({})).skip == fix.SKIP_NO_THING


def test_non_numeric_rows_skip_datastream() -> None:
    """protects=the rules compare numbers; a datastream with a string/NULL result is skipped;
    fails_when=a non-numeric row is updated with RESULT_TYPE left as it is; no_claim=such rows exist in production"""
    assert _build([_row(1, 6, None, rtype=1, text="n/a")], FakeRivm()).skip == fix.SKIP_NON_NUMERIC


def test_kal_window_covers_first_to_last_hour() -> None:
    """protects=RIVM is asked for [first hour, last hour + 1 h) of our rows, once per datastream;
    fails_when=the window misses the last hour (its rows would be deleted under rule 3);
    no_claim=RIVM's own filter semantics (ge/lt on phenomenonTime)"""
    api = FakeRivm()
    _build([_row(1, 9, 1.0), _row(2, 6, 17.234945), fix.Row(3, DS25, _t(12, 59), 0, 1.0, "1.0", "sm-8272")], api)
    assert api.calls == [(KAL25, _t(6), _t(13))]


# ---------------------------------------------------------------------------
# The RIVM client
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code, self.body = status, body

    def json(self) -> Any:
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeSession:
    def __init__(self, answers: list[FakeResponse | Exception]) -> None:
        self.answers = answers
        self.requests: list[tuple[str, dict[str, str] | None]] = []

    def get(self, url: str, params: dict[str, str] | None = None, **_: Any) -> FakeResponse:
        self.requests.append((url, params))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _client(session: FakeSession, sleeps: list[float], retries: int = 3) -> fix.RivmClient:
    return fix.RivmClient("https://rivm.test/v1.0/", timeout=5, retries=retries, session=session,  # type: ignore[arg-type]
                          sleep=sleeps.append)


def test_client_follows_next_link() -> None:
    """protects=pagination: every page of @iot.nextLink is read, the first request carries $filter/$select/$top;
    fails_when=only the first 1000 observations are used (the rest would be deleted under rule 3);
    no_claim=RIVM's page size"""
    session = FakeSession([
        FakeResponse(200, {"value": [{"phenomenonTime": "2026-11-25T07:00:00.000Z", "result": 17.819597}],
                           "@iot.nextLink": "https://rivm.test/v1.0/Datastreams(43002)/Observations?$skip=1"}),
        FakeResponse(200, {"value": [{"phenomenonTime": "2026-11-25T06:00:00.000Z", "result": 17.234945},
                                     {"phenomenonTime": "2026-11-25T05:00:00.000Z", "result": None}]}),
    ])
    out = _client(session, []).observations(KAL25, _t(6), _t(8))
    assert out == [(_t(7), 17.819597), (_t(6), 17.234945)]
    (url, params), (next_url, next_params) = session.requests
    assert url == "https://rivm.test/v1.0/Datastreams(43002)/Observations"
    assert params == {"$filter": "phenomenonTime ge 2026-11-25T06:00:00Z and phenomenonTime lt 2026-11-25T08:00:00Z",
                      "$select": "phenomenonTime,result", "$top": "1000"}
    assert next_url.endswith("$skip=1") and next_params is None


def test_client_retries_with_backoff_then_gives_up() -> None:
    """protects=bounded retries with exponential backoff on timeouts and 5xx; a 404 is not retried;
    fails_when=a transient error skips the datastream at once, or a dead API is retried forever;
    no_claim=the right retry count for production (--rivm-retries)"""
    sleeps: list[float] = []
    session = FakeSession([requests.ConnectTimeout("t"), FakeResponse(503, {}),
                           FakeResponse(200, {"@iot.id": 8272, "name": NAME, "Datastreams": [
                               {"@iot.id": KAL25, "name": f"{NAME}-12-pm25_kal"}]})])
    thing = _client(session, sleeps).thing(8272)
    assert thing == fix.RivmThing(8272, NAME, ((KAL25, f"{NAME}-12-pm25_kal"),))
    assert sleeps == [2.0, 4.0]
    assert session.requests[0][1] == {"$select": "id,name", "$expand": "Datastreams($select=id,name)"}
    sleeps.clear()
    with pytest.raises(fix.RivmError, match="after 3 attempts"):
        _client(FakeSession([FakeResponse(500, {})] * 3), sleeps, retries=2).observations(KAL25, _t(6), _t(7))
    assert sleeps == [2.0, 4.0]
    with pytest.raises(fix.RivmNotFound):
        _client(FakeSession([FakeResponse(404, {})]), sleeps).thing(1)


# ---------------------------------------------------------------------------
# Dry run, apply, idempotence, rollback CSV, revert
# ---------------------------------------------------------------------------


def test_dry_run_reports_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """protects=AC1 (dry run prints processed/skipped and keep/update/delete by reason) and read-only by default;
    fails_when=a write session is opened, a file written, or a count is wrong;
    no_claim=the exact layout of the report"""
    db = FakeDb(_db_rows(), [_sm(DS25), _sm(DS10, "pm10"), _sm(DS_OTHER, thing="GEO_SamenMeten OTHER")])
    db.rows[20] = {"ds": DS_OTHER, "hour": 6, "num": 1.0, "str": "1.0", "sid": "sm-9999"}
    assert _main(db, FakeRivm()) == fix.EXIT_OK
    out = capsys.readouterr().out
    assert db.writes() == [] and list(tmp_path.iterdir()) == []
    assert all(s.startswith(cleanup.READ_ONLY_SESSION) for s in db.sessions)
    assert re.search(r"processed +2\n", out) and re.search(r"  skipped +1\n", out)
    assert re.search(re.escape(fix.SKIP_NO_THING) + r" +1\n", out)
    assert re.search(r"to keep unchanged \(equal to _kal\) +2\n", out)
    assert re.search(r"to update to _kal \(rule 2\) +1\n", out)
    assert re.search(r"to delete +5\n", out)
    assert re.search(r"\(rule 1\) +3\n", out) and re.search(r"\(rule 2\) +1\n", out) and re.search(r"\(rule 3\) +1\n", out)
    assert re.search(rf"\n +{DS25} pm2_5 +8272 +{KAL25} +6 +3 +1 +1 +2 +1 +1 +0 +0  ok\n", out)


def test_apply_archives_records_then_writes_and_is_idempotent(tmp_path: Path) -> None:
    """protects=AC2 (apply does exactly the plan; every deleted row archived and every updated row recorded first)
    and idempotence; rows above --max-id are never touched;
    fails_when=a DELETE precedes its archive line, an UPDATE its CSV line, the result differs from the plan, or a
    re-run plans anything; no_claim=PostgreSQL semantics of the statements (integration test)"""
    archive, rollback = tmp_path / "a.jsonl", tmp_path / "r.csv"
    db = FakeDb(_db_rows(), archive=archive, rollback=rollback)
    args = ("--apply", "--archive", str(archive), "--rollback-csv", str(rollback))
    assert _main(db, FakeRivm(), *args) == fix.EXIT_OK
    assert db.unarchived_at_delete == set() and db.unrecorded_at_update == set()
    assert sorted(db.rows) == [2, 4, 8, 99]
    assert (db.rows[4]["num"], db.rows[4]["str"]) == (19.5, "19.5")
    assert db.rows[99]["num"] == 50.0
    assert _archived_ids(archive) == {1, 3, 5, 6, 7}
    assert any("refreshed" in s for s in db.writes())
    sessions = len(db.sessions)
    lines = rollback.read_text(encoding="utf-8")
    assert _main(db, FakeRivm(), *args) == fix.EXIT_OK
    assert not [s for s in db.sessions[sessions:] if "WITH upd AS" in s or "WITH del AS" in s]
    assert rollback.read_text(encoding="utf-8") == lines
    assert sorted(db.rows) == [2, 4, 8, 99]


def test_rollback_csv_content(tmp_path: Path) -> None:
    """protects=the rollback CSV: header, one line per update with id,datastream,phenomenon_time,old,new and the
    exact old RESULT_STRING (JSON, null-safe); parse round trip;
    fails_when=a value loses precision, a NULL string comes back as '', or a cut-off line is kept;
    no_claim=reading by other tools than this script"""
    archive, rollback = tmp_path / "a.jsonl", tmp_path / "r.csv"
    rows = _db_rows()
    rows[4]["str"] = "20.000"
    db = FakeDb(rows, archive=archive, rollback=rollback)
    assert _main(db, FakeRivm(), "--apply", "--archive", str(archive), "--rollback-csv", str(rollback)) == fix.EXIT_OK
    assert rollback.read_text(encoding="utf-8") == (
        "id,datastream,phenomenon_time,old,new,old_string,new_string\n"
        f'4,{DS25},2026-11-25T08:00:00+00:00,20.0,19.5,"""20.000""",19.5\n')
    odd = fix.Update(5, DS25, _t(9), 0.1 + 0.2, 1e-07, None)
    assert fix.parse_update(next(csv.reader([fix.format_update(odd)]))) == odd
    with rollback.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("6,16094,2026-11-25T")
    fix.prepare_rollback(rollback)
    assert rollback.read_text(encoding="utf-8").endswith('19.5\n')
    bad = tmp_path / "bad.csv"
    bad.write_text("observation_id\n1\n", encoding="utf-8")
    with pytest.raises(fix.RollbackError):
        fix.prepare_rollback(bad)


def test_revert_restores_old_values_only_where_unchanged(tmp_path: Path) -> None:
    """protects=--revert puts RESULT_NUMBER/RESULT_STRING back from the CSV, only on rows still holding the update;
    fails_when=a dry run writes, a row changed since is overwritten, or verification passes with rows left;
    no_claim=restoring deleted rows (cleanup_frost_observations.py --restore, integration test)"""
    archive, rollback = tmp_path / "a.jsonl", tmp_path / "r.csv"
    rows = _db_rows()
    rows[9] = {"ds": DS10, "hour": 8, "num": 40.0, "str": "40", "sid": "sm-8272"}
    db = FakeDb(rows, archive=archive, rollback=rollback)
    api = FakeRivm(observations={KAL25: {6: 17.234945, 8: 19.5}, KAL10: {6: 28.4, 8: 41.0}})
    assert _main(db, api, "--apply", "--archive", str(archive), "--rollback-csv", str(rollback)) == fix.EXIT_OK
    assert db.rows[9]["num"] == 41.0
    db.rows[9]["num"], db.rows[9]["str"] = 42.0, "42.0"  # changed since
    revert = ["--container", "fake", "--rollback-csv", str(rollback), "--revert"]
    assert fix.main(revert, run_sql=db) == fix.EXIT_OK
    assert db.rows[4]["num"] == 19.5
    assert fix.main([*revert, "--apply"], run_sql=db) == fix.EXIT_OK
    assert (db.rows[4]["num"], db.rows[4]["str"]) == (20.0, "20.0")
    assert db.rows[9]["num"] == 42.0


def test_apply_needs_both_files_and_refuses_when_busy(tmp_path: Path) -> None:
    """protects=--apply requires --archive and --rollback-csv; no write next to another repair script's session;
    fails_when=apply runs without a way back, or writes while a cleanup/move/pressure write session is open;
    no_claim=detection of sessions with other application names"""
    with pytest.raises(SystemExit):
        fix.parse_args(["--container", "x", "--max-id", "5", "--apply", "--archive", "a.jsonl"])
    with pytest.raises(SystemExit):
        fix.parse_args(["--container", "x", "--apply", "--archive", "a", "--rollback-csv", "r"])
    archive, rollback = tmp_path / "a.jsonl", tmp_path / "r.csv"
    db = FakeDb(_db_rows(), busy=True)
    assert _main(db, FakeRivm(), "--apply", "--archive", str(archive), "--rollback-csv", str(rollback)) == fix.EXIT_REFUSED
    assert db.writes() == [] and len(db.rows) == 9


def test_datastreams_filter_limits_scope_and_rejects_others(capsys: pytest.CaptureFixture[str]) -> None:
    """protects=--datastreams canary: only the listed datastreams are read and planned; IDs outside the SM PM scope
    are refused; fails_when=the canary touches other datastreams, or a non-Samen-Meten id is accepted;
    no_claim=which datastream makes a good canary"""
    db = FakeDb(_db_rows())
    assert _main(db, FakeRivm(), "--datastreams", str(DS10)) == fix.EXIT_OK
    assert re.search(r"to delete +1\n", capsys.readouterr().out)
    assert all(f"'{{{DS25}}}'" not in s for s in db.sessions)
    assert _main(FakeDb(_db_rows()), FakeRivm(), "--datastreams", f"{DS10},777") == fix.EXIT_ERROR


def test_verify_samples_hours_against_rivm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """protects=AC3 tooling: --verify fails on hours with >1 row or a sampled hour differing from _kal, passes after
    a repair; fails_when=a leftover duplicate or a raw value passes verification; no_claim=sample representativeness"""
    assert _main(FakeDb(_db_rows()), FakeRivm(), "--verify", "--sample", "10") == fix.EXIT_VERIFY
    archive, rollback = tmp_path / "a.jsonl", tmp_path / "r.csv"
    db = FakeDb(_db_rows(), archive=archive, rollback=rollback)
    assert _main(db, FakeRivm(), "--apply", "--archive", str(archive), "--rollback-csv", str(rollback)) == fix.EXIT_OK
    capsys.readouterr()
    assert _main(db, FakeRivm(), "--verify", "--sample", "10") == fix.EXIT_OK
    assert re.search(r"equal to _kal +3\n", capsys.readouterr().out)
