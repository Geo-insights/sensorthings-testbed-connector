from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

from app.models import SensorReading
from app.sta.models import (
    STADatastream,
    STALocation,
    STAObservedProperty,
    STASensor,
    STAThing,
    UnitOfMeasurement,
)


CLIMATE_ADAPTATION_ENTITY_SETS: list[dict[str, Any]] = [
    {
        "site_key": "tgv",
        "site_name": "The Green Village",
        "thing": {
            "name": "TGV Office Lab",
            "description": "Indoor climate monitoring at The Green Village office laboratory, fed from Confluent Cloud Kafka.",
            "properties": {
                "site": "tgv",
                "campus": "TU Delft",
                "source": "kafka",
                "topic": "tud_gv_officelab-climate",
            },
        },
        "location": {
            "name": "TGV Office Lab location",
            "description": "Weather station at The Green Village office laboratory, TU Delft campus. Height: 5 m above ground.",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.377633926937161, 51.99658144237765]},
            "properties": {"altitude_m": 5},
        },
        "sensors": [
            {
                "sensor_id": "tgv-officelab-climate",
                "name": "TGV Office Lab climate sensor",
                "description": "Multi-parameter indoor climate sensor in the TGV office lab.",
                "encodingType": "application/json",
                "metadata": "https://thegreenvillage.org",
                # Keys must match measurement_id values in the Kafka topic.
                "observed_properties": ["temperature", "humidity", "co2", "pressure"],
                "properties": {
                    "long_description": "Bosch BME680 multi-parameter sensor measuring temperature, humidity, CO2, and barometric pressure. Mounted at desk height (1.2 m) in the TGV office laboratory for indoor climate monitoring.",
                    "image_url": None,
                    "installation_notes": "Wall-mounted near the east window, powered via USB.",
                },
            }
        ],
        "observed_properties": {
            "temperature": {
                "name": "Air temperature",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature",
                "description": "Indoor air temperature.",
                "unit": "Cel",
            },
            "humidity": {
                "name": "Relative humidity",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#relative_humidity",
                "description": "Indoor relative humidity.",
                "unit": "%",
            },
            "co2": {
                "name": "CO2 concentration",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#mole_fraction_of_carbon_dioxide_in_air",
                "description": "Indoor CO2 concentration.",
                "unit": "ppm",
            },
            "pressure": {
                "name": "Air pressure",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_pressure",
                "description": "Indoor air pressure.",
                "unit": "hPa",
            },
        },
    },
    {
        "site_key": "tgv",
        "site_name": "The Green Village",
        "thing": {
            "name": "Climate Davis",
            "description": "Davis Vantage Pro2 outdoor weather station at The Green Village, TU Delft campus.",
            "properties": {
                "site": "tgv",
                "campus": "TU Delft",
                "source": "kafka",
                "topic": "tud_gv_officelab-climate",
            },
        },
        "location": {
            "name": "Climate Davis location",
            "description": "Davis weather station at The Green Village, TU Delft campus. Height: 10 m above ground.",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.377633926937161, 51.99658144237765]},
            "properties": {"altitude_m": 10},
        },
        "sensors": [
            {
                "sensor_id": "tgv-climate-davis-weather-station",
                "name": "Davis weather station",
                "description": "Davis Vantage Pro2 outdoor weather station measuring air temperature, wind, and precipitation.",
                "encodingType": "application/json",
                "metadata": "https://thegreenvillage.org",
                "observed_properties": ["air_temperature", "wind_speed", "wind_direction", "precipitation"],
                "properties": {},
            }
        ],
        "observed_properties": {
            "air_temperature": {
                "name": "Air temperature",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature",
                "description": "Outdoor air temperature.",
                "unit": "Cel",
            },
            "wind_speed": {
                "name": "Wind speed",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_speed",
                "description": "Wind speed.",
                "unit": "km/h",
            },
            "wind_direction": {
                "name": "Wind direction",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_from_direction",
                "description": "Wind direction in degrees from north.",
                "unit": "degrees",
            },
            "precipitation": {
                "name": "Precipitation",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#precipitation_amount",
                "description": "Daily rainfall.",
                "unit": "mm",
            },
        },
    },
    {
        "site_key": "tgv",
        "site_name": "The Green Village",
        "thing": {
            "name": "Weather Climatics",
            "description": "Climatics rooftop weather station at The Green Village, TU Delft campus.",
            "properties": {
                "site": "tgv",
                "campus": "TU Delft",
                "source": "kafka",
                "topic": "tud_gv_officelab-climate",
            },
        },
        "location": {
            "name": "Weather Climatics location",
            "description": "Climatics rooftop station at The Green Village, TU Delft campus.",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.377633926937161, 51.99658144237765]},
            "properties": {},
        },
        "sensors": [
            {
                "sensor_id": "tgv-weather-climatics-rooftop-station",
                "name": "Climatics rooftop station",
                "description": "Climatics rooftop weather station measuring air pressure.",
                "encodingType": "application/json",
                "metadata": "https://thegreenvillage.org",
                "observed_properties": ["air_pressure"],
                "properties": {},
            }
        ],
        "observed_properties": {
            "air_pressure": {
                "name": "Air pressure",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_pressure",
                "description": "Atmospheric air pressure.",
                "unit": "hPa",
            },
        },
    },
    {
        "site_key": "tgv",
        "site_name": "The Green Village",
        "thing": {
            "name": "Hitteplein",
            "description": "Hitteplein climate station at The Green Village, TU Delft campus.",
            "properties": {
                "site": "tgv",
                "campus": "TU Delft",
                "source": "kafka",
                "topic": "tud_gv_officelab-climate",
            },
        },
        "location": {
            "name": "Hitteplein location",
            "description": "Hitteplein climate station at The Green Village, TU Delft campus.",
            "encodingType": "application/geo+json",
            "location": {"type": "Point", "coordinates": [4.377633926937161, 51.99658144237765]},
            "properties": {},
        },
        "sensors": [
            {
                "sensor_id": "tgv-hitteplein-climate-station",
                "name": "Hitteplein climate station",
                "description": "Hitteplein climate station measuring solar radiation and relative humidity.",
                "encodingType": "application/json",
                "metadata": "https://thegreenvillage.org",
                "observed_properties": ["solar_radiation", "relative_humidity"],
                "properties": {},
            }
        ],
        "observed_properties": {
            "solar_radiation": {
                "name": "Solar radiation",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#surface_downwelling_shortwave_flux_in_air",
                "description": "Solar radiation.",
                "unit": "W/m2",
            },
            "relative_humidity": {
                "name": "Relative humidity",
                "definition": "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#relative_humidity",
                "description": "Outdoor relative humidity.",
                "unit": "%",
            },
        },
    },
]


