# Implementation Plan

## Goal

Connect one or more sensors or sensor feeds to a central OGC SensorThings API server, validate the reliability of the approach, and provide a reproducible public demonstration.

The preferred demonstration setup follows the Brabantse Delta wastewater digital twin use case, where two pressure sensors and one flow sensor are combined with pump telemetry. A Node-RED intermediary bridge is the most practical route because the tender notes that the pump exposes Modbus output while the other sensors are analog.

## Simple implementation phases

### Phase 1 — Kickoff and alignment

- confirm access to the central FROST SensorThings server
- confirm which real or mock sensors will be used
- choose the integration pattern, with Node-RED bridge as the default path for mixed Modbus and analog inputs

### Phase 2 — Mapping and setup

- map inlet pressure, outlet pressure, flow, and pump telemetry to Thing, Sensor, Datastream, and Observation
- align the observation model with OM Measurement concepts
- set up the connector repository and demo service
- prepare reproducible configuration

### Phase 3 — Connector build

- ingest sensor readings from Node-RED or another bridge source
- transform the readings to SensorThings-compatible payloads
- register Thing, Sensor, ObservedProperty, and Datastream entities
- send observations to the central server or run in preview mode
- include an importable Node-RED flow and quickstart guide for reproducibility

### Phase 4 — Validation

- verify timestamps, units, and expected ranges
- test repeated submissions and error handling
- document accuracy and reliability findings

### Phase 5 — Demonstration and reporting

- prepare a public demo endpoint
- publish the code in an open repository
- deliver the report and lessons learned

## Time management table

| Week | Focus | Output |
| --- | --- | --- |
| 1 | Kickoff and access | Confirmed scope and technical setup |
| 2 | Mapping and configuration | Data model and connector design |
| 3 | Build first connector | Working prototype |
| 4 | Test and improve reliability | Stable proof of concept |
| 5 | Demo and documentation | Demo flow and written report |
| 6 | Final polish and delivery | Public presentation and final handoff |

## Effort split

| Activity | Estimated share |
| --- | ---: |
| Coordination and setup | 15% |
| Mapping and design | 20% |
| Implementation | 30% |
| Validation | 20% |
| Documentation and demo | 15% |
