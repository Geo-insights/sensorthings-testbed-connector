# SensorThings Testbed Connector

A lightweight proof of concept for Geonovum implementation topic 2: connecting one or more sensors to a central OGC SensorThings API server.

The repository is intentionally small, public-facing, and easy to demonstrate. It is designed around the Brabantse Delta wastewater digital twin scenario described in the tender and uses a bridge-based approach that fits mixed sensor inputs such as analog sensors and Modbus pump telemetry.

## Overview

This project demonstrates how to:

- ingest wastewater monitoring readings from a bridge layer such as Node-RED
- map those readings to OGC SensorThings and OM Measurement concepts
- preview and register demo entities against a FROST-compatible server
- submit standardized observations for downstream querying and analysis

## Why this repo exists

This repository is separate from the main UrbanAdapt monitoring service so the tender work can stay:

- focused on the interoperability use case
- easy to explain to reviewers
- simple to publish and keep online
- low-risk for production systems

## Included in the repository

| Area | Purpose |
| --- | --- |
| FastAPI demo app | Local service for previewing, registering, and forwarding observations |
| Wastewater demo source | Mock readings for inlet pressure, outlet pressure, flow, and pump speed |
| FROST-ready client logic | Registration preview and observation push flow |
| Node-RED example | Importable bridge flow for the tender scenario |
| Tender docs | Implementation plan, summary, architecture diagram, and notes |

## Suggested use in the testbed

1. Start with the built-in wastewater-oriented demo readings.
2. Use Node-RED as the intermediary bridge for mixed sensor inputs.
3. Register the Thing, Sensors, Observed Properties, and Datastreams in the central FROST server.
4. Submit observations and validate structure, timing, and reliability.
5. Document the lessons learned and keep the demonstrator available.

## Quick start

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
- <http://127.0.0.1:8010/docs>

## Main endpoints

| Endpoint | Purpose |
| --- | --- |
| GET /health | Basic health check |
| GET /connector/preview | Preview demo observations |
| GET /connector/registration-preview | Preview FROST entity registration payloads |
| POST /connector/register-demo | Register demo entities against a live SensorThings server |
| POST /connector/ingest-preview | Validate bridge payloads without sending externally |
| POST /connector/ingest | Forward bridge payloads to the configured SensorThings server |
| POST /connector/push | Push the built-in demo readings |

## Configuration

Copy .env.example to .env and adjust the values for the live testbed environment.

Key settings include:

- SENSORTHINGS_BASE_URL
- SENSORTHINGS_THINGS_PATH
- SENSORTHINGS_SENSORS_PATH
- SENSORTHINGS_OBSERVED_PROPERTIES_PATH
- SENSORTHINGS_DATASTREAMS_PATH
- SENSORTHINGS_OBSERVATIONS_PATH
- SENSORTHINGS_AUTH_TOKEN
- SENSORTHINGS_DATASTREAM_IDS_JSON
- CONNECTOR_NAME
- DEBUG

If no base URL is configured, the app stays in preview mode and returns payloads locally without posting them to an external service.

## Node-RED starter assets

- quickstart guide in docs/NODE_RED_QUICKSTART.md
- importable flow in examples/node-red/sensorthings-bridge-flow.json

## Tender-facing documentation

- implementation plan in docs/IMPLEMENTATION_PLAN.md
- proposal summary in docs/PROPOSAL_SUMMARY.md
- architecture diagram in docs/ARCHITECTURE_DIAGRAM.md
- tender response notes in docs/TENDER_RESPONSE_NOTES.md

## Recommended next steps

- replace the mock source with the real sensor or bridge feed
- configure the hosted FROST server once credentials are available
- validate live datastream registration and observation delivery
- expand logging and retry handling for a public demonstration setup

## Licensing and publication terms

- all source code in this repository is published under the MIT License
- research results, reports, data, and non-code deliverables for the testbed are intended to be published under CC BY 4.0
- deliverables should remain publicly available for at least six months after completion of the testbed

See the LICENSE file for the code license details and the tender documentation for the publication requirements.
