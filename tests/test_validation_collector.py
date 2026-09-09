"""Unit tests for the validation snapshot collector."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.validation import collector as collector_module
from app.services.validation.collector import (
    _default_freshness_thresholds,
    _dlq_stats,
    build_snapshot,
)
from app.services.validation.recorder import recorder


@pytest.fixture(autouse=True)
def _reset_singleton_recorder(tmp_path: Path):
    """Isolate the singleton recorder for each test.

    Tests that reach for the module-level ``recorder`` singleton must not leak
    state between runs — each test re-configures with its own tmp_path and
    clears in-memory aggregates before yielding.
    """
    recorder.configure(
        enabled=True,
        data_dir=tmp_path,
        events_max_bytes=1_000_000,
        incidents_max_bytes=1_000_000,
        sample_reservoir_size=100,
    )
    recorder.reset()
    yield
    recorder.configure(
        enabled=False,
        data_dir=tmp_path,
        events_max_bytes=1_000_000,
        incidents_max_bytes=1_000_000,
        sample_reservoir_size=100,
    )
    recorder.reset()


class TestDlqStats:
    def test_missing_file_returns_zeros(self, tmp_path: Path):
        stats = _dlq_stats(tmp_path / "does_not_exist.jsonl")
        assert stats == {"bytes": 0, "lines": 0, "oldest_epoch": None}

    def test_counts_lines_and_reads_oldest_timestamp(self, tmp_path: Path):
        p = tmp_path / "failed.jsonl"
        p.write_text(
            '{"timestamp": "2026-09-09T14:00:00Z", "datastream_id": "1"}\n'
            '{"timestamp": "2026-09-09T14:05:00Z", "datastream_id": "2"}\n',
            encoding="utf-8",
        )
        stats = _dlq_stats(p)
        assert stats["lines"] == 2
        assert stats["bytes"] > 0
        assert stats["oldest_epoch"] is not None


class TestFreshnessThresholds:
    def test_thresholds_include_all_sources(self):
        t = _default_freshness_thresholds()
        assert set(t.keys()) == {"kafka", "ohnics", "levellog"}

    def test_all_thresholds_are_at_least_5_minutes(self):
        # A source that hasn't delivered in <5 min shouldn't be flagged stale on
        # the report — that would produce false uptime dips.
        for k, v in _default_freshness_thresholds().items():
            assert v >= 300.0, f"{k} threshold {v} too small"


class TestBuildSnapshot:
    def test_returns_expected_top_level_keys(self, tmp_path: Path):
        snap = build_snapshot(
            uptime_seconds=42.0,
            window_seconds=300,
            dlq_path=tmp_path / "failed.jsonl",
            freshness_thresholds={"kafka": 300.0},
        )
        assert set(snap.keys()) >= {
            "t", "uptime_seconds", "window_seconds",
            "sources", "push_stats", "circuit_breakers", "dlq", "frost_worker",
        }
        assert snap["uptime_seconds"] == 42.0
        assert snap["window_seconds"] == 300

    def test_dlq_missing_reports_zeros(self, tmp_path: Path):
        snap = build_snapshot(
            uptime_seconds=1.0, window_seconds=300,
            dlq_path=tmp_path / "no_such.jsonl",
            freshness_thresholds={"kafka": 300.0},
        )
        assert snap["dlq"]["bytes"] == 0
        assert snap["dlq"]["lines"] == 0

    def test_recorded_push_appears_in_snapshot(self, tmp_path: Path):
        recorder.record_push_attempt(
            source="kafka", target="https://sta.example",
            duration_ms=80.0, readings_count=2, sent_count=2,
        )
        snap = build_snapshot(
            uptime_seconds=1.0, window_seconds=300,
            dlq_path=tmp_path / "failed.jsonl",
            freshness_thresholds={"kafka": 300.0},
        )
        assert "kafka::https://sta.example" in snap["push_stats"]

    def test_snapshot_reset_samples_leaves_counter_visible_but_clears_reservoir(self, tmp_path: Path):
        recorder.record_push_attempt(
            source="kafka", target="https://sta.example",
            duration_ms=80.0, readings_count=2, sent_count=2,
        )
        _ = build_snapshot(
            uptime_seconds=1.0, window_seconds=300,
            dlq_path=tmp_path / "failed.jsonl",
            freshness_thresholds={"kafka": 300.0},
        )
        snap2 = recorder.snapshot_targets(reset_samples=False)
        assert snap2["kafka::https://sta.example"]["attempts"] == 1
        assert snap2["kafka::https://sta.example"]["latency_ms"] == {}


class TestFrostWorkerSnapshotFailsafe:
    def test_returns_disabled_when_flag_off(self, monkeypatch):
        # Settings is a frozen dataclass; replace the module attribute wholesale
        # with a lightweight stand-in that only exposes the field we probe.
        import types

        fake = types.SimpleNamespace(frost_async_push_enabled=False)
        monkeypatch.setattr(collector_module, "settings", fake)
        out = collector_module._frost_worker_snapshot()
        assert out == {"enabled": False}
