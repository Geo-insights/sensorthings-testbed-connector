#!/usr/bin/env python
"""Generate the Geonovum 14-day validation addendum from harness output.

Reads three JSONL files (+ rotated ``.1..N`` backups) under
``data/validation/``:
  * ``snapshots.jsonl``  — 5-min consolidated state snapshots
  * ``events.jsonl``     — per non-clean push attempts (batch-level)
  * ``incidents.jsonl``  — breaker transitions, alerts, harness lifecycle

Aggregates them into:
  * ``reports/geonovum_14d_validation.md``  — markdown addendum for the
    Geonovum submission (sections: exec summary, uptime, latency, error
    rate, breaker/DLQ, incident log, methodology).
  * ``data/validation/metrics.json``       — machine-readable rollup.

Usage:
    python scripts/generate_validation_report.py \\
        [--data-dir data/validation] \\
        [--report-out reports/geonovum_14d_validation.md] \\
        [--metrics-out data/validation/metrics.json] \\
        [--start 2026-09-10T00:00:00Z] \\
        [--end   2026-09-24T00:00:00Z] \\
        [--dlq   data/failed_observations.jsonl]

If ``--start``/``--end`` are omitted the window is inferred from the earliest
and latest snapshot timestamps.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _rotated_files(base: Path) -> list[Path]:
    """Return base + rotated backups (oldest first). Missing files skipped."""
    out: list[Path] = []
    for i in range(10, 0, -1):
        p = base.with_name(base.name + f".{i}")
        if p.exists():
            out.append(p)
    if base.exists():
        out.append(base)
    return out


def _load_jsonl(base: Path) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for p in _rotated_files(base):
        try:
            with p.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        lines.append(json.loads(raw))
                    except ValueError:
                        continue
        except OSError:
            continue
    return lines


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _filter_window(records: list[dict[str, Any]], key: str, start: datetime | None, end: datetime | None) -> list[dict[str, Any]]:
    if start is None and end is None:
        return records
    out: list[dict[str, Any]] = []
    for r in records:
        ts = _parse_ts(r.get(key))
        if ts is None:
            continue
        if start is not None and ts < start:
            continue
        if end is not None and ts > end:
            continue
        out.append(r)
    return out


@dataclass
class PathStats:
    source: str
    target: str
    target_label: str = ""
    attempts: int = 0
    sent: int = 0
    failed: int = 0
    dropped_permanent: int = 0
    dropped_circuit: int = 0
    errors_by_class: dict[str, int] = field(default_factory=dict)
    latency_p50s: list[float] = field(default_factory=list)
    latency_p95s: list[float] = field(default_factory=list)
    latency_maxes: list[float] = field(default_factory=list)
    age_p50s: list[float] = field(default_factory=list)
    age_p95s: list[float] = field(default_factory=list)
    age_maxes: list[float] = field(default_factory=list)
    windows_with_send: int = 0
    windows_total: int = 0

    @property
    def error_rate_pct(self) -> float:
        if self.attempts == 0:
            return 0.0
        errs = self.failed + self.dropped_permanent + self.dropped_circuit
        return round(100.0 * errs / self.attempts, 3)

    @property
    def success_rate_pct(self) -> float:
        return round(100.0 - self.error_rate_pct, 3)

    def add_snapshot(self, entry: dict[str, Any]) -> None:
        self.windows_total += 1
        self.attempts += int(entry.get("attempts", 0))
        self.sent += int(entry.get("sent", 0))
        self.failed += int(entry.get("failed", 0))
        self.dropped_permanent += int(entry.get("dropped_permanent", 0))
        self.dropped_circuit += int(entry.get("dropped_circuit", 0))
        for cls, cnt in (entry.get("errors_by_class") or {}).items():
            self.errors_by_class[cls] = self.errors_by_class.get(cls, 0) + int(cnt)
        if int(entry.get("sent", 0)) > 0:
            self.windows_with_send += 1
        for src_key, dst in (
            ("latency_ms", (self.latency_p50s, self.latency_p95s, self.latency_maxes)),
            ("age_at_push_s", (self.age_p50s, self.age_p95s, self.age_maxes)),
        ):
            data = entry.get(src_key) or {}
            if not data or data.get("count", 0) == 0:
                continue
            if data.get("p50") is not None:
                dst[0].append(float(data["p50"]))
            if data.get("p95") is not None:
                dst[1].append(float(data["p95"]))
            if data.get("max") is not None:
                dst[2].append(float(data["max"]))

    def as_dict(self) -> dict[str, Any]:
        def _summ(p50s: list[float], p95s: list[float], maxes: list[float]) -> dict[str, Any] | None:
            if not p50s and not p95s and not maxes:
                return None
            return {
                "p50_median": round(statistics.median(p50s), 2) if p50s else None,
                "p95_median": round(statistics.median(p95s), 2) if p95s else None,
                "max_max": round(max(maxes), 2) if maxes else None,
                "windows_with_samples": max(len(p50s), len(p95s), len(maxes)),
            }
        return {
            "source": self.source,
            "target": self.target,
            "target_label": self.target_label,
            "attempts": self.attempts,
            "sent": self.sent,
            "failed": self.failed,
            "dropped_permanent": self.dropped_permanent,
            "dropped_circuit": self.dropped_circuit,
            "success_rate_pct": self.success_rate_pct,
            "error_rate_pct": self.error_rate_pct,
            "errors_by_class": dict(self.errors_by_class),
            "latency_ms": _summ(self.latency_p50s, self.latency_p95s, self.latency_maxes),
            "age_at_push_s": _summ(self.age_p50s, self.age_p95s, self.age_maxes),
            "windows_with_send": self.windows_with_send,
            "windows_total": self.windows_total,
            "windows_success_pct": (
                round(100.0 * self.windows_with_send / self.windows_total, 2)
                if self.windows_total else 0.0
            ),
        }


@dataclass
class SourceStats:
    name: str
    windows_fresh: int = 0
    windows_total: int = 0
    longest_gap_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "windows_fresh": self.windows_fresh,
            "windows_total": self.windows_total,
            "uptime_pct": (
                round(100.0 * self.windows_fresh / self.windows_total, 2)
                if self.windows_total else 0.0
            ),
            "longest_gap_seconds": round(self.longest_gap_seconds, 1),
        }


def compute_aggregates(
    snapshots: list[dict[str, Any]],
    events: list[dict[str, Any]],
    incidents: list[dict[str, Any]],
    dlq_lines: list[dict[str, Any]] | None,
    start: datetime | None,
    end: datetime | None,
    snapshot_interval_hint_s: int = 300,
) -> dict[str, Any]:
    """Reduce all raw records into the final metrics dict."""

    snapshots = sorted(
        _filter_window(snapshots, "t", start, end),
        key=lambda r: _parse_ts(r.get("t")) or datetime.min.replace(tzinfo=UTC),
    )
    events = _filter_window(events, "t", start, end)
    incidents = sorted(
        _filter_window(incidents, "t", start, end),
        key=lambda r: _parse_ts(r.get("t")) or datetime.min.replace(tzinfo=UTC),
    )

    if not snapshots:
        return {
            "window": {"start": None, "end": None, "duration_seconds": 0, "snapshot_count": 0},
            "warning": "no snapshots inside the requested window",
        }

    first_ts = _parse_ts(snapshots[0]["t"])
    last_ts = _parse_ts(snapshots[-1]["t"])
    win_start = start or first_ts
    win_end = end or last_ts
    duration_s = 0.0
    if win_start and win_end:
        duration_s = max(0.0, (win_end - win_start).total_seconds())
    snap_interval = int(snapshots[0].get("window_seconds") or snapshot_interval_hint_s)
    expected_snapshots = max(1, int(duration_s / snap_interval)) if duration_s else len(snapshots)

    paths: dict[str, PathStats] = {}
    sources: dict[str, SourceStats] = {}
    target_windows_success: dict[str, int] = defaultdict(int)
    target_windows_total: dict[str, int] = defaultdict(int)
    target_label_lookup: dict[str, str] = {}
    prev_breaker_state: dict[str, bool] = {}
    breaker_events: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"open_events": 0, "total_open_seconds": 0.0, "open_since": None}
    )
    dlq_bytes_series: list[int] = []
    dlq_lines_series: list[int] = []
    source_last_success_ts: dict[str, datetime | None] = {}

    for snap in snapshots:
        snap_ts = _parse_ts(snap.get("t"))

        for src, info in (snap.get("sources") or {}).items():
            s = sources.setdefault(src, SourceStats(name=src))
            s.windows_total += 1
            stale = bool(info.get("stale"))
            if not stale:
                s.windows_fresh += 1
                source_last_success_ts[src] = snap_ts
            else:
                last = source_last_success_ts.get(src)
                if last is not None and snap_ts is not None:
                    gap = (snap_ts - last).total_seconds()
                    if gap > s.longest_gap_seconds:
                        s.longest_gap_seconds = gap

        for key, entry in (snap.get("push_stats") or {}).items():
            if "::" not in key:
                continue
            source_name, target = key.split("::", 1)
            path = paths.setdefault(key, PathStats(source=source_name, target=target))
            path.add_snapshot(entry)
            target_windows_total[target] += 1
            if int(entry.get("sent", 0)) > 0:
                target_windows_success[target] += 1

        for tgt, cb in (snap.get("circuit_breakers") or {}).items():
            was_open = prev_breaker_state.get(tgt, False)
            is_open = bool(cb.get("open"))
            tracker = breaker_events[tgt]
            if is_open and not was_open:
                tracker["open_events"] += 1
                tracker["open_since"] = snap_ts
            if (not is_open) and was_open and tracker["open_since"] is not None and snap_ts is not None:
                tracker["total_open_seconds"] += (snap_ts - tracker["open_since"]).total_seconds()
                tracker["open_since"] = None
            prev_breaker_state[tgt] = is_open

        dlq = snap.get("dlq") or {}
        dlq_bytes_series.append(int(dlq.get("bytes", 0)))
        dlq_lines_series.append(int(dlq.get("lines", 0)))

    for tracker in breaker_events.values():
        if tracker["open_since"] is not None and win_end is not None:
            tracker["total_open_seconds"] += (win_end - tracker["open_since"]).total_seconds()
            tracker["open_since"] = None
        tracker["total_open_seconds"] = round(tracker["total_open_seconds"], 1)

    # Ensure every target seen in circuit_breakers appears in the targets output
    # even if it has zero push_stats (e.g. breaker stayed open the whole window
    # so no pushes were attempted). Missing them would hide the incident from
    # the report.
    for tgt in breaker_events:
        if tgt not in target_windows_total:
            target_windows_total[tgt] = 0
            target_windows_success[tgt] = 0

    for e in events:
        key = f"{e.get('source', '')}::{e.get('target', '')}"
        p = paths.get(key)
        if p is not None and not p.target_label:
            p.target_label = e.get("target_label", "") or ""
        tgt = e.get("target", "")
        if tgt:
            target_label_lookup[tgt] = e.get("target_label", "") or target_label_lookup.get(tgt, "")

    dlq_summary: dict[str, Any] = {
        "peak_lines_in_snapshots": max(dlq_lines_series) if dlq_lines_series else 0,
        "peak_bytes_in_snapshots": max(dlq_bytes_series) if dlq_bytes_series else 0,
        "final_lines_in_snapshots": dlq_lines_series[-1] if dlq_lines_series else 0,
        "final_bytes_in_snapshots": dlq_bytes_series[-1] if dlq_bytes_series else 0,
    }
    if dlq_lines is not None:
        dlq_summary["surviving_entries_at_report_time"] = len(dlq_lines)
        by_host: Counter[str] = Counter()
        for r in dlq_lines:
            ep = str(r.get("endpoint", ""))
            if "://" in ep:
                rest = ep.split("://", 1)[1]
                host = rest.split("/", 1)[0]
            else:
                host = ep or "unknown"
            by_host[host or "unknown"] += 1
        dlq_summary["surviving_by_host"] = dict(by_host)

    inc_counter: Counter[str] = Counter()
    for i in incidents:
        inc_counter[i.get("kind", "unknown")] += 1

    connector_uptime_pct = (
        round(100.0 * min(len(snapshots), expected_snapshots) / expected_snapshots, 2)
        if expected_snapshots else 0.0
    )

    return {
        "window": {
            "start": win_start.isoformat().replace("+00:00", "Z") if win_start else None,
            "end": win_end.isoformat().replace("+00:00", "Z") if win_end else None,
            "duration_seconds": round(duration_s, 1),
            "duration_days": round(duration_s / 86400, 2),
            "snapshot_interval_seconds": snap_interval,
            "snapshot_count": len(snapshots),
            "snapshot_expected": expected_snapshots,
        },
        "connector_uptime": {
            "snapshots_observed": len(snapshots),
            "snapshots_expected": expected_snapshots,
            "uptime_pct": connector_uptime_pct,
        },
        "sources": {name: s.as_dict() for name, s in sorted(sources.items())},
        "targets": {
            tgt: {
                "label": target_label_lookup.get(tgt, ""),
                "windows_success": target_windows_success[tgt],
                "windows_total": target_windows_total[tgt],
                "uptime_pct": (
                    round(100.0 * target_windows_success[tgt] / target_windows_total[tgt], 2)
                    if target_windows_total[tgt] else 0.0
                ),
                "breaker": {
                    "open_events": breaker_events[tgt]["open_events"],
                    "total_open_seconds": breaker_events[tgt]["total_open_seconds"],
                },
            }
            for tgt in sorted(target_windows_total.keys())
        },
        "paths": sorted(
            (p.as_dict() for p in paths.values()),
            key=lambda d: (d["source"], d["target"]),
        ),
        "dlq": dlq_summary,
        "incidents": {
            "count": len(incidents),
            "by_kind": dict(inc_counter),
            "log": incidents,
        },
    }


def _or_dash(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.1f}"
    return str(v)


def render_markdown(metrics: dict[str, Any]) -> str:
    if "warning" in metrics:
        return f"# Validation Report\n\n**Warning:** {metrics['warning']}\n"

    w = metrics["window"]
    cu = metrics["connector_uptime"]
    lines: list[str] = []
    lines.append("# Geonovum Sensor Data Testbed 2026 — Topic #2 Addendum")
    lines.append("## 14-Day Validation Report for the SensorThings Testbed Connector")
    lines.append("")
    lines.append(f"**Window:** {w['start']} → {w['end']}  ")
    lines.append(f"**Duration:** {w['duration_days']:.2f} days ({w['duration_seconds']:.0f} s)  ")
    lines.append(f"**Snapshots:** {w['snapshot_count']} @ {w['snapshot_interval_seconds']} s (expected {w['snapshot_expected']})  ")
    lines.append("")

    lines.append("## 1. Executive summary")
    lines.append("")
    lines.append(f"- **Connector uptime (snapshots observed / expected):** {cu['uptime_pct']:.2f}%")
    if metrics["sources"]:
        src_line = ", ".join(f"{k} {v['uptime_pct']:.2f}%" for k, v in metrics["sources"].items())
        lines.append(f"- **Source data-arrival uptime:** {src_line}")
    if metrics["targets"]:
        tgt_line = ", ".join(f"{v.get('label') or k} {v['uptime_pct']:.2f}%" for k, v in metrics["targets"].items())
        lines.append(f"- **Target push uptime:** {tgt_line}")
    total_attempts = sum(p["attempts"] for p in metrics["paths"])
    total_sent = sum(p["sent"] for p in metrics["paths"])
    overall_success = (100.0 * total_sent / total_attempts) if total_attempts else 0.0
    lines.append(f"- **Overall push success rate:** {overall_success:.3f}% ({total_sent:,} sent / {total_attempts:,} attempts)")
    total_open_events = sum(t["breaker"]["open_events"] for t in metrics["targets"].values())
    total_open_seconds = sum(t["breaker"]["total_open_seconds"] for t in metrics["targets"].values())
    lines.append(f"- **Circuit breaker openings:** {total_open_events} (total open time {total_open_seconds:.0f} s)")
    dlq = metrics["dlq"]
    lines.append(f"- **DLQ peak:** {dlq['peak_lines_in_snapshots']:,} lines / {dlq['peak_bytes_in_snapshots']:,} bytes; final {dlq['final_lines_in_snapshots']:,} lines")
    lines.append(f"- **Incidents recorded:** {metrics['incidents']['count']}")
    lines.append("")

    lines.append("## 2. Uptime")
    lines.append("")
    lines.append("### 2.1 Connector process")
    lines.append("")
    lines.append(f"- Snapshots observed: **{cu['snapshots_observed']:,}** of expected **{cu['snapshots_expected']:,}**")
    lines.append(f"- Uptime: **{cu['uptime_pct']:.2f}%**  ")
    lines.append("  _A missing snapshot for a whole interval means the connector process (or the harness collector inside it) was not running for that window._")
    lines.append("")
    lines.append('### 2.2 Per-source data-arrival uptime ("data actually arriving")')
    lines.append("")
    lines.append("| Source | Fresh windows | Windows total | Uptime | Longest gap (s) |")
    lines.append("|--------|---------------|---------------|--------|-----------------|")
    for name, s in metrics["sources"].items():
        lines.append(f"| {name} | {s['windows_fresh']:,} | {s['windows_total']:,} | {s['uptime_pct']:.2f}% | {s['longest_gap_seconds']:.0f} |")
    lines.append("")
    lines.append("### 2.3 Per-target push uptime")
    lines.append("")
    lines.append("| Target | Label | Success windows | Total windows | Uptime | Breaker open events | Total open (s) |")
    lines.append("|--------|-------|-----------------|---------------|--------|---------------------|----------------|")
    for tgt, t in metrics["targets"].items():
        lines.append(
            f"| `{tgt}` | {t.get('label') or ''} | {t['windows_success']:,} | "
            f"{t['windows_total']:,} | {t['uptime_pct']:.2f}% | "
            f"{t['breaker']['open_events']} | {t['breaker']['total_open_seconds']:.0f} |"
        )
    lines.append("")

    lines.append("## 3. Latency per data path")
    lines.append("")
    lines.append("Two latency dimensions per path:")
    lines.append("- **Batch push duration** (ms): time spent inside `_push_target` — the FROST HTTP round-trip.")
    lines.append("- **Age at push** (s): seconds between the sensor's recorded observation time and the push completing on this target.")
    lines.append("")
    lines.append("Percentiles are aggregated across 5-min windows: `p50_median`/`p95_median` = median across window-level percentiles; `max_max` = worst observation seen.")
    lines.append("")
    lines.append("| Source | Target | Attempts | Sent | Success % | Push p50 (ms) | Push p95 (ms) | Push max (ms) | Age p50 (s) | Age p95 (s) | Age max (s) |")
    lines.append("|--------|--------|----------|------|-----------|----------------|----------------|----------------|--------------|--------------|--------------|")
    for p in metrics["paths"]:
        lat = p.get("latency_ms") or {}
        age = p.get("age_at_push_s") or {}
        lines.append(
            f"| {p['source']} | `{p['target']}` ({p.get('target_label') or ''}) | {p['attempts']:,} | {p['sent']:,} | "
            f"{p['success_rate_pct']:.3f}% | "
            f"{_or_dash(lat.get('p50_median'))} | {_or_dash(lat.get('p95_median'))} | {_or_dash(lat.get('max_max'))} | "
            f"{_or_dash(age.get('p50_median'))} | {_or_dash(age.get('p95_median'))} | {_or_dash(age.get('max_max'))} |"
        )
    lines.append("")

    lines.append("## 4. Error rate")
    lines.append("")
    lines.append("### 4.1 Aggregate")
    lines.append("")
    lines.append("| Source | Target | Attempts | Failed | Dropped (perm) | Dropped (circuit) | Error rate |")
    lines.append("|--------|--------|----------|--------|----------------|-------------------|------------|")
    for p in metrics["paths"]:
        lines.append(
            f"| {p['source']} | `{p['target']}` | {p['attempts']:,} | {p['failed']:,} | "
            f"{p['dropped_permanent']:,} | {p['dropped_circuit']:,} | {p['error_rate_pct']:.3f}% |"
        )
    lines.append("")
    lines.append("### 4.2 Errors by class")
    lines.append("")
    all_classes = sorted({cls for p in metrics["paths"] for cls in p["errors_by_class"]})
    if all_classes:
        header = "| Source | Target | " + " | ".join(all_classes) + " |"
        sep = "|" + "---|" * (len(all_classes) + 2)
        lines.append(header)
        lines.append(sep)
        for p in metrics["paths"]:
            row = [p["source"], f"`{p['target']}`"] + [str(p["errors_by_class"].get(c, 0)) for c in all_classes]
            lines.append("| " + " | ".join(row) + " |")
    else:
        lines.append("_No errors classified in the window._")
    lines.append("")

    lines.append("## 5. Circuit breaker and DLQ activity")
    lines.append("")
    lines.append("### 5.1 Circuit breaker events")
    lines.append("")
    lines.append("| Target | Open events | Total open time (s) |")
    lines.append("|--------|-------------|---------------------|")
    for tgt, t in metrics["targets"].items():
        lines.append(f"| `{tgt}` | {t['breaker']['open_events']} | {t['breaker']['total_open_seconds']:.0f} |")
    lines.append("")
    lines.append("### 5.2 Dead-letter queue")
    lines.append("")
    lines.append(f"- Peak: **{dlq['peak_lines_in_snapshots']:,}** lines / **{dlq['peak_bytes_in_snapshots']:,}** bytes across the window")
    lines.append(f"- End of window: **{dlq['final_lines_in_snapshots']:,}** lines / **{dlq['final_bytes_in_snapshots']:,}** bytes")
    if "surviving_entries_at_report_time" in dlq:
        lines.append(f"- Surviving entries at report time: **{dlq['surviving_entries_at_report_time']:,}**")
        if dlq.get("surviving_by_host"):
            lines.append("- Broken out by target host:")
            for host, cnt in sorted(dlq["surviving_by_host"].items(), key=lambda kv: -kv[1]):
                lines.append(f"  - `{host}` — {cnt:,} entries")
    lines.append("")
    lines.append("_The connector runs a background replay loop that drains the DLQ as targets recover; a low \"final\" count means replay succeeded._")
    lines.append("")

    lines.append("## 6. Incident log")
    lines.append("")
    if metrics["incidents"]["count"] == 0:
        lines.append("_No incidents recorded._")
    else:
        lines.append(f"Total: **{metrics['incidents']['count']}** incidents. By kind:")
        for kind, cnt in sorted(metrics["incidents"]["by_kind"].items(), key=lambda kv: -kv[1]):
            lines.append(f"- `{kind}` — {cnt}")
        lines.append("")
        lines.append("Chronological detail:")
        lines.append("")
        for inc in metrics["incidents"]["log"]:
            ctx = inc.get("context") or {}
            msg = ctx.get("message", "")
            other = ", ".join(f"{k}={v}" for k, v in ctx.items() if k != "message")
            tgt = inc.get("target") or ""
            src = inc.get("source") or ""
            tag_bits = []
            if tgt:
                tag_bits.append(f"target={tgt}")
            if src:
                tag_bits.append(f"source={src}")
            tag = " · ".join(tag_bits)
            lines.append(
                f"- **{inc.get('t')}** `{inc.get('kind')}` [{inc.get('level')}]"
                + (f" — {msg}" if msg else "")
                + (f" ({tag})" if tag else "")
                + (f" — {other}" if other else "")
            )
    lines.append("")

    lines.append("## 7. Lessons learned")
    lines.append("")
    lines.append("_This section is authored by the operator based on the incident log above and post-run analysis. Placeholders below match the Geonovum report format expectations:_")
    lines.append("")
    lines.append("- **What worked as designed:** …")
    lines.append("- **What surprised us:** …")
    lines.append("- **Failure modes observed:** …")
    lines.append("- **Edge cases documented:** …")
    lines.append("- **Recommended follow-ups before Phase 4:** …")
    lines.append("")

    lines.append("## Appendix A: Methodology")
    lines.append("")
    lines.append("- **Data collection:** background async task in `app/services/validation/collector.py` writes a JSON line to `data/validation/snapshots.jsonl` every 5 minutes with per-source freshness, per-(source,target) push aggregates, circuit breaker state, DLQ file stats, and FROST worker stats.")
    lines.append("- **Latency percentiles:** each push cycle contributes a batch duration; each observation contributes an age-at-push sample (source_time → push_completed wall time). Reservoirs are bounded (1000 samples) per (source, target) per window; snapshots persist p50/p95/max of the reservoir, then reset. Cross-window aggregation takes the median of window p50s (`p50_median`) and window p95s (`p95_median`), and the max of window maxes (`max_max`). This is a documented approximation — exact percentiles across the full 14-day sample stream would require keeping all samples.")
    lines.append("- **Error taxonomy:** `app/services/validation/error_classifier.py`. Buckets: `timeout`, `connection`, `dns`, `tls`, `auth_401`, `auth_403`, `malformed_4xx`, `rate_limit`, `server_5xx`, `serialization`, `circuit_open`, `unresolved_datastream`, `unknown`.")
    lines.append("- **Feature flag:** `VALIDATION_HARNESS_ENABLED=true`. With the flag off, all instrumentation is a no-op (zero overhead on the ingest hot path).")
    lines.append("- **Storage layout:** `data/validation/{events,incidents,snapshots}.jsonl` on the Render persistent disk. Events and incidents rotate at configured byte caps with `.1..5` backups.")
    lines.append("")

    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default="data/validation")
    ap.add_argument("--report-out", default="reports/geonovum_14d_validation.md")
    ap.add_argument("--metrics-out", default="data/validation/metrics.json")
    ap.add_argument("--start", default=None, help="Optional window start ISO (e.g. 2026-09-10T00:00:00Z)")
    ap.add_argument("--end", default=None, help="Optional window end ISO")
    ap.add_argument("--dlq", default="data/failed_observations.jsonl")
    args = ap.parse_args(argv)

    data_dir = Path(args.data_dir)
    snapshots = _load_jsonl(data_dir / "snapshots.jsonl")
    events = _load_jsonl(data_dir / "events.jsonl")
    incidents = _load_jsonl(data_dir / "incidents.jsonl")
    dlq_lines: list[dict[str, Any]] | None = None
    dlq_path = Path(args.dlq)
    if dlq_path.exists():
        dlq_lines = _load_jsonl(dlq_path)

    start = _parse_ts(args.start) if args.start else None
    end = _parse_ts(args.end) if args.end else None

    metrics = compute_aggregates(snapshots, events, incidents, dlq_lines, start, end)

    report_path = Path(args.report_out)
    metrics_path = Path(args.metrics_out)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_path.parent.mkdir(parents=True, exist_ok=True)

    metrics_path.write_text(json.dumps(metrics, indent=2, default=str), encoding="utf-8")
    report_path.write_text(render_markdown(metrics), encoding="utf-8")

    print(f"Wrote {report_path} ({report_path.stat().st_size:,} bytes)")
    print(f"Wrote {metrics_path} ({metrics_path.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
