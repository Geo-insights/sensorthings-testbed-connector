"""scripts/fix_knmi_rain_rate.py (#48): which rows move, the skips, apply/verify/revert, the rollback record.

A fake psql keeps in-memory KNMI Things (their solar_radiation and rain_rate datastreams) and OBSERVATIONS rows
(datastream, phenomenon time, PARAMETERS labels) and answers the script's statements. No database.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts import cleanup_frost_observations as cleanup
from scripts import fix_knmi_rain_rate as fix

MAX_ID = 100  # rows above it were written by the fixed connector (real solar radiation)


def _stations() -> list[dict[str, Any]]:
    return [
        {"thing": 3, "name": "GEO_KNMI KNMI-52.36-3.34", "solar": [18], "rain": [1100]},
        {"thing": 11, "name": "GEO_KNMI KNMI-52.14-4.44", "solar": [28], "rain": [1101]},
        {"thing": 14, "name": "GEO_KNMI KNMI-52.46-4.55", "solar": [38], "rain": []},           # not registered yet
        {"thing": 15, "name": "GEO_KNMI KNMI-52.10-5.18", "solar": [48, 49], "rain": [1103]},  # several solar
    ]


def _row(ds: int, minute: int, prop: str = "solar_radiation", unit: str = "W/m2", v: float = 0.0) -> dict[str, Any]:
    return {"ds": ds, "t": f"2026-11-20T10:{minute:02d}:00Z", "prop": prop, "unit": unit, "v": v}


def _rows() -> dict[int, dict[str, Any]]:
    return {
        1: _row(18, 0), 2: _row(18, 10), 3: _row(18, 20),   # to move
        4: _row(18, 30),                                     # twin: rain_rate 1100 holds 10:30
        5: _row(1100, 30, "rain_rate", "mm/h"),
        6: _row(18, 40, "pressure", "hPa"),                  # another label: left
        101: _row(18, 50),                                   # above --max-id: real solar radiation, stays
        7: _row(28, 0), 8: _row(28, 10),                     # to move
        11: _row(28, 20, v=0.4),                             # twin with another result: not in the twins file
        12: _row(1101, 20, "rain_rate", "mm/h", v=0.2),
        9: _row(38, 0),                                      # no rain_rate datastream: skipped
        10: _row(48, 0),                                     # several solar datastreams: skipped
    }


def _ints(text: str) -> list[int]:
    return [int(v) for v in text.split(",") if v]


class FakeDb:
    """A fake psql over in-memory KNMI Things and rows; checks the rollback CSV when a move arrives."""

    def __init__(self, *, rollback: Path | None = None, busy: bool = False, ignore_moves: bool = False) -> None:
        self.stations = _stations()
        self.rows = _rows()
        self.rollback, self.busy, self.ignore_moves = rollback, busy, ignore_moves
        self.sessions: list[str] = []
        self.refreshed: list[int] = []
        self.unrecorded_at_move: set[int] = set()

    def __call__(self, sql: str) -> list[str]:
        self.sessions.append(sql)
        out: list[str] = []
        if "'t', 'knmi'" in sql:
            out += [json.dumps({"t": "knmi", **s}) for s in self.stations]
        if "'t', 'counts'" in sql:
            out += self._counts(sql)
        if "SELECT 'row'" in sql:
            out += self._select(sql)
        if "SELECT 'moved'" in sql:
            out += self._move(sql)
        if "SELECT 'restored'" in sql:
            out += self._restore(sql)
        if "SELECT 'twin'" in sql:
            out += self._twins(sql)
        for ds in re.findall(r"SELECT 'refreshed', ([0-9]+)", sql):
            self.refreshed.append(int(ds))
            out.append(f"refreshed|{ds}|end")
        if "pg_stat_activity" in sql and self.busy:
            out.append(f"busy|4242|{cleanup.APP_NAME}|active|00:00:03|end")
        return out

    # -- the script's statements, interpreted ---------------------------------------------------------

    def _twin(self, row: dict[str, Any], rain: int | None) -> bool:
        return rain is not None and any(r["ds"] == rain and r["t"] == row["t"] for r in self.rows.values())

    def _counts(self, sql: str) -> list[str]:
        solar, rain = (_ints(a) for a in re.findall(r"'\{([0-9,]*)\}'::bigint\[\]", sql)[:2])
        max_id = int(re.search(r'o\."ID" <= ([0-9]+)', sql).group(1))  # type: ignore[union-attr]
        out = []
        for s, r in zip(solar, rain, strict=True):
            mine = {i: row for i, row in self.rows.items() if row["ds"] == s}
            below = {i: row for i, row in mine.items() if i <= max_id}
            labelled = {i: row for i, row in below.items() if row["prop"] == "solar_radiation"}
            out.append(json.dumps({"t": "counts", "ds": s,
                                   "move": sum(1 for row in labelled.values() if not self._twin(row, r)),
                                   "twin": sum(1 for row in labelled.values() if self._twin(row, r)),
                                   "after": len(mine) - len(below), "other": len(below) - len(labelled)}))
        return out

    def _twins(self, sql: str) -> list[str]:
        solar, rain = (_ints(a) for a in re.findall(r"'\{([0-9,]*)\}'::bigint\[\]", sql)[:2])
        max_id = int(re.search(r'o\."ID" <= ([0-9]+)', sql).group(1))  # type: ignore[union-attr]
        out = []
        for s, r in zip(solar, rain, strict=True):
            for i, row in sorted(self.rows.items()):
                if row["ds"] == s and i <= max_id and row["prop"] == "solar_radiation" and self._twin(row, r):
                    same = any(x["ds"] == r and x["t"] == row["t"] and x["v"] == row["v"] for x in self.rows.values())
                    out.append(f"twin|{i}|{s}|{int(same)}|end")
        return out

    def _select(self, sql: str) -> list[str]:
        solar, after, max_id = (int(v) for v in re.search(  # type: ignore[union-attr]
            r'o\."DATASTREAM_ID" = ([0-9]+) AND o\."ID" > ([0-9]+) AND o\."ID" <= ([0-9]+)', sql).groups())
        rain = int(re.search(r'x\."DATASTREAM_ID" = ([0-9]+)', sql).group(1))  # type: ignore[union-attr]
        limit = int(re.search(r"LIMIT ([0-9]+)", sql).group(1))  # type: ignore[union-attr]
        ids = sorted(i for i, row in self.rows.items()
                     if row["ds"] == solar and after < i <= max_id and row["prop"] == "solar_radiation"
                     and not self._twin(row, rain))
        return [f"row|{i}|end" for i in ids[:limit]]

    def _move(self, sql: str) -> list[str]:
        ids = _ints(re.search(r"ANY\('\{([0-9,]*)\}'::bigint\[\]\)", sql).group(1))  # type: ignore[union-attr]
        rain = int(re.search(r'SET "DATASTREAM_ID" = ([0-9]+)', sql).group(1))  # type: ignore[union-attr]
        solar, max_id = (int(v) for v in re.search(  # type: ignore[union-attr]
            r'o\."DATASTREAM_ID" = ([0-9]+) AND o\."ID" <= ([0-9]+)', sql).groups())
        if self.rollback is not None:
            recorded = {int(r["observation_id"]) for r in csv.DictReader(self.rollback.open(encoding="utf-8"))}
            self.unrecorded_at_move |= set(ids) - recorded
        moved = 0
        for i in ids:
            row = self.rows.get(i)
            if (self.ignore_moves or row is None or row["ds"] != solar or i > max_id
                    or row["prop"] != "solar_radiation" or self._twin(row, rain)):
                continue
            row.update(ds=rain, prop="rain_rate", unit="mm/h")
            moved += 1
        return [f"moved|{solar}|{len(ids)}|{moved}|end"]

    def _restore(self, sql: str) -> list[str]:
        ids = _ints(re.search(r"ANY\('\{([0-9,]*)\}'::bigint\[\]\)", sql).group(1))  # type: ignore[union-attr]
        frm = int(re.search(r'SET "DATASTREAM_ID" = ([0-9]+)', sql).group(1))  # type: ignore[union-attr]
        to = int(re.search(r'o\."DATASTREAM_ID" = ([0-9]+) AND', sql).group(1))  # type: ignore[union-attr]
        restored = 0
        for i in ids:
            row = self.rows.get(i)
            if row is not None and row["ds"] == to and row["prop"] == "rain_rate":
                row.update(ds=frm, prop="solar_radiation", unit="W/m2")
                restored += 1
        return [f"restored|{frm}|{len(ids)}|{restored}|end"]

    @property
    def writes(self) -> list[str]:
        return [s for s in self.sessions if "pg_try_advisory_lock" in s]

    def where(self, i: int) -> tuple[int, str, str]:
        row = self.rows[i]
        return row["ds"], row["prop"], row["unit"]


def _main(db: FakeDb, *args: str) -> int:
    return fix.main(["--container", "fake", "--max-id", str(MAX_ID), *args], run_sql=db)


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def test_dry_run_counts_and_skips_and_writes_nothing(capsys) -> None:
    """protects=the plan: rows at or below --max-id labelled solar_radiation move, unless rain_rate holds their time;
    fails_when=rows above --max-id (real qg), twins or other labels are planned, or a dry run writes;
    no_claim=what the rows' values are."""
    db = FakeDb()

    assert _main(db) == fix.EXIT_OK

    text = capsys.readouterr().out
    assert "rows to move to rain_rate: 5 (in 2 datastreams)" in text
    assert "twins (rain_rate already holds a row at that time), left: 2" in text
    assert "rows above --max-id (real solar radiation), staying: 1" in text
    assert "rows at or below --max-id with another label, left: 1" in text
    assert f"skipped, {fix.SKIP_NO_RAIN}: 1" in text
    assert f"skipped, {fix.SKIP_SEVERAL}: 1" in text
    assert db.writes == []


