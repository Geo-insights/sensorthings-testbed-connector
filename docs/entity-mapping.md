# SensorThings Entity Mapping

This document is a preparation draft for OGC SensorThings API v1.1 registration. It maps the confirmed climate-adaptation sensor setups at The Green Village and Diergaarde Blijdorp into Thing, Location, Sensor, ObservedProperty, and Datastream entities.

Notes:

- Locations are approximate and flagged with `[APPROXIMATE - confirm with Lindsey]`.
- Sensor make/model and exact metadata are still unknown and flagged with `[UNKNOWN - confirm with Lindsey]`.
- Items marked `[CONFIRM]` need validation with Lindsey Schwidder.
- Definitions marked `[VERIFY URI]` should be checked against the live FROST/SensorThings interpretation before registration.

## The Green Village

### Hitteplein

| Entity | Draft value |
|---|---|
| Thing | `Hitteplein` |
| Thing description | Open-air climate installation at The Green Village for heat-stress monitoring. |
| Location | GeoJSON Point `"coordinates": [4.37345, 51.99818]` `[APPROXIMATE - confirm with Lindsey]` |
| Location name | `Hitteplein location` |
| Site | `tgv` |

#### Sensors

| Sensor | encodingType | metadata | Notes |
|---|---|---|---|
| Hitteplein climate station | `application/json` | `[UNKNOWN - confirm with Lindsey]` | Multi-parameter station. `[CONFIRM]` |

#### ObservedProperties

| ObservedProperty | definition | unitOfMeasurement |
|---|---|---|
| Air temperature | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature` `[VERIFY URI]` | `Cel` |
| Relative humidity | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#relative_humidity` `[VERIFY URI]` | `%` |
| Solar radiation | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#surface_downwelling_shortwave_flux_in_air` `[VERIFY URI]` | `W.m-2` |

#### Datastreams

| Datastream | Thing | Sensor | ObservedProperty | observationType | unitOfMeasurement |
|---|---|---|---|---|---|
| Hitteplein climate station - Air temperature | Hitteplein | Hitteplein climate station | Air temperature | `OM_Measurement` | `Cel` |
| Hitteplein climate station - Relative humidity | Hitteplein | Hitteplein climate station | Relative humidity | `OM_Measurement` | `%` |
| Hitteplein climate station - Solar radiation | Hitteplein | Hitteplein climate station | Solar radiation | `OM_Measurement` | `W.m-2` |

### Rainaway Parkeerplaatsen

| Entity | Draft value |
|---|---|
| Thing | `Rainaway Parkeerplaatsen` |
| Thing description | Parking-lot rainwater infiltration setup at The Green Village. |
| Location | GeoJSON Point `"coordinates": [4.37358, 51.99802]` `[APPROXIMATE - confirm with Lindsey]` |
| Location name | `Rainaway Parkeerplaatsen location` |
| Site | `tgv` |

#### Sensors

| Sensor | encodingType | metadata | Notes |
|---|---|---|---|
| Rainaway soil profile | `application/json` | `[UNKNOWN - confirm with Lindsey]` | Soil profile probe cluster across multiple depths and plots. `[CONFIRM]` |

#### ObservedProperties

| ObservedProperty | definition | unitOfMeasurement |
|---|---|---|
| Soil moisture | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#volumetric_soil_water_content` `[VERIFY URI]` | `m3.m-3` |
| Soil temperature | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#soil_temperature` `[VERIFY URI]` | `Cel` |

#### Datastreams

| Datastream | Thing | Sensor | ObservedProperty | observationType | unitOfMeasurement |
|---|---|---|---|---|---|
| Rainaway soil profile - Soil moisture | Rainaway Parkeerplaatsen | Rainaway soil profile | Soil moisture | `OM_Measurement` | `m3.m-3` |
| Rainaway soil profile - Soil temperature | Rainaway Parkeerplaatsen | Rainaway soil profile | Soil temperature | `OM_Measurement` | `Cel` |

### Flowsand

