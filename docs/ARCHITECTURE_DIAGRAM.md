# Architecture Diagram

```mermaid
flowchart LR
    A[Analog pressure sensors] --> B[Node-RED bridge]
    C[Flow sensor] --> B
    D[Pump telemetry via Modbus] --> B
    B --> E[SensorThings testbed connector]
    E --> F[FROST SensorThings Server]
    F --> G[Digital twin dashboards and analytics]

    E --> H[Registration preview]
    E --> I[Observation ingest]
```

## Interpretation

This architecture keeps the integration intentionally lightweight:

- Node-RED gathers or simulates heterogeneous sensor input
- the connector normalizes payloads for the SensorThings model
- the FROST server acts as the central standards-based observation store
- downstream clients can query the observations for monitoring, optimization, and digital twin scenarios
