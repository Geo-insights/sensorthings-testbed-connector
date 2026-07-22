# SensorThings Entity Mapping

This document maps the climate-adaptation sensor setups at The Green Village and Diergaarde Blijdorp into Thing, Location, Sensor, ObservedProperty, and Datastream entities for OGC SensorThings API v1.1.

The entity sets defined in `app/sources/climate_adaptation.py` are registered statically via `/connector/register-site/{site_key}`. Ohnics and Levellog entities are registered dynamically by their polling sources on first successful fetch.

All entity names are prefixed with the configured `SENSORTHINGS_ENTITY_NAME_PREFIX` (default: `GEO_`) at registration time.

## The Green Village

### TGV Office Lab

| Entity | Value |
|---|---|
| Thing | `TGV Office Lab` |
| Thing description | Indoor climate monitoring at The Green Village office laboratory, fed from Confluent Cloud Kafka. |
| Location | GeoJSON Point `[4.377634, 51.996581]` |
| Location name | `TGV Office Lab location` |
| Site | `tgv` |
| Data source | Kafka topic `tud_gv_officelab-climate` |

#### Sensors

| Sensor | sensor_id | encodingType |
|---|---|---|
| TGV Office Lab climate sensor | `tgv-officelab-climate` | `application/json` |

#### ObservedProperties and Datastreams

| ObservedProperty | definition | unit | Datastream name |
|---|---|---|---|
| Air temperature | `cf-standard-name-table.html#air_temperature` | `Cel` | TGV Office Lab climate sensor - Air temperature |
| Relative humidity | `cf-standard-name-table.html#relative_humidity` | `%` | TGV Office Lab climate sensor - Relative humidity |
| CO2 concentration | `cf-standard-name-table.html#mole_fraction_of_carbon_dioxide_in_air` | `ppm` | TGV Office Lab climate sensor - CO2 concentration |
| Air pressure | `cf-standard-name-table.html#air_pressure` | `hPa` | TGV Office Lab climate sensor - Air pressure |

### Climate Davis

| Entity | Value |
|---|---|
| Thing | `Climate Davis` |
| Thing description | Davis Vantage Pro2 outdoor weather station at The Green Village, TU Delft campus. |
| Location | GeoJSON Point `[4.377634, 51.996581]` |
| Location name | `Climate Davis location` |
| Site | `tgv` |
| Data source | Kafka topic `tud_gv_officelab-climate` |

#### Sensors

| Sensor | sensor_id | encodingType |
|---|---|---|
| Davis weather station | `tgv-climate-davis-weather-station` | `application/json` |

#### ObservedProperties and Datastreams

| ObservedProperty | definition | unit | Datastream name |
|---|---|---|---|
| Air temperature | `cf-standard-name-table.html#air_temperature` | `Cel` | Davis weather station - Air temperature |
| Wind speed | `cf-standard-name-table.html#wind_speed` | `km/h` | Davis weather station - Wind speed |
| Wind direction | `cf-standard-name-table.html#wind_from_direction` | `degrees` | Davis weather station - Wind direction |
| Precipitation | `cf-standard-name-table.html#precipitation_amount` | `mm` | Davis weather station - Precipitation |

### Weather Climatics

| Entity | Value |
|---|---|
| Thing | `Weather Climatics` |
| Thing description | Climatics rooftop weather station at The Green Village, TU Delft campus. |
| Location | GeoJSON Point `[4.377634, 51.996581]` |
| Location name | `Weather Climatics location` |
| Site | `tgv` |
| Data source | Kafka topic `tud_gv_officelab-climate` |

#### Sensors

| Sensor | sensor_id | encodingType |
|---|---|---|
| Climatics rooftop station | `tgv-weather-climatics-rooftop-station` | `application/json` |

#### ObservedProperties and Datastreams

| ObservedProperty | definition | unit | Datastream name |
|---|---|---|---|
| Air pressure | `cf-standard-name-table.html#air_pressure` | `hPa` | Climatics rooftop station - Air pressure |

### Hitteplein

| Entity | Value |
|---|---|
| Thing | `Hitteplein` |
| Thing description | Hitteplein climate station at The Green Village, TU Delft campus. |
| Location | GeoJSON Point `[4.377634, 51.996581]` |
| Location name | `Hitteplein location` |
| Site | `tgv` |
| Data source | Kafka topic `tud_gv_officelab-climate` |

#### Sensors

| Sensor | sensor_id | encodingType |
|---|---|---|
| Hitteplein climate station | `tgv-hitteplein-climate-station` | `application/json` |

#### ObservedProperties and Datastreams

| ObservedProperty | definition | unit | Datastream name |
|---|---|---|---|
| Solar radiation | `cf-standard-name-table.html#surface_downwelling_shortwave_flux_in_air` | `W/m2` | Hitteplein climate station - Solar radiation |
| Relative humidity | `cf-standard-name-table.html#relative_humidity` | `%` | Hitteplein climate station - Relative humidity |

## Dynamically Registered Sources

### Ohnics Air Quality (SamenMeten, Delft)

Entities are discovered at runtime by polling the Ohnics 5min.json API. Only sensors with the configured prefix (`OHNICS_SENSOR_PREFIX`, default `de-` for Delft) are ingested. Each discovered sensor gets its own Thing, Location, Sensor, and Datastreams.

| ObservedProperty | unit |
|---|---|
| PM2.5 | `ug/m3` |
| Air temperature | `Cel` |

### Levellog / CARS Groundwater

Entities are configured via `LEVELLOG_INSTALLATIONS_JSON` or `LEVELLOG_INSTALLATION_IDS`. Each installation gets its own Thing, Location, Sensor, and Datastream.

| ObservedProperty | unit |
|---|---|
| Groundwater level | `m` |

## Diergaarde Blijdorp

Blijdorp entity sets are not yet defined in `CLIMATE_ADAPTATION_ENTITY_SETS`. The following setups are planned but awaiting field confirmation:

- Microclimate sensors (animal enclosure temperature and humidity)
- Weather station (wind speed, solar radiation)
- Klimaatplein / Bufferblocks (water buffer level, infiltration performance)
- Water quality (ponds) (parameters TBD)

The original draft entity mapping for these setups was captured in the FROST Live Inventory snapshot from 2026-05-26 (see [FROST_LIVE_INVENTORY.md](FROST_LIVE_INVENTORY.md)) and registered as scaffolding entities with the `GEO_` prefix.
