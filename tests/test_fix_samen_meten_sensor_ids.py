"""scripts/fix_samen_meten_sensor_ids.py (#45): the three rules, the skips, apply/verify/revert, RIVM paging.

A fake psql keeps an in-memory DATASTREAMS table (sensor_id, Thing, original_name, rows' sensor ids) and answers the
script's statements; RIVM is a dict of name -> Thing ids, or a fake requests session. No network, no database.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts import cleanup_frost_observations as cleanup
from scripts import fix_samen_meten_sensor_ids as fix

# RIVM Thing 12507 'LTD_94963' with two datastreams with rows, one without; Thing 12506 'LTD_94908' without any rows.
RIVM = {"LTD_94963": [12507], "LTD_94908": [12506], "TWIN": [1, 2]}


def _ds(ds_id: int, thing: int, sid: str | None, orig: str | None, rows: set[str | None]) -> dict[str, Any]:
    return {"id": ds_id, "thing": thing, "sid": sid, "orig": orig, "rows": set(rows)}


def _table() -> dict[int, dict[str, Any]]:
    return {d["id"]: d for d in (
        _ds(4231, 10, "sm-ltd_94963", "LTD_94963", {"sm-12507"}),   # rule 1
        _ds(4232, 10, "sm-ltd_94963", "LTD_94963", {"sm-12507"}),   # rule 1
        _ds(4240, 10, "sm-ltd_94963", "LTD_94963", set()),          # rule 2: a sibling has rows
        _ds(4233, 11, "sm-ltd_94908", "LTD_94908", set()),          # rule 3: RIVM by name
    )}


def _sm(ds_id: int, thing: int = 10, sid: str | None = "sm-x", orig: str | None = "LTD_94963",
        rows: frozenset[str | None] = frozenset()) -> fix.SmDatastream:
    return fix.SmDatastream(ds_id, thing, sid, orig, rows)


class FakeDb:
    """A fake psql over an in-memory DATASTREAMS table; checks the rollback CSV when an UPDATE arrives."""

    def __init__(self, table: dict[int, dict[str, Any]], *, rollback: Path | None = None, busy: bool = False) -> None:
        self.table = table
        self.rollback, self.busy = rollback, busy
        self.sessions: list[str] = []
        self.unrecorded_at_update: set[int] = set()

    def __call__(self, sql: str) -> list[str]:
        self.sessions.append(sql)
        out: list[str] = []
        if "'t', 'smid'" in sql:
            out += [json.dumps({"t": "smid", "id": d["id"], "thing": d["thing"], "sid": d["sid"], "orig": d["orig"],
                                "rows": sorted(d["rows"], key=str)}) for _, d in sorted(self.table.items())]
        if "pg_stat_activity" in sql and self.busy:
            out.append(f"busy|4242|{cleanup.APP_NAME}|active|00:00:03|end")
        for tag in ("updated", "reverted"):
            for chunk in sql.split("WITH chg AS (")[1:]:
                if f"SELECT '{tag}'" not in chunk:
                    continue
                ids, frm, to = self._arrays(chunk)
                if tag == "updated" and self.rollback is not None:
                    recorded = {int(r["id"]) for r in csv.DictReader(self.rollback.open(encoding="utf-8"))}
                    self.unrecorded_at_update |= set(ids) - recorded
                changed = 0
                for ds, old, new in zip(ids, frm, to, strict=True):
                    if ds in self.table and self.table[ds]["sid"] == old:
                        self.table[ds]["sid"] = new
                        changed += 1
                out.append(f"{tag}|{len(ids)}|{changed}|end")
        return out

    @staticmethod
    def _arrays(chunk: str) -> tuple[list[int], list[str | None], list[str | None]]:
        ids = [int(i) for i in re.search(r"unnest\('\{([0-9,]*)\}'::bigint\[\]", chunk).group(1).split(",") if i]  # type: ignore[union-attr]
        texts = [[None if v == "NULL" else v[1:-1].replace("''", "'") for v in body.split(",")] if body else []
                 for body in re.findall(r"ARRAY\[(.*?)\]::text\[\]", chunk)]
        return ids, texts[0], texts[1]

    @property
    def writes(self) -> list[str]:
        return [s for s in self.sessions if "pg_try_advisory_lock" in s]


def _main(db: FakeDb, *args: str, rivm: dict[str, list[int]] | None = None) -> int:
    return fix.main(["--container", "fake", *args], rivm_names=lambda: dict(RIVM if rivm is None else rivm), run_sql=db)


# ---------------------------------------------------------------------------
# Rules and skips
# ---------------------------------------------------------------------------


def test_rules_rows_then_thing_then_rivm() -> None:
    """protects=the id source per datastream: own rows, else its Thing's rows, else RIVM by original_name;
    fails_when=a datastream without rows gets no id although a sibling has rows, or RIVM overrides the rows;
    no_claim=which RIVM Things exist today."""
    db = FakeDb(_table())
    plans = {p.ds.id: p for p in fix.build_plans(fix.load_catalogue(db), RIVM)}

    assert {i: (p.new, p.rule) for i, p in plans.items()} == {
        4231: ("sm-12507", fix.RULE_ROWS), 4232: ("sm-12507", fix.RULE_ROWS),
        4240: ("sm-12507", fix.RULE_THING), 4233: ("sm-12506", fix.RULE_RIVM),
    }
    assert db.writes == []


@pytest.mark.parametrize(("ds", "rivm", "reason"), [
    (_sm(1, rows=frozenset({"sm-1", "sm-2"})), RIVM, fix.SKIP_SEVERAL),
    (_sm(1, rows=frozenset({"sm-ltd_94963"})), RIVM, fix.SKIP_FORM),
    (_sm(1, rows=frozenset({None})), RIVM, fix.SKIP_FORM),
    (_sm(1, rows=frozenset({"sm-999"})), RIVM, fix.SKIP_MISMATCH),
    (_sm(1, orig=None), RIVM, fix.SKIP_NO_NAME),
    (_sm(1, orig="GONE"), RIVM, fix.SKIP_NOT_AT_RIVM),
    (_sm(1, orig="TWIN"), RIVM, fix.SKIP_NAME_TWICE),
    (_sm(1), None, fix.SKIP_NO_RIVM),
])
def test_unclear_datastreams_are_skipped(ds: fix.SmDatastream, rivm: dict[str, list[int]] | None, reason: str) -> None:
    """protects=no guessed id: several ids, a non-numeric id, a RIVM conflict or an unknown name leave it as is;
    fails_when=any of these gets a new sensor_id;
    no_claim=how many such datastreams production has."""
    (plan,) = fix.build_plans({ds.id: ds}, rivm)

    assert (plan.new, plan.skip) == (None, reason)


def test_rows_decide_when_rivm_has_no_or_several_things_of_the_name() -> None:
    """protects=the cross-check only refuses a clear conflict: a renamed or ambiguous RIVM name does not block rows;
    fails_when=a datastream with clear rows is skipped because RIVM's name lookup is empty or ambiguous;
    no_claim=RIVM renames."""
    for orig in ("GONE", "TWIN"):
        (plan,) = fix.build_plans({1: _sm(1, orig=orig, rows=frozenset({"sm-77"}))}, RIVM)
        assert (plan.new, plan.rule) == ("sm-77", fix.RULE_ROWS)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def test_dry_run_reports_and_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """protects=dry run by default: read-only sessions only, no file;
    fails_when=a dry run opens a write session or creates the rollback CSV;
    no_claim=the report's wording."""
    db = FakeDb(_table())

    assert _main(db, "--rollback-csv", str(tmp_path / "rb.csv")) == fix.EXIT_OK

    assert db.writes == [] and not (tmp_path / "rb.csv").exists()
    out = capsys.readouterr().out
    assert "to change: 4" in out and "Dry run" in out
    assert {d["sid"] for d in db.table.values()} == {"sm-ltd_94963", "sm-ltd_94908"}


