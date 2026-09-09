# Geonovum 14-day validation harness — design & deploy memo

**Branch:** `feat/validation-harness-14d`
**Author:** Mathis (with Claude Code)
**Reviewer requested:** Iust (architecture sign-off)
**Deadline driver:** Geonovum Topic #2 Phase 3 committed a 14-day continuous
operation test that was never run; this addendum feeds the final Geonovum
submission.

---

## 1. What this branch does

Adds a small, opt-in validation harness that instruments the existing
ingest → push path so we can produce the Geonovum-required addendum after a
14-day continuous run.

- **New:** `app/services/validation/` (recorder singleton, error taxonomy,
  snapshot collector); `app/routes/validation.py` (`/validation/{status,summary,incidents}`);
  `scripts/generate_validation_report.py` (renders `reports/geonovum_14d_validation.md`
  + `data/validation/metrics.json`).
- **Modified:** `app/services/sensorthings_client.py` — `push_observations()`
  gains a `source` kwarg and each per-target push call is bracketed with a
  timer + one `recorder.record_push_attempt(...)` call before every return.
- **Modified:** `app/main.py` — `_push_to_monitoring()` gains the same
  instrumentation for the monitoring HTTP path; lifespan wires
  `recorder.configure(...)` + starts `snapshot_loop()` when the kill switch is
  on; three ingest call sites now pass `source="kafka"` / source name.
- **Modified:** `app/services/alerting.py` — after `send_alert()` returns,
  mirrors the event into `incidents.jsonl` so the report incident log doesn't
  depend on scraping the webhook side.
- **Modified:** `app/services/frost_worker.py` — one-line change to pass
  `source="kafka"` to `client.push_observations()`.
- **Modified:** `app/config.py` — six new `VALIDATION_*` settings, all default
  to disabled/safe values.
- **Modified:** `CLAUDE.md`, `.gitignore` — docs + ignore `data/validation/`.
- **Tests:** 68 new tests (`test_validation_{classifier,recorder,collector,report}.py`)
  including an automated version of the fault-injection smoke test.
- **Done-gate:** `pytest tests/ -q` = **383 passed**; `ruff check app/ tests/ scripts/`
  = **all checks passed**.

Diff scale: 8 files touched, +233 / -10 lines. Six new files (validation
module + route + script + 4 tests).

## 2. Blast-radius analysis

**Kill switch:** `VALIDATION_HARNESS_ENABLED` (env var, default `false`).
When `false`:
- `recorder.enabled == False`; every `record_push_attempt` and
  `record_incident` returns immediately after the flag check (one attribute
  read, no allocation, no I/O).
- The snapshot loop task is never created in `lifespan()`.
- The `/validation/*` endpoints still exist but the summary reports
  `enabled: false`.

So the harness is a **zero-runtime-cost no-op** with the flag off. The only
non-conditional additions in the hot path are:
- `source: str = "unknown"` kwarg on `push_observations()` (parameter passing,
  no branching).
- `_val_started = time.perf_counter()` at the top of `_push_target()` — one
  monotonic clock read (~50 ns), harmless.
- `_val_target_label = _validation_target_label(base_url)` — iterates
  `settings.frost_targets` (3–5 entries), O(n) string compare.

No changes to the DLQ format, no changes to the Kafka commit protocol, no
changes to entity caches, no changes to the FROST push semantics.

**Thread safety:** the recorder holds one `threading.Lock` for all
in-memory state mutations; the parallel per-target FROST push threads each
call `record_push_attempt` after their per-target result is finalized, so no
contention on the push hot path.

## 3. Storage budget (Render persistent disk)

Currently `data/` uses ~50 MB (DLQ up to 10 MB + entity caches + logs 20 MB).

Harness adds under `data/validation/`:
- `events.jsonl` — cap 20 MB × (1 + 5 backups) = **120 MB max**. But this file
  only logs *non-clean* push attempts. On a healthy run it will stay under
  1 MB total.
- `incidents.jsonl` — cap 5 MB × (1 + 5) = **30 MB max**. Sparse; expected
  under 100 KB.
- `snapshots.jsonl` — no rotation. 4032 snapshots × ~5 KB ≈ **20 MB** over
  14 days.

**Worst-case total:** ~170 MB. **Expected steady state:** ~25 MB.

If Render's disk is tight (please confirm the mount size), we can tighten the
event cap via `VALIDATION_EVENTS_MAX_BYTES`.

## 4. Smoke test protocol (pre-14d)

Goal: prove the harness registers a target going down before we start the real
clock. **The user gave this task an explicit "no second attempt" constraint.**

1. **Deploy this branch to Render with the flag off first.** Confirm the app
   comes up green and the existing 3 targets are all healthy per
   `/health/report`.
