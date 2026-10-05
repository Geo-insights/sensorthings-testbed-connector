# SLA Evidence Generation — Re-validation Runbook

This document describes how to run a validation window and generate SLA evidence artifacts for the WBSO KB-02 dossier.

## Prerequisites

- The connector is deployed on Render with a persistent disk at `/app/data`
- Python 3.12+ with project dependencies installed (locally or in the container)
- Access to the Render dashboard to toggle environment variables

## Procedure

### 1. Enable the validation harness

In the Render dashboard, set:

```
VALIDATION_HARNESS_ENABLED=true
```

Redeploy or wait for the next deploy. The harness begins writing to `data/validation/{snapshots,events,incidents}.jsonl` immediately.

### 2. Monitor during the window

Check that data is flowing:

```
GET /validation/status
```

This returns the enabled flag and file sizes. Verify `snapshots.jsonl` is growing.

For a quick rollup of current metrics:

```
GET /validation/summary
```

For recent incidents:

```
GET /validation/incidents?limit=20
```

### 3. Wait for the measurement window

Minimum useful window: **48 hours** (produces enough snapshot intervals for meaningful percentiles).

Recommended window for quarterly evidence: **7-14 days**.

The harness is designed to run indefinitely with bounded storage (rotation at configurable size caps).

### 4. Generate evidence artifacts

From the project root:

```bash
# Full window (auto-detected from earliest to latest snapshot)
./scripts/generate_sla_evidence.sh

# Specific date range
./scripts/generate_sla_evidence.sh --start 2026-10-01 --end 2026-10-14
```

This produces a timestamped directory under `reports/sla_evidence_<timestamp>/` containing:

| File | Description |
|------|-------------|
| `validation_report.md` | Full markdown report (uptime, latency, errors, incidents) |
| `metrics.json` | Machine-readable metrics rollup |
| `manifest.json` | Generation metadata (timestamp, filters, snapshot count) |

### 5. Disable the harness (optional)

If the validation window is complete and you do not need continuous collection:

```
VALIDATION_HARNESS_ENABLED=false
```

The harness hooks become no-ops with zero overhead on the ingest hot path. Existing data files are preserved.

### 6. Commit evidence to the WBSO dossier

Copy the staged artifacts into the MVP repository:

```bash
cp -r reports/sla_evidence_<timestamp>/ \
  ../Geo-Insights-MVP/docs/WBSO/evidence/
```

Commit in both repositories:
- This repo: the generated `reports/` output (optional, for traceability)
- `Geo-Insights-MVP`: the evidence copy in `docs/WBSO/evidence/`

## Review amendment

Any completed measurement window produces usable evidence; the report documents actual metrics, not pass/fail. The SLA baseline (KB-02.1) defines target thresholds, but the evidence artifacts record what actually happened. Deviations from targets are documented in the incident log and lessons-learned sections of the report.

## Smoke test (optional)

Before starting a real measurement window, verify the full pipeline:

1. Add a synthetic bad target to `SENSORTHINGS_BASE_URLS` (e.g. `https://smoke-fail.invalid`)
2. Enable the harness and let it run for ~2 hours
3. Generate evidence: `./scripts/generate_sla_evidence.sh`
4. Verify the report shows an uptime dip, circuit-open incidents, and DLQ growth for the synthetic host
5. Remove the synthetic target before starting the real measurement window

## File layout reference

```
data/validation/
  snapshots.jsonl      # 5-min consolidated state (main data source)
  events.jsonl         # Per non-clean push attempt
  incidents.jsonl      # Breaker transitions, alerts, lifecycle events
  metrics.json         # Generated rollup (output of report generator)

reports/
  geonovum_14d_validation.md         # Latest generated report
  sla_evidence_<timestamp>/          # Timestamped evidence bundles
    validation_report.md
    metrics.json
    manifest.json
```
