# Testbed Hold Point

## Where we stand

The connector-to-FROST path is now proven.

What has been validated:

- live FROST connectivity
- HTTP Basic authentication
- `GEO_`-prefixed entity registration
- site-scoped registration for `tgv` and `blijdorp`
- repeated live observation delivery
- bridge-style payload ingestion using a file-backed source for local runs

What has not been validated yet:

- access to the real TGV sensors
- access to the real Blijdorp sensors
- the true payload contract from the field or bridge
- whether the draft site and datastream model is the final correct representation

## Recommended posture

Hold further live expansion until real sensor or bridge access is available.

This is a good stopping point for the testbed because the integration mechanism is proven, while the remaining uncertainty is now clearly on the source-data side.

## Safe actions during the hold

- prepare presentation material from the validated connector results
- compare the real field inventory against [docs/entity-mapping.md](docs/entity-mapping.md) when it becomes available
- capture sample source payloads before making any more live model changes
- reuse the onboarding checklist in [docs/SENSOR_ONBOARDING_CHECKLIST.md](docs/SENSOR_ONBOARDING_CHECKLIST.md)

## First actions once access arrives

1. get a real payload sample from the bridge or sensor source
2. run `GET /connector/preview`
3. run `GET /connector/registration-preview/{site_key}`
4. decide whether the existing live `GEO_` entities should be reused
5. send a tiny live payload through `POST /connector/ingest`

## Presentation-ready message

The connector is ready for sensor onboarding, but the real field integration should wait for confirmed access to the TGV and Blijdorp sources. The current live FROST entities prove the interoperability approach, not yet the final operational deployment.
