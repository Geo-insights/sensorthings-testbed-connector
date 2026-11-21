"""scripts/cleanup_frost_observations.py: selections, the archive-first delete, restore, and their safety rails.

Unit tests drive the script against a fake psql that records every session; the integration tests
(tests/integration/test_cleanup_frost_observations.py, python -m pytest -m integration) run it end to
end, through docker exec, on a throwaway FROST-Server 2.8 + PostGIS 16.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from scripts import cleanup_frost_observations as cleanup

COLUMNS = ["PHENOMENON_TIME_START", "PHENOMENON_TIME_END", "RESULT_TIME", "RESULT_TYPE", "RESULT_NUMBER",
           "RESULT_BOOLEAN", "RESULT_JSON", "RESULT_STRING", "RESULT_QUALITY", "VALID_TIME_START", "VALID_TIME_END",
           "PARAMETERS", "DATASTREAM_ID", "FEATURE_ID", "ID"]
SC_DS, KNMI_DS, TGV_DS = 10, 20, 30


def _row(obs_id: int, ds: int, sensor_id: str = "tgv-officelab-climate", feature: int = 7, value: float = 45.2) -> str:
    """An observation as row_to_json prints it on FROST 2.8 (key order, spacing of the jsonb columns)."""
    params = {"unit": "%", "location": "tgv", "sensor_id": sensor_id, "thing_name": "TGV Office Lab | lab",
              "observed_property": "humidity", "sensor_name": "Office Lab °C \"sensor\""}
    return (
        f'{{"PHENOMENON_TIME_START":"2026-11-12T14:17:27.788+00:00","PHENOMENON_TIME_END":"2026-11-12T14:17:27.788+00:00",'
        f'"RESULT_TIME":null,"RESULT_TYPE":0,"RESULT_NUMBER":{value},"RESULT_BOOLEAN":null,"RESULT_JSON":null,'
        f'"RESULT_STRING":"{value}","RESULT_QUALITY":["good"],"VALID_TIME_START":null,"VALID_TIME_END":null,'
        f'"PARAMETERS":{json.dumps(params, ensure_ascii=False)},"DATASTREAM_ID":{ds},"FEATURE_ID":{feature},"ID":{obs_id}}}'
    )


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8"), usedforsecurity=False).hexdigest()


def _cand_line(obs_id: int, ds: int, target: int | None = None, **kwargs: Any) -> str:
    row = _row(obs_id, ds, **kwargs)
    return f"cand|{ds}|{obs_id}|{_md5(row)}|{'' if target is None else target}|{row}"


def _json(**fields: Any) -> str:
    return json.dumps(fields)


def _archived_ids(archive: Path) -> set[int]:
    return {cleanup.parse_row_line(line).id for line in archive.read_text(encoding="utf-8").splitlines()
            if line.startswith('{"sel":')}


class FakeDb:
    """Answers the script's statements for a --tgv-misplaced run and records every session.

    rows: the TGV rows to delete, observation ID -> datastream. Each DELETE session is checked against
    the archive at the moment it arrives.
    """

    def __init__(self, rows: dict[int, int], archive: Path | None = None, *, busy: bool = False,
                 foreign_keys: tuple[str, ...] = (), columns: list[str] = COLUMNS, remaining: int = 0,
                 fail_delete: bool = False) -> None:
        self.rows = dict(rows)
        self.archive = archive
        self.busy = busy
        self.foreign_keys = foreign_keys
        self.columns = columns
        self.remaining = remaining
        self.fail_delete = fail_delete
        self.sessions: list[str] = []
        self.deleted: set[int] = set()
        self.unarchived_at_delete: list[set[int]] = []
        self.scans = 0
        self.inserted: list[int] = []

    def __call__(self, sql: str) -> list[str]:
        self.sessions.append(sql)
        if "'t', 'bounds'" in sql:
            return [
                _json(t="ds", id=SC_DS, name="GEO_SensorCommunity 1 sensor - humidity", thing="GEO_SensorCommunity 1",
                      source="sensor_community", sensor_id="sensor_community-1", protected=False),
                _json(t="ds", id=KNMI_DS, name="GEO_KNMI 06344 sensor - relative_humidity", thing="GEO_KNMI 06344",
                      source="knmi_weather", sensor_id="knmi-06344", protected=False),
                _json(t="ds", id=TGV_DS, name="GEO_TGV Office Lab sensor - humidity", thing="GEO_TGV Office Lab",
                      source="kafka", sensor_id="tgv-officelab-climate", protected=True),
                _json(t="bounds", min=1, max=1000),
                *(_json(t="fk", name=fk) for fk in self.foreign_keys),
                *(_json(t="col", name=c) for c in self.columns),
            ]
        if "FROM pg_stat_activity" in sql:
            return ["busy|4242|fix_sc_pressure_units|active|00:00:12|end"] if self.busy else []
        if "'t', 'tgv'" in sql:
            self.scans += 1
            left = {i: ds for i, ds in self.rows.items() if i not in self.deleted}
            if self.scans > 1 and self.remaining:  # the verification scan finds rows that arrived meanwhile
                left = dict(list(self.rows.items())[: self.remaining])
            lines = [_json(t="tgv", w=1, ds=ds, sid="tgv-officelab-climate", prop="humidity", foi=7, **{"class": "delete"},
                           n=sum(1 for d in left.values() if d == ds), min_id=min(i for i, d in left.items() if d == ds),
                           max_id=max(i for i, d in left.items() if d == ds), first="2026-11-12T14:17:27.788+00:00",
                           last="2026-11-12T14:17:27.788+00:00", bytes=600) for ds in sorted(set(left.values()))]
            lines.append(_json(t="tgv", w=1, ds=TGV_DS, sid="tgv-officelab-climate", prop="humidity", foi=8,
                               **{"class": "protected"}, n=3, min_id=900, max_id=902, first="", last="", bytes=600))
            lines.append(_json(t="tgv", w=1, ds=KNMI_DS, sid="tgv-levellog-abcd1234", prop="water_level", foi=9,
                               **{"class": "other"}, n=2, min_id=910, max_id=911, first="", last="", bytes=400))
            return [*lines, _json(t="pages", w=1, pages=len(left))]
        if "'t', 'foi'" in sql:
            return [_json(t="foi", foi=7, n=len(self.rows), total=len(self.rows) + 5, generated=True)]
        if "SELECT 'cand'" in sql:
            after = int(re.search(r'o\."ID" > (\d+)', sql)[1])  # type: ignore[index]
            limit = int(re.search(r"LIMIT (\d+)", sql)[1])  # type: ignore[index]
            ids = sorted(i for i in self.rows if i > after and i not in self.deleted)[:limit]
            return [_cand_line(i, self.rows[i]) for i in ids]
        if "DELETE FROM" in sql:
            return self._delete(sql)
        if "SELECT 'refreshed'" in sql:
            return [f"refreshed|{ds}|end" for ds in re.findall(r"SELECT 'refreshed', (\d+)", sql)]
        return []

    def _delete(self, sql: str) -> list[str]:
        archived = _archived_ids(self.archive) if self.archive and self.archive.exists() else set()
        lines = []
        for ids, ds in re.findall(r"unnest\('\{([0-9,]+)\}'::bigint\[\].*?o\.\"DATASTREAM_ID\" = (\d+)", sql, re.S):
            listed = {int(i) for i in ids.split(",")}
            self.unarchived_at_delete.append(listed - archived)
            if self.fail_delete:
                raise cleanup.PsqlError("psql session did not finish within 4 s", lines)
            self.deleted |= listed
            lines.append(f"deleted|{ds}|{len(listed)}|{len(listed)}|end")
        return lines


def _args(*argv: str) -> Any:
    return cleanup.parse_args(["--container", "db", *argv])


ROWS = {101: SC_DS, 102: SC_DS, 103: KNMI_DS, 104: SC_DS, 105: KNMI_DS}


# ---------------------------------------------------------------------------
# Runs against the fake database
# ---------------------------------------------------------------------------


def test_dry_run_only_opens_read_only_sessions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = FakeDb(ROWS)
    archive = tmp_path / "tgv.jsonl"

    assert cleanup.run_cleanup(_args("--tgv-misplaced", "--archive", str(archive)), db) == 0

    assert db.sessions
    for sql in db.sessions:
        assert sql.startswith(cleanup.READ_ONLY_SESSION)
        assert not re.search(r"\b(DELETE|INSERT|UPDATE)\b", sql)
    assert not archive.exists()
    out = capsys.readouterr().out
    assert "to delete: 5 in 2 datastreams" in out
    assert "left alone, in datastreams of TGV Things: 3" in out
    assert "left alone, not a TGV Kafka label (a Levellog sensor, or location not 'tgv'): 2" in out
    assert "Dry run: nothing was written." in out


def test_apply_archives_every_page_before_deleting_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = tmp_path / "tgv.jsonl"
    db = FakeDb(ROWS, archive)

    code = cleanup.run_cleanup(_args("--tgv-misplaced", "--apply", "--archive", str(archive), "--page-rows", "2",
                                     "--batch-rows", "1"), db)

    assert code == 0
    assert db.deleted == set(ROWS)
    assert db.unarchived_at_delete and all(not ids for ids in db.unarchived_at_delete)  # archived first, always
    reads = [sql for sql in db.sessions if "SELECT 'cand'" in sql]
    assert len(reads) == 3  # pages of 2, 2 and 1 rows, continued by ID
    writes = [sql for sql in db.sessions if "DELETE FROM" in sql]
    assert len(writes) == 3
    for sql in writes:
        assert sql.startswith(cleanup.WRITE_SESSION)
        assert "pg_try_advisory_lock" in sql
        assert 'md5(row_to_json(o)::text) = v.h' in sql  # only rows still exactly as archived
        assert cleanup.protected_ds('o."DATASTREAM_ID"') in sql  # never in a TGV datastream
        assert cleanup.tgv_label("o") in sql and 'od."PROPERTIES"->>\'sensor_id\'' in sql  # still selected
    lines = archive.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"archive": "cleanup_frost_observations", "version": 1}
    assert json.loads(lines[1])["selection"] == "tgv-misplaced"
    rows = [cleanup.parse_row_line(line) for line in lines if line.startswith('{"sel":')]
    assert {r.id: r.row for r in rows} == {i: _row(i, ds) for i, ds in ROWS.items()}  # verbatim
    refreshed = [sql for sql in db.sessions if 'UPDATE "DATASTREAMS"' in sql]
    assert len(refreshed) == 1 and refreshed[0].startswith(cleanup.WRITE_SESSION)
    assert set(re.findall(r"SELECT 'refreshed', (\d+)", refreshed[0])) == {str(SC_DS), str(KNMI_DS)}
    assert json.loads(lines[-1])["datastreams"] == [SC_DS, KNMI_DS]
    out = capsys.readouterr().out
    assert "Deleted 5 observations (planned 5); 5 rows archived" in out
    assert "Verification: no rows left in the selection" in out


def test_refuses_to_write_next_to_another_repair_script(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = FakeDb(ROWS, tmp_path / "tgv.jsonl", busy=True)

    assert cleanup.run_cleanup(_args("--tgv-misplaced", "--apply", "--archive", str(tmp_path / "tgv.jsonl")), db) == 3

    assert not any("DELETE FROM" in sql for sql in db.sessions)
    assert "pid 4242: fix_sc_pressure_units" in capsys.readouterr().out
    assert {"cleanup_frost_observations", "fix_sc_pressure_units", "fix_sc_misrouted_observations", "fix_knmi_observations"} == set(cleanup.WRITERS)


def test_refuses_to_delete_when_a_foreign_key_references_observations(tmp_path: Path) -> None:
    db = FakeDb(ROWS, tmp_path / "tgv.jsonl", foreign_keys=('"OBS_RELATIONS".fk_obs',))

    assert cleanup.run_cleanup(_args("--tgv-misplaced", "--apply", "--archive", str(tmp_path / "tgv.jsonl")), db) == 3
    assert not any("DELETE FROM" in sql for sql in db.sessions)


def test_a_failed_delete_session_leaves_its_rows_in_the_archive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = tmp_path / "tgv.jsonl"
    db = FakeDb(ROWS, archive, fail_delete=True)

    with pytest.raises(cleanup.PsqlError, match="is already in"):
        cleanup.run_cleanup(_args("--tgv-misplaced", "--apply", "--archive", str(archive)), db)

    assert _archived_ids(archive) == set(ROWS)  # what the server may still delete is on disk
    assert not any('UPDATE "DATASTREAMS"' in sql for sql in db.sessions)
    assert cleanup.prepare_archive(archive) == {SC_DS, KNMI_DS}  # the next run refreshes them


def test_verification_reports_rows_still_selected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = tmp_path / "tgv.jsonl"
    db = FakeDb(ROWS, archive, remaining=2)

    assert cleanup.run_cleanup(_args("--tgv-misplaced", "--apply", "--archive", str(archive)), db) == 4
    assert "Verification: 2 rows are still selected" in capsys.readouterr().out


def test_ctrl_c_in_a_session_exits_130(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def interrupted(sql: str) -> list[str]:
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt as exc:
            raise cleanup.PsqlError("interrupted (Ctrl-C)") from exc

    monkeypatch.setattr(cleanup.pressure, "psql_runner", lambda argv, timeout: interrupted)

    assert cleanup.main(["--container", "db", "--duplicates"]) == 130
    assert "Interrupted." in capsys.readouterr().err


def test_unknown_datastream_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert cleanup.run_cleanup(_args("--tgv-misplaced", "--datastream", "999"), FakeDb(ROWS)) == 1
    assert "Unknown datastreams: [999]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Rows read in full, and the archive
# ---------------------------------------------------------------------------


def test_candidates_are_checked_against_their_md5_id_and_datastream() -> None:
    (cand,) = cleanup.parse_candidates([_cand_line(101, SC_DS, target=4)])
    assert (cand.ds, cand.id, cand.target, cand.feature, cand.row) == (SC_DS, 101, 4, 7, _row(101, SC_DS))

    row = _row(101, SC_DS)
    with pytest.raises(ValueError, match="does not match its md5"):
        cleanup.parse_candidates([f"cand|{SC_DS}|101|{_md5(row + ' ')}||{row}"])
    with pytest.raises(ValueError, match="is not observation 102"):
        cleanup.parse_candidates([f"cand|{SC_DS}|102|{_md5(row)}||{row}"])
    with pytest.raises(ValueError, match="unexpected psql output"):
        cleanup.parse_candidates(["cand|10|101"])


def test_archive_lines_keep_the_row_text_verbatim() -> None:
    row = _row(101, SC_DS)  # '|', a quote, a non-ASCII character
    cand = cleanup.Candidate(SC_DS, 101, _md5(row), 4, row, 7)

    line = cleanup.format_row_line("20261120T120000Z-abc123", cleanup.LEFTOVERS, cand)

    assert line.endswith("}\n") and "\n" not in line[:-1]
    parsed = cleanup.parse_row_line(line.rstrip("\n"))
    assert (parsed.sel, parsed.ds, parsed.id, parsed.md5, parsed.target, parsed.row) == (
        "misrouted-leftovers", SC_DS, 101, cand.md5, 4, row)
    assert parsed.columns == tuple(COLUMNS)
    assert json.loads(line)["row"]["ID"] == 101  # also plain JSON lines for other tools
    with pytest.raises(ValueError, match="md5"):
        cleanup.parse_row_line(line.rstrip("\n").replace('"RESULT_NUMBER":45.2', '"RESULT_NUMBER":45.3'))


def _write_archive(path: Path, *lines: str) -> None:
    path.write_text("".join(line + "\n" for line in (cleanup.ARCHIVE_HEADER, *lines)), encoding="utf-8")


def _line(obs_id: int, ds: int, run: str = "r1", **kwargs: Any) -> str:
    row = _row(obs_id, ds, **kwargs)
    return cleanup.format_row_line(run, cleanup.TGV, cleanup.Candidate(ds, obs_id, _md5(row), None, row, 7)).rstrip("\n")


def test_prepare_archive_creates_checks_and_finds_what_to_refresh(tmp_path: Path) -> None:
    new = tmp_path / "new.jsonl"
    assert cleanup.prepare_archive(new) == set()
    assert new.read_text(encoding="utf-8") == cleanup.ARCHIVE_HEADER + "\n"

    archive = tmp_path / "a.jsonl"
    _write_archive(archive, '{"run":"r1"}', _line(1, SC_DS), '{"refreshed":"r1","datastreams":[10]}',
                   '{"run":"r2"}', _line(2, KNMI_DS, run="r2"))
    with archive.open("a", encoding="utf-8", newline="\n") as out:
        out.write(_line(3, TGV_DS, run="r2")[:50])  # an append cut off by an interruption
    assert cleanup.prepare_archive(archive) == {KNMI_DS}  # rows after the last refresh
    assert archive.read_text(encoding="utf-8").endswith(_line(2, KNMI_DS, run="r2") + "\n")

    bad = tmp_path / "bad.jsonl"
    bad.write_text("datastream_id,observation_id\n", encoding="utf-8")
    with pytest.raises(cleanup.ArchiveError, match="not an archive"):
        cleanup.prepare_archive(bad)
    packed = tmp_path / "a.jsonl.gz"
    packed.write_bytes(gzip.compress(archive.read_bytes()))
    with pytest.raises(cleanup.ArchiveError, match="compressed"):
        cleanup.prepare_archive(packed)


def test_restore_uses_the_last_copy_of_each_row_and_reads_gzip(tmp_path: Path) -> None:
    archive = tmp_path / "a.jsonl"
    _write_archive(archive, '{"run":"r1"}', _line(1, SC_DS, value=1.0), _line(2, SC_DS),
                   '{"run":"r2"}', _line(1, SC_DS, run="r2", value=2.0), _line(3, KNMI_DS, run="r2"))
    packed = tmp_path / "a.jsonl.gz"
    packed.write_bytes(gzip.compress(archive.read_bytes()))

    for path in (archive, packed):
        idx = cleanup.index_archive(path)
        assert (idx.rows, idx.distinct, idx.copies, idx.runs) == (4, 3, 1, 2)
        batches = list(cleanup.restore_batches(path, idx, 5000, set()))
        assert [[(r.ds, r.id, r.row) for r in b] for b in batches] == [
            [(SC_DS, 2, _row(2, SC_DS)), (SC_DS, 1, _row(1, SC_DS, value=2.0))],  # the copy a DELETE can have matched
            [(KNMI_DS, 3, _row(3, KNMI_DS))],
        ]
        assert [[r.id for r in b] for b in cleanup.restore_batches(path, idx, 1, {KNMI_DS})] == [[3]]


def test_restore_sql_reinserts_exact_rows_without_overwriting() -> None:
    rows = [cleanup.parse_row_line(_line(1, SC_DS)), cleanup.parse_row_line(_line(2, SC_DS))]

    sql = cleanup.restore_sql(rows)

    assert 'json_populate_recordset(NULL::"OBSERVATIONS", ' in sql
    assert 'INSERT INTO "OBSERVATIONS" OVERRIDING SYSTEM VALUE' in sql
    assert "ON CONFLICT DO NOTHING" in sql
    assert f'v."DATASTREAM_ID" = {SC_DS}' in sql
    assert cleanup.protected_ds('v."DATASTREAM_ID"') in sql
    assert all(row.row.replace("'", "''") in sql for row in rows)  # the JSON text as archived


class RestoreDb:
    def __init__(self, columns: list[str] = COLUMNS) -> None:
        self.columns = columns
        self.sessions: list[str] = []
        self.present: set[int] = set()

    def __call__(self, sql: str) -> list[str]:
        self.sessions.append(sql)
        if "'t', 'bounds'" in sql:
            return [_json(t="col", name=c) for c in self.columns]
        if "FROM pg_stat_activity" in sql:
            return []
        lines = []
        for block in sql.split("SELECT 'rstate', ")[1:]:
            ds = int(block.split(",", 1)[0])
            ids = [int(i) for i in re.search(r"unnest\('\{([0-9,]+)\}'", block)[1].split(",")]  # type: ignore[index]
            present = len([i for i in ids if i in self.present])
            lines.append(f"rstate|{ds}|{len(ids)}|{present}|{present}|0|0|0|end")
        for block in sql.split("json_populate_recordset")[1:]:
            ids = [int(i) for i in re.findall(r'"ID":(\d+)\}', block)]
            ds = int(re.search(r'v\."DATASTREAM_ID" = (\d+)', block)[1])  # type: ignore[index]
            new = [i for i in ids if i not in self.present]
            self.present |= set(ids)
            lines.append(f"restored|{ds}|{len(ids)}|{len(new)}|end")
        lines += [f"refreshed|{ds}|end" for ds in re.findall(r"SELECT 'refreshed', (\d+)", sql)]
        return lines


def test_restore_dry_run_reads_only_then_apply_restores_and_verifies(tmp_path: Path,
                                                                     capsys: pytest.CaptureFixture[str]) -> None:
    archive = tmp_path / "a.jsonl"
    _write_archive(archive, '{"run":"r1"}', _line(1, SC_DS), _line(2, SC_DS), _line(3, KNMI_DS))
    db = RestoreDb()

    assert cleanup.run_restore(_args("--restore", "--archive", str(archive)), db) == 0
    assert all(sql.startswith(cleanup.READ_ONLY_SESSION) for sql in db.sessions)
    assert re.search(r"absent: to restore\s+3\n", capsys.readouterr().out)

    assert cleanup.run_restore(_args("--restore", "--archive", str(archive), "--apply"), db) == 0
    out = capsys.readouterr().out
    assert "Restored 3 observations (planned 3)." in out
    assert "every restorable row is back, byte for byte (3 identical" in out
    inserts = [sql for sql in db.sessions if "json_populate_recordset" in sql]
    assert inserts and all(sql.startswith(cleanup.WRITE_SESSION) for sql in inserts)
    assert any("SELECT 'refreshed', 10" in sql and "SELECT 'refreshed', 20" in sql for sql in db.sessions)


def test_restore_refuses_rows_with_other_columns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    archive = tmp_path / "a.jsonl"
    _write_archive(archive, _line(1, SC_DS))
    db = RestoreDb(columns=[*COLUMNS, "MULTI_DATASTREAM_ID"])

    assert cleanup.run_restore(_args("--restore", "--archive", str(archive), "--apply"), db) == 3
    assert not any("json_populate_recordset" in sql for sql in db.sessions)
    assert "Refusing to restore" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# SQL building blocks and the selections' plans
# ---------------------------------------------------------------------------


def test_same_reading_compares_every_result_column_and_the_label_null_safe() -> None:
    sql = cleanup.same_reading("k", "o")
    for column in ("PHENOMENON_TIME_END", "RESULT_TYPE", "RESULT_NUMBER", "RESULT_STRING", "RESULT_BOOLEAN",
                   "RESULT_JSON"):
        assert f'k."{column}" IS NOT DISTINCT FROM o."{column}"' in sql
        assert f'x."{column}"' in cleanup.reading_key("x")
    # sensor_name: co-located Sensor.Community sensors share a sensor_id (and a datastream); a missing
    # sensor_name on both sides is equal (IS NOT DISTINCT FROM), one on one side only is not.
    for key in ("sensor_id", "observed_property", "sensor_name"):
        assert f"""(k."PARAMETERS"->>'{key}') IS NOT DISTINCT FROM (o."PARAMETERS"->>'{key}')""" in sql
        assert f"""(x."PARAMETERS"->>'{key}')""" in cleanup.reading_key("x")
    assert " = " not in sql  # NULL-safe throughout
    assert cleanup.same_reading("k", "o", by_station=True) in cleanup.dup_recheck()
    assert cleanup.same_reading("x", "o") in cleanup._twin_in("v.target")


def test_same_reading_by_station_takes_a_knmi_rows_thing_name() -> None:
    sql = cleanup.same_reading("x", "o", by_station=True)
    assert f"{cleanup.station('x')} IS NOT DISTINCT FROM {cleanup.station('o')}" in sql
    assert """(x."PARAMETERS"->>'sensor_id') IS NOT DISTINCT FROM""" not in sql
    for key in ("observed_property", "sensor_name"):
        assert f"""(x."PARAMETERS"->>'{key}') IS NOT DISTINCT FROM (o."PARAMETERS"->>'{key}')""" in sql
    assert cleanup.station("o") == (
        """(CASE WHEN (o."PARAMETERS"->>'sensor_id') LIKE 'knmi-%' """
        """THEN coalesce('knmi:' || (o."PARAMETERS"->>'thing_name'), (o."PARAMETERS"->>'sensor_id')) """
        """ELSE (o."PARAMETERS"->>'sensor_id') END)"""
    )


def test_tgv_label_needs_location_tgv_and_excludes_levellog() -> None:
    sql = cleanup.tgv_label("o")
    assert """(o."PARAMETERS"->>'sensor_id') LIKE 'tgv-%'""" in sql
    assert """(o."PARAMETERS"->>'sensor_id') NOT LIKE 'tgv-levellog-%'""" in sql
    assert """coalesce((o."PARAMETERS"->>'location') = 'tgv', false)""" in sql
    assert sql in cleanup.tgv_read_sql('o."ID" <= 10', 0, 100)
    assert cleanup.TgvSelection.recheck(None).startswith(sql)  # type: ignore[arg-type]
    scan = cleanup.tgv_scan_sql('o."ID" <= 10', 1)
    assert f"WHEN NOT {sql} THEN 'other'" in scan  # every 'tgv-' label is scanned and counted, not only TGV ones
    assert scan.index("THEN 'protected'") < scan.index("THEN 'other'")


def test_tgv_things_are_protected_by_either_contract_property() -> None:
    sql = cleanup.protected_thing("t")
    assert """t."PROPERTIES"->>'site' = 'tgv'""" in sql and """t."PROPERTIES"->>'source' = 'kafka'""" in sql
    assert " OR " in sql and sql.startswith("coalesce(")


def test_duplicate_deletes_need_a_lower_twin_and_no_other_reading_at_that_time() -> None:
    recheck = cleanup.dup_recheck()
    same = cleanup.same_reading("k", "o", by_station=True)  # KNMI rows: their station, not their sensor_id
    assert 'k."ID" < o."ID"' in recheck and same in recheck
    assert "NOT EXISTS" in recheck and f"NOT ({same})" in recheck
    read = cleanup.dup_read_sql(1, 50, 0, 1000)
    assert '"DATASTREAM_ID" BETWEEN 1 AND 50' in read and "HAVING count(*) > 1" in read
    assert cleanup.DELETABLE_DUP in read and "row_number() OVER" in read
    assert cleanup.reading_key("x", by_station=True) in read and cleanup.station("x") in read
    assert cleanup.reading_key("x", by_station=True) in cleanup.dup_scan_sql(1, 50)


def test_leftovers_need_the_same_reading_in_their_target() -> None:
    assert cleanup._twin_in("v.target") == (
        'EXISTS (SELECT 1 FROM "OBSERVATIONS" x WHERE x."DATASTREAM_ID" = v.target '
        f'AND x."PHENOMENON_TIME_START" = o."PHENOMENON_TIME_START" AND {cleanup.same_reading("x", "o")})'
    )


def test_dup_chunks_bound_rows_and_never_span_a_tgv_datastream() -> None:
    counts = {1: 4, 2: 3, 3: 10, 5: 1, 6: 2, 7: 1, 9: 1}
    assert cleanup.dup_chunks(counts, {6}, 7) == [(1, 2), (3, 3), (5, 5), (7, 9)]
    assert cleanup.dup_chunks(counts, set(), 100) == [(1, 9)]
    assert cleanup.dup_chunks({4: 1, 8: 2}, set(), 100, contiguous=False) == [(4, 4), (8, 8)]


def test_merge_windows_joins_adjacent_windows_up_to_the_page_size() -> None:
    windows = [(1, 10, 3), (11, 20, 3), (21, 30, 3), (41, 50, 1), (51, 60, 9)]
    assert cleanup._merge_windows(windows, 6, 5) == [(1, 20), (21, 30), (41, 50), (51, 60)]
    assert cleanup._merge_windows(windows, 100, 2) == [(1, 20), (21, 30), (41, 60)]


def test_md5_array_takes_only_hex_digests() -> None:
    assert cleanup.md5_array(["0" * 32]) == "'{" + "0" * 32 + "}'::text[]"
    with pytest.raises(ValueError):
        cleanup.md5_array(["x'; DROP TABLE"])


# ---------------------------------------------------------------------------
# The IDs file and the command line
# ---------------------------------------------------------------------------


def test_read_ids_file_plain_and_csv(tmp_path: Path) -> None:
    plain = tmp_path / "ids.txt"
    plain.write_text("# KNMI cross-station rows\n12\n\n11\n12\n", encoding="utf-8")
    assert cleanup.read_ids_file(plain) == [(11, None), (12, None)]

    csv_file = tmp_path / "ids.csv"
    csv_file.write_text("﻿observation_id, datastream_id\n7,3\n5,\n", encoding="utf-8")
    assert cleanup.read_ids_file(csv_file) == [(5, None), (7, 3)]

    for text, error in (("id\n1\n1x\n", "not an observation"), ("1\nabc\n", "not an observation ID"),
                        ("observation_id,datastream_id\n1,2\n1,3\n", "different datastreams"),
                        ("datastream_id\n1\n", "no observation_id")):
        bad = tmp_path / "bad.csv"
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(cleanup.IdsFileError, match=error):
            cleanup.read_ids_file(bad)


def _ids_db(classes: dict[str, int]) -> Any:
    """A fake psql for an --ids-file dry run: the catalogue, and the scan's counts per class."""
    sessions: list[str] = []

    def run_sql(sql: str) -> list[str]:
        sessions.append(sql)
        if "'t', 'bounds'" in sql:
            return [_json(t="ds", id=KNMI_DS, name="GEO_KNMI 06344 sensor - relative_humidity", thing="GEO_KNMI 06344",
                          source="knmi_weather", sensor_id="knmi-06344", protected=False),
                    _json(t="bounds", min=1, max=1000), *(_json(t="col", name=c) for c in COLUMNS)]
        if "'t', 'ids'" in sql:
            return [_json(t="ids", ds=KNMI_DS, foi=7, n=n, bytes=600 * n, pages=1, **{"class": c})
                    for c, n in classes.items()]
        if "'t', 'foi'" in sql:
            return [_json(t="foi", foi=7, n=classes.get("delete", 0), total=100, generated=True)]
        return []

    run_sql.sessions = sessions  # type: ignore[attr-defined]
    return run_sql