def test_build_plans_skips_things_without_one_target() -> None:
    """protects=a Thing needs exactly one solar_radiation and one rain_rate datastream;
    fails_when=rows are moved to an arbitrary one of several datastreams;
    no_claim=registration of the missing rain_rate datastream."""
    plans = {p.station.thing: p.skip for p in fix.build_plans(fix.load_stations(FakeDb()))}

    assert plans == {3: "", 11: "", 14: fix.SKIP_NO_RAIN, 15: fix.SKIP_SEVERAL}


# ---------------------------------------------------------------------------
# Apply, verify, revert
# ---------------------------------------------------------------------------


def test_apply_moves_and_relabels_records_first_and_refreshes(tmp_path: Path) -> None:
    """protects=the moved rows go to their station's rain_rate datastream as rain_rate / mm/h, recorded before the move,
    and both datastreams' times are recomputed; the rest stays;
    fails_when=a row moves unrecorded, keeps its solar labels, a real qg row or a twin moves, or no refresh follows;
    no_claim=FROST's own triggers."""
    rollback = tmp_path / "knmi.csv"
    db = FakeDb(rollback=rollback)

    assert _main(db, "--apply", "--rollback-csv", str(rollback), "--batch", "2") == fix.EXIT_OK

    assert {i: db.where(i) for i in (1, 2, 3, 7, 8)} == {
        1: (1100, "rain_rate", "mm/h"), 2: (1100, "rain_rate", "mm/h"), 3: (1100, "rain_rate", "mm/h"),
        7: (1101, "rain_rate", "mm/h"), 8: (1101, "rain_rate", "mm/h"),
    }
    assert db.where(4) == (18, "solar_radiation", "W/m2")  # twin
    assert db.where(101) == (18, "solar_radiation", "W/m2")  # above --max-id
    assert db.where(6) == (18, "pressure", "hPa")
    assert db.where(9) == (38, "solar_radiation", "W/m2") and db.where(10) == (48, "solar_radiation", "W/m2")
    assert db.unrecorded_at_move == set()
    with rollback.open(encoding="utf-8") as f:
        assert list(csv.reader(f)) == [fix.ROLLBACK_HEADER, ["1", "18", "1100"], ["2", "18", "1100"],
                                       ["3", "18", "1100"], ["7", "28", "1101"], ["8", "28", "1101"]]
    assert set(db.refreshed) == {18, 1100, 28, 1101}


