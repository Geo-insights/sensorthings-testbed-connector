# SensorThings Testbed Connector

This repository contains the FastAPI connector used for the Geonovum Sensor Data Testbed 2026. It ingests climate-adaptation sensor data from multiple sources and pushes observations to one or more OGC SensorThings API FROST servers.

The current focus is the integration of two real pilot locations:

- The Green Village, TU Delft campus in Delft
- Diergaarde Blijdorp in Rotterdam

## Project Purpose

The connector translates site-specific readings into SensorThings entities and observations:

- it models each installation as a Thing with its own Location
- it registers Sensors, ObservedProperties, Datastreams, and Observations in FROST
- it fans out observations to multiple FROST targets (v1.1 and v2.0) with per-target auth
- it keeps a dead-letter queue for failed observations so the stream can be replayed later
- it runs background polling loops for Kafka, Ohnics, and Levellog data sources
- it forwards observations to the UrbanAdapt Monitoring Module (direct HTTP push and MQTT)
- it exposes status checks for verifying connectivity before live testing

## Data Sources

| Source | Protocol | Description |
| --- | --- | --- |
| Kafka TGV | Confluent Cloud Kafka (Avro) | Indoor climate from TGV Office Lab (temperature, humidity, CO2, pressure) |
| Ohnics | REST polling (JSON) | Outdoor air quality in Delft, SamenMeten network (PM2.5, temperature) |
| Levellog / CARS | REST polling (OData + OAuth2) | Groundwater levels at TGV installations |
| Bridge payload | JSON file or HTTP POST | Generic readings from Node-RED or other sources |
| Demo | In-memory | Synthetic readings for preview mode |

## Sites In Scope

### The Green Village, TU Delft, Delft

Active setups: TGV Office Lab (indoor climate via Kafka), Climate Davis (outdoor weather station), Weather Climatics (rooftop air pressure), Hitteplein (solar radiation and humidity). Additional setups planned: Rainaway Parkeerplaatsen, Flowsand, Koers / Zoak / Nus.

### Diergaarde Blijdorp, Rotterdam

Planned setups include microclimate sensors for animal enclosures, a weather station, Klimaatplein / Bufferblocks, and pond water quality monitoring.

The entity model is documented in [docs/entity-mapping.md](docs/entity-mapping.md).

## Quick Start

```powershell
py -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8010
```

After startup, open:

- <http://127.0.0.1:8010/health>
- <http://127.0.0.1:8010/connector/preview>
- <http://127.0.0.1:8010/connector/registration-preview>
- <http://127.0.0.1:8010/frost/status>
- <http://127.0.0.1:8010/docs>

## Endpoints

### Health and status

| Endpoint | Purpose |
| --- | --- |
| GET / | Root message confirming the connector is running |
| GET /health | Basic health check |
| GET /health/report | Full health monitor summary (JSON) |
| GET /health/report/html | HTML rendering of health data |
| GET /frost/status | Check FROST connectivity, response time, and Thing count |
| GET /frost/capabilities | Report the FROST root collections, conformance, and detected extensions (Projects, Tasking, OpenCitySense) |
| GET /connector/push-health | Circuit-breaker state per FROST target |

### Registration

| Endpoint | Purpose |
| --- | --- |
| GET /connector/registration-preview | Preview the SensorThings registration payloads |
| GET /connector/registration-preview/{site_key} | Preview registration payloads for one site (`tgv` or `blijdorp`) |
| POST /connector/register-demo | Register the default climate-adaptation entity sets |
| POST /connector/register-site/{site_key} | Register one site at a time (`tgv` or `blijdorp`) |
| POST /connector/tasking/register-site/{site_key} | Register per-site Actuators and TaskingCapabilities |

### Observation push

| Endpoint | Purpose |
| --- | --- |
| GET /connector/preview | Preview bridge payload observations, falling back to demo data |
| POST /connector/ingest-preview | Validate custom bridge payloads without posting them |
| POST /connector/ingest | Push supplied readings to the configured SensorThings server |
| POST /connector/push | Push bridge payload readings, falling back to demo data |
| POST /connector/kafka-push | Manual Kafka consume-and-push cycle (requires `KAFKA_TGV_ENABLED=true`) |
| POST /connector/ohnics-push | Manual Ohnics fetch-and-push cycle (requires `OHNICS_ENABLED=true`) |
| POST /connector/levellog-push | Manual Levellog fetch-and-push cycle (requires `LEVELLOG_ENABLED=true`) |
| POST /connector/replay-failed | Replay observations stored in the dead-letter queue |