| Entity | Draft value |
|---|---|
| Thing | `Flowsand` |
| Thing description | Soil moisture and temperature monitoring point at The Green Village. |
| Location | GeoJSON Point `"coordinates": [4.37375, 51.99822]` `[APPROXIMATE - confirm with Lindsey]` |
| Location name | `Flowsand location` |
| Site | `tgv` |

#### Sensors

| Sensor | encodingType | metadata | Notes |
|---|---|---|---|
| Flowsand probe | `application/json` | `[UNKNOWN - confirm with Lindsey]` | Subsurface soil probe. `[CONFIRM]` |

#### ObservedProperties

| ObservedProperty | definition | unitOfMeasurement |
|---|---|---|
| Soil moisture | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#volumetric_soil_water_content` `[VERIFY URI]` | `m3.m-3` |
| Soil temperature | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#soil_temperature` `[VERIFY URI]` | `Cel` |

#### Datastreams

| Datastream | Thing | Sensor | ObservedProperty | observationType | unitOfMeasurement |
|---|---|---|---|---|---|
| Flowsand probe - Soil moisture | Flowsand | Flowsand probe | Soil moisture | `OM_Measurement` | `m3.m-3` |
| Flowsand probe - Soil temperature | Flowsand | Flowsand probe | Soil temperature | `OM_Measurement` | `Cel` |

### Koers / Zoak / Nus

| Entity | Draft value |
|---|---|
| Thing | `Koers / Zoak / Nus` |
| Thing description | Combined soil monitoring cluster at The Green Village. |
| Location | GeoJSON Point `"coordinates": [4.37393, 51.99804]` `[APPROXIMATE - confirm with Lindsey]` |
| Location name | `Koers Zoak Nus location` |
| Site | `tgv` |

#### Sensors

| Sensor | encodingType | metadata | Notes |
|---|---|---|---|
| Koers / Zoak / Nus soil cluster | `application/json` | `[UNKNOWN - confirm with Lindsey]` | Soil cluster with EC support. `[CONFIRM]` |

#### ObservedProperties

| ObservedProperty | definition | unitOfMeasurement |
|---|---|---|
| Soil moisture | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#volumetric_soil_water_content` `[VERIFY URI]` | `m3.m-3` |
| Soil temperature | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#soil_temperature` `[VERIFY URI]` | `Cel` |
| Electrical conductivity | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#soil_electrical_conductivity` `[VERIFY URI]` | `mS.cm-1` |

#### Datastreams

| Datastream | Thing | Sensor | ObservedProperty | observationType | unitOfMeasurement |
|---|---|---|---|---|---|
| Koers / Zoak / Nus soil cluster - Soil moisture | Koers / Zoak / Nus | Koers / Zoak / Nus soil cluster | Soil moisture | `OM_Measurement` | `m3.m-3` |
| Koers / Zoak / Nus soil cluster - Soil temperature | Koers / Zoak / Nus | Koers / Zoak / Nus soil cluster | Soil temperature | `OM_Measurement` | `Cel` |
| Koers / Zoak / Nus soil cluster - Electrical conductivity | Koers / Zoak / Nus | Koers / Zoak / Nus soil cluster | Electrical conductivity | `OM_Measurement` | `mS.cm-1` |

### Climate Davis

| Entity | Draft value |
|---|---|
| Thing | `Climate Davis` |
| Thing description | Davis weather station at The Green Village. |
| Location | GeoJSON Point `"coordinates": [4.37362, 51.99831]` `[APPROXIMATE - confirm with Lindsey]` |
| Location name | `Climate Davis location` |
| Site | `tgv` |

#### Sensors

| Sensor | encodingType | metadata | Notes |
|---|---|---|---|
| Davis weather station | `application/json` | `[UNKNOWN - confirm with Lindsey]` | Weather station with precipitation and wind measurements. `[CONFIRM]` |

#### ObservedProperties

| ObservedProperty | definition | unitOfMeasurement |
|---|---|---|
| Air temperature | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature` `[VERIFY URI]` | `Cel` |
| Precipitation | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#lwe_thickness_of_precipitation_amount` `[VERIFY URI]` | `mm` |
| Wind speed | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_speed` `[VERIFY URI]` | `m.s-1` |
| Wind direction | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_from_direction` `[VERIFY URI]` | `deg` |

