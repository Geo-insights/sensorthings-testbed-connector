"""Unit tests for the validation harness recorder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.validation.recorder import (
    MONITORING_TARGET,
    ValidationRecorder,
    _percentiles,
)


@pytest.fixture()
def enabled(tmp_path: Path) -> ValidationRecorder:
    rec = ValidationRecorder()
    rec.configure(
        enabled=True,
        data_dir=tmp_path,
        events_max_bytes=1_000_000,
        incidents_max_bytes=1_000_000,
        sample_reservoir_size=100,
    )
    return rec


@pytest.fixture()
def disabled(tmp_path: Path) -> ValidationRecorder:
    rec = ValidationRecorder()
    rec.configure(
        enabled=False,
        data_dir=tmp_path,
        events_max_bytes=1_000_000,
        incidents_max_bytes=1_000_000,
        sample_reservoir_size=100,
    )
    return rec


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestDisabledIsNoOp:
    def test_configure_does_not_create_data_dir(self, tmp_path: Path):
        sub = tmp_path / "notcreated"
        rec = ValidationRecorder()
        rec.configure(
            enabled=False,
            data_dir=sub,
            events_max_bytes=1024,
            incidents_max_bytes=1024,
            sample_reservoir_size=10,
        )
        assert not sub.exists()

    def test_record_push_attempt_writes_nothing(self, disabled: ValidationRecorder, tmp_path: Path):
        disabled.record_push_attempt(
            source="kafka", target="https://x", target_label="t",
            duration_ms=42.0, readings_count=1, sent_count=0, failed_count=1,
            error_class="timeout",
        )
        assert not (tmp_path / "events.jsonl").exists()

    def test_record_incident_writes_nothing(self, disabled: ValidationRecorder, tmp_path: Path):
        disabled.record_incident(kind="test", level="info")
        assert not (tmp_path / "incidents.jsonl").exists()

    def test_snapshot_returns_empty(self, disabled: ValidationRecorder):
        assert disabled.snapshot_targets() == {}


class TestRecordPushAttempt:
    def test_clean_push_updates_counters_but_writes_no_event(self, enabled: ValidationRecorder, tmp_path: Path):
        enabled.record_push_attempt(
            source="kafka", target="https://sta.example", target_label="primary",
            duration_ms=87.3, readings_count=5, sent_count=5,
        )
        snap = enabled.snapshot_targets(reset_samples=False)
        assert snap["kafka::https://sta.example"]["attempts"] == 1
        assert snap["kafka::https://sta.example"]["sent"] == 5
        assert snap["kafka::https://sta.example"]["failed"] == 0
        assert _read_jsonl(tmp_path / "events.jsonl") == []

    def test_failure_writes_event_and_increments(self, enabled: ValidationRecorder, tmp_path: Path):
        enabled.record_push_attempt(
            source="kafka", target="https://sta.example", target_label="primary",
            duration_ms=150.0, readings_count=3, sent_count=0, failed_count=3,
            error_class="timeout", error_msg="Read timed out",
        )
        events = _read_jsonl(tmp_path / "events.jsonl")
        assert len(events) == 1
        e = events[0]
        assert e["source"] == "kafka"
        assert e["target"] == "https://sta.example"
        assert e["sent"] == 0
        assert e["failed"] == 3
        assert e["error_class"] == "timeout"
        assert e["error_msg"] == "Read timed out"

    def test_error_msg_is_truncated_to_400_chars(self, enabled: ValidationRecorder, tmp_path: Path):
        long_msg = "x" * 5000
        enabled.record_push_attempt(
            source="kafka", target="https://x", duration_ms=1, readings_count=1,
            sent_count=0, failed_count=1, error_class="server_5xx", error_msg=long_msg,
        )
        events = _read_jsonl(tmp_path / "events.jsonl")
        assert len(events[0]["error_msg"]) == 400

    def test_dropped_circuit_recorded_as_incident_free_event(self, enabled: ValidationRecorder, tmp_path: Path):
        enabled.record_push_attempt(
            source="kafka", target="https://x", duration_ms=1, readings_count=10,
            sent_count=0, dropped_circuit_count=10, error_class="circuit_open",
        )
        snap = enabled.snapshot_targets(reset_samples=False)
        assert snap["kafka::https://x"]["dropped_circuit"] == 10
        assert snap["kafka::https://x"]["errors_by_class"]["circuit_open"] == 1


class TestLatencyReservoirs:
    def test_batch_duration_samples_collect(self, enabled: ValidationRecorder):
        for d in (10.0, 20.0, 30.0, 40.0, 50.0):
            enabled.record_push_attempt(
                source="kafka", target="https://x", duration_ms=d,
                readings_count=1, sent_count=1,
            )
        snap = enabled.snapshot_targets(reset_samples=False)
        lat = snap["kafka::https://x"]["latency_ms"]
        assert lat["count"] == 5
        assert lat["min"] == 10.0
        assert lat["max"] == 50.0
        assert lat["p50"] == 30.0

    def test_reservoir_is_bounded(self, tmp_path: Path):
        rec = ValidationRecorder()
        rec.configure(
            enabled=True, data_dir=tmp_path,
            events_max_bytes=1_000_000, incidents_max_bytes=1_000_000,
            sample_reservoir_size=100,
        )
        for i in range(250):
            rec.record_push_attempt(
                source="s", target="t", duration_ms=float(i),
                readings_count=1, sent_count=1,
            )
        snap = rec.snapshot_targets(reset_samples=False)
        assert snap["s::t"]["latency_ms"]["count"] == 100

    def test_age_samples_stored(self, enabled: ValidationRecorder):
        enabled.record_push_attempt(
            source="kafka", target="https://x", duration_ms=100.0,
            readings_count=3, sent_count=3,
            age_at_push_samples=[5.0, 10.0, 15.0],
        )
        snap = enabled.snapshot_targets(reset_samples=False)
        age = snap["kafka::https://x"]["age_at_push_s"]
        assert age["count"] == 3
        assert age["min"] == 5.0
        assert age["max"] == 15.0


class TestSnapshotReset:
    def test_reset_clears_samples_but_keeps_counters(self, enabled: ValidationRecorder):
        enabled.record_push_attempt(
            source="s", target="t", duration_ms=100.0,
            readings_count=1, sent_count=1,
        )
        snap1 = enabled.snapshot_targets(reset_samples=True)
        assert snap1["s::t"]["latency_ms"]["count"] == 1
        snap2 = enabled.snapshot_targets(reset_samples=False)
        assert snap2["s::t"]["attempts"] == 1
        assert snap2["s::t"]["latency_ms"] == {}


class TestIncidents:
    def test_incident_writes_line(self, enabled: ValidationRecorder, tmp_path: Path):
        enabled.record_incident(
            kind="circuit_open",
            target="https://x",
            source="kafka",
            level="warning",
            context={"consecutive_failures": 3},
        )
        lines = _read_jsonl(tmp_path / "incidents.jsonl")
        assert len(lines) == 1
        assert lines[0]["kind"] == "circuit_open"
        assert lines[0]["target"] == "https://x"
        assert lines[0]["level"] == "warning"
        assert lines[0]["context"] == {"consecutive_failures": 3}


class TestSummary:
    def test_summary_reports_flag_and_data_dir(self, enabled: ValidationRecorder, tmp_path: Path):
        s = enabled.summary()
        assert s["enabled"] is True
        assert Path(s["data_dir"]) == tmp_path
        assert s["targets"] == {}

    def test_summary_is_non_destructive(self, enabled: ValidationRecorder):
        enabled.record_push_attempt(
            source="s", target="t", duration_ms=1.0,
            readings_count=1, sent_count=1,
        )
        _ = enabled.summary()
        s2 = enabled.summary()
        assert s2["targets"]["s::t"]["latency_ms"]["count"] == 1


class TestFileRotation:
    def test_rotates_when_event_file_exceeds_cap(self, tmp_path: Path):
        rec = ValidationRecorder()
        rec.configure(
            enabled=True, data_dir=tmp_path,
            events_max_bytes=200,
            incidents_max_bytes=1_000_000,
            sample_reservoir_size=10,
            rotate_keep=3,
        )
        for i in range(50):
            rec.record_push_attempt(
                source="s", target="t", duration_ms=1.0,
                readings_count=1, sent_count=0, failed_count=1,
                error_class="timeout", error_msg=f"attempt {i}",
            )
        assert (tmp_path / "events.jsonl").exists()
        assert (tmp_path / "events.jsonl.1").exists()


class TestMonitoringTargetConstant:
    def test_constant_is_exported(self):
        # Report generator groups by this key; changing it breaks the report.
        assert MONITORING_TARGET == "monitoring"


class TestPercentileHelper:
    def test_empty_returns_empty_dict(self):
        assert _percentiles([]) == {}

    def test_computes_expected_shape(self):
        out = _percentiles([1, 2, 3, 4, 5])
        assert out["count"] == 5
        assert out["min"] == 1
        assert out["max"] == 5
        assert "p50" in out and "p95" in out and "p99" in out