### Monitoring bridge

| Endpoint | Purpose |
| --- | --- |
| GET /connector/monitoring-mqtt-preview | Resolve bridge readings into Monitoring MQTT payloads |
| POST /connector/monitoring-mqtt-preview | Preview Monitoring MQTT payloads for custom bridge readings |
| POST /connector/monitoring-mqtt-push | Publish bridge readings to Monitoring via MQTT |

### Tasking

| Endpoint | Purpose |
| --- | --- |
| POST /connector/tasking/tasks | Create a Task for a per-site capability |
| GET /connector/tasking/tasks | List Tasks for a site, optionally filtered by capability key |

### Sensor metadata and diagnostics

| Endpoint | Purpose |
| --- | --- |
| GET /connector/sensors/{sensor_id} | Get sensor config, properties, and FROST registration ID |
| POST /connector/sensors/{sensor_id}/image | Upload an installation photo for a sensor |
| GET /connector/ohnics-diag | Diagnostic: count Ohnics datastreams, check duplicates, test DELETE |

## Configuration

Copy `.env.example` to `.env` and adjust the values for the live testbed environment.

### Core SensorThings

| Variable | Purpose |
| --- | --- |
| `SENSORTHINGS_BASE_URL` | Primary FROST root URL, including `/v1.1` |
| `SENSORTHINGS_BASE_URLS` | Optional comma-separated list or JSON array of FROST root URLs for multi-target fan-out; supports JSON objects with `version`, `label`, and per-target auth |
| `SENSORTHINGS_THINGS_PATH` | Thing collection path, default `/Things` |
| `SENSORTHINGS_LOCATIONS_PATH` | Location collection path, default `/Locations` |
| `SENSORTHINGS_SENSORS_PATH` | Sensor collection path, default `/Sensors` |
| `SENSORTHINGS_OBSERVED_PROPERTIES_PATH` | ObservedProperty collection path, default `/ObservedProperties` |
| `SENSORTHINGS_DATASTREAMS_PATH` | Datastream collection path, default `/Datastreams` |
| `SENSORTHINGS_OBSERVATIONS_PATH` | Observation collection path, default `/Observations` |
| `SENSORTHINGS_PROJECTS_PATH` | Projects extension collection path, default `/Projects` |
| `SENSORTHINGS_ACTUATORS_PATH` | Tasking Actuator collection path, default `/Actuators` |
| `SENSORTHINGS_TASKING_CAPABILITIES_PATH` | TaskingCapability collection path, default `/TaskingCapabilities` |
| `SENSORTHINGS_TASKS_PATH` | Tasks collection path, default `/Tasks` |
| `SENSORTHINGS_ENTITY_NAME_PREFIX` | Optional prefix applied to Thing, Location, Sensor, and Datastream names (e.g. `GEO_`) |
| `SENSORTHINGS_DATASTREAM_IDS_JSON` | Optional JSON map for preloaded Datastream IDs |
| `SENSORTHINGS_REGISTERED_ENTITIES_PATH` | Path for cached `@iot.id` mappings, default `data/registered_entities.json` |
| `SENSORTHINGS_FAILED_OBSERVATIONS_PATH` | Path for the dead-letter queue, default `data/failed_observations.jsonl` |
| `SENSORTHINGS_REQUEST_TIMEOUT_SECONDS` | HTTP timeout for FROST requests, default `15` |
| `SENSOR_CONFIG_DIR` | Directory for YAML site configs, default `config/sites` |

### Authentication

| Variable | Purpose |
| --- | --- |
| `SENSORTHINGS_AUTH_TOKEN` | Optional bearer token for authenticated FROST access |
| `SENSORTHINGS_AUTH_USERNAME` | Optional username for HTTP Basic auth |
| `SENSORTHINGS_AUTH_PASSWORD` | Optional password for HTTP Basic auth |

For the WBD-RD FROST environment, use `SENSORTHINGS_AUTH_USERNAME` and `SENSORTHINGS_AUTH_PASSWORD`; the connector will send HTTP Basic auth automatically. Per-target auth can be set in the JSON objects of `SENSORTHINGS_BASE_URLS`.

### Projects and tasking

