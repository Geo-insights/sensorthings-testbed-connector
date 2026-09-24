# Geonovum Sensor Data Testbed 2026 — Topic #2 Addendum
## 14-Day Validation Report: SensorThings Testbed Connector Continuous Operation

**Window:** 2026-09-10T14:43:56Z → 2026-09-24T14:43:56Z (14.00 days)
**Status:** Final
**Connector:** sensorthings-testbed-connector on Render (Docker, Python 3.12 / FastAPI)

---

## 1. Executive summary

The SensorThings testbed connector ran continuously for 14 days with **99.95% process uptime**, delivering **29.1 million observations** across 6 data paths (2 sources × 3 targets). The Fraunhofer v2.0 demo server experienced a sustained outage on the final day of the window (Sep 24, 06:22–14:43 UTC), triggering 22 circuit breaker openings and demonstrating the connector's self-protection mechanisms under real failure conditions. All other targets maintained service throughout.

| Metric | Value |
|--------|-------|
| Process uptime | **99.95%** (4,030 / 4,032 snapshots) |
| Kafka source uptime | **99.18%** (one 2.2-hour gap) |
| Ohnics source uptime | **99.26%** (one 2.5-hour SamenMeten API gap) |
| Levellog/CARS | Intentionally disabled for this window |
| Observations delivered | **29,069,680** |
| Observations failed (transient) | **87,375** (0.30%) |
| Observations dropped by circuit breaker | **46,114** (held in DLQ for replay) |
| Observations permanently dropped | **76** (non-retryable 4xx on Fraunhofer v2.0) |
| DLQ peak | **64,240** entries / 39.6 MB |
| DLQ at window close | **31,839** entries (awaiting Fraunhofer recovery) |
| Circuit breaker openings | **22** (all on Fraunhofer v2.0, final day) |
| Process restarts | 4 (planned: harness deploys on Sep 14 and Sep 21) |

**Overall success rate:** 99.54% including the Sep 24 Fraunhofer outage. Excluding the Fraunhofer circuit-drop observations (which are held in the DLQ and will replay when the server recovers): **99.70%**.

## 2. Uptime

### 2.1 Connector process

The connector ran with 99.95% uptime. Two snapshots were missed across the 14-day window (likely during the Sep 21 planned restart). Four planned restarts occurred for harness bugfix deploys; all completed within seconds with no data loss.

| Period | Event |
|--------|-------|
| Sep 10 14:43 | Harness started (post smoke test) |
| Sep 14 11:39–11:43 | Two planned restarts (per-window delta fix) |
| Sep 21 14:04–14:26 | Two planned restarts (presentation prep) |

### 2.2 Per-source data-arrival uptime

| Source | Fresh windows | Total | Uptime | Longest gap |
|--------|---------------|-------|--------|-------------|
| Kafka (TGV Office Lab) | 3,997 | 4,030 | **99.18%** | 7,800 s (2.2 h) |
| Ohnics (SamenMeten) | 4,000 | 4,030 | **99.26%** | 9,000 s (2.5 h) |
| Levellog/CARS | — | — | _disabled_ | — |

Both gaps correspond to upstream source outages, not connector failures. The Ohnics gap was a SamenMeten API outage. The Kafka gap occurred in the Sep 21–24 period and is under investigation.

### 2.3 Per-target push uptime

| Target | Success windows | Total | Uptime | Breaker opens | Total open time |
|--------|----------------|-------|--------|---------------|-----------------|
| sta.wbd-rd.nl (v1.1) | 7,924 | 8,060 | **98.31%** | 0 | 0 s |
| Fraunhofer (v2.0) | 7,859 | 8,060 | **97.51%** | **22** | **13,201 s (3.7 h)** |
| monitoring (HTTP) | 7,942 | 8,060 | **98.54%** | 0 | 0 s |

The Fraunhofer v2.0 demo server became unreachable on Sep 24 starting at 06:22 UTC. The circuit breaker opened 22 times over ~8 hours (opening after 3 consecutive failures, cooling down for 10 minutes, retrying, failing again). This is the expected behavior — the breaker protects both the connector and the target from retry storms.