def test_canary_then_full_apply_records_first_verifies_and_is_idempotent(tmp_path: Path) -> None:
    """protects=--datastreams limits the writes; old ids are on disk before each UPDATE; a re-run changes nothing;
    fails_when=a canary touches other datastreams, an UPDATE precedes its rollback record, or verification fails;
    no_claim=batch sizes."""
    rollback = tmp_path / "rb.csv"
    db = FakeDb(_table(), rollback=rollback)

    assert _main(db, "--datastreams", "4231", "--apply", "--rollback-csv", str(rollback)) == fix.EXIT_OK
    assert {i: d["sid"] for i, d in db.table.items()} == {
        4231: "sm-12507", 4232: "sm-ltd_94963", 4240: "sm-ltd_94963", 4233: "sm-ltd_94908"}

    assert _main(db, "--apply", "--rollback-csv", str(rollback), "--batch", "2") == fix.EXIT_OK
    assert {i: d["sid"] for i, d in db.table.items()} == {
        4231: "sm-12507", 4232: "sm-12507", 4240: "sm-12507", 4233: "sm-12506"}
    assert db.unrecorded_at_update == set()
    with rollback.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [(r["id"], r["old"], r["new"]) for r in rows] == [
        ("4231", "sm-ltd_94963", "sm-12507"), ("4232", "sm-ltd_94963", "sm-12507"),
        ("4233", "sm-ltd_94908", "sm-12506"), ("4240", "sm-ltd_94963", "sm-12507")]

    writes = len(db.writes)
    assert _main(db, "--apply", "--rollback-csv", str(rollback)) == fix.EXIT_OK
    assert len(db.writes) == writes