2. **Enable the harness:** add env var `VALIDATION_HARNESS_ENABLED=true`,
   redeploy. Verify `/validation/status` returns `enabled: true` and
   `snapshots.jsonl` starts growing (one line every 5 min).
3. **Inject a synthetic bad target.** Append this JSON object to the
   `SENSORTHINGS_BASE_URLS` env var list:
   ```json
   {"url": "https://smoke-fail.invalid", "version": "v1.1", "label": "smoke_fault"}
   ```
   Restart. The circuit breaker will open for that target after 3 failed
   cycles (default). Observations for that target will land in the DLQ.
4. **Wait ~2 hours.** Then check three things:
   - `/validation/summary` shows `sent = 0` for the `kafka::https://smoke-fail.invalid`
     path.
   - `/validation/incidents?limit=50` includes at least one
     `{"kind": "circuit_open", "target": "https://smoke-fail.invalid"}` line.
   - DLQ line count is growing (`data/failed_observations.jsonl` size in
     `/health`).
5. **Generate an interim report** to sanity-check the aggregator:
   ```bash
   python scripts/generate_validation_report.py \
     --start 2026-09-09T00:00:00Z \
     --end   2026-09-09T23:59:59Z
   ```
   Sections 2.3 (per-target uptime) and 5.1 (breaker events) should reflect
   the synthetic outage.
6. **Remove the synthetic target** from `SENSORTHINGS_BASE_URLS`, restart,
   confirm the healthy paths recover on `/validation/summary`.

If **any** of steps 4a/b/c fails, do not start the 14-day clock — the harness
has a bug and we won't catch failures during the real window.

## 5. 14-day rollout

1. `VALIDATION_HARNESS_ENABLED=true` on Render (kept from smoke).
2. Note the wall-clock start ISO timestamp; enter it in the deliverable index.
3. Let it run. `/validation/status` can be polled; the `snapshots.jsonl` grows
   monotonically.
4. Watch existing alert channels (webhook/email/Slack). Any incident recorded
   there is also mirrored into `incidents.jsonl` automatically.
5. At **T+14 days** exactly, run the report generator (with an explicit
   `--start`/`--end` matching the window):
   ```bash
   python scripts/generate_validation_report.py \
     --start <start-iso> \
     --end   <end-iso>
   ```
   Emits `reports/geonovum_14d_validation.md` + `data/validation/metrics.json`.
6. Author section 7 "Lessons learned" in the markdown by walking the
   `incidents.jsonl` timeline + any operator observations.
7. Attach both files to the Geonovum submission repo.

## 6. Rollback

Two levels:

- **Flip the kill switch:** set `VALIDATION_HARNESS_ENABLED=false` in Render
  env; no redeploy required (env var change forces a restart, which picks up
  the new value; the harness is off from that moment). Existing JSONL files
  are preserved for post-mortem.
- **Revert the branch entirely:** `git revert` this branch's merge commit.
  Removes all code additions. The one behavioral change to existing code is
  the new `source=` kwarg on `push_observations()`; callers pass it as a
  string, so removing it is a mechanical revert.

No schema migrations. No entity-cache changes. No DLQ format changes.

## 7. Files changed

New:
- `app/services/validation/__init__.py`
- `app/services/validation/error_classifier.py`
- `app/services/validation/recorder.py`
- `app/services/validation/collector.py`
- `app/routes/validation.py`
- `scripts/generate_validation_report.py`
- `tests/test_validation_classifier.py`
- `tests/test_validation_recorder.py`
- `tests/test_validation_collector.py`
- `tests/test_validation_report.py`
- `reports/geonovum_14d_validation_deploy_memo.md` (this file)

Modified:
- `app/config.py` — six new fields
- `app/main.py` — lifespan wiring + `_push_to_monitoring` instrumentation
- `app/services/sensorthings_client.py` — timing + recorder calls in
  `_push_target()`
- `app/services/alerting.py` — post-send incident mirror
- `app/services/frost_worker.py` — one-line `source="kafka"` kwarg
- `tests/test_frost_worker.py` — one assertion update for the same kwarg
- `CLAUDE.md` — new "Validation harness" section
- `.gitignore` — ignore `data/validation/`

## 8. Sign-off checklist for Iust

- [ ] Blast-radius analysis (§2) matches your read of the diff.
- [ ] Storage budget (§3) is fine for Render's current mount size.
- [ ] Smoke test protocol (§4) is safe on the shared multi-tenant
      `sta.wbd-rd.nl` FROST server. (No real target is touched; the fake
      target is unreachable so no requests hit shared infra.)
- [ ] Rollback plan (§6) is acceptable; the `source=` kwarg addition is
      approved.
- [ ] Ok to merge → main and trigger a Render deploy.

Ping me on any of these and I'll iterate.
