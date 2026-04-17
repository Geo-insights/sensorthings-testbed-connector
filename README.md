# SensorThings Testbed Connector

A small, public-facing proof-of-concept repository for Geonovum implementation topic #2:
connect one or more sensors to a central OGC SensorThings API server.

This repository is intentionally lean and separate from the main `monitoring_module` so the tender work can stay:

- focused on the SensorThings connector use case
- easy to publish and demonstrate
- low-risk for the production UrbanAdapt monitoring service

## What is included

- a minimal FastAPI app for demo and validation
- a mock wastewater-oriented sensor source
- a SensorThings and OMS-aligned payload builder
- a simple connector service that can preview, register demo entities, and post observations to a configured FROST-compatible server
- starter tests
- a concise implementation plan and timeline

## Suggested use in the testbed

1. Start with the built-in wastewater-oriented mock sensor feed
2. Use a Node-RED intermediary bridge for the Brabantse Delta setup
3. Map the analog pressure and flow sensors plus the Modbus pump telemetry to SensorThings entities
4. Validate accuracy and reliability
5. Keep the demo online for the required period

## Project structure

```text
sensorthings-testbed-connector/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── models.py
│   ├── routes/
│   └── services/
├── docs/
│   └── IMPLEMENTATION_PLAN.md
├── tests/
├── .env.example
├── requirements.txt
└── README.md
```

## Quick start

```powershell
py -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8010
```

Then open:

- `http://127.0.0.1:8010/health`
- `http://127.0.0.1:8010/connector/preview`
- `http://127.0.0.1:8010/connector/registration-preview`
- `http://127.0.0.1:8010/docs`

## Environment variables

Copy `.env.example` to `.env` and adjust if you have a live SensorThings server:

- `SENSORTHINGS_BASE_URL`
- `SENSORTHINGS_THINGS_PATH`
- `SENSORTHINGS_SENSORS_PATH`
- `SENSORTHINGS_OBSERVED_PROPERTIES_PATH`
- `SENSORTHINGS_DATASTREAMS_PATH`
- `SENSORTHINGS_OBSERVATIONS_PATH`
- `SENSORTHINGS_AUTH_TOKEN`
- `SENSORTHINGS_DATASTREAM_IDS_JSON`
- `CONNECTOR_NAME`
- `DEBUG`

If no base URL is configured, the connector stays in **preview mode** and returns the generated payloads without sending them externally.

## Suggested demo flow

1. Open the registration preview endpoint to inspect the Thing, Sensor, ObservedProperty, and Datastream payloads
2. Configure the provided FROST server base URL
3. Call the registration endpoint to create the demo entities
4. Use the ingest preview endpoint for Node-RED or bridge payloads
5. Call the live ingest or push endpoint to submit observations

## Node-RED starter assets

- quickstart guide in docs/NODE_RED_QUICKSTART.md
- importable flow in examples/node-red/sensorthings-bridge-flow.json

## Tender-facing documentation

- proposal summary in docs/PROPOSAL_SUMMARY.md
- architecture diagram in docs/ARCHITECTURE_DIAGRAM.md
- tender response notes in docs/TENDER_RESPONSE_NOTES.md

## Next recommended steps

- replace the mock source with the actual sensor feed or Node-RED bridge
- register real `Thing`, `Sensor`, and `Datastream` entities against the central server
- add persistent logging and retry handling
- publish the demo and report the lessons learned