| Variable | Purpose |
| --- | --- |
| `SENSORTHINGS_DEFAULT_PROJECT_NAME` | Optional Project name; when set, Things, Locations, and Sensors are linked to this Project on registration |
| `SENSORTHINGS_DEFAULT_PROJECT_ID` | Optional existing Project `@iot.id` to link to instead of creating one by name |
| `SENSORTHINGS_DEFAULT_PROJECT_DESCRIPTION` | Optional description used when creating the Project |
| `SENSORTHINGS_DEFAULT_PROJECT_PUBLIC` | Whether a created Project is public, default `true` |
| `SENSORTHINGS_SITE_PROJECTS_JSON` | Optional JSON map with per-site Project config |
| `SENSORTHINGS_SITE_TASKING_JSON` | Optional JSON map for per-site Tasking Actuator/Capability templates |
| `SENSORTHINGS_TASKING_ALLOWED_COMMANDS` | Optional comma-separated list or JSON array of allowed capability keys for task creation |

### Bridge / connector

| Variable | Purpose |
| --- | --- |
| `CONNECTOR_NAME` | Friendly service name shown by FastAPI |
| `CONNECTOR_BRIDGE_PAYLOAD_PATH` | Optional JSON payload file used by `/connector/preview` and `/connector/push` |
| `PUBLIC_BASE_URL` | Public-facing URL for sensor image links, default `http://localhost:8000` |
| `DEBUG` | Enable debug mode |

### Kafka TGV source

| Variable | Purpose |
| --- | --- |
| `KAFKA_TGV_ENABLED` | Enable Kafka TGV consumer, default `false` |
| `KAFKA_TGV_BOOTSTRAP_SERVERS` | Confluent Cloud bootstrap server |
| `KAFKA_TGV_API_KEY` | Confluent Cloud API key |
| `KAFKA_TGV_API_PASSWORD` | Confluent Cloud API secret |
| `KAFKA_TGV_SCHEMA_REGISTRY_URL` | Avro Schema Registry URL |
| `KAFKA_TGV_SCHEMA_REGISTRY_USERNAME` | Schema Registry API key |
| `KAFKA_TGV_SCHEMA_REGISTRY_PASSWORD` | Schema Registry API secret |
| `KAFKA_TGV_CONSUMER_GROUP` | Kafka consumer group ID |
| `KAFKA_TGV_CLIENT_ID_CONSUMER` | Kafka client ID |
| `KAFKA_TGV_TOPIC` | Kafka topic name, default `tud_gv_officelab-climate` |
| `KAFKA_TGV_POLL_SECONDS` | Background poll interval, default `300` |
| `KAFKA_TGV_DEVICE_MAPPING_JSON` | Optional JSON override for measurement_id to FROST entity mapping |

### Ohnics air quality source

| Variable | Purpose |
| --- | --- |
| `OHNICS_ENABLED` | Enable Ohnics polling, default `false` |
| `OHNICS_API_URL` | Ohnics 5min.json endpoint |
| `OHNICS_POLL_SECONDS` | Poll interval, default `300` |
| `OHNICS_SENSOR_PREFIX` | Sensor name prefix filter (e.g. `de-` for Delft) |

### Levellog / CARS groundwater source

| Variable | Purpose |
| --- | --- |
| `LEVELLOG_ENABLED` | Enable Levellog polling, default `false` |
| `LEVELLOG_API_URL` | CARS Online OData API URL |
| `LEVELLOG_TOKEN_URL` | OAuth2 token endpoint |
| `LEVELLOG_CLIENT_ID` | OAuth2 client ID |
| `LEVELLOG_CLIENT_SECRET` | OAuth2 client secret |
| `LEVELLOG_INSTALLATION_IDS` | Comma-separated CARS installation UUIDs |
| `LEVELLOG_INSTALLATIONS_JSON` | JSON array with per-installation name and coordinates |
| `LEVELLOG_POLL_SECONDS` | Poll interval, default `900` |

### Monitoring module

| Variable | Purpose |
| --- | --- |
| `MONITORING_MQTT_ENABLED` | Enable Monitoring MQTT bridge publishing |
| `MONITORING_MQTT_HOST` | MQTT broker host |
| `MONITORING_MQTT_PORT` | MQTT broker port, default `1883` |
| `MONITORING_MQTT_TOPIC` | Topic to publish Monitoring bridge payloads to |
| `MONITORING_MQTT_USERNAME` | Optional MQTT username |
| `MONITORING_MQTT_PASSWORD` | Optional MQTT password |
| `MONITORING_MQTT_TLS` | Enable TLS for the Monitoring MQTT bridge |
| `MONITORING_PUSH_URL` | Direct HTTP push URL for the monitoring module |
| `MONITORING_PUSH_KEY` | Authorization key for direct push |

