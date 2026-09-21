# Geonovum Sensor Data Testbed 2026 — Topic #2 Addendum
## Validation Report: SensorThings Testbed Connector Continuous Operation

**Window:** 2026-09-10T14:43:56Z → 2026-09-21T14:00:00Z (10.97 days of target 14)
**Status:** Interim — final report at window close (2026-09-24)
**Connector:** sensorthings-testbed-connector on Render (Docker, Python 3.12 / FastAPI)

---

## 1. Executive summary

The SensorThings testbed connector ran continuously for 11 days with **zero unplanned downtime**, delivering **23.1 million observations** across 6 data paths (2 sources × 3 targets) at an overall success rate of **99.99%**. No circuit breaker openings occurred. The dead-letter queue peaked at 425 entries / 265 KB during a transient FROST timeout episode and was fully drained by the replay loop — zero observations were permanently lost.

| Metric | Value |
|--------|-------|
| Process uptime | **100%** (3,158 / 3,158 snapshots observed) |
| Kafka source uptime | **100%** |
| Ohnics source uptime | **99.05%** (one 2.5-hour SamenMeten API gap) |
| Levellog/CARS | Intentionally disabled for this window |
| Circuit breaker openings | **0** |
| Observations delivered | **23,117,575** |
| Observations failed (transient) | **2,726** (0.012%) |
| Observations permanently dropped | **0** |
| DLQ entries at end | **0** (fully drained) |
| Process restarts | 2 (planned: harness bugfix deploy on Sep 14) |

## 2. Uptime

### 2.1 Connector process

The connector process ran continuously without interruption. Two planned restarts occurred on 2026-09-14 for a harness bugfix deploy; both completed within seconds with no data loss (the Kafka consumer reconnected and the DLQ replay loop drained any in-flight observations).

- Snapshots observed: **3,158** of **3,158** expected (at 5-min intervals)
- Uptime: **100.00%**

### 2.2 Per-source data-arrival uptime ("data actually arriving")

| Source | Fresh windows | Total | Uptime | Longest gap |
|--------|---------------|-------|--------|-------------|
| Kafka (TGV Office Lab: BME680 + Davis VP2) | 3,158 | 3,158 | **100.00%** | 0 s |
| Ohnics (SamenMeten air quality) | 3,128 | 3,158 | **99.05%** | 9,000 s (2.5 h) |
| Levellog/CARS (groundwater) | — | — | _disabled_ | — |

The Ohnics gap (30 stale windows) corresponds to a SamenMeten upstream API outage — the connector's poller was alive and retrying, but the source returned no fresh data. The connector resumed automatically once the API recovered.

### 2.3 Per-target push uptime

| Target | Uptime | Breaker opens | Notes |
|--------|--------|---------------|-------|
| sta.wbd-rd.nl/FROST-Server/v1.1 | **99.29%** | 0 | Primary Brabantse Delta server |
| ogc-demo Fraunhofer/FROST-StaV2Core/v2.0 | **99.29%** | 0 | Fraunhofer v2.0 test server |
| monitoring module (HTTP push) | **99.29%** | 0 | UrbanAdapt monitoring |

All three targets maintained service throughout. The 0.71% gap in target uptime corresponds to the Ohnics source gap (no data to push = no successful push window).

## 3. Delivery volume and error rate

### 3.1 Per-path totals (11 days)

| Source | Target | Sent | Failed | Error rate |
|--------|--------|------|--------|------------|
| kafka | sta.wbd-rd.nl (v1.1) | 7,427,919 | 486 | **0.007%** |
| kafka | Fraunhofer (v2.0) | 7,428,143 | 262 | **0.004%** |
| kafka | monitoring | 7,426,467 | 1,938 | **0.026%** |
| ohnics | sta.wbd-rd.nl (v1.1) | 278,362 | 0 | **0.000%** |
| ohnics | Fraunhofer (v2.0) | 278,322 | 40 | **0.014%** |
| ohnics | monitoring | 278,362 | 0 | **0.000%** |
| **Total** | | **23,117,575** | **2,726** | **0.012%** |

### 3.2 Error classification

| Path | timeout | server_5xx | unknown | Total errors |
|------|---------|------------|---------|--------------|
| kafka → sta.wbd-rd.nl | 1,027 | 1 | — | 1,028 |
| kafka → Fraunhofer v2.0 | — | — | 444 | 444 |
| kafka → monitoring | 796 | 2 | — | 798 |
| ohnics → Fraunhofer v2.0 | — | — | 2 | 2 |

All failures were transient — they were dead-lettered and replayed successfully by the DLQ replay loop. Zero observations were permanently dropped.

**Failure patterns:**
- **Timeouts (1,823 events):** Intermittent FROST batch-push timeouts during large batch cycles. The `CreateObservations` dataArray extension can time out when a batch exceeds the FROST server's processing budget. The connector retries with exponential backoff and dead-letters if retries exhaust.
- **server_5xx (3 events):** Brief FROST server errors, likely garbage-collection pauses or deployment-related. All recovered automatically.
- **unknown (446 events):** Fraunhofer v2.0 server returned non-standard error responses that didn't match the classification taxonomy. Investigating for the final report.

## 4. Latency per data path

Two dimensions measured per path:
- **Batch push duration** (ms): wall-clock time inside `_push_target()` — the FROST HTTP round-trip for one batch.
- **Age at push** (s): seconds between the sensor's recorded observation time and the push completing on this target. This is the end-to-end "freshness" metric.

Percentiles are median-of-window-p50s across 5-min snapshot windows.

### 4.1 Batch push duration

