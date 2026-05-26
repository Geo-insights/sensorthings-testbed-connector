# SensorThings Testbed Connector

This repository contains the FastAPI connector used for the Geonovum Sensor Data Testbed 2026. It prepares climate-adaptation sensor data for an OGC SensorThings API v1.1 FROST server at `https://frost.wbd-rd.nl/FROST-Server/v1.1`.

The current focus is the integration of two real pilot locations:

- The Green Village, TU Delft campus in Delft
- Diergaarde Blijdorp in Rotterdam

The codebase already includes a SensorThings client, preview endpoints, registration helpers, and a stub climate-adaptation source so the connector can be tested end-to-end before Lindsey Schwidder confirms the final sensor protocols.

## Project Purpose

The connector translates site-specific readings into SensorThings entities and observations:

- it models each installation as a Thing with its own Location
- it registers Sensors, ObservedProperties, Datastreams, and Observations in FROST
- it keeps a dead-letter queue for failed observations so the stream can be replayed later
- it exposes a status check for verifying connectivity before live testing

## Sites In Scope

### The Green Village, TU Delft, Delft

Planned setups include Hitteplein, Rainaway Parkeerplaatsen, Flowsand, Koers / Zoak / Nus, Climate Davis, and Weather Climatics. These cover climate stress, soil moisture, soil temperature, conductivity, precipitation, wind, and air pressure measurements.

### Diergaarde Blijdorp, Rotterdam

Planned setups include microclimate sensors for animal enclosures, a weather station, Klimaatplein / Bufferblocks, and pond water quality monitoring.

The full draft model is documented in [docs/entity-mapping.md](docs/entity-mapping.md).

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

| Endpoint | Purpose |
| --- | --- |
| GET /health | Basic health check |
| GET /connector/preview | Preview bridge payload observations, falling back to demo data |
| GET /connector/registration-preview | Preview the SensorThings registration payloads |
| GET /connector/registration-preview/{site_key} | Preview registration payloads for one site (`tgv` or `blijdorp`) |
| POST /connector/register-demo | Register the default climate-adaptation entity sets |
| POST /connector/register-site/{site_key} | Register one site at a time (`tgv` or `blijdorp`) |
| POST /connector/ingest-preview | Validate custom bridge payloads without posting them |
| POST /connector/ingest | Push supplied readings to the configured SensorThings server |
| POST /connector/push | Push bridge payload readings, falling back to demo data |
| POST /connector/replay-failed | Replay observations stored in the dead-letter queue |
| GET /frost/status | Check FROST connectivity, response time, and Thing count |

## Configuration

Copy `.env.example` to `.env` and adjust the values for the live testbed environment.

| Variable | Purpose |
| --- | --- |
| `CONNECTOR_NAME` | Friendly service name shown by FastAPI |
| `CONNECTOR_BRIDGE_PAYLOAD_PATH` | Optional JSON payload file used by `/connector/preview` and `/connector/push` |
| `SENSORTHINGS_BASE_URL` | FROST root URL, including `/v1.1` |
| `SENSORTHINGS_THINGS_PATH` | Thing collection path, default `/Things` |
| `SENSORTHINGS_LOCATIONS_PATH` | Location collection path, default `/Locations` |
| `SENSORTHINGS_SENSORS_PATH` | Sensor collection path, default `/Sensors` |
| `SENSORTHINGS_OBSERVED_PROPERTIES_PATH` | ObservedProperty collection path, default `/ObservedProperties` |
| `SENSORTHINGS_DATASTREAMS_PATH` | Datastream collection path, default `/Datastreams` |
| `SENSORTHINGS_OBSERVATIONS_PATH` | Observation collection path, default `/Observations` |
| `SENSORTHINGS_AUTH_TOKEN` | Optional bearer token for authenticated FROST access |
| `SENSORTHINGS_AUTH_USERNAME` | Optional username for HTTP Basic auth |
| `SENSORTHINGS_AUTH_PASSWORD` | Optional password for HTTP Basic auth |
| `SENSORTHINGS_ENTITY_NAME_PREFIX` | Optional prefix applied to Thing, Location, Sensor, and Datastream names |
| `SENSORTHINGS_DATASTREAM_IDS_JSON` | Optional JSON map for preloaded Datastream IDs |
| `SENSORTHINGS_REGISTERED_ENTITIES_PATH` | Path for cached `@iot.id` mappings, default `data/registered_entities.json` |
| `SENSORTHINGS_FAILED_OBSERVATIONS_PATH` | Path for the dead-letter queue, default `data/failed_observations.jsonl` |
| `SENSORTHINGS_REQUEST_TIMEOUT_SECONDS` | HTTP timeout used for FROST requests |
| `DEBUG` | Enable debug mode |

If no base URL is configured, the app stays in preview mode and returns payloads locally without posting to FROST.

For the WBD-RD FROST environment, use `SENSORTHINGS_AUTH_USERNAME` and `SENSORTHINGS_AUTH_PASSWORD`; the connector will send HTTP Basic auth automatically.

If `CONNECTOR_BRIDGE_PAYLOAD_PATH` is set, the connector treats that file as the active bridge source for preview and push flows. The file can contain either a top-level `readings` array or a raw array of reading objects.

## Status

Live now:

- FastAPI app and existing preview endpoints
- bridge-payload sourcing for preview and push, with demo fallback
- FROST connectivity probe at `/frost/status`
- registration caching in `data/registered_entities.json`
- dead-letter replay from `data/failed_observations.jsonl`

Stub-only pending Lindsey confirmation:

- exact sensor make/model details
- final protocol and payload shapes for the field devices
- the pond water quality parameter list
- the final placement of several Things and sensors within each site

## Node-RED Starter Assets

- quickstart guide in [docs/NODE_RED_QUICKSTART.md](docs/NODE_RED_QUICKSTART.md)
- importable flow in [examples/node-red/sensorthings-bridge-flow.json](examples/node-red/sensorthings-bridge-flow.json)
- sample bridge payload in [examples/node-red/bridge-payload.json](examples/node-red/bridge-payload.json)

## Operations And Handoff

- sensor onboarding checklist in [docs/SENSOR_ONBOARDING_CHECKLIST.md](docs/SENSOR_ONBOARDING_CHECKLIST.md)
- live FROST inventory in [docs/FROST_LIVE_INVENTORY.md](docs/FROST_LIVE_INVENTORY.md)
- current hold-point summary in [docs/TESTBED_HOLD_POINT.md](docs/TESTBED_HOLD_POINT.md)

## Tender-Facing Documentation

- implementation plan in [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md)
- proposal summary in [docs/PROPOSAL_SUMMARY.md](docs/PROPOSAL_SUMMARY.md)
- architecture diagram in [docs/ARCHITECTURE_DIAGRAM.md](docs/ARCHITECTURE_DIAGRAM.md)
- tender response notes in [docs/TENDER_RESPONSE_NOTES.md](docs/TENDER_RESPONSE_NOTES.md)

## Recommended Next Steps

- point the bridge or Node-RED export at `CONNECTOR_BRIDGE_PAYLOAD_PATH` for local preview and push runs
- register sites incrementally with `/connector/register-site/tgv` and `/connector/register-site/blijdorp`
- validate repeated live observation delivery through `/connector/ingest`

## Licensing and Publication Terms

- all source code in this repository is published under the MIT License
- research results, reports, data, and non-code deliverables for the testbed are intended to be published under CC BY 4.0
- deliverables should remain publicly available for at least six months after completion of the testbed

See the [LICENSE](LICENSE) file for the code license details and the tender documentation for the publication requirements.