### Observation push reliability

| Variable | Purpose |
| --- | --- |
| `FROST_BATCH_PUSH_ENABLED` | Use the SensorThings CreateObservations (dataArray) extension, default `true` |
| `FROST_BATCH_MAX_OBSERVATIONS` | Max observations per batch request, default `500` |
| `FROST_BATCH_TIMEOUT_SECONDS` | Read timeout for batch requests, default `60` |
| `FROST_CB_FAILURE_THRESHOLD` | Circuit breaker: skip target after N consecutive failures, default `3` |
| `FROST_CB_COOLDOWN_SECONDS` | Circuit breaker: cooldown before retry, default `600` |
| `FAILED_REPLAY_ENABLED` | Enable scheduled dead-letter queue replay, default `true` |
| `FAILED_REPLAY_INTERVAL_SECONDS` | DLQ replay interval, default `900` |
| `FAILED_REPLAY_MAX_LINES` | Max observations per replay cycle, default `500` |
| `FROST_REPLAY_DEDUP_PROBE` | Probe for an existing `(Datastream, phenomenonTime)` before re-posting a dead-letter entry, so replay can't duplicate on servers that don't enforce observation uniqueness, default `true` |

### Observation delivery semantics

The connector writes observations **optimistically**: it POSTs (single or, by
default, batched via the `CreateObservations` dataArray extension) without a
pre-write existence probe, and treats an `HTTP 409 Conflict` as "already
delivered". This keeps the hot path free of the round-trip-per-observation cost
that a `GET ?$filter=phenomenonTime eq ...` probe would add.

This deduplicates correctly **only when the target server enforces uniqueness on
`(Datastream, phenomenonTime)` and returns 409**. Standard FROST does not enforce
this by default, so two safeguards apply:

- The live streaming/polling paths seed the last-pushed timestamp per datastream
  from FROST on startup (`_seed_timestamps_from_frost`) and drop readings at or
  before it — a forward-only cursor that needs one query per datastream per run
  rather than one per observation.
- The dead-letter replay path probes for an existing `(Datastream,
  phenomenonTime)` before re-posting (`FROST_REPLAY_DEDUP_PROBE`, default `true`),
  so a write that timed out after the server committed it is not duplicated.

Back-dated corrections (a new value for a `phenomenonTime` already stored) are
not handled — the connector's sources are forward-only.

### Configuration notes

If no base URL is configured, the app stays in preview mode and returns payloads locally without posting to FROST.

If `CONNECTOR_BRIDGE_PAYLOAD_PATH` is set, the connector treats that file as the active bridge source for preview and push flows. The file can contain either a top-level `readings` array or a raw array of reading objects.

When `SENSORTHINGS_DATASTREAM_IDS_JSON` does not contain a mapping for a reading, the connector attempts live Datastream lookup with OData filters. The preferred lookup keys are:

- `device_eui` + `stream_key` on the reading payload (mapped to `Thing/properties/deviceEui` and `Datastream/properties/streamKey`)
- fallback to `thing_name` + `stream_key`
- fallback to `thing_name` + `observed_property_name`

For deterministic lookup, register Datastreams with a stable `properties.streamKey` value.

## Background Processing

When the connector starts, it launches background loops via the FastAPI lifespan manager:

- **Kafka ingest loop** (when `KAFKA_TGV_ENABLED=true`): polls Confluent Cloud every `KAFKA_TGV_POLL_SECONDS` (default 300s), deserializes Avro records, maps them to SensorReadings, and pushes observations to FROST.
- **Polling source loops** (when `OHNICS_ENABLED` or `LEVELLOG_ENABLED` is true): each enabled source polls its API at its configured interval, eagerly registers entity sets on first successful fetch, deduplicates against the latest observation already in FROST, and pushes new readings.
- **Dead-letter queue replay** (when `FAILED_REPLAY_ENABLED=true`): replays failed observations from `data/failed_observations.jsonl` every `FAILED_REPLAY_INTERVAL_SECONDS` (default 900s).

After each successful FROST push, observations are also forwarded to the UrbanAdapt Monitoring Module (fire-and-forget) if `MONITORING_PUSH_URL` is configured.

## Multi-Target FROST and v2.0 Support

The connector can fan out observations and entity registrations to multiple FROST targets simultaneously. Use `SENSORTHINGS_BASE_URLS` with JSON objects to configure per-target version and auth:

