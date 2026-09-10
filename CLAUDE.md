# SensorThings Testbed Connector

## What this project is
FastAPI service that consumes sensor data from multiple sources (TGV Kafka, Ohnics, Levellog, bridge sensors) and pushes it to one or more OGC SensorThings FROST servers. Handles entity registration, observation posting, and multi-target fan-out.

## Relationship to main project
Standalone service that feeds real-time sensor data into FROST servers consumed by the monitoring_module and Geo-Insights-MVP. Shares no database with the other services — all state is in local JSON entity caches and Kafka offsets.

## Team
Same as main Geo Insights project:
- Mathis (CEO): full-stack development, product decisions, all coding day-to-day
- Iust Kuipers (CTO): technical lead, architecture owner

## Architecture
- `app/main.py` — FastAPI app with persistent Kafka consumer in background thread
- `app/config.py` — Settings from env vars, FROST target parsing, datastream map loading
- `app/routes/` — health and connector control endpoints
- `app/services/sensorthings_client.py` — SensorThings entity registration and observation posting
- `app/sta/models.py` — Pydantic models for OGC SensorThings entities (Sensor, Thing, Datastream, Observation, etc.)
- `app/frost/` — Per-target FROST stack: HTTP client, entity cache, entity manager, v2 adapter, circuit breaker
- `app/sources/` — Source-specific Kafka message mapping (TGV, bridge, Ohnics, Levellog)
- `data/` — Persistent entity caches (`registered_entities.json`, `entities_{target}.json`)
- `scripts/` — Utility scripts (catchup, discovery, demo reset)

## Stack
- Python 3.12 (Dockerfile: `python:3.12-slim`)
- FastAPI >=0.115,<1.0
- confluent-kafka[avro] (Kafka consumer with Avro schema registry)
- Pydantic 2.x (SensorThings entity models)
- requests (FROST HTTP client)
- paho-mqtt (MQTT bridge source)
- Deployed on Render (Docker runtime, persistent disk at /app/data)

