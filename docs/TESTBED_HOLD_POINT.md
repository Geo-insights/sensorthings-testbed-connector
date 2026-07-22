# Testbed Hold Point

## Where we stand

The connector is operational and actively ingesting live data from multiple sources.

### Validated

- live FROST connectivity to v1.1 and v2.0 targets
- HTTP Basic authentication and per-target auth
- `GEO_`-prefixed entity registration
- site-scoped registration for `tgv` and `blijdorp`
- repeated live observation delivery
- bridge-style payload ingestion using a file-backed source for local runs
- Kafka TGV consumer: real indoor climate data from Confluent Cloud (temperature, humidity, CO2, pressure)
- Ohnics air quality: live polling of SamenMeten network sensors in Delft (PM2.5, temperature)
- Levellog groundwater: live polling via CARS Online OData API with OAuth2
- multi-target observation fan-out (v1.1 + v2.0 servers)
- circuit breaker per target with automatic recovery
- batch observation push with chunking (dataArray extension)
- dead-letter queue with scheduled replay
- deduplication against latest FROST observations on startup
- monitoring module direct HTTP push (bypasses 5-minute polling delay)
- Tasking support: Actuators, TaskingCapabilities, and Tasks per site

### Not yet validated

- access to the real Blijdorp sensors
- final Blijdorp sensor setup and pond water quality parameters
- Rainaway Parkeerplaatsen, Flowsand, and Koers / Zoak / Nus entity sets (registered as scaffolding but not receiving live data)
- exact sensor make/model metadata for some installations

## Recommended posture

Continue live ingestion from Kafka, Ohnics, and Levellog sources. Hold Blijdorp expansion until real sensor access is available.

The integration mechanism is proven across three live data sources. The remaining uncertainty is on the Blijdorp source-data side and on finalizing the remaining TGV entity sets.

## Safe actions during the hold

- prepare presentation material from the validated connector results
- compare the real field inventory against [docs/entity-mapping.md](entity-mapping.md) when new sensors come online
- onboard new Ohnics sensors by adjusting `OHNICS_SENSOR_PREFIX`
- onboard new Levellog installations by adding UUIDs to `LEVELLOG_INSTALLATIONS_JSON`

## First actions once Blijdorp access arrives

1. get a real payload sample from the Blijdorp sensor source
2. run `GET /connector/preview`
3. run `GET /connector/registration-preview/blijdorp`
4. decide whether the existing scaffolding `GEO_` entities should be reused or replaced
5. send a tiny live payload through `POST /connector/ingest`
