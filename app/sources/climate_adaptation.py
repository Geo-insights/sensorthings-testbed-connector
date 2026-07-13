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
]


def generate_demo_readings() -> list[SensorReading]:
    now = datetime.now(UTC)
    rng = random.Random(int(now.timestamp()))
    ranges = {
        "temperature": (15.0, 30.0),
        "humidity": (30.0, 80.0),
        "co2": (400.0, 1200.0),
        "pressure": (980.0, 1045.0),
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