#### Datastreams

| Datastream | Thing | Sensor | ObservedProperty | observationType | unitOfMeasurement |
|---|---|---|---|---|---|
| Davis weather station - Air temperature | Climate Davis | Davis weather station | Air temperature | `OM_Measurement` | `Cel` |
| Davis weather station - Precipitation | Climate Davis | Davis weather station | Precipitation | `OM_Measurement` | `mm` |
| Davis weather station - Wind speed | Climate Davis | Davis weather station | Wind speed | `OM_Measurement` | `m.s-1` |
| Davis weather station - Wind direction | Climate Davis | Davis weather station | Wind direction | `OM_Measurement` | `deg` |

### Weather Climatics

| Entity | Draft value |
|---|---|
| Thing | `Weather Climatics` |
| Thing description | Rooftop weather station at The Green Village. |
| Location | GeoJSON Point `"coordinates": [4.37351, 51.99844]` `[APPROXIMATE - confirm with Lindsey]` |
| Location name | `Weather Climatics location` |
| Site | `tgv` |

#### Sensors

| Sensor | encodingType | metadata | Notes |
|---|---|---|---|
| Climatics rooftop station | `application/json` | `[UNKNOWN - confirm with Lindsey]` | Rooftop weather station. `[CONFIRM]` |

#### ObservedProperties

| ObservedProperty | definition | unitOfMeasurement |
|---|---|---|
| Air temperature | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature` `[VERIFY URI]` | `Cel` |
| Precipitation | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#lwe_thickness_of_precipitation_amount` `[VERIFY URI]` | `mm` |
| Air pressure | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_pressure` `[VERIFY URI]` | `hPa` |
| Wind speed | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_speed` `[VERIFY URI]` | `m.s-1` |
| Wind direction | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_from_direction` `[VERIFY URI]` | `deg` |

#### Datastreams

| Datastream | Thing | Sensor | ObservedProperty | observationType | unitOfMeasurement |
|---|---|---|---|---|---|
| Climatics rooftop station - Air temperature | Weather Climatics | Climatics rooftop station | Air temperature | `OM_Measurement` | `Cel` |
| Climatics rooftop station - Precipitation | Weather Climatics | Climatics rooftop station | Precipitation | `OM_Measurement` | `mm` |
| Climatics rooftop station - Air pressure | Weather Climatics | Climatics rooftop station | Air pressure | `OM_Measurement` | `hPa` |
| Climatics rooftop station - Wind speed | Weather Climatics | Climatics rooftop station | Wind speed | `OM_Measurement` | `m.s-1` |
| Climatics rooftop station - Wind direction | Weather Climatics | Climatics rooftop station | Wind direction | `OM_Measurement` | `deg` |

## Diergaarde Blijdorp

### Microclimate sensors

| Entity | Draft value |
|---|---|
| Thing | `Microclimate sensors` |
| Thing description | Animal enclosure microclimate monitoring at Diergaarde Blijdorp. |
| Location | GeoJSON Point `"coordinates": [4.46263, 51.92678]` `[APPROXIMATE - confirm with Lindsey]` |
| Location name | `Microclimate sensors location` |
| Site | `blijdorp` |

#### Sensors

| Sensor | encodingType | metadata | Notes |
|---|---|---|---|
| Enclosure microclimate sensor | `application/json` | `[UNKNOWN - confirm with Lindsey]` | Temperature and humidity sensor in an animal enclosure. `[CONFIRM]` |

#### ObservedProperties

| ObservedProperty | definition | unitOfMeasurement |
|---|---|---|
| Air temperature | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature` `[VERIFY URI]` | `Cel` |
| Relative humidity | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#relative_humidity` `[VERIFY URI]` | `%` |

#### Datastreams

| Datastream | Thing | Sensor | ObservedProperty | observationType | unitOfMeasurement |
|---|---|---|---|---|---|
| Enclosure microclimate sensor - Air temperature | Microclimate sensors | Enclosure microclimate sensor | Air temperature | `OM_Measurement` | `Cel` |
| Enclosure microclimate sensor - Relative humidity | Microclimate sensors | Enclosure microclimate sensor | Relative humidity | `OM_Measurement` | `%` |

