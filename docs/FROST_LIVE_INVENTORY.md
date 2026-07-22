# FROST Live Inventory

> **Historical snapshot from 2026-05-26.** This inventory records the original `GEO_` entities created during the initial proof-of-path validation. The live FROST environment has since evolved: TGV Office Lab entities are fed from Kafka, Ohnics air quality and Levellog groundwater entities are registered dynamically, and observations now fan out to multiple targets (v1.1 and v2.0). Query the live FROST servers directly for the current state.

## Summary

- FROST root: `https://sta.wbd-rd.nl/FROST-Server/v1.1`
- authentication mode: HTTP Basic auth
- naming prefix used: `GEO_`
- live write path verified: yes
- repeated observation posts verified: yes

## Things

- `3` `GEO_Hitteplein`
- `4` `GEO_Rainaway Parkeerplaatsen`
- `5` `GEO_Flowsand`
- `6` `GEO_Koers / Zoak / Nus`
- `7` `GEO_Climate Davis`
- `8` `GEO_Weather Climatics`
- `9` `GEO_Microclimate sensors`
- `10` `GEO_Weather station`
- `11` `GEO_Klimaatplein / Bufferblocks`
- `12` `GEO_Water quality (ponds)`

## Locations

- `1` `GEO_Hitteplein location`
- `2` `GEO_Rainaway Parkeerplaatsen location`
- `3` `GEO_Flowsand location`
- `4` `GEO_Koers Zoak Nus location`
- `5` `GEO_Climate Davis location`
- `6` `GEO_Weather Climatics location`
- `7` `GEO_Microclimate sensors location`
- `8` `GEO_Weather station location`
- `9` `GEO_Klimaatplein Bufferblocks location`
- `10` `GEO_Water quality ponds location`

## Sensors

- `1` `GEO_Hitteplein climate station`
- `2` `GEO_Rainaway soil profile`
- `3` `GEO_Flowsand probe`
- `4` `GEO_Koers / Zoak / Nus soil cluster`
- `5` `GEO_Davis weather station`
- `6` `GEO_Climatics rooftop station`
- `7` `GEO_Enclosure microclimate sensor`
- `8` `GEO_Blijdorp weather station`
- `9` `GEO_Bufferblocks water buffer sensor`
- `10` `GEO_Pond water quality sensor`

## ObservedProperties

- `1` `Air temperature`
- `2` `Relative humidity`
- `3` `Solar radiation`
- `4` `Soil moisture`
- `5` `Soil temperature`
- `6` `Electrical conductivity`
- `7` `Precipitation`
- `8` `Wind speed`
- `9` `Wind direction`
- `10` `Air pressure`
- `11` `Water buffer level`
- `12` `Infiltration performance`
- `13` `Water quality parameter`

## Datastreams

- `1` `GEO_Hitteplein climate station - Air temperature`
- `2` `GEO_Hitteplein climate station - Relative humidity`
- `3` `GEO_Hitteplein climate station - Solar radiation`
- `4` `GEO_Rainaway soil profile - Soil moisture`
- `5` `GEO_Rainaway soil profile - Soil temperature`
- `6` `GEO_Flowsand probe - Soil moisture`
- `7` `GEO_Flowsand probe - Soil temperature`
- `8` `GEO_Koers / Zoak / Nus soil cluster - Soil moisture`
- `9` `GEO_Koers / Zoak / Nus soil cluster - Soil temperature`
- `10` `GEO_Koers / Zoak / Nus soil cluster - Electrical conductivity`
- `11` `GEO_Davis weather station - Air temperature`
- `12` `GEO_Davis weather station - Precipitation`
- `13` `GEO_Davis weather station - Wind speed`
- `14` `GEO_Davis weather station - Wind direction`
- `15` `GEO_Climatics rooftop station - Air temperature`
- `16` `GEO_Climatics rooftop station - Precipitation`
- `17` `GEO_Climatics rooftop station - Air pressure`
- `18` `GEO_Climatics rooftop station - Wind speed`
- `19` `GEO_Climatics rooftop station - Wind direction`
- `20` `GEO_Enclosure microclimate sensor - Air temperature`
- `21` `GEO_Enclosure microclimate sensor - Relative humidity`
- `22` `GEO_Blijdorp weather station - Wind speed`
- `23` `GEO_Blijdorp weather station - Solar radiation`
- `24` `GEO_Bufferblocks water buffer sensor - Water buffer level`
- `25` `GEO_Bufferblocks water buffer sensor - Infiltration performance`
- `26` `GEO_Pond water quality sensor - Water quality parameter`

## Cleanup guidance

Before changing or deleting anything in the live FROST environment, compare the real TGV and Blijdorp sensor layouts against the entity model in [docs/entity-mapping.md](entity-mapping.md).

Keep the current live entities if the real field setup matches closely enough.

Plan cleanup or replacement if:

- Thing boundaries are wrong
- sensor naming is materially misleading
- units differ from the real source
- datastream granularity is wrong
- one or more draft entities should not exist in production

## Operational note

This inventory is a hold-point reference for the testbed presentation and for future sensor onboarding work.