```json
[
  {"url": "https://server1/v1.1", "version": "v1.1"},
  {"url": "https://v2-server/v2.0", "version": "v2.0", "label": "frost-v2-demo", "auth_username": "user", "auth_password": "pass"}
]
```

Each target gets its own HTTP client, entity cache, and entity manager. The v2.0 payload adapter converts v1.1 entity payloads to the v2.0 format at the HTTP boundary.

## Reliability

- **Circuit breaker**: per-target fault tolerance. After `FROST_CB_FAILURE_THRESHOLD` consecutive push failures, the circuit opens and the target is skipped until the cooldown elapses. Check state via `GET /connector/push-health`.
- **Dead-letter queue**: observations that fail to push are appended to `data/failed_observations.jsonl` and replayed on a schedule.
- **Batch chunking**: large observation sets are split into chunks of `FROST_BATCH_MAX_OBSERVATIONS` (default 500) to avoid server timeouts.
- **Deduplication**: on startup, the connector seeds the latest observation timestamps from FROST per datastream and skips readings that are not newer.

## Deployment

### Docker

```bash
docker build -t sensorthings-connector .
docker run -p 8010:8010 --env-file .env sensorthings-connector
```

The Dockerfile uses Python 3.12-slim and exposes port 8010. The `data/` directory stores entity caches and the DLQ file.

### Render

`render.yaml` defines a Starter plan ($7/month, always-on) service with a 1 GB persistent disk mounted at `/app/data`. The persistent disk ensures entity caches and the DLQ survive redeployment.

### Integration tests

```bash
docker compose -f docker-compose.test.yaml up -d   # start ephemeral FROST + PostGIS
pytest -m integration                                # run integration tests
docker compose -f docker-compose.test.yaml down      # tear down
```

## Status

Active and ingesting live data:

- Kafka TGV consumer polling Confluent Cloud for indoor climate data (TGV Office Lab)
- Ohnics air quality polling for Delft SamenMeten sensors
- Levellog groundwater polling via CARS Online OData API
- Multi-target observation push to v1.1 and v2.0 FROST servers
- Monitoring module direct HTTP push (bypasses 5-minute polling delay)
- Circuit breaker, batch push, deduplication, and dead-letter queue replay
- Tasking support (Actuators, TaskingCapabilities, Tasks per site)

Pending field confirmation:

- exact sensor make/model details for some TGV and Blijdorp installations
- final Blijdorp sensor setup and pond water quality parameters
- Rainaway Parkeerplaatsen, Flowsand, and Koers / Zoak / Nus entity sets (registered as scaffolding but not yet receiving live data)

## Node-RED Starter Assets

- quickstart guide in [docs/NODE_RED_QUICKSTART.md](docs/NODE_RED_QUICKSTART.md)
- importable flow in [examples/node-red/sensorthings-bridge-flow.json](examples/node-red/sensorthings-bridge-flow.json)
- sample bridge payload in [examples/node-red/bridge-payload.json](examples/node-red/bridge-payload.json)

## Operations And Handoff

- sensor onboarding checklist in [docs/SENSOR_ONBOARDING_CHECKLIST.md](docs/SENSOR_ONBOARDING_CHECKLIST.md)
- live FROST inventory snapshot in [docs/FROST_LIVE_INVENTORY.md](docs/FROST_LIVE_INVENTORY.md)
- hold-point summary in [docs/TESTBED_HOLD_POINT.md](docs/TESTBED_HOLD_POINT.md)

## Tender-Facing Documentation (historical)

These documents were written for the initial tender response and describe the original wastewater use case. The project subsequently pivoted to climate-adaptation monitoring.

- implementation plan in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)
- proposal summary in [docs/PROPOSAL_SUMMARY.md](docs/PROPOSAL_SUMMARY.md)
- architecture diagram in [docs/ARCHITECTURE_DIAGRAM.md](docs/ARCHITECTURE_DIAGRAM.md)
- tender response notes in [docs/TENDER_RESPONSE_NOTES.md](docs/TENDER_RESPONSE_NOTES.md)

## Licensing and Publication Terms

- all source code in this repository is published under the MIT License
- research results, reports, data, and non-code deliverables for the testbed are intended to be published under CC BY 4.0
- deliverables should remain publicly available for at least six months after completion of the testbed

See the [LICENSE](LICENSE) file for the code license details and the tender documentation for the publication requirements.
