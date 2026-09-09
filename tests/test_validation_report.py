"""Unit tests for the 14-day report generator.

Verifies:
  * The aggregator computes correct uptime, error, and breaker rollups from a
    seeded snapshot stream.
  * A simulated 2-hour target-down window (the smoke test's success condition)
    produces the expected uptime dip, breaker events, and DLQ backlog in the
    report — this is the automated version of the injected-failure smoke test.
  * The markdown renderer emits the seven required sections.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.generate_validation_report import (
    _load_jsonl,
    compute_aggregates,
    render_markdown,
)


def _ts(offset_min: int, base: datetime | None = None) -> str:
    base = base or datetime(2026, 9, 10, 0, 0, tzinfo=UTC)
    return (base + timedelta(minutes=offset_min)).isoformat().replace("+00:00", "Z")


def _snap(
    minute: int,
    *,
    sources: dict | None = None,
    push_stats: dict | None = None,
    breakers: dict | None = None,
    dlq_bytes: int = 0,
    dlq_lines: int = 0,
) -> dict[str, Any]:
    return {
        "t": _ts(minute),
        "uptime_seconds": 60.0 * minute,
        "window_seconds": 300,
        "sources": sources or {"kafka": {"stale": False, "age_seconds": 30}},
        "push_stats": push_stats or {},
        "circuit_breakers": breakers or {},
        "dlq": {"bytes": dlq_bytes, "lines": dlq_lines, "oldest_epoch": None},
        "frost_worker": {"enabled": False},
    }


class TestLoadJsonl:
    def test_returns_empty_for_missing(self, tmp_path: Path):
        assert _load_jsonl(tmp_path / "nope.jsonl") == []

    def test_reads_base_plus_rotated_backups(self, tmp_path: Path):
        base = tmp_path / "events.jsonl"
        base.write_text('{"a": 1}\n', encoding="utf-8")
        (tmp_path / "events.jsonl.1").write_text('{"a": 2}\n', encoding="utf-8")
        (tmp_path / "events.jsonl.2").write_text('{"a": 3}\n', encoding="utf-8")
        loaded = _load_jsonl(base)
        # Ordering: oldest first (.2, .1, base) — matches append-only history.
        assert [r["a"] for r in loaded] == [3, 2, 1]


class TestComputeAggregatesEmpty:
    def test_empty_snapshots_returns_warning(self):
        m = compute_aggregates([], [], [], None, None, None)
        assert "warning" in m


class TestComputeAggregatesBasic:
    def test_single_clean_snapshot_reports_100pct_uptime(self):
        snap = _snap(0, push_stats={
            "kafka::https://sta.example": {
                "attempts": 5, "sent": 5, "failed": 0, "dropped_permanent": 0, "dropped_circuit": 0,
                "errors_by_class": {}, "latency_ms": {"count": 5, "min": 50, "max": 90, "p50": 70, "p95": 88},
                "age_at_push_s": {"count": 5, "min": 10, "max": 20, "p50": 15, "p95": 19},
            },
        })
        m = compute_aggregates([snap], [], [], None, None, None)
        assert m["sources"]["kafka"]["uptime_pct"] == 100.0
        path = next(p for p in m["paths"] if p["source"] == "kafka")
        assert path["attempts"] == 5 and path["sent"] == 5
        assert path["error_rate_pct"] == 0.0

    def test_error_rate_computed_across_snapshots(self):
        snaps = [
            _snap(0, push_stats={
                "kafka::https://x": {
                    "attempts": 10, "sent": 8, "failed": 2, "dropped_permanent": 0, "dropped_circuit": 0,
                    "errors_by_class": {"timeout": 2},
                    "latency_ms": {"count": 10, "p50": 80, "p95": 200, "max": 300, "min": 40},
                },
            }),
            _snap(5, push_stats={
                "kafka::https://x": {
                    "attempts": 10, "sent": 9, "failed": 1, "dropped_permanent": 0, "dropped_circuit": 0,
                    "errors_by_class": {"server_5xx": 1},
                    "latency_ms": {"count": 10, "p50": 90, "p95": 220, "max": 350, "min": 50},
                },
            }),
        ]
        m = compute_aggregates(snaps, [], [], None, None, None)
        path = m["paths"][0]
        assert path["attempts"] == 20
        assert path["sent"] == 17
        assert path["failed"] == 3
        assert path["errors_by_class"] == {"timeout": 2, "server_5xx": 1}
        assert path["error_rate_pct"] == pytest.approx(15.0, abs=0.001)


class TestSimulatedTargetDown:
    """Automated smoke-test analog: a target flatlines for the middle of a
    2-hour window; the harness should register uptime dip, circuit_open,
    and DLQ growth in the report."""

    @pytest.fixture()
    def scenario(self) -> list[dict]:
        snaps: list[dict] = []
        target = "https://smoke-fault.example"
        for m in range(0, 24 * 5, 5):
            healthy = m in {0, 5, 10, 115}  # only first three and last snapshot see health
            if healthy:
                cb_open = False
                sent = 5
                failed = 0
                errors: dict[str, int] = {}
                dlq_lines = 0
            else:
                cb_open = True
                sent = 0
                failed = 5
                errors = {"circuit_open": 1}
                dlq_lines = 100 + m
            snaps.append(_snap(
                m,
                sources={"kafka": {"stale": False, "age_seconds": 30}},
                push_stats={
                    f"kafka::{target}": {
                        "attempts": 5, "sent": sent, "failed": failed,
                        "dropped_permanent": 0, "dropped_circuit": 0,
                        "errors_by_class": errors,
                        "latency_ms": {"count": 5, "p50": 60, "p95": 90, "max": 120, "min": 40} if sent else {},
                    }
                },
                breakers={target: {"open": cb_open, "consecutive_failures": 3 if cb_open else 0}},
                dlq_lines=dlq_lines,
            ))
        return snaps

    def test_uptime_dip_reflected(self, scenario: list[dict]):
        m = compute_aggregates(scenario, [], [], None, None, None)
        tgt = m["targets"]["https://smoke-fault.example"]
        assert tgt["uptime_pct"] < 25.0
        assert tgt["breaker"]["open_events"] >= 1

    def test_dlq_peak_and_final_captured(self, scenario: list[dict]):
        m = compute_aggregates(scenario, [], [], None, None, None)
        assert m["dlq"]["peak_lines_in_snapshots"] >= 100

    def test_render_markdown_produces_seven_sections(self, scenario: list[dict]):
        m = compute_aggregates(scenario, [], [], None, None, None)
        md = render_markdown(m)
        for heading in [
            "## 1. Executive summary",
            "## 2. Uptime",
            "## 3. Latency per data path",
            "## 4. Error rate",
            "## 5. Circuit breaker and DLQ activity",
            "## 6. Incident log",
            "## 7. Lessons learned",
        ]:
            assert heading in md, f"Missing section: {heading}"


class TestBreakerTransitionsFromSnapshotDeltas:
    def test_open_close_pair_counted_and_measured(self):
        target = "https://x"
        snaps = [
            _snap(0, breakers={target: {"open": False}}),
            _snap(5, breakers={target: {"open": True}}),
            _snap(10, breakers={target: {"open": True}}),
            _snap(15, breakers={target: {"open": False}}),
        ]
        m = compute_aggregates(snaps, [], [], None, None, None)
        tgt = m["targets"][target]
        assert tgt["breaker"]["open_events"] == 1
        # 10 min of open (snapshots at minute 5 and 10) = 600 s.
        assert tgt["breaker"]["total_open_seconds"] == 600.0


class TestIncidentLogPassthrough:
    def test_incidents_included_and_counted(self):
        snap = _snap(0)
        incidents = [
            {"t": _ts(1), "kind": "alert.kafka.stall", "level": "critical", "context": {"message": "no data"}},
            {"t": _ts(2), "kind": "circuit_open", "level": "warning", "target": "https://x", "context": {}},
        ]
        m = compute_aggregates([snap], [], incidents, None, None, None)
        assert m["incidents"]["count"] == 2
        assert m["incidents"]["by_kind"] == {"alert.kafka.stall": 1, "circuit_open": 1}


class TestDlqSurvivingEntries:
    def test_dlq_lines_grouped_by_host(self):
        snap = _snap(0)
        dlq = [
            {"endpoint": "https://sta.wbd-rd.nl/v1.1/Observations", "datastream_id": "1"},
            {"endpoint": "https://sta.wbd-rd.nl/v1.1/Observations", "datastream_id": "2"},
            {"endpoint": "https://frost-v2-demo.example/Observations", "datastream_id": "3"},
        ]
        m = compute_aggregates([snap], [], [], dlq, None, None)
        assert m["dlq"]["surviving_entries_at_report_time"] == 3
        assert m["dlq"]["surviving_by_host"]["sta.wbd-rd.nl"] == 2
        assert m["dlq"]["surviving_by_host"]["frost-v2-demo.example"] == 1