def test_require_twin_rechecks_each_listed_row(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ids_file = tmp_path / "knmi_twins.csv"
    ids_file.write_text("observation_id,datastream_id\n101,20\n102,20\n103,20\n", encoding="utf-8")

    plain = cleanup.IdsSelection(None, _args("--ids-file", str(ids_file)))  # type: ignore[arg-type]
    assert plain.recheck() == "true" and plain.twin is None
    assert "'no-twin'" not in cleanup.ids_read_sql(plain.batches[0], set())

    args = _args("--ids-file", str(ids_file), "--require-twin", "--prefix", "GEO_")
    sel = cleanup.IdsSelection(None, args)  # type: ignore[arg-type]
    twin = cleanup.twin_sql("o", "GEO_")
    assert sel.recheck() == twin  # each DELETE checks again that the copy is still there
    for sql in (cleanup.ids_scan_sql(sel.batches[0], set(), twin), cleanup.ids_read_sql(sel.batches[0], set(), twin)):
        assert f"WHEN NOT {twin} THEN 'no-twin'" in sql
    delete = cleanup.delete_sql(20, [cleanup.Candidate(20, 101, "0" * 32, None, "{}", 7)], sel.recheck())
    assert twin in delete
    # the definition of --duplicates, a KNMI row's station by thing_name; own datastream (a lower ID: one copy of
    # rows listed together stays) or another datastream of its Thing or of the Thing its thing_name label names
    assert cleanup.same_reading("x", "o", by_station=True) in twin
    assert 'x."PHENOMENON_TIME_START" = o."PHENOMENON_TIME_START"' in twin
    assert '(x."DATASTREAM_ID" <> o."DATASTREAM_ID" OR x."ID" < o."ID")' in twin
    assert 'WHERE sd."ID" = o."DATASTREAM_ID"' in twin
    assert """tt."NAME" = 'GEO_' || (o."PARAMETERS"->>'thing_name')""" in twin

    db = _ids_db({"delete": 2, "no-twin": 1})
    assert cleanup.run_cleanup(args, db) == 0
    out = capsys.readouterr().out
    assert "to delete: 2" in out
    assert "left alone, no other row holds the same reading (--require-twin): 1" in out
    assert any(twin in sql for sql in db.sessions)


def test_command_line_rules(tmp_path: Path) -> None:
    for argv in (["--tgv-misplaced", "--apply"],  # no archive
                 ["--duplicates", "--require-twin"],  # only with --ids-file
                 ["--restore"],
                 ["--restore", "--archive", str(tmp_path / "missing.jsonl")],
                 ["--tgv-misplaced", "--duplicates"],
                 ["--ids-file", str(tmp_path / "missing.txt")],
                 ["--duplicates", "--batch-rows", "0"]):
        with pytest.raises(SystemExit):
            _args(*argv)
    with pytest.raises(SystemExit):
        cleanup.parse_args(["--duplicates"])  # no --ssh or --container
    args = _args("--duplicates", "--apply", "--archive", str(tmp_path / "d.jsonl"), "--datastream", "3",
                 "--datastream", "4")
    assert (cleanup.selection_name(args), args.datastream, args.batch_rows, args.page_rows) == (
        cleanup.DUPLICATES, [3, 4], 5000, 50_000)