def test_a_second_run_moves_nothing_and_refreshes_the_recorded_datastreams(tmp_path: Path, capsys) -> None:
    """protects=re-running with the same file is safe: nothing left to move, and an interrupted run's datastreams
    get their times recomputed at the start;
    fails_when=a re-run moves rows again or the file is overwritten;
    no_claim=a run killed between a move and its record (the record is written first)."""
    rollback = tmp_path / "knmi.csv"
    db = FakeDb(rollback=rollback)
    assert _main(db, "--apply", "--rollback-csv", str(rollback)) == fix.EXIT_OK
    lines = rollback.read_text(encoding="utf-8")
    db.refreshed.clear()

    assert _main(db, "--apply", "--rollback-csv", str(rollback)) == fix.EXIT_OK

    assert "Nothing to move." in capsys.readouterr().out
    assert rollback.read_text(encoding="utf-8") == lines
    assert set(db.refreshed) == {18, 1100, 28, 1101}


def test_canary_moves_only_its_datastreams(tmp_path: Path) -> None:
    """protects=--datastreams limits the writes to those solar_radiation datastreams;
    fails_when=a canary run moves rows of other stations;
    no_claim=the choice of canary."""
    rollback = tmp_path / "knmi.csv"
    db = FakeDb(rollback=rollback)

    assert _main(db, "--datastreams", "28", "--apply", "--rollback-csv", str(rollback)) == fix.EXIT_OK

    assert db.where(7) == (1101, "rain_rate", "mm/h")
    assert db.where(1) == (18, "solar_radiation", "W/m2")


def test_an_unknown_canary_datastream_is_an_error(tmp_path: Path) -> None:
    """protects=a typo in --datastreams stops the run before anything is written;
    fails_when=the run goes on with no datastreams (or all);
    no_claim=the message wording."""
    db = FakeDb()

    assert _main(db, "--datastreams", "1100", "--apply", "--rollback-csv", str(tmp_path / "x.csv")) == fix.EXIT_ERROR
    assert db.writes == []