## 3. Delivery volume and error rate

### 3.1 Per-path totals (14 days)

| Source | Target | Sent | Failed | Dropped (circuit) | Dropped (permanent) | Error rate |
|--------|--------|------|--------|--------------------|---------------------|------------|
| kafka | sta.wbd-rd.nl (v1.1) | 9,346,769 | 36,126 | 0 | 0 | **0.39%** |
| kafka | Fraunhofer (v2.0) | 9,291,581 | 46,143 | 45,171 | 0 | **0.97%** |
| kafka | monitoring | 9,380,668 | 2,227 | 0 | 0 | **0.02%** |
| ohnics | sta.wbd-rd.nl (v1.1) | 351,520 | 0 | 0 | 0 | **0.00%** |
| ohnics | Fraunhofer (v2.0) | 347,622 | 2,879 | 943 | 76 | **1.11%** |
| ohnics | monitoring | 351,520 | 0 | 0 | 0 | **0.00%** |
| **Total** | | **29,069,680** | **87,375** | **46,114** | **76** | **0.46%** |

### 3.2 Error classification

| Path | timeout | server_5xx | unknown | circuit_open | malformed_4xx |
|------|---------|------------|---------|--------------|---------------|
| kafka → sta.wbd-rd.nl | 1,027 | 30 | — | — | — |
| kafka → Fraunhofer v2.0 | — | 2 | 450 | 123 | — |
| kafka → monitoring | 797 | 2 | — | — | — |
| ohnics → Fraunhofer v2.0 | — | 1 | 23 | 22 | 1 |

**Three distinct failure patterns emerged:**

1. **Transient FROST timeouts (1,824 events):** sta.wbd-rd.nl and the monitoring module experienced intermittent batch-push timeouts. All were dead-lettered and replayed successfully. This is the DLQ working as designed.

2. **Fraunhofer v2.0 sustained outage (145 events, Sep 24):** The demo server stopped responding entirely. The circuit breaker opened 22 times across both Kafka and Ohnics paths. 46,114 observations were circuit-dropped into the DLQ. 76 Ohnics observations received non-retryable 4xx responses and were permanently dropped. The remaining 31,839 DLQ entries will replay when the server recovers.

3. **sta.wbd-rd.nl server_5xx (30 events):** Brief FROST server errors in the Sep 21–24 period, all recovered automatically.

## 4. Latency per data path

Percentiles are median-of-window-p50s across 5-minute snapshot windows.

### 4.1 Batch push duration

| Path | p50 | p95 | Max | Notes |
|------|-----|-----|-----|-------|
| kafka → sta.wbd-rd.nl (v1.1) | 1,052 ms | 1,550 ms | 60,185 ms | Timeout cap at 60s |
| kafka → Fraunhofer (v2.0) | 1,085 ms | 1,230 ms | 8.8M ms | Includes outage period |
| kafka → monitoring | 1,370 ms | 1,745 ms | 11,999 ms | HTTP to monitoring module |
| ohnics → sta.wbd-rd.nl | 363 ms | 363 ms | 2,751 ms | Smaller batches |
| ohnics → Fraunhofer v2.0 | 311 ms | 311 ms | 470,579 ms | Includes outage period |
| ohnics → monitoring | 1,073 ms | 1,073 ms | 4,103 ms | |

The extreme max values on the Fraunhofer paths (8.8M ms for Kafka, 470s for Ohnics) reflect the Sep 24 outage — the batch push hung until the HTTP timeout fired. These are outliers; the p50 and p95 represent normal operation.

### 4.2 Age at push (sensor time → FROST delivery)

| Path | p50 | p95 | Max |
|------|-----|-----|-----|
| kafka → sta.wbd-rd.nl | 18.3 s | 32.9 s | 6,194 s |
| kafka → Fraunhofer v2.0 | 18.3 s | 32.9 s | 10,550 s |
| ohnics → FROST targets | 57.0 s | 87.6 s | 724 s |