### Weather station

| Entity | Draft value |
|---|---|
| Thing | `Weather station` |
| Thing description | Outdoor weather station at Diergaarde Blijdorp. |
| Location | GeoJSON Point `"coordinates": [4.46292, 51.92659]` `[APPROXIMATE - confirm with Lindsey]` |
| Location name | `Weather station location` |
| Site | `blijdorp` |

#### Sensors

| Sensor | encodingType | metadata | Notes |
|---|---|---|---|
| Blijdorp weather station | `application/json` | `[UNKNOWN - confirm with Lindsey]` | Weather station with wind and solar radiation measurements. `[CONFIRM]` |

#### ObservedProperties

| ObservedProperty | definition | unitOfMeasurement |
|---|---|---|
| Wind speed | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_speed` `[VERIFY URI]` | `m.s-1` |
| Solar radiation | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#surface_downwelling_shortwave_flux_in_air` `[VERIFY URI]` | `W.m-2` |

#### Datastreams

| Datastream | Thing | Sensor | ObservedProperty | observationType | unitOfMeasurement |
|---|---|---|---|---|---|
| Blijdorp weather station - Wind speed | Weather station | Blijdorp weather station | Wind speed | `OM_Measurement` | `m.s-1` |
| Blijdorp weather station - Solar radiation | Weather station | Blijdorp weather station | Solar radiation | `OM_Measurement` | `W.m-2` |

### Klimaatplein / Bufferblocks

| Entity | Draft value |
|---|---|
| Thing | `Klimaatplein / Bufferblocks` |
| Thing description | Water buffering and infiltration demonstrator at Diergaarde Blijdorp. |
| Location | GeoJSON Point `"coordinates": [4.46306, 51.92674]` `[APPROXIMATE - confirm with Lindsey]` |
| Location name | `Klimaatplein Bufferblocks location` |
| Site | `blijdorp` |

#### Sensors

| Sensor | encodingType | metadata | Notes |
|---|---|---|---|
| Bufferblocks water buffer sensor | `application/json` | `[UNKNOWN - confirm with Lindsey]` | Water level and infiltration performance monitoring. `[CONFIRM]` |

#### ObservedProperties

| ObservedProperty | definition | unitOfMeasurement |
|---|---|---|
| Water buffer level | `https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#water_level` `[VERIFY URI]` | `m` |
| Infiltration performance | `[VERIFY URI]` | `1` |

#### Datastreams

| Datastream | Thing | Sensor | ObservedProperty | observationType | unitOfMeasurement |
|---|---|---|---|---|---|
| Bufferblocks water buffer sensor - Water buffer level | Klimaatplein / Bufferblocks | Bufferblocks water buffer sensor | Water buffer level | `OM_Measurement` | `m` |
| Bufferblocks water buffer sensor - Infiltration performance | Klimaatplein / Bufferblocks | Bufferblocks water buffer sensor | Infiltration performance | `OM_Measurement` | `1` |

### Water quality (ponds)

| Entity | Draft value |
|---|---|
| Thing | `Water quality (ponds)` |
| Thing description | Pond water quality monitoring at Diergaarde Blijdorp. |
| Location | GeoJSON Point `"coordinates": [4.46274, 51.92692]` `[APPROXIMATE - confirm with Lindsey]` |
| Location name | `Water quality ponds location` |
| Site | `blijdorp` |

#### Sensors

| Sensor | encodingType | metadata | Notes |
|---|---|---|---|
| Pond water quality sensor | `application/json` | `[UNKNOWN - confirm with Lindsey]` | Water quality sensor placeholder pending protocol confirmation. `[CONFIRM]` |

#### ObservedProperties

| ObservedProperty | definition | unitOfMeasurement |
|---|---|---|
| Water quality parameter | `[VERIFY URI]` | `TBD` |

#### Datastreams

| Datastream | Thing | Sensor | ObservedProperty | observationType | unitOfMeasurement |
|---|---|---|---|---|---|
| Pond water quality sensor - Water quality parameter | Water quality (ponds) | Pond water quality sensor | Water quality parameter | `OM_Measurement` | `TBD` |
