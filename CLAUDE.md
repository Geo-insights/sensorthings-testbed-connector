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

# Tests
pytest tests/ -q
```

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