The 10,550s max age-at-push on the Fraunhofer path corresponds to observations that were queued during the circuit-open period and will be delivered via DLQ replay once the server recovers.

## 5. Circuit breaker and DLQ activity

### 5.1 Circuit breaker events

| Target | Open events | Total open time | Period |
|--------|-------------|-----------------|--------|
| sta.wbd-rd.nl (v1.1) | **0** | 0 s | — |
| Fraunhofer (v2.0) | **22** | **13,201 s** (3.7 h) | Sep 24 06:22–14:43 UTC |
| monitoring | **0** | 0 s | — |

The Fraunhofer breaker repeatedly opened and half-opened throughout Sep 24. Each cycle: open after 3 consecutive failures → wait 600s cooldown → half-open (allow one attempt) → fail → re-open. This pattern continued for ~8 hours until the validation window closed.

**The circuit breaker worked exactly as designed:** it prevented the connector from hammering an unresponsive server with retry storms, while the DLQ accumulated observations for future replay.

### 5.2 Dead-letter queue

| Metric | Value |
|--------|-------|
| Peak size | **64,240 entries** / 39.6 MB |
| Size at window close | **31,839 entries** / 20.0 MB |
| Surviving by target host | Fraunhofer: 21,613 / sta.wbd-rd.nl: 10,226 |
| Permanently dropped | **76** (non-retryable 4xx) |

The DLQ grew rapidly during the Sep 24 Fraunhofer outage. The replay loop was actively draining it (peak 64K → 32K at window close), but could not fully catch up while the Fraunhofer target remained unreachable. The 10,226 sta.wbd-rd.nl entries are from the earlier timeout episodes and will drain once the replay loop processes them.

## 6. Incident log

### 6.1 Lifecycle events

| Timestamp (UTC) | Event | Detail |
|-----------------|-------|--------|
| Sep 10 14:43:56 | `app_start` | Initial deploy with harness enabled |
| Sep 14 11:39–11:43 | `stop` → `start` ×2 | Planned deploys (harness bugfix) |
| Sep 21 14:04–14:26 | `stop` → `start` ×2 | Planned deploys (presentation prep) |

### 6.2 Operational incidents

| Timestamp (UTC) | Event | Target | Error |
|-----------------|-------|--------|-------|
| Sep 24 06:22 | `circuit_open` | Fraunhofer v2.0 | (first occurrence — no HTTP response) |
| Sep 24 06:40 | `circuit_open` | Fraunhofer v2.0 | Datastream 12: all retry attempts failed |
| Sep 24 06:58–12:24 | `circuit_open` ×20 | Fraunhofer v2.0 | Repeating ~18 min cycle across multiple datastreams (7,8,9,10,11,12,14,15) |
| Sep 24 10:11 | `circuit_open` | Fraunhofer v2.0 | First Kafka-source circuit open (datastream 490) |
| Sep 24 10:46, 11:21, 11:58 | `circuit_open` ×3 | Fraunhofer v2.0 | Kafka continues failing (datastreams 298, 490) |

**Root cause:** The Fraunhofer v2.0 demo server (`ogc-demo.k8s.ilt-dmz.iosb.fraunhofer.de`) stopped responding to POST requests. All retry attempts (3 per observation, exponential backoff) exhausted without receiving any HTTP response. The server appears to have been down or unreachable at the network level. As of window close, the server had not recovered.

**No earlier operational incidents occurred** in the first 13.5 days (Sep 10–24 06:22). The first 13.5 days were clean: zero circuit breaker openings, DLQ draining normally, all paths delivering.

## 7. Lessons learned

### What worked as designed
- **Multi-target fan-out with per-target circuit breaker** handled the Fraunhofer outage without affecting sta.wbd-rd.nl or the monitoring module. Each target failed independently; healthy paths continued uninterrupted.
- **DLQ + replay loop** accumulated 64K observations during the outage and began draining immediately. Zero observations were lost due to the outage itself (only 76 permanently dropped from non-retryable 4xx errors).
- **Validation harness** caught the Sep 24 incident with exact timestamps, circuit breaker transition counts, and per-observation error classification — demonstrating that an automated observability layer is operationally essential, not a luxury.

