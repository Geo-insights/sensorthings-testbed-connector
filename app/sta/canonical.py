"""Canonical datastream names and metadata.

Enforces consistent observation field names from configuration
through normalization to FROST upload.  Each member carries its
canonical display name, unit symbol, and CF conventions definition URL.
"""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple


class DatastreamMeta(NamedTuple):
    display_name: str
    unit: str
    definition: str


class CanonicalDatastream(str, Enum):
    """Allowed observation field names across the entire system."""

    TEMPERATURE = "temperature"
    HUMIDITY = "humidity"
    CO2 = "co2"
    PRESSURE = "pressure"
    AIR_TEMPERATURE = "air_temperature"
    WIND_SPEED = "wind_speed"
    WIND_DIRECTION = "wind_direction"
    PRECIPITATION = "precipitation"
    AIR_PRESSURE = "air_pressure"
    SOLAR_RADIATION = "solar_radiation"
    RELATIVE_HUMIDITY = "relative_humidity"

    @property
    def meta(self) -> DatastreamMeta:
        return _META[self]


_META: dict[CanonicalDatastream, DatastreamMeta] = {
    CanonicalDatastream.TEMPERATURE: DatastreamMeta(
        "Air temperature", "Cel",
        "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature",
    ),
    CanonicalDatastream.HUMIDITY: DatastreamMeta(
        "Relative humidity", "%",
        "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#relative_humidity",
    ),
    CanonicalDatastream.CO2: DatastreamMeta(
        "CO2 concentration", "ppm",
        "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#mole_fraction_of_carbon_dioxide_in_air",
    ),
    CanonicalDatastream.PRESSURE: DatastreamMeta(
        "Air pressure", "hPa",
        "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_pressure",
    ),
    CanonicalDatastream.AIR_TEMPERATURE: DatastreamMeta(
        "Air temperature", "Cel",
        "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_temperature",
    ),
    CanonicalDatastream.WIND_SPEED: DatastreamMeta(
        "Wind speed", "km/h",
        "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_speed",
    ),
    CanonicalDatastream.WIND_DIRECTION: DatastreamMeta(
        "Wind direction", "degrees",
        "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#wind_from_direction",
    ),
    CanonicalDatastream.PRECIPITATION: DatastreamMeta(
        "Precipitation", "mm",
        "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#precipitation_amount",
    ),
    CanonicalDatastream.AIR_PRESSURE: DatastreamMeta(
        "Air pressure", "hPa",
        "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#air_pressure",
    ),
    CanonicalDatastream.SOLAR_RADIATION: DatastreamMeta(
        "Solar radiation", "W/m2",
        "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#surface_downwelling_shortwave_flux_in_air",
    ),
    CanonicalDatastream.RELATIVE_HUMIDITY: DatastreamMeta(
        "Relative humidity", "%",
        "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html#relative_humidity",
    ),
}
