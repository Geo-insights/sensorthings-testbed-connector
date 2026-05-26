# Sensor Onboarding Checklist

## Purpose

Use this checklist when access to the real TGV or Blijdorp sensor feeds becomes available.

The connector-to-FROST path has already been proven. The remaining risk is the real sensor and bridge integration.

## Before connecting a real source

- confirm which site is being onboarded: `tgv` or `blijdorp`
- confirm the actual sensor inventory for that site
- confirm the bridge payload shape or protocol used by the source system
- confirm the source timestamps are in UTC or clearly convertible to UTC
- confirm each measurement unit matches the registered SensorThings datastream expectation
- confirm whether the existing `GEO_` entities should be reused or replaced

## Payload contract checks

The connector expects readings equivalent to the examples in [examples/node-red/bridge-payload.json](examples/node-red/bridge-payload.json).

Each reading should include:

- `sensor_id`
- `sensor_name`
- `observed_property`
- `unit`
- `value`
- `timestamp`
- `quality`
- `location`
- `thing_name`

## First connection sequence

1. Point `CONNECTOR_BRIDGE_PAYLOAD_PATH` or the live bridge output at a payload sample from the real source.
2. Call `GET /connector/preview` and confirm the payload is accepted.
3. Compare the preview values, units, timestamps, and names against the real source.
4. Call `GET /connector/registration-preview/{site_key}` and verify the site mapping is still correct.
5. Decide whether the existing live `GEO_` entities can be reused.
6. If reuse is correct, call `POST /connector/ingest` with a tiny payload.
7. If the mapping is wrong, pause and correct the model before any more live writes.

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

## Hold criteria

Stop the rollout and correct the mapping before continuing if any of the following happen:

- the bridge payload shape differs from the expected contract
- units do not match the registered datastream definitions
- the source sends unstable or missing timestamps
- the real installation names do not map cleanly to the draft `GEO_` entities
- duplicate or incorrect live entities would be created by continuing