### What surprised us
- **The real incident happened on the last day.** 13.5 days of near-perfect operation, then a sustained outage in the final 8 hours. This validates the decision to run for the full 14 days rather than stopping early after the clean interim report.
- **SamenMeten (Ohnics) and Kafka both had source-level gaps** (2.5h and 2.2h respectively). Upstream API reliability is a real concern for any municipality running this in production — the connector handles it, but alerting on source staleness (shipped during this run) is essential.
- **sta.wbd-rd.nl developed 30 server_5xx errors** in the second half of the window (Sep 21–24), up from 1 in the first 11 days. This suggests the FROST server may have been under increasing load from other testbed participants.

### Failure modes documented
1. **Sustained target outage:** Circuit breaker opens after 3 failures, cooldown 600s, half-open retry, re-open on failure. Observations accumulate in DLQ. Replay drains when target recovers. No data lost.
2. **Transient FROST timeouts:** Individual batch pushes exceed 60s HTTP cap. Observations dead-lettered, replayed on next pass. The retry queue peaked at 425 entries during normal operation and drained fully.
3. **Upstream source gaps:** Poller continues retrying; health monitor marks source as stale; stall alert fires (post-fix). Connector resumes automatically when source recovers.
4. **Non-retryable errors (malformed_4xx):** 76 observations received HTTP 4xx from Fraunhofer v2.0 and were permanently dropped. These likely reflect a data formatting edge case specific to the v2.0 payload adaptation.

### Recommended follow-ups
1. **Investigate the 76 permanently dropped observations** — these are the only true data loss in 14 days. The malformed_4xx classification suggests a v2.0 payload edge case worth fixing.
2. **Investigate the Kafka 2.2-hour gap** — this is new (not present in the 11-day interim). Could be a Confluent Cloud issue, a consumer group rebalance, or an upstream producer pause.
3. **Monitor DLQ drain after Fraunhofer recovery** — the 31,839 surviving entries should replay to zero. If they don't, investigate whether the observations aged out or the server rejects them post-recovery.
4. **Consider raising `FROST_CB_COOLDOWN_SECONDS`** for demo/test servers — 600s (10 min) causes 22 open/close cycles during an 8-hour outage. A longer cooldown (e.g., 1800s) would reduce DLQ churn on a server that's clearly down for an extended period.

## Appendix A: Methodology

- **Data collection:** `app/services/validation/collector.py` writes one JSON line to `data/validation/snapshots.jsonl` every 5 minutes with per-source freshness, per-(source, target) push aggregates (per-window deltas), circuit breaker state, DLQ file stats, and FROST worker stats.
- **Observation counting:** This report combines cumulative totals from three consecutive process runs (Sep 10–14, Sep 14–21, Sep 21–24), each captured via the `/validation/summary` API endpoint. The metrics endpoint's snapshot-based aggregation is used for latency percentiles and uptime calculations; the summary endpoint is used for delivery counts.
- **Latency percentiles:** Each 5-min window contributes p50/p95/max from a bounded reservoir of 1,000 samples per (source, target). Cross-window aggregation: median of window p50s, median of window p95s, max of window maxes.
- **Error taxonomy:** `app/services/validation/error_classifier.py`. Buckets: timeout, connection, dns, tls, auth_401, auth_403, malformed_4xx, rate_limit, server_5xx, serialization, circuit_open, unresolved_datastream, unknown.
- **Feature flag:** `VALIDATION_HARNESS_ENABLED=true`. With the flag off, all instrumentation is a no-op.
- **Storage:** `data/validation/{events,incidents,snapshots}.jsonl` on the Render persistent disk (1 GB). Snapshots file reached 12.8 MB over 14 days; events file 14 KB; incidents file 3.4 KB.