## Key constraints
- **Multi-target FROST fan-out** — each target server gets its own TargetStack (HTTP client + cache + entity manager)
- **FROST v1.1 + v2.0** — v2 adapter rewrites payloads at the HTTP boundary; v1.1 servers require integer `@iot.id`
- **Dual datastream source (D9)** — `SENSORTHINGS_DATASTREAM_IDS_JSON` env var and `data/registered_entities.json` can both provide mappings; precedence is env var first, then cache
- **Entity caches are persistent** — stored on Render's persistent disk; losing them means re-registering all entities
- **Kafka consumer is single-threaded** — runs in a background thread, decoupled from FROST push via queue
- **Circuit breaker on FROST targets** — unreachable targets are temporarily bypassed, not retried indefinitely
- **Unit + name normalization at the source boundary** — `app/sta/canonical.py` is the single source of truth for observed-property names, UCUM unit symbols, and CF definition URLs. Every source mapper (`app/sources/*`, `app/pipeline/kafka_tgv.py`) resolves raw names through `canonical.resolve()` and reads unit + display name from `.meta`. Upstream units (e.g. Avro payload `unit` field) are logged when they disagree with canonical but never propagated; canonical always wins. Aligns with the backend-side enforcement position in [Geonovum discussion #24](https://github.com/Geonovum/testbed-sensordata-2026/discussions/24).

## Entry points
```bash
# Development
uvicorn app.main:app --host 0.0.0.0 --port 8010 --reload

# Production (Dockerfile CMD)
uvicorn app.main:app --host 0.0.0.0 --port 8010
```

## Verified commands
```bash
# Unit tests (315 tests, ~13s, excludes integration by default)
python -m pytest tests/ -q

# Integration tests (needs Docker: docker compose -f docker-compose.test.yaml up -d)
python -m pytest tests/ -q -m integration

# Lint
python -m ruff check app/ tests/ scripts/

# Lint auto-fix
python -m ruff check app/ tests/ scripts/ --fix

# Type-check (not configured yet — no mypy/pyright in the project)
# Syntax-check all app code
python -c "import ast; [ast.parse(open(f).read()) for f in __import__('glob').glob('app/**/*.py', recursive=True)]"
```

### Done-gate (verification gate for any task)
A task is done when **both** pass with zero errors:
```bash
python -m pytest tests/ -q && python -m ruff check app/ tests/ scripts/
```

## Parallel work
Multiple Claude Code sessions and `isolation: "worktree"` subagents can work on this repo simultaneously. Coordination rules:

1. **Claim before touching.** Before editing any file, add an entry to `TASK_LEDGER.md` with your task name, branch, and the files you will touch.
2. **No overlapping files.** Never edit a file already claimed by another open task. If you need to, coordinate with that task's owner first.
3. **Use worktree isolation for subagents.** Code-writing subagents must use `isolation: "worktree"` so they get their own branch and working copy.
4. **Main session stays on main.** Only the main interactive session works on `main`; all parallel work happens on feature branches.
5. **Run the done-gate before merging.** Every branch must pass `pytest + ruff check` before merging back to main.

## Validation harness (14-day Geonovum SLO test)

`app/services/validation/` instruments the ingest → push path so uptime, per-path latency (batch push ms + age-at-push s), error-rate broken out by taxonomy class, circuit-breaker activity, and DLQ activity can be reported at the end of a multi-day run. Off by default — flip `VALIDATION_HARNESS_ENABLED=true` in Render env vars to enable. When disabled, all hooks are cheap no-ops (zero cost on the ingest hot path).

- **Storage layout** — `data/validation/{events,incidents,snapshots}.jsonl` on the persistent disk. Events + incidents rotate at size caps (defaults 20 MB / 5 MB) with `.1..5` backups. Snapshots is one line per interval (default 5 min); at 5-min for 14 days that's ~4032 lines / ~20 MB.
- **HTTP surface** — `/validation/status` (enabled flag + file sizes), `/validation/summary` (in-memory rollup), `/validation/incidents?limit=N` (recent incidents).
- **Report** — `python scripts/generate_validation_report.py` (optional `--start`/`--end` ISO for windowing) reads the JSONL files and emits `reports/geonovum_14d_validation.md` + `data/validation/metrics.json`.
- **Smoke test** — add a synthetic bad target to `SENSORTHINGS_BASE_URLS` (URL that will never resolve, e.g. `https://smoke-fail.invalid`), let the harness run ~2 h, verify the report shows an uptime dip + circuit_open incident + DLQ growth for that host, then remove the synthetic entry before starting the real 14-day clock. Design memo: `reports/geonovum_14d_validation_deploy_memo.md`.

## Working autonomously
For unattended runs, use a concrete exit condition so the session doesn't run forever.

**Recommended invocation:**
```bash
claude --dangerously-skip-permissions \
  -p "TASK DESCRIPTION. Exit when done-gate passes (pytest + ruff clean)." \
  --max-turns 30
```

**Rules for autonomous sessions:**
- Always set `--max-turns` (30 is a good default; raise to 50 for larger tasks).
- The task prompt must include the done-gate command and an explicit exit instruction.
- Claim your task in `TASK_LEDGER.md` at the start; mark it done at the end.
- Do not push to remote unless the task prompt explicitly says to.

## Skills
See ../gi-skills/skills/connector-review/SKILL.md
See ../gi-skills/skills/ogc-check/SKILL.md
See ../gi-skills/skills/security-check/SKILL.md

## Workflow skills
See ../gi-skills/skills/office-hours/SKILL.md
See ../gi-skills/skills/spec/SKILL.md
See ../gi-skills/skills/impact/SKILL.md
See ../gi-skills/skills/plan/SKILL.md
See ../gi-skills/skills/review/SKILL.md
See ../gi-skills/skills/preflight/SKILL.md
See ../gi-skills/skills/ship/SKILL.md
See ../gi-skills/skills/retro/SKILL.md
See ../gi-skills/skills/learn/SKILL.md

## Global workflow skills (gstack)
The following slash commands are available globally via gstack (`~/.claude/skills/gstack/`):
- `/spec` — turn vague intent into a precise, executable spec
- `/plan` — plan implementation strategy before coding
- `/review` — pre-landing PR review
- `/ship` — detect base branch, run tests, review diff, create PR
- `/retro` — weekly engineering retrospective
- `/office-hours` — YC-style office hours
- `/qa` — systematically QA test a web app and fix bugs found
- `/investigate` — systematic debugging with root cause investigation
- `/health` — code quality dashboard
- `/diagram` — generate diagrams from English or Mermaid source
- `/learn` — manage project learnings