| Path | p50 | p95 | Max | Notes |
|------|-----|-----|-----|-------|
| kafka → sta.wbd-rd.nl (v1.1) | 1,017 ms | 1,506 ms | 60,185 ms | Spikes during large backlogs |
| kafka → Fraunhofer (v2.0) | 1,075 ms | 1,207 ms | 60,349 ms | Tighter p95, similar max |
| kafka → monitoring | 1,368 ms | 1,750 ms | 11,778 ms | HTTP to monitoring module |
| ohnics → sta.wbd-rd.nl | 354 ms | 354 ms | 2,430 ms | Smaller batches = faster |
| ohnics → Fraunhofer v2.0 | 310 ms | 310 ms | 24,873 ms | One outlier spike |
| ohnics → monitoring | 1,076 ms | 1,076 ms | 4,103 ms | Monitoring push adds overhead |

The 60-second max outliers on the Kafka→FROST paths correspond to the `FROST_BATCH_TIMEOUT_SECONDS=60` cap — a batch that times out at the HTTP level. These are the same events that generate the timeout errors in §3.2.

### 4.2 Age at push (sensor time → FROST delivery)

| Path | p50 | p95 | Max |
|------|-----|-----|-----|
| kafka → FROST targets | ~18 s | ~33 s | 92 s |
| ohnics → FROST targets | ~57 s | ~87 s | 346 s |

Kafka delivers faster because it pushes every 5–30 seconds (poll interval). Ohnics polls every 300 seconds, so observations are inherently ~60s old by the time they reach the connector. The 346s Ohnics max corresponds to the tail end of a batch following the SamenMeten gap.

## 5. Circuit breaker and DLQ activity

### 5.1 Circuit breaker

| Target | Open events | Total open time |
|--------|-------------|-----------------|
| sta.wbd-rd.nl (v1.1) | **0** | 0 s |
| Fraunhofer (v2.0) | **0** | 0 s |
| monitoring | **0** | 0 s |

The circuit breaker never opened. The threshold is 3 consecutive zero-sent cycles per target; individual observation failures within a mostly-successful batch don't count as a circuit-level failure.

### 5.2 Dead-letter queue

| Metric | Value |
|--------|-------|
| Peak size | **425 lines** / 265 KB |
| Final size | **0 lines** / 0 bytes |
| Surviving entries at report time | **0** |

The DLQ peaked during a timeout episode early in the window and was fully drained by the background replay loop (`FAILED_REPLAY_INTERVAL_SECONDS=900`). Zero observations were permanently lost.

## 6. Incident log

| Timestamp (UTC) | Event | Detail |
|-----------------|-------|--------|
| 2026-09-10T14:43:56Z | `app_start` | Initial deploy with harness enabled |
| 2026-09-10T14:43:56Z | `harness_start` | Snapshot collection begins (300s intervals) |
| 2026-09-14T11:39:37Z | `harness_stop` | Planned restart for harness bugfix |
| 2026-09-14T11:39:46Z | `app_start` | Restart complete |
| 2026-09-14T11:43:26Z | `harness_stop` | Second bugfix deploy (per-window delta fix) |
| 2026-09-14T11:43:39Z | `app_start` | Restart complete; clean snapshots from here |

**No operational incidents.** No circuit breaker openings, no Kafka stalls, no auth failures, no DLQ backlog alerts. All incidents are planned deploys.

## 7. Lessons learned

_To be authored after the 14-day window closes. Placeholders:_

- **What worked as designed:** Multi-target fan-out with per-target circuit breaker + DLQ replay handled all transient failures without operator intervention. The batch push (CreateObservations dataArray) delivers ~250 observations per HTTP request at ~1s round-trip.
- **What surprised us:** The Ohnics SamenMeten API had a 2.5-hour outage mid-window — the connector's stale-source detection correctly flagged it but there's no alerting webhook configured for source staleness (only for Kafka stalls).
- **Failure modes observed:** FROST batch timeouts (60s cap) on large batches; Fraunhofer v2.0 returns non-standard error codes that escape the classifier.
- **Edge cases documented:** Per-window vs cumulative counter confusion in the harness snapshot format required a mid-run fix; old snapshots have inflated counters. The final report should use only post-fix snapshots for counter aggregation.
- **Recommended follow-ups:** (1) Add alerting for Ohnics/Levellog source staleness, not just Kafka. (2) Investigate and classify the Fraunhofer "unknown" errors. (3) Consider lowering `FROST_BATCH_MAX_OBSERVATIONS` to avoid the 60s timeout spikes.

## Appendix A: Methodology

- **Data collection:** `app/services/validation/collector.py` writes a JSON line to `data/validation/snapshots.jsonl` every 5 minutes with per-source freshness, per-(source, target) push aggregates, circuit breaker state, DLQ file stats, and FROST worker stats.
- **Observation counting:** This report combines two data sources: (1) the `/validation/summary` API endpoint (correct cumulative totals since the last process start) for the Sep 14–21 period, and (2) the Sep 10–14 summary snapshot captured before the Sep 14 bugfix deploy. The formal 14-day report will use exclusively per-window delta snapshots (available from Sep 14 onward).
- **Latency percentiles:** Each 5-min window contributes p50/p95/max from a bounded reservoir of 1,000 samples per (source, target). Cross-window aggregation: median of window p50s, median of window p95s, max of window maxes.
- **Error taxonomy:** `app/services/validation/error_classifier.py`. Buckets: timeout, connection, dns, tls, auth_401, auth_403, malformed_4xx, rate_limit, server_5xx, serialization, circuit_open, unresolved_datastream, unknown.
- **Feature flag:** `VALIDATION_HARNESS_ENABLED=true`. With the flag off, all instrumentation is a no-op.
- **Storage:** `data/validation/{events,incidents,snapshots}.jsonl` on the Render persistent disk (1 GB). Events and incidents rotate at configured byte caps with `.1..5` backups.