def entity_set_to_sta_models(
    entity_set: dict[str, Any],
) -> tuple[STAThing, STALocation, list[STASensor], dict[str, STAObservedProperty], list[STADatastream]]:
    """Convert a raw entity-set dict into typed STA domain models.

    Returns (thing, location, sensors, observed_properties, datastreams).
    Datastreams don't have linked IDs set — those are resolved at registration time.
    """
    thing = STAThing(
        name=entity_set["thing"]["name"],
        description=entity_set["thing"]["description"],
        properties=entity_set["thing"].get("properties", {}),
    )

    location = STALocation(
        name=entity_set["location"]["name"],
        description=entity_set["location"]["description"],
        encodingType=entity_set["location"].get("encodingType", "application/geo+json"),
        location=entity_set["location"]["location"],
        properties=entity_set["location"].get("properties", {}),
    )

    sensors: list[STASensor] = []
    observed_properties: dict[str, STAObservedProperty] = {}
    datastreams: list[STADatastream] = []

    for sensor_def in entity_set["sensors"]:
        sensor = STASensor(
            name=sensor_def["name"],
            description=sensor_def["description"],
            encodingType=sensor_def.get("encodingType", "application/json"),
            metadata=sensor_def.get("metadata", ""),
            properties=sensor_def.get("properties", {}),
        )
        sensors.append(sensor)

        for op_key in sensor_def["observed_properties"]:
            if op_key not in observed_properties:
                op_def = entity_set["observed_properties"][op_key]
                observed_properties[op_key] = STAObservedProperty(
                    name=op_def["name"],
                    definition=op_def["definition"],
                    description=op_def["description"],
                )

            op_def = entity_set["observed_properties"][op_key]
            datastreams.append(
                STADatastream(
                    name=f"{sensor_def['name']} - {op_def['name']}",
                    description=f"{op_def['name']} observations for {entity_set['thing']['name']}",
                    unitOfMeasurement=UnitOfMeasurement(
                        name=op_def["name"],
                        symbol=op_def["unit"],
                        definition=op_def["definition"],
                    ),
                )
            )

    return thing, location, sensors, observed_properties, datastreams


def generate_demo_readings() -> list[SensorReading]:
    now = datetime.now(UTC)
    rng = random.Random(int(now.timestamp()))
    ranges = {
        "temperature": (15.0, 30.0),
        "humidity": (30.0, 80.0),
        "co2": (400.0, 1200.0),
        "pressure": (980.0, 1045.0),
        "air_temperature": (5.0, 35.0),
        "wind_speed": (0.0, 50.0),
        "wind_direction": (0.0, 360.0),
        "precipitation": (0.0, 20.0),
        "air_pressure": (980.0, 1045.0),
        "solar_radiation": (0.0, 1000.0),
        "relative_humidity": (30.0, 80.0),
    }
    readings: list[SensorReading] = []
    for entity_set in CLIMATE_ADAPTATION_ENTITY_SETS:
        thing_name = entity_set["thing"]["name"]
        location = entity_set["site_key"]
        for sensor in entity_set["sensors"]:
            for observed_property in sensor["observed_properties"]:
                property_definition = entity_set["observed_properties"][observed_property]
                low, high = ranges.get(observed_property, (0.0, 1.0))
                readings.append(
                    SensorReading(
                        sensor_id=sensor["sensor_id"],
                        sensor_name=sensor["name"],
                        observed_property=observed_property,
                        unit=property_definition["unit"],
                        value=round(rng.uniform(low, high), 3),
                        timestamp=now,
                        quality="good",
                        location=location,
                        thing_name=thing_name,
                    )
                )
    return readings