def test_datastream_changed_since_the_scan_is_left_and_verification_fails(tmp_path: Path,
                                                                           capsys: pytest.CaptureFixture[str]) -> None:
    """protects=the UPDATE's old-value check: a datastream changed between scan and write is not overwritten;
    fails_when=the script overwrites it, or reports success although it does not hold the new id;
    no_claim=who changed it."""
    rollback = tmp_path / "rb.csv"
    db = FakeDb(_table(), rollback=rollback)
    real = db.__call__

    def racing(sql: str) -> list[str]:
        if "WITH chg AS (" in sql:
            db.table[4232]["sid"] = "sm-someone-else"
        return real(sql)

    status = fix.main(["--container", "fake", "--apply", "--rollback-csv", str(rollback)],
                      rivm_names=lambda: dict(RIVM), run_sql=racing)

    assert status == fix.EXIT_VERIFY
    assert db.table[4232]["sid"] == "sm-someone-else"
    assert "1 changed since the scan" in capsys.readouterr().out


def test_busy_server_refuses_to_write(tmp_path: Path) -> None:
    """protects=one writer at a time: a repair script's write session on the server stops --apply;
    fails_when=the script writes next to another repair script;
    no_claim=how the other session is ended."""
    db = FakeDb(_table(), busy=True)

    assert _main(db, "--apply", "--rollback-csv", str(tmp_path / "rb.csv")) == fix.EXIT_REFUSED
    assert db.writes == []


def test_revert_sets_back_only_what_this_script_wrote(tmp_path: Path) -> None:
    """protects=rollback: --revert restores the old ids, and leaves a datastream someone changed since;
    fails_when=revert overwrites a later change or misses a recorded datastream;
    no_claim=reverting a partial run's unsent records."""
    rollback = tmp_path / "rb.csv"
    db = FakeDb(_table(), rollback=rollback)
    assert _main(db, "--apply", "--rollback-csv", str(rollback)) == fix.EXIT_OK
    db.table[4240]["sid"] = "sm-changed-later"

    assert _main(db, "--revert", "--rollback-csv", str(rollback)) == fix.EXIT_OK  # dry run
    assert db.table[4231]["sid"] == "sm-12507"
    assert _main(db, "--revert", "--apply", "--rollback-csv", str(rollback)) == fix.EXIT_OK

    assert {i: d["sid"] for i, d in db.table.items()} == {
        4231: "sm-ltd_94963", 4232: "sm-ltd_94963", 4240: "sm-changed-later", 4233: "sm-ltd_94908"}


