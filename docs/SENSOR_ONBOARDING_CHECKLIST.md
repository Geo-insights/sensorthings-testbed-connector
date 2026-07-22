# Sensor Onboarding Checklist

## Purpose

Use this checklist when connecting a new sensor source to the connector.

The connector supports multiple integration paths: Kafka topics, REST API polling sources (Ohnics, Levellog), bridge payloads (file-backed or HTTP POST), and Node-RED flows.

## Before connecting a real source

- confirm which site is being onboarded: `tgv` or `blijdorp`
- confirm the actual sensor inventory for that site
- confirm the payload shape or protocol used by the source system
- confirm the source timestamps are in UTC or clearly convertible to UTC
- confirm each measurement unit matches the registered SensorThings datastream expectation
- confirm whether the existing `GEO_` entities should be reused or replaced

## Payload contract

The connector expects readings matching the `SensorReading` model. Each reading should include:

### Required fields

- `sensor_id` — unique identifier for the sensor
- `sensor_name` — human-readable sensor name
- `observed_property` — measurement key (e.g. `temperature`, `humidity`)
- `unit` — unit of measurement (e.g. `Cel`, `%`, `ppm`)
- `value` — numeric measurement value
- `timestamp` — ISO 8601 UTC timestamp

### Optional fields

- `quality` — data quality flag, default `"good"`
- `location` — site key (e.g. `tgv`, `blijdorp`), default `"tgv"`
- `thing_name` — name of the SensorThings Thing this reading belongs to
- `device_eui` — LoRaWAN device EUI for datastream lookup via `Thing/properties/deviceEui`
- `stream_key` — datastream key for lookup via `Datastream/properties/streamKey`
- `observed_property_name` — human-readable property name for fallback datastream lookup

See [examples/node-red/bridge-payload.json](../examples/node-red/bridge-payload.json) for a complete example.

## Source-specific onboarding

### Bridge payload (file or HTTP POST)

1. Point `CONNECTOR_BRIDGE_PAYLOAD_PATH` at a JSON file with `{"readings": [...]}` or a raw array.
2. Call `GET /connector/preview` to verify the payload is accepted.
3. Call `POST /connector/ingest` with a small payload to test live push.

### Kafka topic

1. Set `KAFKA_TGV_ENABLED=true` and fill in all `KAFKA_TGV_*` credentials in `.env`.
2. Configure `KAFKA_TGV_DEVICE_MAPPING_JSON` to map `measurement_id` values to FROST entities. Key format: `"<device_id>/<measurement_id>"` for exact match, or `"*/<measurement_id>"` for wildcard.
3. Call `POST /connector/kafka-push` for a manual test cycle.
4. The background Kafka loop will poll automatically every `KAFKA_TGV_POLL_SECONDS`.

### Ohnics air quality

1. Set `OHNICS_ENABLED=true` in `.env`.
2. Adjust `OHNICS_SENSOR_PREFIX` to filter by location (e.g. `de-` for Delft).
3. Entities are discovered and registered automatically on first fetch.
4. Call `POST /connector/ohnics-push` for a manual test cycle.

### Levellog / CARS groundwater

1. Set `LEVELLOG_ENABLED=true` and fill in `LEVELLOG_CLIENT_ID` / `LEVELLOG_CLIENT_SECRET` in `.env`.
2. Add installation UUIDs via `LEVELLOG_INSTALLATIONS_JSON` (preferred, includes name and coordinates) or `LEVELLOG_INSTALLATION_IDS` (comma-separated).
3. Use `scripts/discover_levellog.py` to enumerate available installations.
4. Entities are registered automatically on first fetch.
5. Call `POST /connector/levellog-push` for a manual test cycle.

## First connection sequence

1. Preview the payload via the appropriate preview endpoint.
2. Compare the preview values, units, timestamps, and names against the real source.
3. Call `GET /connector/registration-preview/{site_key}` and verify the site mapping.
4. Decide whether the existing live `GEO_` entities can be reused.
5. If reuse is correct, push a small live payload.
6. If the mapping is wrong, pause and correct the model before any more live writes.

## Reuse vs re-register decision

Reuse the current live entities only if:

- Thing boundaries match the real installations
- sensor names are still acceptable for the field deployment
- observed properties and units match exactly
- datastream granularity matches the real source

Do not reuse the current entities if:

- one Thing should really be split into multiple Things
- a draft sensor name is materially wrong
- the real unit differs from the registered unit
- one real source should publish to more or fewer datastreams than currently modeled

## First-day validation checks

- verify at least one successful `201` observation write per real datastream
- verify timestamps are correct and not shifted by timezone handling
- verify repeated submissions behave as expected
- verify bad payloads are rejected cleanly
- verify failed observations land in the dead-letter file when appropriate
- check circuit breaker state via `GET /connector/push-health`

## Hold criteria

Stop the rollout and correct the mapping before continuing if any of the following happen:

- the payload shape differs from the expected contract
- units do not match the registered datastream definitions
- the source sends unstable or missing timestamps
- the real installation names do not map cleanly to the draft `GEO_` entities
- duplicate or incorrect live entities would be created by continuing
