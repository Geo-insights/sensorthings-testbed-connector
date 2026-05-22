from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading


CLIMATE_ADAPTATION_ENTITY_SETS: list[dict[str, Any]] = [
    {
        "site_key": "tgv",
        "site_name": "The Green Village",
        "thing": {
            "name": "Hitteplein",
            "description": "Open-air climate installation at The Green Village for heat-stress monitoring.",
            "properties": {"site": "tgv", "campus": "TU Delft", "status": "stub"},
        },
        "location": {
            "name": "Hitteplein location",
            "description": "Approximate location for the Hitteplein installation. [APPROXIMATE - confirm with Lindsey]",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.37345, 51.99818]},
        },
        "sensors": [
            {
                "sensor_id": "tgv-hitteplein-climate-station",
                "name": "Hitteplein climate station",
                "description": "Multi-parameter climate sensor installation. [CONFIRM]",
                "encodingType": "application/json",
                "metadata": "[UNKNOWN - confirm with Lindsey]",
                "observed_properties": ["air_temperature", "relative_humidity", "solar_radiation"],
            }
        ],
        "observed_properties": {
            "air_temperature": {
                "name": "Air temperature",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature",
                "description": "Ambient air temperature.",
                "unit": "Cel",
            },
            "relative_humidity": {
                "name": "Relative humidity",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#relative_humidity",
                "description": "Relative humidity of the ambient air.",
                "unit": "%",
            },
            "solar_radiation": {
                "name": "Solar radiation",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#surface_downwelling_shortwave_flux_in_air",
                "description": "Incoming shortwave radiation at the installation.",
                "unit": "W.m-2",
            },
        },
    },
    {
        "site_key": "tgv",
        "site_name": "The Green Village",
        "thing": {
            "name": "Rainaway Parkeerplaatsen",
            "description": "Parking-lot rainwater infiltration setup at The Green Village.",
            "properties": {"site": "tgv", "campus": "TU Delft", "status": "stub"},
        },
        "location": {
            "name": "Rainaway Parkeerplaatsen location",
            "description": "Approximate location for the Rainaway parking installation. [APPROXIMATE - confirm with Lindsey]",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.37358, 51.99802]},
        },
        "sensors": [
            {
                "sensor_id": "tgv-rainaway-soil-profile",
                "name": "Rainaway soil profile",
                "description": "Soil profile probe cluster across multiple depths and plots. [CONFIRM]",
                "encodingType": "application/json",
                "metadata": "[UNKNOWN - confirm with Lindsey]",
                "observed_properties": ["soil_moisture", "soil_temperature"],
            }
        ],
        "observed_properties": {
            "soil_moisture": {
                "name": "Soil moisture",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#volumetric_soil_water_content",
                "description": "Volumetric soil water content.",
                "unit": "m3.m-3",
            },
            "soil_temperature": {
                "name": "Soil temperature",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#soil_temperature",
                "description": "Temperature within the soil profile.",
                "unit": "Cel",
            },
        },
    },
    {
        "site_key": "tgv",
        "site_name": "The Green Village",
        "thing": {
            "name": "Flowsand",
            "description": "Soil moisture and temperature monitoring point at The Green Village.",
            "properties": {"site": "tgv", "campus": "TU Delft", "status": "stub"},
        },
        "location": {
            "name": "Flowsand location",
            "description": "Approximate location for the Flowsand installation. [APPROXIMATE - confirm with Lindsey]",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.37375, 51.99822]},
        },
        "sensors": [
            {
                "sensor_id": "tgv-flowsand-probe",
                "name": "Flowsand probe",
                "description": "Subsurface soil probe. [CONFIRM]",
                "encodingType": "application/json",
                "metadata": "[UNKNOWN - confirm with Lindsey]",
                "observed_properties": ["soil_moisture", "soil_temperature"],
            }
        ],
        "observed_properties": {
            "soil_moisture": {
                "name": "Soil moisture",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#volumetric_soil_water_content",
                "description": "Volumetric soil water content.",
                "unit": "m3.m-3",
            },
            "soil_temperature": {
                "name": "Soil temperature",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#soil_temperature",
                "description": "Temperature within the soil profile.",
                "unit": "Cel",
            },
        },
    },
    {
        "site_key": "tgv",
        "site_name": "The Green Village",
        "thing": {
            "name": "Koers / Zoak / Nus",
            "description": "Combined soil monitoring cluster at The Green Village.",
            "properties": {"site": "tgv", "campus": "TU Delft", "status": "stub"},
        },
        "location": {
            "name": "Koers Zoak Nus location",
            "description": "Approximate location for the Koers / Zoak / Nus cluster. [APPROXIMATE - confirm with Lindsey]",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.37393, 51.99804]},
        },
        "sensors": [
            {
                "sensor_id": "tgv-koers-zoak-nus-soil-cluster",
                "name": "Koers / Zoak / Nus soil cluster",
                "description": "Soil cluster with EC support. [CONFIRM]",
                "encodingType": "application/json",
                "metadata": "[UNKNOWN - confirm with Lindsey]",
                "observed_properties": ["soil_moisture", "soil_temperature", "electrical_conductivity"],
            }
        ],
        "observed_properties": {
            "soil_moisture": {
                "name": "Soil moisture",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#volumetric_soil_water_content",
                "description": "Volumetric soil water content.",
                "unit": "m3.m-3",
            },
            "soil_temperature": {
                "name": "Soil temperature",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#soil_temperature",
                "description": "Temperature within the soil profile.",
                "unit": "Cel",
            },
            "electrical_conductivity": {
                "name": "Electrical conductivity",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#soil_electrical_conductivity",
                "description": "Bulk electrical conductivity in the soil profile.",
                "unit": "mS.cm-1",
            },
        },
    },
    {
        "site_key": "tgv",
        "site_name": "The Green Village",
        "thing": {
            "name": "Climate Davis",
            "description": "Davis weather station at The Green Village.",
            "properties": {"site": "tgv", "campus": "TU Delft", "status": "stub"},
        },
        "location": {
            "name": "Climate Davis location",
            "description": "Approximate location for the Davis weather station. [APPROXIMATE - confirm with Lindsey]",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.37362, 51.99831]},
        },
        "sensors": [
            {
                "sensor_id": "tgv-climate-davis-weather-station",
                "name": "Davis weather station",
                "description": "Weather station with precipitation and wind measurements. [CONFIRM]",
                "encodingType": "application/json",
                "metadata": "[UNKNOWN - confirm with Lindsey]",
                "observed_properties": ["air_temperature", "precipitation", "wind_speed", "wind_direction"],
            }
        ],
        "observed_properties": {
            "air_temperature": {
                "name": "Air temperature",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature",
                "description": "Ambient air temperature.",
                "unit": "Cel",
            },
            "precipitation": {
                "name": "Precipitation",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#lwe_thickness_of_precipitation_amount",
                "description": "Precipitation amount over the sampling interval.",
                "unit": "mm",
            },
            "wind_speed": {
                "name": "Wind speed",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_speed",
                "description": "Horizontal wind speed.",
                "unit": "m.s-1",
            },
            "wind_direction": {
                "name": "Wind direction",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_from_direction",
                "description": "Meteorological wind direction.",
                "unit": "deg",
            },
        },
    },
    {
        "site_key": "tgv",
        "site_name": "The Green Village",
        "thing": {
            "name": "Weather Climatics",
            "description": "Rooftop weather station at The Green Village.",
            "properties": {"site": "tgv", "campus": "TU Delft", "status": "stub"},
        },
        "location": {
            "name": "Weather Climatics location",
            "description": "Approximate location for the rooftop Climatics weather station. [APPROXIMATE - confirm with Lindsey]",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.37351, 51.99844]},
        },
        "sensors": [
            {
                "sensor_id": "tgv-weather-climatics-rooftop-station",
                "name": "Climatics rooftop station",
                "description": "Rooftop weather station. [CONFIRM]",
                "encodingType": "application/json",
                "metadata": "[UNKNOWN - confirm with Lindsey]",
                "observed_properties": ["air_temperature", "precipitation", "air_pressure", "wind_speed", "wind_direction"],
            }
        ],
        "observed_properties": {
            "air_temperature": {
                "name": "Air temperature",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature",
                "description": "Ambient air temperature.",
                "unit": "Cel",
            },
            "precipitation": {
                "name": "Precipitation",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#lwe_thickness_of_precipitation_amount",
                "description": "Precipitation amount over the sampling interval.",
                "unit": "mm",
            },
            "air_pressure": {
                "name": "Air pressure",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_pressure",
                "description": "Atmospheric air pressure.",
                "unit": "hPa",
            },
            "wind_speed": {
                "name": "Wind speed",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_speed",
                "description": "Horizontal wind speed.",
                "unit": "m.s-1",
            },
            "wind_direction": {
                "name": "Wind direction",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_from_direction",
                "description": "Meteorological wind direction.",
                "unit": "deg",
            },
        },
    },
    {
        "site_key": "blijdorp",
        "site_name": "Diergaarde Blijdorp",
        "thing": {
            "name": "Microclimate sensors",
            "description": "Animal enclosure microclimate monitoring at Diergaarde Blijdorp.",
            "properties": {"site": "blijdorp", "city": "Rotterdam", "status": "stub"},
        },
        "location": {
            "name": "Microclimate sensors location",
            "description": "Approximate location for the enclosure microclimate sensors. [APPROXIMATE - confirm with Lindsey]",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.46263, 51.92678]},
        },
        "sensors": [
            {
                "sensor_id": "blijdorp-enclosure-microclimate",
                "name": "Enclosure microclimate sensor",
                "description": "Temperature and humidity sensor in an animal enclosure. [CONFIRM]",
                "encodingType": "application/json",
                "metadata": "[UNKNOWN - confirm with Lindsey]",
                "observed_properties": ["air_temperature", "relative_humidity"],
            }
        ],
        "observed_properties": {
            "air_temperature": {
                "name": "Air temperature",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature",
                "description": "Ambient air temperature.",
                "unit": "Cel",
            },
            "relative_humidity": {
                "name": "Relative humidity",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#relative_humidity",
                "description": "Relative humidity of the ambient air.",
                "unit": "%",
            },
        },
    },
    {
        "site_key": "blijdorp",
        "site_name": "Diergaarde Blijdorp",
        "thing": {
            "name": "Weather station",
            "description": "Outdoor weather station at Diergaarde Blijdorp.",
            "properties": {"site": "blijdorp", "city": "Rotterdam", "status": "stub"},
        },
        "location": {
            "name": "Weather station location",
            "description": "Approximate location for the Blijdorp weather station. [APPROXIMATE - confirm with Lindsey]",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.46292, 51.92659]},
        },
        "sensors": [
            {
                "sensor_id": "blijdorp-weather-station",
                "name": "Blijdorp weather station",
                "description": "Weather station with wind and solar radiation measurements. [CONFIRM]",
                "encodingType": "application/json",
                "metadata": "[UNKNOWN - confirm with Lindsey]",
                "observed_properties": ["wind_speed", "solar_radiation"],
            }
        ],
        "observed_properties": {
            "wind_speed": {
                "name": "Wind speed",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_speed",
                "description": "Horizontal wind speed.",
                "unit": "m.s-1",
            },
            "solar_radiation": {
                "name": "Solar radiation",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#surface_downwelling_shortwave_flux_in_air",
                "description": "Incoming shortwave radiation at the installation.",
                "unit": "W.m-2",
            },
        },
    },
    {
        "site_key": "blijdorp",
        "site_name": "Diergaarde Blijdorp",
        "thing": {
            "name": "Klimaatplein / Bufferblocks",
            "description": "Water buffering and infiltration demonstrator at Diergaarde Blijdorp.",
            "properties": {"site": "blijdorp", "city": "Rotterdam", "status": "stub"},
        },
        "location": {
            "name": "Klimaatplein Bufferblocks location",
            "description": "Approximate location for the bufferblock installation. [APPROXIMATE - confirm with Lindsey]",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.46306, 51.92674]},
        },
        "sensors": [
            {
                "sensor_id": "blijdorp-bufferblocks-water-buffer",
                "name": "Bufferblocks water buffer sensor",
                "description": "Water level and infiltration performance monitoring. [CONFIRM]",
                "encodingType": "application/json",
                "metadata": "[UNKNOWN - confirm with Lindsey]",
                "observed_properties": ["water_buffer_level", "infiltration_performance"],
            }
        ],
        "observed_properties": {
            "water_buffer_level": {
                "name": "Water buffer level",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#water_level",
                "description": "Buffer water level in the demonstrator.",
                "unit": "m",
            },
            "infiltration_performance": {
                "name": "Infiltration performance",
                "definition": "[VERIFY URI]",
                "description": "Placeholder metric until Lindsey confirms the exact sensor parameter.",
                "unit": "1",
            },
        },
    },
    {
        "site_key": "blijdorp",
        "site_name": "Diergaarde Blijdorp",
        "thing": {
            "name": "Water quality (ponds)",
            "description": "Pond water quality monitoring at Diergaarde Blijdorp.",
            "properties": {"site": "blijdorp", "city": "Rotterdam", "status": "stub"},
        },
        "location": {
            "name": "Water quality ponds location",
            "description": "Approximate location for the pond water quality installation. [APPROXIMATE - confirm with Lindsey]",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.46274, 51.92692]},
        },
        "sensors": [
            {
                "sensor_id": "blijdorp-ponds-water-quality",
                "name": "Pond water quality sensor",
                "description": "Water quality sensor placeholder pending protocol confirmation. [CONFIRM]",
                "encodingType": "application/json",
                "metadata": "[UNKNOWN - confirm with Lindsey]",
                "observed_properties": ["water_quality_tbd"],
            }
        ],
        "observed_properties": {
            "water_quality_tbd": {
                "name": "Water quality parameter",
                "definition": "[VERIFY URI]",
                "description": "Placeholder until the exact pond parameter list is confirmed.",
                "unit": "TBD",
            },
        },
    },
]