def test_unknown_canary_datastream_is_an_error(tmp_path: Path) -> None:
    """protects=a canary id outside the scope is refused before anything is written;
    fails_when=a typo in --datastreams silently runs on nothing (or on everything);
    no_claim=the message text."""
    db = FakeDb(_table())

    assert _main(db, "--datastreams", "99", "--apply", "--rollback-csv", str(tmp_path / "rb.csv")) == fix.EXIT_ERROR
    assert db.writes == []


def test_sql_sets_only_sensor_id_and_checks_the_old_value() -> None:
    """protects=the UPDATE's shape: jsonb_set of sensor_id only, guarded by the recorded old value, quoted literals;
    fails_when=the UPDATE replaces PROPERTIES, drops the old-value guard, or breaks on a quote in an id;
    no_claim=PostgreSQL executes it (the dry run on production reads with the same session settings)."""
    plan = fix.Plan(_sm(4231, sid="sm-o'brien"), "sm-12507", fix.RULE_ROWS)

    sql = fix.update_sql([plan])

    assert "jsonb_set(coalesce(d.\"PROPERTIES\", '{}'::jsonb), '{sensor_id}', to_jsonb(v.to_id))" in sql
    assert "d.\"PROPERTIES\"->>'sensor_id' IS NOT DISTINCT FROM v.from_id" in sql
    assert "ARRAY['sm-o''brien']::text[]" in sql and "'{4231}'::bigint[]" in sql
    assert "SELECT 'updated', 1, count(*), 'end' FROM chg;" in sql


# ---------------------------------------------------------------------------
# RIVM
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code, self.body = status, body

    def json(self) -> Any:
        return self.body


class FakeSession:
    def __init__(self, answers: list[FakeResponse]) -> None:
        self.answers = answers
        self.requests: list[tuple[str, dict[str, str] | None]] = []

    def get(self, url: str, params: dict[str, str] | None = None, **_: Any) -> FakeResponse:
        self.requests.append((url, params))
        return self.answers.pop(0)


def _page(things: list[dict[str, Any]], more: bool) -> FakeResponse:
    body: dict[str, Any] = {"value": things}
    if more:
        body["@iot.nextLink"] = "https://rivm.test/v1.0/Things?$skip=400"  # never followed: paging is by id
    return FakeResponse(200, body)


def test_rivm_names_pages_by_id_until_the_last_page() -> None:
    """protects=rule 3 sees every RIVM Thing: pages by 'id gt <last>' (RIVM's deep $skip pages time out),
    a repeated name kept as several ids;
    fails_when=only the first page is read, $skip links are followed, or a duplicate name overwrites the first id;
    no_claim=RIVM's page size."""
    session = FakeSession([
        _page([{"@iot.id": 1, "name": "TWIN"}, {"@iot.id": 12507, "name": "LTD_94963"}], more=True),
        _page([{"@iot.id": 12600, "name": "TWIN"}, {"@iot.id": 12601}], more=False),
    ])
    client = fix.RivmThings("https://rivm.test/v1.0", session=session, sleep=lambda _s: None)  # type: ignore[arg-type]

    assert client.names() == {"LTD_94963": [12507], "TWIN": [1, 12600]}
    assert [(url, params["$filter"]) for url, params in session.requests if params] == [
        ("https://rivm.test/v1.0/Things", "id gt 0"), ("https://rivm.test/v1.0/Things", "id gt 12507")]
    assert session.requests[0][1] == {"$filter": "id gt 0", "$orderby": "id asc", "$top": "400", "$select": "id,name"}


def test_rivm_names_stops_on_a_page_that_does_not_move_on() -> None:
    """protects=no endless loop when RIVM ignores the id filter and serves the same page again;
    fails_when=names() loops forever or returns a partial map as if complete;
    no_claim=RIVM ever does this."""
    same = [{"@iot.id": 5, "name": "A"}]
    session = FakeSession([_page(same, more=True), _page(same, more=True)])
    client = fix.RivmThings("https://rivm.test/v1.0", session=session, sleep=lambda _s: None)  # type: ignore[arg-type]

    with pytest.raises(fix.sm_kal.RivmError, match="does not move on"):
        client.names()