def test_refuses_while_a_repair_script_writes(tmp_path: Path) -> None:
    """protects=no write while another repair script's write session is on the server;
    fails_when=the move starts anyway;
    no_claim=how long the other session runs."""
    db = FakeDb(busy=True)

    assert _main(db, "--apply", "--rollback-csv", str(tmp_path / "knmi.csv")) == fix.EXIT_REFUSED
    assert db.writes == []


def test_verification_fails_when_rows_are_left(tmp_path: Path) -> None:
    """protects=after --apply the rows are counted again; rows still to move fail the run (exit 4);
    fails_when=the run reports success without checking;
    no_claim=why rows were left."""
    db = FakeDb(rollback=tmp_path / "knmi.csv", ignore_moves=True)

    assert _main(db, "--apply", "--rollback-csv", str(tmp_path / "knmi.csv")) == fix.EXIT_VERIFY


def test_revert_moves_the_recorded_rows_back_with_their_labels(tmp_path: Path) -> None:
    """protects=--revert restores datastream and labels of every recorded row still where the script put it;
    fails_when=rows go back with rain_rate labels, or rows moved elsewhere since are pulled back;
    no_claim=rows the connector wrote to rain_rate after the move (they are not in the file)."""
    rollback = tmp_path / "knmi.csv"
    db = FakeDb(rollback=rollback)
    assert _main(db, "--apply", "--rollback-csv", str(rollback)) == fix.EXIT_OK
    db.rows[8]["ds"] = 999  # moved elsewhere since: left
    db.refreshed.clear()

    assert _main(db, "--revert", "--rollback-csv", str(rollback)) == fix.EXIT_OK
    assert db.where(1) == (1100, "rain_rate", "mm/h")  # a dry run by default

    assert _main(db, "--revert", "--rollback-csv", str(rollback), "--apply") == fix.EXIT_OK

    assert {i: db.where(i) for i in (1, 2, 3, 7)} == {
        1: (18, "solar_radiation", "W/m2"), 2: (18, "solar_radiation", "W/m2"),
        3: (18, "solar_radiation", "W/m2"), 7: (28, "solar_radiation", "W/m2"),
    }
    assert db.where(8) == (999, "rain_rate", "mm/h")
    assert set(db.refreshed) == {18, 1100, 28, 1101}


# ---------------------------------------------------------------------------
# SQL and arguments
# ---------------------------------------------------------------------------


def test_the_move_is_guarded_in_sql() -> None:
    """protects=the UPDATE itself re-checks datastream, --max-id, label and twin, and relabels to the canonical unit;
    fails_when=a guard is only checked when selecting (a row changed in between would be moved);
    no_claim=the database's plan for it."""
    plan = fix.Plan(fix.Station(3, "x", (18,), (1100,)))

    sql = fix.move_sql(plan, [1, 2], 48614867)

    assert 'o."DATASTREAM_ID" = 18 AND o."ID" <= 48614867' in sql
    assert "o.\"PARAMETERS\"->>'observed_property' = 'solar_radiation'" in sql
    assert 'NOT EXISTS (SELECT 1 FROM "OBSERVATIONS" x WHERE x."DATASTREAM_ID" = 1100' in sql
    assert "jsonb_build_object('observed_property', 'rain_rate', 'unit', 'mm/h')" in sql


@pytest.mark.parametrize("argv", [
    ["--container", "c"],                                        # no --max-id
    ["--container", "c", "--max-id", "5", "--apply"],            # --apply without --rollback-csv
    ["--max-id", "5"],                                           # no target
    ["--container", "c", "--max-id", "0"],
])
def test_bad_arguments_are_refused(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        fix.parse_args(argv)


def test_twins_file_lists_the_twins_with_the_same_result(tmp_path: Path, capsys) -> None:
    """protects=--twins-file lists, for cleanup_frost_observations.py --ids-file, the twins whose rain_rate copy holds
    the same result, and only those; a dry run writes nothing to the database;
    fails_when=a twin with another result is listed for deletion, or the file is not in --ids-file's format;
    no_claim=the deletion itself (cleanup_frost_observations.py archives first)."""
    twins = tmp_path / "twins.csv"
    db = FakeDb()

    assert _main(db, "--twins-file", str(twins)) == fix.EXIT_OK

    assert twins.read_text(encoding="utf-8") == "observation_id,datastream_id\n4,18\n"
    assert cleanup.read_ids_file(twins) == [(4, 18)]
    assert "1 rows whose rain_rate copy has the same result" in capsys.readouterr().out
    assert db.writes == []