def _build_value(observed_property: str, rng: random.Random) -> float:
    ranges = {
        "air_temperature": (5.0, 35.0),
        "relative_humidity": (35.0, 98.0),
        "solar_radiation": (0.0, 950.0),
        "soil_moisture": (0.1, 0.45),
        "soil_temperature": (4.0, 30.0),
        "electrical_conductivity": (0.1, 2.5),
        "precipitation": (0.0, 12.0),
        "wind_speed": (0.0, 20.0),
        "wind_direction": (0.0, 360.0),
        "air_pressure": (980.0, 1045.0),
        "water_buffer_level": (0.1, 1.5),
        "infiltration_performance": (0.2, 0.95),
        "water_quality_tbd": (0.0, 1.0),
    }
    low, high = ranges.get(observed_property, (0.0, 1.0))
    value = rng.uniform(low, high)
    if observed_property in {"wind_direction", "water_quality_tbd"}:
        return round(value, 1)
    if observed_property in {"precipitation", "water_buffer_level"}:
        return round(value, 2)
    return round(value, 3)


def generate_demo_readings() -> list[SensorReading]:
    now = datetime.now(UTC)
    rng = random.Random(int(now.timestamp()))
    readings: list[SensorReading] = []

    for entity_set in CLIMATE_ADAPTATION_ENTITY_SETS:
        thing_name = entity_set["thing"]["name"]
        location = entity_set["site_key"]
        for sensor in entity_set["sensors"]:
            for observed_property in sensor["observed_properties"]:
                property_definition = entity_set["observed_properties"][observed_property]
                # TODO: replace this stubbed reading with the live sensor feed once Lindsey confirms the protocol.
                readings.append(
                    SensorReading(
                        sensor_id=sensor["sensor_id"],
                        sensor_name=sensor["name"],
                        observed_property=observed_property,
                        unit=property_definition["unit"],
                        value=_build_value(observed_property, rng),
                        timestamp=now,
                        quality="good",
                        location=location,
                        thing_name=thing_name,
                    )
                )

    return readings
